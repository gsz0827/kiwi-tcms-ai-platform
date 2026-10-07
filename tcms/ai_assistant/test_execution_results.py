import json
import uuid

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from tcms.management.models import Classification, Product
from tcms.web_testing.models import WebRun, WebResult
from tcms.allure_reporting.models import AllureReport
from .crypto import encrypt_api_key
from .execution_results import result_context
from .models import APIRun, APIResult, AutomationArchive


class ExecutionResultsTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="execution-ui-owner")
        self.other = get_user_model().objects.create_user(
            username="execution-ui-other", is_superuser=True
        )
        self.product = Product.objects.create(
            name="UI project", classification=Classification.objects.create(name="UI classification")
        )
        self.client.force_login(self.owner)

    def web(self, status="failed", total=3):
        cases = [{"id": i, "name": f"Frozen {i}"} for i in range(1, total + 1)]
        return WebRun.objects.create(
            owner=self.owner,
            product=self.product,
            name="UI Web run",
            execution_mode="debug",
            status=status,
            total=total,
            completed_count=2,
            submission_token=uuid.uuid4(),
            snapshot_encrypted=encrypt_api_key(
                json.dumps({"cases": cases, "base_url": "https://kiwi-web:8443"})
            ),
        )

    def api(self, status="completed"):
        return APIRun.objects.create(
            owner=self.owner,
            product=self.product,
            environment_name="UI API environment",
            status=status,
            submission_token=uuid.uuid4(),
            snapshot_encrypted=encrypt_api_key("{}"),
        )

    def web_results(self, run):
        WebResult.objects.create(
            run=run,
            position=1,
            name="Old config title",
            status="passed",
            elapsed_ms=10,
            steps=[{"action": "打开页面", "status": "passed"}],
        )
        WebResult.objects.create(
            run=run,
            position=2,
            name="Failed",
            status="failed",
            elapsed_ms=20,
            steps=[{"action": "断言包含文本", "status": "failed"}],
            error="Assertion failure",
            screenshot=b"PNG fixture",
        )

    def api_results(self, run):
        APIResult.objects.create(
            run=run,
            position=0,
            name="API pass",
            status="passed",
            status_code=200,
            elapsed_ms=5,
            request_summary={"headers": {"authorization": "[已隐藏]"}},
            response_summary='{"status":"ok"}',
            checks=[{"label": "HTTP 状态码", "passed": True, "expected": 200, "actual": 200}],
        )
        APIResult.objects.create(
            run=run,
            position=1,
            name="API failure",
            status="failed",
            status_code=400,
            elapsed_ms=0,
            error="Bad assertion",
            writeback="未回写",
            checks=[{"label": "状态码", "passed": False, "expected": 200, "actual": 400}],
        )
        APIResult.objects.create(
            run=run, position=2, name="API network error", status="error", error="Network failure"
        )
        APIResult.objects.create(run=run, position=3, name="API skipped", status="skipped")

    def url(self, kind, run):
        return reverse(
            "web_testing:run" if kind == "web" else "ai_assistant:api_report", args=[run.pk]
        )

    def test_web_compact_table_includes_unexecuted_snapshot_rows(self):
        run = self.web()
        self.web_results(run)
        response = self.client.get(self.url("web", run))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "execution-result-row", count=3)
        self.assertContains(response, "execution-diagnostic-row", count=2)
        self.assertContains(
            response,
            'class="execution-diagnostic-toggle" type="button" aria-expanded="false"',
            count=2,
        )
        self.assertContains(response, "Frozen 3")
        self.assertEqual(response.context["execution_unexecuted"], 1)
        self.assertEqual(response.context["execution_counts"]["passed"], 1)
        self.assertContains(response, "原始诊断")

    def test_frozen_names_used_not_live_config_names(self):
        run = self.web()
        self.web_results(run)
        response = self.client.get(self.url("web", run))
        self.assertContains(response, "Frozen 1")
        self.assertNotContains(response, "Old config title")

    def test_steps_and_screenshot_kept_inside_hidden_diagnostics(self):
        run = self.web()
        self.web_results(run)
        response = self.client.get(self.url("web", run))
        failed = run.results.get(position=2)
        self.assertContains(response, f'id="diagnostic-web-{run.pk}-2" hidden')
        self.assertContains(response, "断言包含文本")
        self.assertContains(response, reverse("web_testing:screenshot", args=[failed.pk]))
        self.assertNotIn("screenshot", response.context["execution_rows"][1]["result"].__dict__)

    def test_pending_and_cancelled_missing_rows_not_failures(self):
        for status, expected in [
            ("queued", "pending"),
            ("running", "pending"),
            ("cancelled", "skipped"),
            ("interrupted", "skipped"),
        ]:
            run = self.web(status)
            context = result_context("web", run, [])
            self.assertEqual(context["execution_counts"][expected], 3)
            self.assertEqual(context["execution_counts"]["failed"], 0)
            self.assertEqual(context["execution_unexecuted"], 3)

    def test_legacy_missing_snapshot_names_still_shows_saved_results(self):
        run = self.web()
        run.snapshot_encrypted = encrypt_api_key("{}")
        self.web_results(run)
        context = result_context("web", run, list(run.results.all()))
        self.assertEqual(len(context["execution_rows"]), 2)
        self.assertEqual(context["execution_unexecuted"], 1)

    def test_corrupt_snapshot_helper_does_not_erase_stored_results(self):
        run = self.web()
        self.web_results(run)
        run.snapshot_encrypted = "invalid encrypted data"
        self.assertEqual(
            len(result_context("web", run, list(run.results.all()))["execution_rows"]), 2
        )

    def test_unknown_state_not_counted_failed_or_passed(self):
        run = self.web(total=1)
        result = WebResult.objects.create(run=run, position=1, name="Unknown", status="unknown")
        context = result_context("web", run, [result])
        self.assertEqual(context["execution_unknown"], 1)
        self.assertEqual(context["execution_counts"]["failed"], 0)
        self.assertEqual(context["execution_counts"]["passed"], 0)

    def test_api_table_and_diagnostic_fields_preserved(self):
        run = self.api()
        self.api_results(run)
        response = self.client.get(self.url("api", run))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "execution-result-row", count=4)
        self.assertContains(
            response,
            'class="execution-diagnostic-toggle" type="button" aria-expanded="false"',
            count=4,
        )
        self.assertContains(response, "请求信息")
        self.assertContains(response, "响应摘要")
        self.assertContains(response, "[已隐藏]")
        self.assertEqual(response.context["execution_counts"]["error"], 1)
        self.assertEqual(response.context["execution_unexecuted"], 1)
        self.assertContains(response, "0 ms")
        self.assertContains(response, "导出原始结果 JSON")
        self.assertContains(response, "执行详情")

    def test_api_unknown_duration_shown_as_dash(self):
        run = self.api()
        APIResult.objects.create(run=run, position=0, name="Skipped", status="skipped")
        response = self.client.get(self.url("api", run))
        self.assertContains(response, "<td>—</td>", html=True)
        self.assertNotContains(response, "None ms")

    def test_allure_missing_or_error_keeps_diagnostics_accessible(self):
        for kind in ("web", "api"):
            run = self.web() if kind == "web" else self.api()
            self.web_results(run) if kind == "web" else self.api_results(run)
            AllureReport.objects.filter(**{kind + "_run": run}).update(
                status="error", error="Temporary report failure"
            )
            response = self.client.get(self.url(kind, run))
            self.assertContains(response, "重试生成报告")
            self.assertContains(response, "原始诊断")
            AllureReport.objects.filter(**{kind + "_run": run}).delete()
            response = self.client.get(self.url(kind, run))
            self.assertContains(response, "生成 Allure 报告")
            self.assertContains(response, "原始诊断")

    def test_owner_isolation_for_both_pages(self):
        runs = [("web", self.web()), ("api", self.api())]
        self.client.force_login(self.other)
        for kind, run in runs:
            self.assertEqual(self.client.get(self.url(kind, run)).status_code, 404)

    def test_pages_and_results_remain_private_no_store(self):
        for kind, run in [("web", self.web()), ("api", self.api())]:
            response = self.client.get(self.url(kind, run))
            self.assertIn("no-store", response["Cache-Control"])

    def test_read_only_actions_hidden_but_diagnostics_retained(self):
        self.owner.groups.add(Group.objects.get_or_create(name="AI 只读")[0])
        for kind in ("web", "api"):
            run = self.web() if kind == "web" else self.api()
            self.web_results(run) if kind == "web" else self.api_results(run)
            response = self.client.get(self.url(kind, run))
            self.assertContains(response, "原始诊断")
            self.assertNotContains(response, "失败项重跑（调试）")
            self.assertNotContains(response, "重新执行…")
            self.assertNotContains(response, "归档报告 / 创建缺陷草稿")

    def test_task_controls_and_polling_remain_for_active_runs(self):
        web = self.web("running")
        api = self.api("running")
        response = self.client.get(self.url("web", web))
        self.assertContains(response, "取消执行")
        self.assertContains(response, reverse("web_testing:status", args=[web.pk]))
        response = self.client.get(self.url("api", api))
        self.assertContains(response, "停止后续用例")
        self.assertContains(response, 'data-terminal="false"')

    def test_display_is_read_only_no_reexecution_or_archive(self):
        web = self.web()
        self.web_results(web)
        api = self.api()
        self.api_results(api)
        web_snapshot = web.snapshot_encrypted
        api_snapshot = api.snapshot_encrypted
        checks = api.results.first().checks
        for kind, run in [("web", web), ("api", api)]:
            self.client.get(self.url(kind, run))
        web.refresh_from_db()
        api.refresh_from_db()
        self.assertEqual(web.snapshot_encrypted, web_snapshot)
        self.assertEqual(api.snapshot_encrypted, api_snapshot)
        self.assertEqual(api.results.first().checks, checks)
        self.assertEqual(WebRun.objects.count(), 1)
        self.assertEqual(APIRun.objects.count(), 1)
        self.assertEqual(AutomationArchive.objects.count(), 0)

    def test_names_and_diagnostics_are_html_escaped(self):
        run = self.api()
        APIResult.objects.create(
            run=run,
            position=0,
            name="<script>alert(1)</script>",
            status="failed",
            error="<img src=x onerror=alert(1)>",
            response_summary="<script>steal()</script>",
        )
        response = self.client.get(self.url("api", run))
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(response, "<script>steal()</script>")
