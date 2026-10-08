import importlib
import json
import threading
import uuid
from datetime import timedelta
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from deployment.api_demo import Handler
from tcms.tests.factories import ProductFactory, UserFactory
from .api_forms import APICaseForm, SuiteForm
from .api_runner import execute_next_api_run, submit_run
from .api_scheduling import queue_suite, rotate_token, suite_data
from .crypto import decrypt_api_key
from .models import APICase, APIEnvironment, APIRun, APISuite


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://api-demo:8080"])
class SuiteOrderTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True)
        self.product = ProductFactory()
        self.env = APIEnvironment.objects.create(owner=self.owner, product=self.product,
                                                name="local", base_url="http://api-demo:8080")
        self.first = APICase.objects.create(owner=self.owner, product=self.product,
                                           name="first", path="/health", sequence=90)
        self.second = APICase.objects.create(owner=self.owner, product=self.product,
                                            name="second", path="/users/1", sequence=10)
        self.suite = APISuite.objects.create(owner=self.owner, product=self.product,
            name="ordered", environment=self.env, case_ids=[self.first.pk, self.second.pk])
        self.client.force_login(self.owner)

    def payload(self, order=None):
        return dict(name="ordered", environment=self.env.pk, interval_minutes=60,
                    cases=[self.first.pk, self.second.pk],
                    ordered_case_ids=json.dumps(order if order is not None else self.suite.case_ids))

    def snapshot_ids(self, run):
        return [item["case_id"] for item in json.loads(decrypt_api_key(run.snapshot_encrypted))["cases"]]

    def test_script_create_and_edit_do_not_expose_global_sequence(self):
        for instance in (APICase(owner=self.owner, product=self.product), self.first):
            form = APICaseForm(instance=instance, owner=self.owner, product=self.product)
            self.assertNotIn("sequence", form.fields)
        response = self.client.get(reverse("ai_assistant:api_case_new", args=[self.product.pk]), secure=True)
        self.assertNotContains(response, 'name="sequence"')

    def test_suite_save_reopen_and_detail_follow_explicit_order(self):
        order = [self.second.pk, self.first.pk]
        response = self.client.post(reverse("ai_assistant:api_suite_edit",
            args=[self.product.pk, self.suite.pk]), self.payload(order), secure=True)
        self.assertEqual(response.status_code, 302)
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.case_ids, order)
        page = self.client.get(reverse("ai_assistant:api_suite_edit", args=[self.product.pk, self.suite.pk]), secure=True)
        self.assertEqual(page.context["form"].initial["ordered_case_ids"], order)
        detail = self.client.get(reverse("ai_assistant:api_suite", args=[self.suite.pk]), secure=True)
        self.assertEqual([step["case_id"] for step in detail.context["suite_steps"]], order)

    def test_form_orders_cleaned_cases_not_just_saved_ids(self):
        order = [self.second.pk, self.first.pk]
        form = SuiteForm(self.payload(order), instance=self.suite, owner=self.owner, product=self.product)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual([case.pk for case in form.cleaned_data["cases"]], order)
        self.assertEqual(form.save().case_ids, order)

    def test_no_javascript_post_uses_selection_order(self):
        data = self.payload()
        del data["ordered_case_ids"]
        data["cases"] = [self.second.pk, self.first.pk]
        form = SuiteForm(data, owner=self.owner, product=self.product)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save(commit=False).case_ids, data["cases"])

    def test_invalid_order_duplicate_foreign_or_missing_rejected(self):
        foreign = APICase.objects.create(owner=UserFactory(), product=self.product, name="foreign", path="/health")
        for order in ([self.first.pk, self.first.pk], [self.first.pk], [self.first.pk, foreign.pk],
                      [True, self.second.pk], {}, False, "wrong"):
            with self.subTest(order=order):
                form = SuiteForm(self.payload(order), owner=self.owner, product=self.product)
                self.assertFalse(form.is_valid())
        data = self.payload()
        data["cases"] = [self.first.pk, self.first.pk, self.second.pk]
        del data["ordered_case_ids"]
        self.assertFalse(SuiteForm(data, owner=self.owner, product=self.product).is_valid())

    def test_suites_can_have_opposite_orders_and_ignore_script_sequence(self):
        other = APISuite.objects.create(owner=self.owner, product=self.product, name="reverse",
            environment=self.env, case_ids=list(reversed(self.suite.case_ids)))
        a = queue_suite(self.suite.pk, self.owner.pk)
        b = queue_suite(other.pk, self.owner.pk)
        self.assertEqual(self.snapshot_ids(a), self.suite.case_ids)
        self.assertEqual(self.snapshot_ids(b), other.case_ids)
        APICase.objects.filter(pk=self.first.pk).update(sequence=0)
        self.assertEqual(self.snapshot_ids(a), self.suite.case_ids)
        APIRun.objects.filter(pk=a.pk).update(status="completed")
        c = queue_suite(self.suite.pk, self.owner.pk)
        self.assertEqual(self.snapshot_ids(c), self.suite.case_ids)

    def test_schedule_uses_suite_order(self):
        self.suite.schedule_enabled = True
        self.suite.next_run_at = timezone.now() - timedelta(seconds=1)
        self.suite.save()
        run = queue_suite(self.suite.pk, self.owner.pk, trigger="schedule")
        self.assertEqual(self.snapshot_ids(run), self.suite.case_ids)

    def test_ci_uses_suite_order_and_retry_keeps_original_snapshot(self):
        token = rotate_token(self.suite)
        key = uuid.uuid4()
        url = reverse("ai_assistant:api_ci_submit", args=[self.suite.pk])
        headers = dict(HTTP_AUTHORIZATION="Bearer " + token, HTTP_IDEMPOTENCY_KEY=str(key), secure=True)
        response = self.client.post(url, **headers)
        self.assertEqual(response.status_code, 202)
        run = APIRun.objects.get(pk=response.json()["run_id"])
        self.assertEqual(self.snapshot_ids(run), self.suite.case_ids)
        APISuite.objects.filter(pk=self.suite.pk).update(case_ids=list(reversed(self.suite.case_ids)))
        retry = self.client.post(url, **headers)
        self.assertEqual(retry.json()["run_id"], str(run.pk))
        self.assertEqual(self.snapshot_ids(run), self.suite.case_ids)

    def test_suite_edit_does_not_change_queued_snapshot(self):
        run = queue_suite(self.suite.pk, self.owner.pk)
        original = run.snapshot_encrypted
        APISuite.objects.filter(pk=self.suite.pk).update(case_ids=list(reversed(self.suite.case_ids)))
        run.refresh_from_db()
        self.assertEqual(run.snapshot_encrypted, original)

    def test_rerun_initial_order_comes_from_actual_snapshot(self):
        run = queue_suite(self.suite.pk, self.owner.pk)
        APIRun.objects.filter(pk=run.pk).update(status="completed")
        APISuite.objects.filter(pk=self.suite.pk).update(case_ids=list(reversed(self.suite.case_ids)))
        APICase.objects.filter(pk=self.first.pk).update(sequence=1)
        response = self.client.get(reverse("ai_assistant:api_rerun", args=[run.pk]), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].initial["ordered_case_ids"], self.suite.case_ids)

    def test_order_is_part_of_submission_idempotency(self):
        data = suite_data(self.suite, uuid.uuid4())
        run = submit_run(self.owner, self.product, data)
        self.assertEqual(submit_run(self.owner, self.product, data).pk, run.pk)
        data["ordered_case_ids"] = list(reversed(data["ordered_case_ids"]))
        with self.assertRaises(ValueError):
            submit_run(self.owner, self.product, data)

    def test_dataset_groups_repeat_suite_order(self):
        data = suite_data(self.suite, uuid.uuid4())
        data["datasets"] = [{"n": 1}, {"n": 2}]
        run = submit_run(self.owner, self.product, data)
        self.assertEqual(self.snapshot_ids(run), self.suite.case_ids * 2)

    def test_dependency_validation_uses_suite_order_not_global_sequence(self):
        self.first.extracts = {"user_id": "data.id"}
        self.first.path = "/users/1"
        self.first.save()
        self.second.path = "/users/{{user_id}}"
        self.second.save()
        response = self.client.post(reverse("ai_assistant:api_suite_edit",
            args=[self.product.pk, self.suite.pk]), self.payload(), secure=True)
        self.assertEqual(response.status_code, 302)
        run = queue_suite(self.suite.pk, self.owner.pk)
        self.assertEqual(self.snapshot_ids(run), self.suite.case_ids)
        bad = self.client.post(reverse("ai_assistant:api_suite_edit",
            args=[self.product.pk, self.suite.pk]), self.payload(list(reversed(self.suite.case_ids))), secure=True)
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "缺少变量")
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.case_ids, [self.first.pk, self.second.pk])

    def test_invalid_suite_references_fail_closed(self):
        for order in ([self.first.pk, self.first.pk], [self.first.pk, 987654321]):
            self.suite.case_ids = order
            with self.assertRaises(ValueError):
                suite_data(self.suite, uuid.uuid4())
        self.assertFalse(APIRun.objects.exists())

    def test_migration_preserves_previous_execution_order_and_missing_ids(self):
        self.suite.case_ids = [self.first.pk, self.second.pk, 987654321]
        self.suite.save()
        module = importlib.import_module("tcms.ai_assistant.migrations.0039_suite_script_order")
        state = MigrationExecutor(connection).loader.project_state()
        module.preserve_execution_order(state.apps, SimpleNamespace(connection=connection))
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.case_ids, [self.second.pk, self.first.pk, 987654321])
        module.preserve_execution_order(state.apps, SimpleNamespace(connection=connection))
        self.suite.refresh_from_db()
        self.assertEqual(self.suite.case_ids, [self.second.pk, self.first.pk, 987654321])
        self.assertEqual(APICase.objects.count(), 2)


class SuiteOrderExecutionTests(TestCase):
    def test_worker_executes_dependency_chain_in_suite_order(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with override_settings(API_AUTOMATION_ALLOWED_ORIGINS=[origin]):
                owner, product = UserFactory(is_superuser=True), ProductFactory()
                env = APIEnvironment.objects.create(owner=owner, product=product, name="local", base_url=origin)
                consumer = APICase.objects.create(owner=owner, product=product, name="consumer",
                    path="/users/{{user_id}}", sequence=1,
                    assertions=[{"path": "data.id", "operator": "equals", "expected": 1}])
                producer = APICase.objects.create(owner=owner, product=product, name="producer",
                    path="/users/1", sequence=99, extracts={"user_id": "data.id"})
                suite = APISuite.objects.create(owner=owner, product=product, name="chain",
                    environment=env, case_ids=[producer.pk, consumer.pk])
                run = queue_suite(suite.pk, owner.pk)
                execute_next_api_run()
                self.assertEqual(list(run.results.values_list("name", "status")), [
                    ("producer", "passed"), ("consumer", "passed")])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)
