import json
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse

from deployment.api_demo import Handler
from tcms.tests.factories import ProductFactory, TestCaseFactory, TestExecutionFactory, TestRunFactory, UserFactory
from tcms.testruns.models import TestExecutionStatus

from .api_forms import APICaseForm, EnvironmentForm
from .api_runner import check_response, execute_next_api_run, secrets_for, submit_run
from .api_validation import redact, validate_destination
from .crypto import decrypt_api_key, encrypt_api_key
from .models import APICase, APIEnvironment, APIRun


class DemoHandler(Handler):
    calls = []

    def do_GET(self):
        self.calls.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://unapproved.invalid/internal")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(2)
            try:
                self.reply(200, {"status": "ok"})
            except BrokenPipeError:
                pass
        elif self.path == "/large":
            self.reply(200, {"large": "x" * (1024 * 1024 + 1)})
        elif self.path == "/credentials":
            self.reply(200, {"token": "private-response-token", "nested": {"password": "private"}})
        else:
            super().do_GET()


class APISecretRedactionTests(SimpleTestCase):
    def test_custom_auth_header_variable_is_redacted_after_expansion(self):
        environment = {"variables": {"session": "private-session"},
                       "secret_headers": {"X-Custom": "Bearer {{session}}"}}
        secrets = secrets_for({}, environment)
        self.assertEqual(redact("private-session", secrets), "[已隐藏]")
        self.assertEqual(redact("Bearer private-session", secrets), "[已隐藏]")

    def test_encoded_secret_path_is_redacted_but_normal_id_is_preserved(self):
        environment = {"variables": {"api_key": "s/ecret", "user_id": 1}, "secret_headers": {}}
        secrets = secrets_for({}, environment)
        self.assertEqual(redact("/users/1/s%2Fecret", secrets), "/users/1/[已隐藏]")


class WorkerQueueStopTests(TestCase):
    """Worker 的启动巡检要读数据库，所以这条不能用 SimpleTestCase。"""

    def test_worker_stop_between_queues_does_not_claim_another_job(self):
        def stop():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return False
        with patch("tcms.ai_assistant.management.commands.ai_worker.execute_next_api_run", side_effect=stop), patch(
            "tcms.ai_assistant.management.commands.ai_worker.execute_next_job"
        ) as ai_queue:
            call_command("ai_worker", once=True, stdout=StringIO())
        ai_queue.assert_not_called()


class APIAutomationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        super().tearDownClass()

    def setUp(self):
        self.settings_override = override_settings(API_AUTOMATION_ALLOWED_ORIGINS=[self.base_url])
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        DemoHandler.calls = []
        self.owner = UserFactory()
        from django.contrib.auth.models import Permission
        self.owner.user_permissions.add(Permission.objects.get(content_type__app_label="testcases", codename="add_testcase"))
        self.other = UserFactory()
        self.product = ProductFactory()
        self.env = APIEnvironment.objects.create(
            owner=self.owner, product=self.product, name="本地测试", base_url=self.base_url,
            timeout=1, variables={"user_id": 1},
        )
        self.case = APICase.objects.create(
            owner=self.owner, product=self.product, name="健康检查", path="/health",
            assertions=[{"path": "status", "operator": "equals", "expected": "ok"}],
        )
        self.client.force_login(self.owner)

    def data(self, **changes):
        return dict(environment=self.env, cases=[self.case], submission_token=uuid.uuid4(),
                    test_run=None, passed_status=None, failed_status=None) | changes

    def run_case(self, **changes):
        run = submit_run(self.owner, self.product, self.data(**changes))
        self.assertTrue(execute_next_api_run())
        run.refresh_from_db()
        return run

    def test_real_http_success_and_failure_batch_without_model(self):
        failure = APICase.objects.create(owner=self.owner, product=self.product, name="不存在", path="/users/999")
        run = self.run_case(cases=[self.case, failure])
        self.assertEqual(run.status, "completed")
        results = list(run.results.all())
        self.assertEqual([item.status for item in results], ["passed", "failed"])
        self.assertEqual([item.status_code for item in results], [200, 404])
        self.assertEqual(DemoHandler.calls, ["/health", "/users/999"])
        response = self.client.get(reverse("ai_assistant:api_report", args=[run.pk]), secure=True)
        self.assertContains(response, "断言失败")
        self.assertContains(response, "健康检查")

    def test_environment_variables_and_json_post(self):
        self.case.path = "/users/{{user_id}}"
        self.case.assertions = [{"path": "data.id", "operator": "equals", "expected": 1}]
        self.case.save()
        self.assertEqual(self.run_case().results.get().status, "passed")
        self.case.path, self.case.method, self.case.send_body = "/login", "POST", True
        self.case.body = {"username": "demo", "password": "demo-password"}
        self.case.assertions = [{"path": "token", "operator": "exists"}]
        self.case.save()
        result = self.run_case().results.get()
        self.assertEqual(result.status, "passed")
        self.assertNotIn("demo-password", json.dumps(result.request_summary))
        self.assertNotIn("demo-token-not-a-real-credential", result.response_summary)
        self.assertNotIn("demo-token-not-a-real-credential", json.dumps(result.checks))

    def test_snapshot_stays_unchanged_and_secrets_are_encrypted(self):
        self.env.secret_headers_encrypted = encrypt_api_key(json.dumps({"Authorization": "Bearer secret-token"}))
        self.env.save()
        run = submit_run(self.owner, self.product, self.data())
        self.assertNotIn("secret-token", run.snapshot_encrypted)
        self.case.path = "/users/999"
        self.case.save()
        execute_next_api_run()
        result = run.results.get()
        self.assertEqual(result.status, "passed")
        self.assertNotIn("secret-token", json.dumps(result.request_summary))

    def test_double_submit_returns_same_run_and_rejects_changed_selection(self):
        data = self.data()
        first = submit_run(self.owner, self.product, data)
        self.assertEqual(submit_run(self.owner, self.product, data).pk, first.pk)
        execute_next_api_run()
        self.assertEqual(submit_run(self.owner, self.product, data).pk, first.pk)
        self.assertEqual(APIRun.objects.count(), 1)
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, data | {"cases": []})

    def test_owner_and_product_isolation_on_forms_reports_and_actions(self):
        run = submit_run(self.owner, self.product, self.data())
        self.client.force_login(self.other)
        for name in ("api_report", "api_status"):
            self.assertEqual(self.client.get(reverse(f"ai_assistant:{name}", args=[run.pk]), secure=True).status_code, 404)
        self.assertEqual(self.client.post(reverse("ai_assistant:api_cancel", args=[run.pk]), secure=True).status_code, 404)
        self.assertEqual(self.client.get(reverse("ai_assistant:api_case_edit", args=[self.product.pk, self.case.pk]), secure=True).status_code, 404)
        response = self.client.post(reverse("ai_assistant:api_submit", args=[self.product.pk]), {
            "environment": self.env.pk, "cases": [self.case.pk], "submission_token": uuid.uuid4(),
        }, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(APIRun.objects.count(), 1)
        different_product = ProductFactory()
        self.client.force_login(self.owner)
        response = self.client.post(reverse("ai_assistant:api_submit", args=[different_product.pk]), {
            "environment": self.env.pk, "cases": [self.case.pk], "submission_token": uuid.uuid4(),
        }, secure=True)
        self.assertTrue(response.context["form"].errors)

    def test_csrf_required_and_get_does_not_cancel(self):
        run = submit_run(self.owner, self.product, self.data())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        url = reverse("ai_assistant:api_cancel", args=[run.pk])
        self.assertEqual(client.post(url, secure=True).status_code, 403)
        self.assertEqual(self.client.get(url, secure=True).status_code, 405)

    def test_cancel_queued_sends_nothing(self):
        run = submit_run(self.owner, self.product, self.data())
        self.client.post(reverse("ai_assistant:api_cancel", args=[run.pk]), secure=True)
        self.assertFalse(execute_next_api_run())
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.results.get().status, "skipped")
        self.assertEqual(DemoHandler.calls, [])

    def test_stop_during_request_finishes_current_and_skips_remaining(self):
        other_case = APICase.objects.create(owner=self.owner, product=self.product, name="下一条", path="/health")
        run = submit_run(self.owner, self.product, self.data(cases=[self.case, other_case]))
        def send(*args):
            APIRun.objects.filter(pk=run.pk).update(status="cancel_requested")
            return 200, b'{"status":"ok"}', 1
        with patch("tcms.ai_assistant.api_runner.send_http", side_effect=send) as transport:
            execute_next_api_run()
        self.assertEqual(transport.call_count, 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["passed", "skipped"])

    def test_redirect_is_recorded_without_following(self):
        self.case.path, self.case.expected_status, self.case.assertions = "/redirect", 302, []
        self.case.save()
        self.assertEqual(self.run_case().results.get().status, "passed")
        self.assertEqual(DemoHandler.calls, ["/redirect"])

    def test_timeout_and_large_response_are_errors_without_retry(self):
        for path in ("/slow", "/large"):
            with self.subTest(path=path):
                self.case.path = path
                self.case.save()
                self.assertEqual(self.run_case().results.get().status, "error")
                self.assertEqual(DemoHandler.calls.count(path), 1)

    def test_unapproved_destination_rejected_at_submission_and_execution(self):
        for url in ("file:///etc/passwd", "http://169.254.169.254/latest", "http://user:pass@127.0.0.1", "https://example.com"):
            with self.assertRaises(ValueError):
                validate_destination(url)
        run = submit_run(self.owner, self.product, self.data())
        with override_settings(API_AUTOMATION_ALLOWED_ORIGINS=[]):
            execute_next_api_run()
        self.assertEqual(run.results.get().status, "error")
        self.assertEqual(DemoHandler.calls, [])

    def test_invalid_variable_or_header_fails_before_queueing(self):
        self.case.path = "/users/{{unknown}}"
        self.case.save()
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, self.data())
        self.case.path, self.case.headers = "/health", {"Host": "other.example"}
        self.case.save()
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, self.data())
        self.assertEqual(APIRun.objects.count(), 0)

    def test_json_assertions_arrays_missing_values_and_type(self):
        case = {"expected_status": 200, "max_elapsed_ms": 5, "assertions": [
            {"path": "items.0.id", "operator": "equals", "expected": 1},
            {"path": "missing", "operator": "exists"},
            {"path": "flag", "operator": "equals", "expected": 1},
        ]}
        checks, _ = check_response(case, 200, b'{"items":[{"id":1}],"flag":true}', 10)
        self.assertEqual([item["passed"] for item in checks], [True, False, True, False, False])

    def linked_data(self):
        self.owner.is_superuser = True
        self.owner.save()
        platform_case = TestCaseFactory(category=self.product.category.first())
        self.case.test_case = platform_case
        self.case.save()
        target = TestRunFactory(plan__product=self.product)
        execution = TestExecutionFactory(case=platform_case, run=target)
        return self.data(test_run=target,
                         passed_status=TestExecutionStatus.objects.filter(weight__gt=0).first(),
                         failed_status=TestExecutionStatus.objects.filter(weight__lt=0).first()), execution

    def test_writeback_pass_and_fail_and_history_actor(self):
        data, execution = self.linked_data()
        run = submit_run(self.owner, self.product, data)
        execute_next_api_run()
        execution.refresh_from_db()
        self.assertGreater(execution.status.weight, 0)
        self.assertEqual(execution.tested_by_id, self.owner.pk)
        self.assertEqual(execution.history.latest().history_user_id, self.owner.pk)
        self.assertIn("已回写", run.results.get().writeback)
        self.case.expected_status = 201
        self.case.save()
        run = submit_run(self.owner, self.product, data | {"submission_token": uuid.uuid4()})
        execute_next_api_run()
        execution.refresh_from_db()
        self.assertLess(execution.status.weight, 0)
        self.assertEqual(run.results.get().status, "failed")

    def test_stale_execution_and_permission_revocation_prevent_writeback(self):
        data, execution = self.linked_data()
        run = submit_run(self.owner, self.product, data)
        execution.sortkey += 1
        execution.save()
        original_status = execution.status_id
        execute_next_api_run()
        execution.refresh_from_db()
        self.assertEqual(execution.status_id, original_status)
        self.assertIn("已被修改", run.results.get().writeback)
        run = submit_run(self.owner, self.product, data | {"submission_token": uuid.uuid4()})
        self.owner.is_superuser = False
        self.owner.save()
        execute_next_api_run()
        self.assertIn("权限已变更", run.results.get().writeback)

    def test_active_run_and_ambiguous_execution_cannot_be_submitted(self):
        data, execution = self.linked_data()
        submit_run(self.owner, self.product, data)
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, data | {"submission_token": uuid.uuid4()})
        APIRun.objects.all().delete()
        TestExecutionFactory(case=execution.case, run=execution.run)
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, data)

    def test_environment_secret_form_never_echoes_credentials(self):
        response = self.client.post(reverse("ai_assistant:api_environment_new", args=[self.product.pk]), {
            "name": "Secret", "base_url": "http://denied.invalid", "timeout": 10,
            "secret_headers": '{"Authorization":"Bearer do-not-echo"}',
        }, secure=True)
        self.assertNotContains(response, "do-not-echo")
        form = EnvironmentForm(data={"name": "Secret", "base_url": self.base_url, "timeout": 10,
                                     "secret_headers": '{"Authorization":"Bearer hidden"}'}, instance=self.env)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertNotIn("hidden", self.env.secret_headers_encrypted)
        self.assertIn("hidden", decrypt_api_key(self.env.secret_headers_encrypted))

    def test_form_validation_reports_bad_declarations(self):
        data = {"name": "bad", "method": "GET", "path": "//other-host", "expected_status": 200, "max_elapsed_ms": 0}
        form = APICaseForm(data=data, owner=self.owner, product=self.product)
        self.assertFalse(form.is_valid())
        form = APICaseForm(data=data | {"path": "/health", "assertions": '[{"path":"id","operator":"eval"}]'}, owner=self.owner, product=self.product)
        self.assertFalse(form.is_valid())

    def test_all_pages_render_and_demo_seeding_is_idempotent(self):
        for name, args in (("api_home", []), ("api_environment_new", [self.product.pk]),
                           ("api_case_new", [self.product.pk]), ("api_submit", [self.product.pk])):
            self.assertEqual(self.client.get(reverse(f"ai_assistant:{name}", args=args), secure=True).status_code, 200)
        url = reverse("ai_assistant:api_demo", args=[self.product.pk])
        self.client.post(url, secure=True)
        self.client.post(url, secure=True)
        self.assertEqual(APICase.objects.filter(owner=self.owner).count(), 5)
        self.assertEqual(APIEnvironment.objects.filter(owner=self.owner).count(), 2)

    def test_recovery_never_requeues_or_replays_requests(self):
        run = submit_run(self.owner, self.product, self.data())
        APIRun.objects.filter(pk=run.pk).update(status="running")
        with self.assertRaises(CommandError):
            call_command("api_recover_runs", str(run.pk))
        call_command("api_recover_runs", str(run.pk), workers_stopped=True, stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")
        self.assertFalse(execute_next_api_run())
        self.assertEqual(DemoHandler.calls, [])

    def make_chain(self):
        # Deliberately create the consumer first: sequence, not creation ID, governs execution.
        consumer = APICase.objects.create(
            owner=self.owner, product=self.product, name="认证访问", sequence=20, path="/me",
            headers={"Authorization": "Bearer {{session_token}}"},
            extracts={"account_id": "data.id"},
        )
        login = APICase.objects.create(
            owner=self.owner, product=self.product, name="登录", sequence=10, path="/login",
            method="POST", send_body=True, body={"username": "demo", "password": "demo-password"},
            extracts={"session_token": "token"},
        )
        detail = APICase.objects.create(
            owner=self.owner, product=self.product, name="用户详情", sequence=30,
            path="/users/{{account_id}}",
            assertions=[{"path": "data.name", "operator": "equals", "expected": "demo"}],
        )
        return [login, consumer, detail]

    def test_real_login_chain_order_extraction_and_run_isolation(self):
        cases = self.make_chain()
        run = self.run_case(cases=cases)
        results = list(run.results.all())
        self.assertEqual([item.name for item in results], ["登录", "认证访问", "用户详情"])
        self.assertEqual([item.status for item in results], ["passed"] * 3)
        self.assertEqual(DemoHandler.calls, ["/me", "/users/1"])
        serialized = json.dumps(list(run.results.values()), default=str)
        self.assertNotIn("demo-token-not-a-real-credential", serialized)
        self.assertNotIn("demo-token-not-a-real-credential", decrypt_api_key(run.snapshot_encrypted))
        self.env.refresh_from_db()
        self.assertNotIn("session_token", self.env.variables)
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, self.data(cases=cases[1:]))

    def test_invalid_order_is_rejected_before_any_request(self):
        cases = self.make_chain()
        cases[0].sequence = 50
        cases[0].save()
        with self.assertRaisesMessage(ValueError, "缺少变量"):
            submit_run(self.owner, self.product, self.data(cases=cases))
        self.assertEqual(APIRun.objects.count(), 0)

    def test_failed_producer_clears_stale_environment_value_and_skips_dependencies(self):
        cases = self.make_chain()
        self.env.variables["session_token"] = "old-token"
        self.env.variables["account_id"] = 999
        self.env.save()
        cases[0].body["password"] = "wrong"
        cases[0].save()
        run = self.run_case(cases=cases + [self.case])
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["failed", "skipped", "skipped", "passed"])
        self.assertEqual(DemoHandler.calls, ["/health"])
        self.assertIn("session_token", run.results.get(position=1).error)

    def test_assertion_failure_does_not_publish_extracted_token(self):
        cases = self.make_chain()
        cases[0].expected_status = 201
        cases[0].save()
        run = self.run_case(cases=cases)
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["failed", "skipped", "skipped"])
        self.assertNotIn("demo-token-not-a-real-credential", json.dumps(list(run.results.values()), default=str))

    def test_failed_extraction_is_a_visible_check_and_blocks_dependent_request(self):
        cases = self.make_chain()
        cases[0].extracts = {"session_token": "missing.field"}
        cases[0].save()
        run = self.run_case(cases=cases)
        result = run.results.get(position=0)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.checks[-1]["passed"])
        self.assertIn("missing.field", result.checks[-1]["label"])
        self.assertEqual(DemoHandler.calls, [])

    def test_stop_on_failure_and_policy_participates_in_deduplication(self):
        failure = APICase.objects.create(owner=self.owner, product=self.product, name="失败", path="/users/999", sequence=1)
        data = self.data(cases=[failure, self.case], stop_on_failure=True)
        run = submit_run(self.owner, self.product, data)
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, data | {"stop_on_failure": False})
        execute_next_api_run()
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["failed", "skipped"])
        self.assertEqual(DemoHandler.calls, ["/users/999"])
        self.assertIn("前序用例失败", run.results.get(position=1).error)

    def test_boolean_extract_does_not_turn_failed_check_into_truthy_string(self):
        self.case.extracts = {"flag": "enabled"}
        self.case.expected_status = 201
        self.case.save()
        with patch("tcms.ai_assistant.api_runner.send_http", return_value=(200, b'{"enabled":true,"status":"ok"}', 1)):
            result = self.run_case().results.get()
        self.assertEqual(result.status, "failed")
        self.assertIs(result.checks[0]["passed"], False)
        self.assertIs(result.checks[-1]["passed"], True)

    def test_rerun_requires_confirmation_uses_current_config_and_links_reports(self):
        original = self.run_case()
        self.case.expected_status = 201
        self.case.save()
        url = reverse("ai_assistant:api_rerun", args=[original.pk])
        response = self.client.get(url, secure=True)
        self.assertEqual(APIRun.objects.count(), 1)
        self.assertEqual(response.context["form"].initial["cases"], [self.case.pk])
        self.assertNotIn("test_run", response.context["form"].initial)
        post = {"environment": self.env.pk, "cases": [self.case.pk], "submission_token": uuid.uuid4()}
        response = self.client.post(url, post, secure=True)
        self.assertEqual(response.status_code, 302)
        new = APIRun.objects.get(source_run=original)
        self.assertEqual(self.client.post(url, post, secure=True).url, response.url)
        self.assertEqual(APIRun.objects.count(), 2)
        execute_next_api_run()
        self.assertEqual(new.results.get().status, "failed")
        self.assertEqual(original.results.get().status, "passed")
        page = self.client.get(reverse("ai_assistant:api_report", args=[original.pk]), secure=True)
        self.assertContains(page, reverse("ai_assistant:api_report", args=[new.pk]))

    def test_rerun_export_permissions_and_no_partial_report(self):
        run = submit_run(self.owner, self.product, self.data())
        for action in ("api_rerun", "api_export"):
            url = reverse(f"ai_assistant:{action}", args=[run.pk])
            self.assertEqual(self.client.get(url, secure=True).status_code, 409)
            self.client.force_login(self.other)
            self.assertEqual(self.client.get(url, secure=True).status_code, 404)
            self.client.force_login(self.owner)
        execute_next_api_run()
        export = self.client.get(reverse("ai_assistant:api_export", args=[run.pk]), secure=True)
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment", export["Content-Disposition"])
        self.assertNotIn("snapshot_encrypted", export.content.decode())
        self.assertEqual(export.json()["results"][0]["status"], "passed")
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(reverse("ai_assistant:api_rerun", args=[run.pk]), secure=True).status_code, 403)

    def test_export_contains_no_extracted_credentials(self):
        run = self.run_case(cases=self.make_chain())
        export = self.client.get(reverse("ai_assistant:api_export", args=[run.pk]), secure=True)
        self.assertNotIn("demo-token-not-a-real-credential", export.content.decode())
        self.assertNotIn("demo-password", export.content.decode())

    def test_queued_legacy_snapshot_still_executes(self):
        run = submit_run(self.owner, self.product, self.data())
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        for case in snapshot["cases"]:
            case.pop("sequence")
            case.pop("extracts")
        snapshot.pop("stop_on_failure")
        run.snapshot_encrypted = encrypt_api_key(json.dumps(snapshot))
        run.save()
        execute_next_api_run()
        self.assertEqual(run.results.get().status, "passed")

    def test_chain_demo_is_additive_and_does_not_replace_user_edits(self):
        url = reverse("ai_assistant:api_chain_demo", args=[self.product.pk])
        self.client.post(url, secure=True)
        login = APICase.objects.get(owner=self.owner, name="链路演示：登录并提取令牌")
        login.body = {"custom": "keep"}
        login.save()
        self.client.post(url, secure=True)
        self.assertEqual(APICase.objects.filter(owner=self.owner).count(), 4)
        login.refresh_from_db()
        self.assertEqual(login.body, {"custom": "keep"})


