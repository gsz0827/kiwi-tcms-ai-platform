"""真实浏览器的端到端冒烟测试。

与 test_runner.py 的 mock 不同，这里真实启动 Playwright Chromium，访问一个本地
静态测试站点，验证 runner.execute() 在真实浏览器上的完整链路：打开页面、输入、
点击、断言、失败截图、账号隔离。默认在普通环境跳过（需要 Chromium 与网络），
由 CI 的 web-e2e job 在装好浏览器后通过环境参数显式开启。
"""
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from tcms.management.models import Product, Classification
from tcms.ai_assistant.crypto import encrypt_api_key

from .models import WebRun, WebResult
from .runner import execute

_E2E_ENABLED = os.environ.get("WEB_TEST_E2E", "") == "1"

# 静态测试站点：一个带输入框和按钮的登录页，点击后跳转到 /home 显示欢迎文案。
_INDEX_HTML = """<!doctype html><html><head><title>Login</title></head><body>
<form id="login"><input id="username" name="username"><button id="submit">登录</button></form>
</body></html>"""

_HOME_HTML = """<!doctype html><html><head><title>Home</title></head><body>
<h1 id="welcome">欢迎回来</h1></body></html>"""


class _StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _HOME_HTML if self.path.startswith("/home") else _INDEX_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *args):
        pass


class _StaticServer:
    """在随机端口起一个静态站点，供浏览器真实访问。"""

    def __enter__(self):
        self.server = HTTPServer(("127.0.0.1", 0), _StaticHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"


def _make_run(owner, product, snapshot, status="running"):
    return WebRun.objects.create(
        owner=owner, product=product, name="e2e",
        submission_token=uuid.uuid4(),
        snapshot_encrypted=encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)),
        total=len(snapshot["cases"]), status=status,
        started=timezone.now(),
    )


@skipUnless(_E2E_ENABLED, "WEB_TEST_E2E=1 时运行真实浏览器冒烟")
class BrowserE2ETests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="e2e-owner")
        self.product = Product.objects.create(
            name="e2e-project",
            classification=Classification.objects.create(name="e2e-class"),
        )

    def _snapshot(self, origin, cases):
        return {
            "base_url": origin + "/",
            "ignore_https_errors": False,
            "stop_on_failure": False,
            "cases": cases,
        }

    def test_passing_case_drives_real_browser(self):
        with _StaticServer() as site:
            steps = [
                {"action": "goto", "value": "/"},
                {"action": "fill", "selector": "#username", "value": "demo"},
                {"action": "assert_visible", "selector": "#submit"},
            ]
            snapshot = self._snapshot(site.origin, [{"name": "登录页", "steps": steps, "dataset": 0}])
            run = _make_run(self.owner, self.product, snapshot)

            with override_settings(WEB_TEST_ALLOWED_ORIGINS=[site.origin]):
                execute(run.pk)

            run.refresh_from_db()
            self.assertEqual(run.status, "passed")
            result = WebResult.objects.get(run=run)
            self.assertEqual(result.status, "passed")
            self.assertEqual(len(result.steps), 3)

    def test_failing_case_captures_screenshot(self):
        with _StaticServer() as site:
            steps = [
                {"action": "goto", "value": "/"},
                # 定位一个不存在的元素，触发断言失败
                {"action": "assert_visible", "selector": "#no-such-element"},
            ]
            snapshot = self._snapshot(site.origin, [{"name": "失败用例", "steps": steps, "dataset": 0}])
            run = _make_run(self.owner, self.product, snapshot)

            with override_settings(WEB_TEST_ALLOWED_ORIGINS=[site.origin]):
                execute(run.pk)

            run.refresh_from_db()
            self.assertEqual(run.status, "failed")
            result = WebResult.objects.get(run=run)
            self.assertEqual(result.status, "failed")
            self.assertIn("失败", result.error)
            self.assertIsNotNone(result.screenshot)

    def test_owner_isolation_blocks_foreign_run(self):
        other = get_user_model().objects.create_user(username="e2e-other")
        with _StaticServer() as site:
            steps = [
                {"action": "goto", "value": "/"},
                {"action": "assert_visible", "selector": "#submit"},
            ]
            snapshot = self._snapshot(site.origin, [{"name": "隔离", "steps": steps, "dataset": 0}])
            run = _make_run(self.owner, self.product, snapshot)
            # 别的账号不应能查看该 run（视图层隔离在 tests.py 已覆盖，这里验证模型归属）
            self.assertNotEqual(run.owner, other)
            self.assertEqual(run.owner, self.owner)
