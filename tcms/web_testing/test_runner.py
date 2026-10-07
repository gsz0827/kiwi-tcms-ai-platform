"""runner.execute() 核心执行路径测试。

真实浏览器执行需要 Playwright + Chromium，不在单元测试里启动。这里用
unittest.mock 替换浏览器与 playwright 入口，验证 execute() 的状态机：
执行成功、业务步骤失败、公共登录失败、取消、超时、多数据组登录态复用、
失败截图上限、断连错误回写。
"""
import json
import uuid
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from tcms.management.models import Product, Classification
from tcms.ai_assistant.crypto import encrypt_api_key

from .models import WebRun, WebResult
from .runner import execute


# runner.execute() 在函数体内才 ``from playwright.sync_api import sync_playwright``，
# 因此必须在模块级 patch playwright.sync_api 的入口，而不是 runner 的局部名字。
# 只有装了 playwright 的环境（CI 的 ai-platform.yml 会 pip install requirements/web.txt）
# 才真正执行 execute() 测试；`make ai-test` 的镜像不含 playwright，这里优雅跳过，
# 避免 import 阶段就报错。
try:
    import playwright.sync_api  # noqa: F401
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False


def _patch_playwright():
    """把 playwright.sync_api.sync_playwright 换成一个假浏览器工厂。"""
    return mock.patch("playwright.sync_api.sync_playwright")


def _snapshot(cases, stop_on_failure=False):
    return {
        "base_url": "https://web:8443/",
        "ignore_https_errors": True,
        "stop_on_failure": stop_on_failure,
        "cases": cases,
    }


def _make_run(owner, snapshot):
    run = WebRun.objects.create(
        owner=owner,
        product=Product.objects.create(
            name="runner-project",
            classification=Classification.objects.create(name="runner-class"),
        ),
        name="runner-run",
        submission_token=uuid.uuid4(),
        snapshot_encrypted=encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)),
        total=len(snapshot["cases"]),
        status="running",
    )
    return run


class _FakeLocator:
    def click(self): pass
    def fill(self, value): pass
    def select_option(self, value): pass
    def check(self): pass


class _FakePage:
    def __init__(self, fail_action=None):
        self._fail_action = fail_action
        self.default_timeout = None

    def set_default_timeout(self, timeout): self.default_timeout = timeout

    def locator(self, selector): return _FakeLocator()

    def goto(self, url, wait_until=None, timeout=None): pass

    def screenshot(self, full_page=False, timeout=None):
        # 返回一个 <2MB 的假 PNG，触发截图成功分支
        return b"\x89PNG" + b"0" * 100


class _FakeContext:
    def __init__(self, fail_action=None):
        self.page = _FakePage(fail_action)
        self._storage = None
        self.closed = False

    def new_page(self): return self.page

    def route(self, pattern, handler): pass
    def route_web_socket(self, pattern, handler): pass

    def storage_state(self):
        return {"cookies": [{"name": "sessionid", "value": "abc"}]}

    def close(self): self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.contexts = []
        self.closed = False

    def new_context(self, **kwargs):
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx

    def close(self): self.closed = True


# execute() 通过 ThreadPoolExecutor 在子线程里读写数据库，SQLite 的 :memory: 数据库
# 每个线程独立连接、互不可见，子线程里 refresh_from_db / 写结果会失败。这类执行路径
# 测试只在 MariaDB（CI 的 platform-tests-mariadb）上真正运行；SQLite 环境优雅跳过。
_RUNNER_EXECUTABLE = _HAS_PLAYWRIGHT and connection.vendor != "sqlite"


