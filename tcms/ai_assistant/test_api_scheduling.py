import importlib
import json
import threading
import uuid
from datetime import timedelta
from http.server import ThreadingHTTPServer
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from types import SimpleNamespace
from django.db import connection, close_old_connections
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from guardian.shortcuts import assign_perm

from deployment.api_demo import Handler
from deployment.kiwi_ci import write_reports, main
from tcms.tests.factories import ProductFactory, TestCaseFactory, UserFactory
from tcms.testcases.models import TestCase as PlatformCase
from .api_runner import execute_next_api_run, submit_run
from .api_scheduling import dispatch_due_suites, queue_suite, rotate_token
from .crypto import decrypt_api_key
from .models import APICase, APIEnvironment, APIRun, APISuite


class CookieHandler(Handler):
    def do_GET(self):
        if self.path == "/cookie/set":
            self.reply(200, {"status": "ok", "echo": "private-session-cookie"},
                       "session=private-session-cookie; Path=/cookie/; HttpOnly")
        elif self.path == "/cookie/delete":
            self.reply(200, {"status": "ok"}, "session=deleted-cookie; Path=/cookie/; Max-Age=0")
        elif self.path == "/cookie/secure":
            self.reply(200, {"status": "ok"}, "session=secure-cookie; Path=/cookie/; Secure")
        elif self.path in ("/cookie/echo", "/outside"):
            self.reply(200, {"received": self.headers.get("Cookie", "")})
        else:
            super().do_GET()


class SuiteFlowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CookieHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        super().tearDownClass()

    def setUp(self):
        self.override = override_settings(API_AUTOMATION_ALLOWED_ORIGINS=[self.origin])
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.owner = UserFactory(is_superuser=True)
        self.other = UserFactory()
        self.product = ProductFactory()
        self.env = APIEnvironment.objects.create(owner=self.owner, product=self.product,
            name="test", base_url=self.origin)
        self.case = APICase.objects.create(owner=self.owner, product=self.product, name="health", path="/health")
        self.suite = APISuite.objects.create(owner=self.owner, product=self.product, name="smoke",
            environment=self.env, case_ids=[self.case.pk])
        self.client.force_login(self.owner)

    def run_configs(self, cases, share=True):
        run = submit_run(self.owner, self.product, dict(environment=self.env, cases=cases,
            share_cookies=share, submission_token=uuid.uuid4()))
        execute_next_api_run()
        run.refresh_from_db()
        return run

    def config(self, path, **kwargs):
        return APICase.objects.create(owner=self.owner, product=self.product, name=path, path=path, **kwargs)

    def ci(self, token, key=None, client=None):
        return (client or self.client).post(reverse("ai_assistant:api_ci_submit", args=[self.suite.pk]),
            HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_IDEMPOTENCY_KEY=str(key or uuid.uuid4()), secure=True)

    def test_cookies_shared_only_within_run_and_redacted(self):
        login = self.config("/cookie/set")
        who = self.config("/cookie/echo", assertions=[{"path": "received", "operator": "equals",
                                                      "expected": "session=private-session-cookie"}])
        run = self.run_configs([login, who])
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["passed", "passed"])
        self.assertNotIn("private-session-cookie", json.dumps(list(run.results.values()), default=str))
        self.assertNotIn('"_cookies"', decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(self.run_configs([who]).results.get().status, "failed")
        self.assertEqual(list(self.run_configs([login, who], share=False).results.values_list("status", flat=True)), ["passed", "failed"])

    def test_cookie_path_secure_expiration_and_explicit_header(self):
        for setter, target in (("/cookie/set", "/outside"), ("/cookie/secure", "/cookie/echo")):
            cases = [self.config(setter), self.config(target, assertions=[{"path": "received", "operator": "equals", "expected": ""}])]
            self.assertEqual(list(self.run_configs(cases).results.values_list("status", flat=True)), ["passed", "passed"])
        cases = [self.config("/cookie/set"), self.config("/cookie/delete"), self.config("/cookie/echo",
            assertions=[{"path": "received", "operator": "equals", "expected": ""}])]
        self.assertTrue(all(r.status == "passed" for r in self.run_configs(cases).results.all()))
        explicit = self.config("/cookie/echo", headers={"Cookie": "explicit=yes"},
            assertions=[{"path": "received", "operator": "equals", "expected": "explicit=yes"}])
        self.assertEqual(self.run_configs([cases[0], explicit]).results.last().status, "passed")

    def test_schedule_due_once_coalesces_and_skips_overlap(self):
        self.suite.schedule_enabled = True
        self.suite.next_run_at = timezone.now() - timedelta(days=2)
        self.suite.save()
        self.assertEqual(dispatch_due_suites(), 1)
        self.assertEqual(dispatch_due_suites(), 0)
        self.assertEqual(APIRun.objects.get().trigger, "schedule")
        self.suite.refresh_from_db()
        self.assertGreater(self.suite.next_run_at, timezone.now())
        APISuite.objects.filter(pk=self.suite.pk).update(next_run_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(dispatch_due_suites(), 0)
        self.assertEqual(APIRun.objects.count(), 1)
        self.suite.refresh_from_db()
        self.assertIn("跳过", self.suite.last_error)

    def test_schedule_invalid_configuration_records_failure_without_partial_run(self):
        self.suite.schedule_enabled = True
        self.suite.next_run_at = timezone.now() - timedelta(seconds=1)
        self.suite.case_ids = [987654321]
        self.suite.save()
        self.assertEqual(dispatch_due_suites(), 0)
        self.suite.refresh_from_db()
        self.assertTrue(self.suite.last_error)
        self.assertGreater(self.suite.next_run_at, timezone.now())
        self.assertFalse(APIRun.objects.exists())

    def test_inactive_owner_and_paused_schedules_do_not_run(self):
        self.suite.next_run_at = timezone.now() - timedelta(seconds=1)
        self.suite.save()
        self.assertEqual(dispatch_due_suites(), 0)
        self.suite.schedule_enabled = True
        self.suite.save()
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(dispatch_due_suites(), 0)
        self.suite.refresh_from_db()
        self.assertFalse(self.suite.schedule_enabled)

    def test_ci_requires_scoped_bearer_and_uuid_even_when_browser_logged_in(self):
        token = rotate_token(self.suite)
        self.assertNotIn(token, self.suite.ci_token_hash)
        self.assertEqual(self.ci("bad").status_code, 401)
        self.assertEqual(self.client.post(reverse("ai_assistant:api_ci_submit", args=[self.suite.pk]), secure=True).status_code, 401)
        self.assertEqual(self.ci(token, key="not-uuid").status_code, 400)
        self.assertFalse(APIRun.objects.exists())
        response = self.ci(token, client=Client(enforce_csrf_checks=True))
        self.assertEqual(response.status_code, 202)

    def test_ci_idempotence_polling_and_failed_exit_contract(self):
        token, key = rotate_token(self.suite), uuid.uuid4()
        first = self.ci(token, key)
        self.assertEqual(first.status_code, 202)
        # Same build retry returns its immutable run even if suite settings change.
        self.suite.name = "changed"
        self.suite.save()
        self.assertEqual(self.ci(token, key).json()["run_id"], first.json()["run_id"])
        self.assertEqual(self.ci(token).status_code, 409)
        execute_next_api_run()
        report_url = reverse("ai_assistant:api_ci_run", args=[self.suite.pk, first.json()["run_id"]])
        payload = self.client.get(report_url, HTTP_AUTHORIZATION=f"Bearer {token}", secure=True).json()
        self.assertTrue(payload["terminal"])
        self.assertTrue(payload["passed"])
        self.case.expected_status = 404
        self.case.save()
        fail = self.ci(token).json()
        execute_next_api_run()
        payload = self.client.get(reverse("ai_assistant:api_ci_run", args=[self.suite.pk, fail["run_id"]]),
                                 HTTP_AUTHORIZATION=f"Bearer {token}", secure=True).json()
        self.assertFalse(payload["passed"])
        with TemporaryDirectory() as directory:
            output = str(Path(directory) / "report.json")
            write_reports(payload, output)
            self.assertEqual(ElementTree.parse(Path(directory) / "report.xml").getroot().attrib["failures"], "1")

    def test_ci_token_rotation_revocation_expiry_and_report_isolation(self):
        token = rotate_token(self.suite)
        run_id = self.ci(token).json()["run_id"]
        other_suite = APISuite.objects.create(owner=self.other, product=self.product, name="other",
            environment=self.env, case_ids=[self.case.pk])
        other_token = rotate_token(other_suite)
        url = reverse("ai_assistant:api_ci_run", args=[other_suite.pk, run_id])
        self.assertEqual(self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {other_token}", secure=True).status_code, 404)
        rotate_token(self.suite)
        self.assertEqual(self.ci(token).status_code, 401)
        token = rotate_token(self.suite)
        self.suite.ci_token_expires = timezone.now() - timedelta(seconds=1)
        self.suite.save()
        self.assertEqual(self.ci(token).status_code, 401)
        rotate_token(self.suite)
        self.client.post(reverse("ai_assistant:api_suite_action", args=[self.suite.pk, "revoke-token"]), secure=True)
        self.suite.refresh_from_db()
        self.assertFalse(self.suite.ci_token_hash)

    def test_suite_pages_token_shown_once_csrf_and_ownership(self):
        for name, args in (("api_suite", [self.suite.pk]), ("api_suite_edit", [self.product.pk, self.suite.pk]),
                           ("api_suite_new", [self.product.pk]), ("case_library", [])):
            self.assertEqual(self.client.get(reverse(f"ai_assistant:{name}", args=args), secure=True).status_code, 200)
        action = reverse("ai_assistant:api_suite_action", args=[self.suite.pk, "rotate-token"])
        self.assertEqual(self.client.get(action, secure=True).status_code, 405)
        secure_client = Client(enforce_csrf_checks=True)
        secure_client.force_login(self.owner)
        self.assertEqual(secure_client.post(action, secure=True).status_code, 403)
        response = self.client.post(action, secure=True)
        token = response.context["new_token"]
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotContains(self.client.get(reverse("ai_assistant:api_suite", args=[self.suite.pk]), secure=True), token)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(action, secure=True).status_code, 404)

    def test_create_suite_validates_scope_and_saves_timing_and_cookies(self):
        url = reverse("ai_assistant:api_suite_new", args=[self.product.pk])
        data = dict(name="nightly", environment=self.env.pk, cases=[self.case.pk],
            interval_minutes=5, schedule_enabled="on", share_cookies="on")
        response = self.client.post(url, data, secure=True)
        self.assertEqual(response.status_code, 302)
        suite = APISuite.objects.get(name="nightly")
        self.assertTrue(suite.share_cookies)
        self.assertTrue(suite.schedule_enabled)
        self.assertGreater(suite.next_run_at, timezone.now())
        self.assertEqual(suite.case_ids, [self.case.pk])
        self.assertEqual(self.client.post(url, data | {"interval_minutes": 0}, secure=True).status_code, 200)
        self.assertEqual(APISuite.objects.count(), 2)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, data, secure=True).status_code, 200)
        self.assertEqual(APISuite.objects.count(), 2)

    def test_new_automatic_case_continues_to_config_with_one_business_record(self):
        seed = TestCaseFactory(category__product=self.product)
        response = self.client.post(reverse("ai_assistant:library_case_new", args=[self.product.pk]),
            dict(summary="统一场景", category=seed.category_id, priority=seed.priority_id,
                 case_status=seed.case_status_id, execution_type="api", text="步骤与预期"), secure=True)
        case = PlatformCase.objects.get(summary="统一场景")
        self.assertTrue(case.is_automated)
        self.assertIn(f"test_case={case.pk}", response.url)
        config_page = self.client.get(response.url, secure=True)
        self.assertEqual(config_page.context["form"].initial["name"], case.summary)
        response = self.client.post(reverse("ai_assistant:api_case_new", args=[self.product.pk]),
            dict(name="config", sequence=10, method="GET", path="/health", expected_status=200,
                 max_elapsed_ms=0, test_case=case.pk), secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(PlatformCase.objects.filter(summary="统一场景").count(), 1)
        config = APICase.objects.get(test_case=case)
        run = self.run_configs([config])
        self.assertEqual(run.results.get().test_case_id, case.pk)
        self.assertEqual(run.results.get().name, "统一场景")
        self.assertContains(self.client.get(reverse("testcases-get", args=[case.pk]), secure=True), "自动化执行")

    def test_library_groups_linked_configs_filters_and_preserves_permissions(self):
        manual = TestCaseFactory(category__product=self.product, is_automated=False)
        automated = TestCaseFactory(category=manual.category, is_automated=False)
        self.case.test_case = automated
        self.case.save()
        url = reverse("ai_assistant:case_library")
        auto_page = self.client.get(url, {"product": self.product.pk, "type": "automated"}, secure=True)
        self.assertContains(auto_page, automated.summary)
        self.assertNotContains(auto_page, manual.summary)
        self.assertContains(auto_page, "自动化执行")
        manual_page = self.client.get(url, {"product": self.product.pk, "type": "manual"}, secure=True)
        self.assertContains(manual_page, manual.summary)
        self.assertNotContains(manual_page, automated.summary)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(url, {"product": self.product.pk}, secure=True), automated.summary)
        assign_perm("view_testcase", self.other, automated)
        other_page = self.client.get(url, {"product": self.product.pk}, secure=True)
        self.assertContains(other_page, automated.summary)
        self.assertNotContains(other_page, reverse("ai_assistant:api_case_edit", args=[self.product.pk, self.case.pk]))

    def test_legacy_migration_preserves_config_and_creates_history_once(self):
        TestCaseFactory(category__product=self.product)
        migration = importlib.import_module("tcms.ai_assistant.migrations.0021_unified_case_repository")
        state = MigrationExecutor(connection).loader.project_state()
        editor = SimpleNamespace(connection=connection)
        migration.unify_cases(state.apps, editor)
        migration.unify_cases(state.apps, editor)
        self.case.refresh_from_db()
        case = self.case.test_case
        self.assertTrue(case.is_automated)
        self.assertEqual(case.history.count(), 1)
        self.assertEqual(case.summary, "health")
        self.assertEqual(APICase.objects.count(), 1)


class SuiteConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    @override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://api-demo:8080"])
    def test_two_schedulers_enqueue_one_due_tick(self):
        owner = UserFactory()
        product = ProductFactory()
        env = APIEnvironment.objects.create(owner=owner, product=product, name="local", base_url="http://api-demo:8080")
        case = APICase.objects.create(owner=owner, product=product, name="health", path="/health")
        suite = APISuite.objects.create(owner=owner, product=product, name="scheduled", environment=env,
            case_ids=[case.pk], schedule_enabled=True, next_run_at=timezone.now() - timedelta(minutes=1))
        barrier = threading.Barrier(2)
        errors = []
        def submit():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                queue_suite(suite.pk, owner.pk, trigger="schedule")
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertEqual(APIRun.objects.count(), 1)


class CIClientTests(SimpleTestCase):
    def test_exit_codes_and_authorization_headers(self):
        from unittest.mock import MagicMock
        for passed, expected_code in ((True, 0), (False, 1)):
            response = MagicMock()
            payload = dict(run_id=str(uuid.uuid4()), terminal=True, passed=passed, status="completed",
                           results=[dict(name="health", status="passed" if passed else "failed", elapsed_ms=2)])
            response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            opener = MagicMock()
            opener.open.return_value = response
            with TemporaryDirectory() as directory, patch.dict("os.environ", {
                "KIWI_BASE_URL": "https://kiwi.example.test", "KIWI_CI_TOKEN": "private-ci-token"}, clear=True), patch(
                    "deployment.kiwi_ci.build_opener", return_value=opener), patch("builtins.print") as printed:
                self.assertEqual(main(["--suite", "1", "--output", str(Path(directory) / "report.json")]), expected_code)
                self.assertTrue((Path(directory) / "report.xml").exists())
                self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer private-ci-token")
                self.assertNotIn("private-ci-token", str(printed.call_args_list))

    def test_plain_http_and_network_failure_fail_closed(self):
        with patch.dict("os.environ", {"KIWI_BASE_URL": "http://example.test", "KIWI_CI_TOKEN": "token"}, clear=True), patch(
            "deployment.kiwi_ci.build_opener") as opener, patch("builtins.print"):
            self.assertEqual(main(["--suite", "1"]), 2)
            opener.assert_not_called()
        with patch.dict("os.environ", {"KIWI_BASE_URL": "https://example.test", "KIWI_CI_TOKEN": "token"}, clear=True), patch(
            "deployment.kiwi_ci.build_opener", side_effect=OSError), patch("builtins.print"):
            self.assertEqual(main(["--suite", "1"]), 2)