@skipUnlessDBFeature("has_select_for_update")
@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://api-demo:8080"])
class APIQueueConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.owner = UserFactory()
        self.product = ProductFactory()
        self.env = APIEnvironment.objects.create(
            owner=self.owner, product=self.product, name="Queue", base_url="http://api-demo:8080"
        )
        self.case = APICase.objects.create(
            owner=self.owner, product=self.product, name="Queue", path="/health"
        )
        self.data = dict(environment=self.env, cases=[self.case], submission_token=uuid.uuid4(),
                         test_run=None, passed_status=None, failed_status=None)

    def concurrent(self, callback):
        barrier = threading.Barrier(2)
        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return callback()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run) for _ in range(2)]
            return [future.result(timeout=20) for future in futures]

    def test_concurrent_submit_creates_one_run(self):
        results = self.concurrent(lambda: submit_run(self.owner, self.product, self.data).pk)
        self.assertEqual(results[0], results[1])
        self.assertEqual(APIRun.objects.count(), 1)
        self.assertEqual(APIRun.objects.get().results.count(), 1)

    def test_two_workers_send_request_only_once(self):
        submit_run(self.owner, self.product, self.data)
        with patch("tcms.ai_assistant.api_runner.send_http", return_value=(200, b'{}', 1)) as transport:
            results = self.concurrent(execute_next_api_run)
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(APIRun.objects.get().status, "completed")