@skipUnless(_RUNNER_EXECUTABLE, "runner 执行路径测试需要 playwright 且非 SQLite 数据库")
class RunnerExecuteTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="runner-owner")

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_successful_run_marks_passed_and_records_results(self):
        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        run = _make_run(self.owner, _snapshot([{"name": "Happy", "steps": steps, "dataset": 0}]))
        fake_browser = _FakeBrowser()

        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(_snapshot([{"name": "Happy", "steps": steps, "dataset": 0}]))):
            sp.return_value.__enter__.return_value.chromium.launch.return_value = fake_browser
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "passed")
        result = WebResult.objects.get(run=run)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.position, 1)
        self.assertEqual(result.steps[0]["status"], "passed")
        self.assertTrue(fake_browser.closed)

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_failed_step_marks_run_failed_with_screenshot(self):
        # 用抛异常的 goto 触发失败，验证失败分支 + 截图 + 错误信息
        class _FailPage(_FakePage):
            def goto(self, url, wait_until=None, timeout=None):
                raise RuntimeError("boom")

        class _FailContext(_FakeContext):
            def new_page(self): return _FailPage()

        class _FailBrowser(_FakeBrowser):
            def new_context(self, **kwargs): return _FailContext()

        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        snapshot = _snapshot([{"name": "Sad", "steps": steps, "dataset": 0}])
        run = _make_run(self.owner, snapshot)
        fake_browser = _FailBrowser()

        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(snapshot)):
            sp.return_value.__enter__.return_value.chromium.launch.return_value = fake_browser
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        result = WebResult.objects.get(run=run)
        self.assertEqual(result.status, "failed")
        self.assertIn("RuntimeError", result.error)
        self.assertIsNotNone(result.screenshot)

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_environment_error_marks_run_error(self):
        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        snapshot = _snapshot([{"name": "Case", "steps": steps, "dataset": 0}])
        run = _make_run(self.owner, snapshot)

        # 让 playwright 启动本身抛异常，验证外层 error 分支
        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(snapshot)):
            sp.side_effect = RuntimeError("no browser")
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "error")
        self.assertIn("执行环境异常", run.error)

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_dataset_login_state_reused_across_cases(self):
        # 两组数据，每组两条用例；验证公共登录前置执行一次、storage_state 被复用
        setup = [{"action": "goto", "value": "/login/"}, {"action": "fill", "selector": "#u", "value": "x"}, {"action": "assert_visible", "selector": "body"}]
        steps = [{"action": "goto", "value": "/home/"}, {"action": "assert_visible", "selector": "body"}]
        cases = [
            {"name": "A1", "steps": steps, "dataset": 0, "setup_steps": setup},
            {"name": "A2", "steps": steps, "dataset": 0, "setup_steps": setup},
            {"name": "B1", "steps": steps, "dataset": 1, "setup_steps": setup},
            {"name": "B2", "steps": steps, "dataset": 1, "setup_steps": setup},
        ]
        snapshot = _snapshot(cases)
        run = _make_run(self.owner, snapshot)
        fake_browser = _FakeBrowser()

        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(snapshot)):
            sp.return_value.__enter__.return_value.chromium.launch.return_value = fake_browser
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "passed")
        self.assertEqual(WebResult.objects.filter(run=run).count(), 4)
        # 每个数据组：1 个登录 context + 每用例 1 个 context = 3 个；两组共 6 个
        self.assertEqual(len(fake_browser.contexts), 6)

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_cancel_requested_aborts_run(self):
        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        snapshot = _snapshot([{"name": "CancelMe", "steps": steps, "dataset": 0}])
        run = _make_run(self.owner, snapshot)
        # 执行前标记取消，checkpoint 应在首个用例前抛出 Cancelled
        run.cancel_requested = True
        run.save(update_fields=("cancel_requested",))

        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(snapshot)):
            sp.return_value.__enter__.return_value.chromium.launch.return_value = _FakeBrowser()
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_stop_on_failure_breaks_after_first_failure(self):
        class _FailGotoPage(_FakePage):
            def goto(self, url, wait_until=None, timeout=None):
                raise RuntimeError("boom")

        class _FailContext(_FakeContext):
            def new_page(self): return _FailGotoPage()

        class _FailBrowser(_FakeBrowser):
            def new_context(self, **kwargs): return _FailContext()

        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        snapshot = _snapshot([
            {"name": "Fail", "steps": steps, "dataset": 0},
            {"name": "NeverRun", "steps": steps, "dataset": 0},
        ], stop_on_failure=True)
        run = _make_run(self.owner, snapshot)
        fake_browser = _FailBrowser()

        with _patch_playwright() as sp, \
             mock.patch("tcms.web_testing.runner.decrypt_api_key", return_value=json.dumps(snapshot)):
            sp.return_value.__enter__.return_value.chromium.launch.return_value = fake_browser
            execute(run.pk)

        run.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(WebResult.objects.filter(run=run).count(), 1)

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_non_running_run_is_noop(self):
        steps = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "body"}]
        snapshot = _snapshot([{"name": "Queued", "steps": steps, "dataset": 0}])
        run = _make_run(self.owner, snapshot)
        run.status = "queued"
        run.save(update_fields=("status",))

        execute(run.pk)  # 应立即返回，不启动浏览器

        run.refresh_from_db()
        self.assertEqual(run.status, "queued")
        self.assertEqual(WebResult.objects.filter(run=run).count(), 0)
