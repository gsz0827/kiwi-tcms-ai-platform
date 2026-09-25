"""Regression tests for interrupted workflows and untrusted configuration input."""

import io
import json
import threading
import urllib.error
import urllib.request
import uuid
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xml.etree import ElementTree

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from deployment.kiwi_ci import write_reports
from tcms.management.models import Priority
from tcms.testcases.models import TestCaseStatus
from tcms.tests.factories import CategoryFactory, ProductFactory, TestCaseFactory, UserFactory

from .api_ai import import_drafts
from .api_forms import EnvironmentForm
from .api_runner import execute_next_api_run, submit_run
from .api_validation import validate_case
from .crypto import encrypt_api_key
from .health import DEFAULT_READINESS_TIMEOUT, _readiness_timeout, _run_bounded
from .jobs import execute_job
from .models import (
    AIJob,
    AIModelConfig,
    AIRequest,
    AITestCaseDraft,
    AITestCaseReview,
    AIUsageLog,
    APIAIDraft,
    APIAIRequest,
    APICase,
    APIEnvironment,
    APIRun,
    APISuite,
)
from .services import MAX_AI_RESPONSE_BYTES, _open_ai_request, _request_config_content


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://api-demo:8080"])
class WorkflowRegressionTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True)
        self.product = ProductFactory()
        self.category = CategoryFactory(product=self.product)
        self.client.force_login(self.owner)
        self.config = AIModelConfig.objects.create(
            owner=self.owner, name="audit", model="example", api_base="https://model.example.test/v1",
            api_key_encrypted=encrypt_api_key("private-test-key"), is_active=True,
        )
        self.env = APIEnvironment.objects.create(
            owner=self.owner, product=self.product, name="demo", base_url="http://api-demo:8080",
        )
        self.evidence = "GET /health 返回服务状态，成功时状态码为 200。"
        self.batch = APIAIRequest.objects.create(
            owner=self.owner, product=self.product, category=self.category, title="audit",
            submission_token=uuid.uuid4(), fingerprint="test", generated=True,
            input_encrypted=encrypt_api_key(json.dumps(dict(
                documentation=self.evidence, requirements="", rules={}, environment_variables=[],
            ))),
        )
        TestCaseStatus.objects.get_or_create(name="audit-proposed", defaults={"is_confirmed": False})
        Priority.objects.get_or_create(value="P1")

    def draft(self, position=0, **configuration):
        return APIAIDraft.objects.create(
            request=self.batch, position=position, name=f"case-{position}", evidence=self.evidence,
            reviewed_at=timezone.now(), configuration=dict(
                method="GET", path="/health", expected_status=200, sequence=position * 10,
            ) | configuration,
        )

    def api_case(self, **changes):
        return APICase.objects.create(
            owner=self.owner, product=self.product, name="health", path="/health", **changes
        )

    def suite(self, **changes):
        case = self.api_case()
        return APISuite.objects.create(
            owner=self.owner, product=self.product, name="suite",
            environment=self.env, case_ids=[case.pk], **changes,
        )

    def suite_post(self, suite, **changes):
        return self.client.post(
            reverse("ai_assistant:api_suite_edit", args=[self.product.pk, suite.pk]),
            dict(
                name=suite.name, environment=self.env.pk, cases=suite.case_ids,
                interval_minutes=suite.interval_minutes, schedule_enabled="on",
            ) | changes,
        )

    def test_invalid_review_config_is_a_field_error_and_does_not_save(self):
        draft = self.draft()
        response = self.client.post(
            reverse("ai_assistant:api_ai_review", args=[draft.pk]),
            dict(
                name=draft.name, description="", evidence=draft.evidence,
                revision=draft.revision,
                configuration=json.dumps(
                    draft.configuration | {"expected_status": None}
                ),
                confirmed="on",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("configuration", response.context["form"].errors)
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 1)
        self.assertFalse(APICase.objects.exists())

    def test_malformed_sequence_is_rejected_before_sorting(self):
        first = self.draft(sequence={"bad": 1})
        second = self.draft(1)
        with self.assertRaisesMessage(ValueError, "未复核或不完整"):
            import_drafts(self.owner, self.batch.pk, [first.pk, second.pk])
        self.assertFalse(APICase.objects.exists())

    def test_deleted_prerequisite_cannot_supply_a_variable(self):
        first = self.draft(extracts={"user_id": "data.id"})
        import_drafts(self.owner, self.batch.pk, [first.pk])
        first.refresh_from_db()
        first.api_case.delete()
        second = self.draft(1, path="/users/{{user_id}}")
        with self.assertRaisesMessage(ValueError, "缺少变量来源"):
            import_drafts(self.owner, self.batch.pk, [second.pk])
        self.assertFalse(APICase.objects.exists())

    def test_edited_prerequisite_uses_its_current_extracts(self):
        first = self.draft(extracts={"user_id": "data.id"})
        import_drafts(self.owner, self.batch.pk, [first.pk])
        first.refresh_from_db()
        APICase.objects.filter(pk=first.api_case_id).update(extracts={})
        second = self.draft(1, path="/users/{{user_id}}")
        with self.assertRaisesMessage(ValueError, "缺少变量来源"):
            import_drafts(self.owner, self.batch.pk, [second.pk])
        self.assertEqual(APICase.objects.count(), 1)

    def test_import_is_idempotent_and_batch_execution_selects_all_cases(self):
        first, second = self.draft(), self.draft(1)
        for _ in range(2):
            import_drafts(self.owner, self.batch.pk, [first.pk, second.pk])
        self.assertEqual(APICase.objects.count(), 2)
        detail = self.client.get(reverse("ai_assistant:api_ai_detail", args=[self.batch.pk]))
        response = self.client.get(detail.context["execute_url"])
        expected = {str(pk) for pk in APICase.objects.values_list("pk", flat=True)}
        self.assertEqual(set(response.context["form"].initial["cases"]), expected)

    def test_partial_import_cannot_reorder_equal_sequence_dependencies(self):
        # An existing case gets the lower ID even if the new draft has an
        # earlier position. Validation must follow the runner's actual order.
        existing = self.draft(1, path="/users/{{user_id}}", sequence=10)
        source = self.draft(0, extracts={"user_id": "data.id"}, sequence=0)
        import_drafts(self.owner, self.batch.pk, [source.pk, existing.pk])
        source.refresh_from_db()
        source.api_case.delete()
        replacement = self.draft(2, extracts={"user_id": "data.id"}, sequence=10)
        with self.assertRaisesMessage(ValueError, "缺少变量来源"):
            import_drafts(self.owner, self.batch.pk, [replacement.pk])

    def test_invalid_import_ids_are_reported_without_echoing_input(self):
        response = self.client.post(reverse("ai_assistant:api_ai_import", args=[self.batch.pk]),
                                    {"draft_ids": ["<script>alert(1)</script>"]}, follow=True)
        self.assertContains(response, "草稿编号无效")
        self.assertNotContains(response, "<script>alert(1)</script>")

    def test_user_supplied_model_name_is_escaped_in_messages(self):
        name = '<img src=x onerror="alert(1)">'
        response = self.client.post(reverse("ai_assistant:model_settings"), dict(
            name=name, model="model", api_base="https://model.example.test/v1",
            timeout=60, api_key="test-key",
        ), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, escape(name))
        self.assertNotContains(response, name)

    def test_interval_change_reschedules_from_now(self):
        now = timezone.now()
        suite = self.suite(
            schedule_enabled=True, interval_minutes=60, next_run_at=now + timedelta(minutes=50)
        )
        with patch("tcms.ai_assistant.api_suite_views.timezone.now", return_value=now):
            self.assertEqual(self.suite_post(suite, interval_minutes=5).status_code, 302)
        suite.refresh_from_db()
        self.assertEqual(suite.next_run_at, now + timedelta(minutes=5))

    def test_edit_does_not_restore_a_tick_already_advanced_by_scheduler(self):
        now = timezone.now()
        suite = self.suite(
            schedule_enabled=True, interval_minutes=60, next_run_at=now - timedelta(minutes=1)
        )
        advanced = now + timedelta(minutes=60)

        def dispatch_during_validation(*_):
            APISuite.objects.filter(pk=suite.pk).update(next_run_at=advanced)

        with patch(
            "tcms.ai_assistant.api_suite_views.prepare_case",
            side_effect=dispatch_during_validation,
        ):
            self.assertEqual(self.suite_post(suite).status_code, 302)
        suite.refresh_from_db()
        self.assertEqual(suite.next_run_at, advanced)

    def test_disabled_account_never_calls_model(self):
        job = AIJob.objects.create(
            owner=self.owner, model_config=self.config,
            operation="connection_test", status="running",
        )
        type(self.owner).objects.filter(pk=self.owner.pk).update(is_active=False)
        with patch("tcms.ai_assistant.jobs.test_model_connection") as call:
            execute_job(job)
        call.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")

    def test_requirement_change_during_generation_rejects_old_results(self):
        request = AIRequest.objects.create(created_by=self.owner, category=self.category,
                                           title="old", requirement="old requirement")
        operations = {
            "requirement_analysis": "analyze_requirement",
            "test_case_generation": "generate_test_cases",
            "coverage_analysis": "analyze_test_coverage",
            "coverage_supplement": "generate_coverage_gap_test_cases",
        }
        for operation, function in operations.items():
            with self.subTest(operation=operation):
                def update_requirement(*_args, **_kwargs):
                    request.refresh_from_db()
                    AIRequest.objects.filter(pk=request.pk).update(
                        version=request.version + 1, requirement="new requirement"
                    )
                    if operation in ("test_case_generation", "coverage_supplement"):
                        return []
                    return {"summary": "outdated"}, self.config, "old raw"

                job = AIJob.objects.create(
                    owner=self.owner, model_config=self.config, operation=operation,
                    status="running", payload={"request_id": request.pk},
                )
                with patch(f"tcms.ai_assistant.jobs.{function}", side_effect=update_requirement):
                    execute_job(job)
                job.refresh_from_db()
                request.refresh_from_db()
                self.assertEqual(job.status, "failed")
                self.assertIn("本次结果未保存", job.error_message)
                self.assertEqual(request.analysis, {})
                self.assertEqual(request.coverage_analysis, {})
                self.assertFalse(request.drafts.exists())

    def test_coverage_rejects_results_if_draft_was_edited_during_call(self):
        request = AIRequest.objects.create(created_by=self.owner, title="test", requirement="test")
        draft = AITestCaseDraft.objects.create(request=request, case_number="TC-1", summary="before")
        job = AIJob.objects.create(
            owner=self.owner, model_config=self.config, operation="coverage_analysis",
            status="running", payload={"request_id": request.pk},
        )

        def edit_draft(*_args, **_kwargs):
            AITestCaseDraft.objects.filter(pk=draft.pk).update(summary="after")
            return {"overall_score": 100}, self.config, "raw"

        with patch("tcms.ai_assistant.jobs.analyze_test_coverage", side_effect=edit_draft):
            execute_job(job)
        job.refresh_from_db()
        request.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(request.coverage_analysis, {})

    def test_apply_review_does_not_overwrite_later_manual_changes(self):
        case = TestCaseFactory(category=self.category, summary="before", text="original")
        review = AITestCaseReview.objects.create(
            owner=self.owner, test_case=case,
            original_summary=case.summary, original_text=case.text,
            optimized_summary="AI replacement", score=90,
        )
        case.summary = "new manual title"
        case.save()
        response = self.client.post(
            reverse("ai_assistant:apply_review", args=[review.pk]), follow=True
        )
        self.assertContains(response, "测试用例已在评审后修改")
        case.refresh_from_db()
        review.refresh_from_db()
        self.assertEqual(case.summary, "new manual title")
        self.assertIsNone(review.applied_at)

    def test_attaching_generated_configuration_marks_existing_case_automated(self):
        case = TestCaseFactory(category=self.category, is_automated=False)
        self.batch.target_case = case
        self.batch.save(update_fields=("target_case",))
        draft = self.draft()
        import_drafts(self.owner, self.batch.pk, [draft.pk])
        case.refresh_from_db()
        self.assertTrue(case.is_automated)

    def test_failed_generation_is_not_displayed_as_running(self):
        self.batch.generated = False
        self.batch.save(update_fields=("generated",))
        job = AIJob.objects.create(
            owner=self.owner, model_config=self.config, operation="api_case_generation",
            status="failed", dedupe_key=f"api-generation:{self.batch.pk}",
        )
        response = self.client.get(reverse("ai_assistant:api_ai_generate", args=[self.product.pk]))
        self.assertEqual(response.context["batches"][0].status_label, job.get_status_display())

    def test_nonfinite_environment_values_are_rejected_before_database_write(self):
        form = EnvironmentForm(data=dict(name="invalid", base_url=self.env.base_url, timeout=5,
                                         variables='{"value":NaN}', headers="{}"))
        self.assertFalse(form.is_valid())

    def test_stopped_job_keeps_its_terminal_state(self):
        job = AIJob.objects.create(
            owner=self.owner, model_config=self.config,
            operation="connection_test", status="failed",
        )
        with patch("tcms.ai_assistant.jobs.test_model_connection") as call:
            execute_job(job)
        call.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")

    def test_interrupted_run_and_recovery_leave_no_pending_results(self):
        case = self.api_case()
        for recovery in (False, True):
            run = submit_run(self.owner, self.product, dict(
                environment=self.env, cases=[case], submission_token=uuid.uuid4(),
            ))
            if recovery:
                APIRun.objects.filter(pk=run.pk).update(status="running")
                call_command(
                    "api_recover_runs", str(run.pk),
                    workers_stopped=True, stdout=io.StringIO(),
                )
            else:
                with patch("tcms.ai_assistant.api_runner.decrypt_api_key", side_effect=ValueError):
                    execute_next_api_run()
            run.refresh_from_db()
            self.assertEqual(run.status, "interrupted")
            self.assertFalse(run.results.filter(status="pending").exists())
            self.assertIn("请求可能已发送", run.results.get().error)

    def test_model_errors_never_store_provider_bodies_or_secrets(self):
        for response in (
            urllib.error.HTTPError(
                self.config.api_base, 401, "bad", {},
                io.BytesIO(b"private-test-key and private requirements"),
            ),
            io.BytesIO(b"private-test-key is not JSON"),
            io.BytesIO(
                json.dumps(
                    {"choices": [{"message": {"content": {"token": "private-test-key"}}}]}
                ).encode()
            ),
            io.BytesIO(b"x" * (MAX_AI_RESPONSE_BYTES + 1)),
        ):
            kwargs = (
                {"side_effect": response}
                if isinstance(response, Exception)
                else {"return_value": response}
            )
            with patch(
                "tcms.ai_assistant.services._open_ai_request", **kwargs
            ), self.assertRaises(RuntimeError) as error:
                _request_config_content(self.config, "system", "private requirements")
            self.assertNotIn("private-test-key", str(error.exception))
            self.assertNotIn("private requirements", str(error.exception))
        for log in AIUsageLog.objects.all():
            self.assertNotIn("private-test-key", log.error_message)
            self.assertNotIn("private requirements", log.error_message)


class BoundaryRegressionTests(SimpleTestCase):
    def test_nonfinite_request_json_is_rejected(self):
        with self.assertRaisesMessage(ValueError, "有效 JSON"):
            validate_case(dict(method="GET", path="/health", expected_status=200, max_elapsed_ms=0,
                               body={"value": float("inf")}))

    def test_junit_marks_incomplete_or_empty_runs_as_errors(self):
        cases = (
            ("cancelled", []),
            ("interrupted", [dict(name="ok", status="passed")]),
            ("completed", []),
        )
        for status, results in cases:
            with self.subTest(status=status), TemporaryDirectory() as directory:
                output = Path(directory) / "report.json"
                write_reports(dict(status=status, results=results), output)
                root = ElementTree.parse(output.with_suffix(".xml")).getroot()
                self.assertEqual(root.attrib["errors"], "1")
                self.assertIsNotNone(root.find("testcase/error"))

    def test_junit_remains_parseable_with_json_control_characters(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            write_reports(
                dict(
                    status="completed",
                    results=[dict(name="name\x00", status="error", error="bad\x01")],
                ),
                output,
            )
            case = ElementTree.parse(output.with_suffix(".xml")).getroot().find("testcase")
            self.assertEqual(case.attrib["name"], "name\ufffd")
            self.assertEqual(case.find("error").attrib["message"], "bad\ufffd")

    def test_hung_readiness_probe_does_not_spawn_more_database_calls(self):
        release = threading.Event()
        closed = threading.Event()

        def slow_check():
            release.wait(2)
            return True

        with patch(
            "tcms.ai_assistant.health._PROBE_SLOT", threading.BoundedSemaphore(1)
        ), patch("tcms.ai_assistant.health.connection") as db, patch(
            "tcms.ai_assistant.health._collect_database_state", side_effect=slow_check
        ) as collect:
            db.close.side_effect = closed.set
            try:
                self.assertEqual(_run_bounded(collect, 0.01)[0], "timeout")
                for _ in range(5):
                    self.assertEqual(_run_bounded(collect, 0.01)[0], "timeout")
                self.assertEqual(collect.call_count, 1)
            finally:
                release.set()
                self.assertTrue(closed.wait(2))

    def test_readiness_rejects_nonfinite_timeouts(self):
        for value in ("inf", "nan", "-inf"):
            with patch.dict("os.environ", {"KIWI_READINESS_TIMEOUT": value}):
                self.assertEqual(_readiness_timeout(), DEFAULT_READINESS_TIMEOUT)

    def test_model_redirect_never_forwards_credentials(self):
        requests = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-sink")
                self.end_headers()

            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
                data=b"private requirements",
                headers={"Authorization": "Bearer test-key"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                _open_ai_request(request, timeout=2)
            caught.exception.close()
            self.assertEqual(requests, ["/v1/chat/completions"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
