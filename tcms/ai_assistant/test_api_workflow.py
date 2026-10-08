import json
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import (UserFactory, ProductFactory, VersionFactory, BuildFactory,
                                  TestPlanFactory, TestCaseFactory)
from tcms.testruns.models import TestRun, TestExecutionStatus
from .api_runner import execute_next_api_run, submit_run
from .automation_archive import ArchiveForm, publish
from .crypto import decrypt_api_key, encrypt_api_key
from .models import APICase, APIEnvironment, APIRun, APISuite, AutomationArchive


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://127.0.0.1:8080"],
                   EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class APIFormalWorkflowTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True, is_active=True)
        self.product = ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version, is_active=True)
        self.build = BuildFactory(version=self.version, is_active=True)
        self.business = TestCaseFactory(category__product=self.product)
        self.business.save()
        self.plan.add_case(self.business)
        self.env = APIEnvironment.objects.create(owner=self.owner, product=self.product, name="QA",
            base_url="http://127.0.0.1:8080", variables={"password": "never-show-this"},
            secret_headers_encrypted=encrypt_api_key('{"Authorization":"secret-header"}'))
        self.config = APICase.objects.create(owner=self.owner, product=self.product,
            name="Health", path="/health", test_case=self.business)
        self.suite = APISuite.objects.create(owner=self.owner, product=self.product, name="Smoke",
            environment=self.env, case_ids=[self.config.pk])
        self.states = {key: TestExecutionStatus.objects.create(name="API formal " + key, weight=weight)
                       for key, weight in (("passed", 1), ("failed", -1), ("pending", 0))}
        self.client.force_login(self.owner)
        self.url = reverse("ai_assistant:api_suite_submit", args=[self.suite.pk])

    def submit(self, **changes):
        data = dict(token=str(uuid.uuid4()), execution_mode="formal", plan=self.plan.pk,
                    build=self.build.pk, environment=self.env.pk)
        data.update(changes)
        return self.client.post(self.url, data)

    def archive(self, run, **changes):
        data = dict(self.states, plan=self.plan, build=self.build, confirm=True, defects=[])
        data.update(changes)
        return publish(self.owner, "api", run.pk, data)

    def finish(self, run, status="passed"):
        run.status, run.completed = "completed", timezone.now()
        run.save(update_fields=("status", "completed"))
        run.results.update(status=status)

    def test_preview_is_read_only_and_does_not_leak_secrets(self):
        response = self.client.get(self.url)
        self.assertContains(response, "执行预览")
        self.assertContains(response, "新建环境")
        self.assertNotContains(response, "never-show-this")
        self.assertNotContains(response, "secret-header")
        self.assertFalse(APIRun.objects.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_formal_freezes_context_and_creates_pending_native_task(self):
        self.assertEqual(self.submit().status_code, 302)
        run = APIRun.objects.get()
        execution = run.test_run.executions.get()
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(run.execution_mode, "formal")
        self.assertEqual(run.test_run.plan_id, self.plan.pk)
        self.assertEqual(run.test_run.build_id, self.build.pk)
        self.assertEqual(execution.status.weight, 0)
        self.assertEqual(execution.case_text_version, self.business.history.latest().history_id)
        self.assertEqual(snapshot["execution_context"]["test_run_id"], run.test_run_id)
        self.assertEqual(snapshot["cases"][0]["execution_id"], execution.pk)

    def test_worker_does_not_write_back_before_confirmation(self):
        self.submit()
        with patch("tcms.ai_assistant.api_runner.send_http", return_value=(200, b'{"ok":true}', 3)) as send:
            self.assertTrue(execute_next_api_run())
        self.assertEqual(send.call_count, 1)
        run = APIRun.objects.get()
        self.assertEqual(run.status, "completed")
        self.assertEqual(run.test_run.executions.get().status.weight, 0)
        self.assertEqual(run.results.get().writeback, "待确认后归档回写")
        saved = self.archive(run)
        self.assertEqual(saved.test_run_id, run.test_run_id)
        self.assertEqual(saved.test_run.executions.get().status.weight, 1)
        self.assertEqual(self.archive(run).pk, saved.pk)
        self.assertEqual(TestRun.objects.count(), 1)

    def test_debug_creates_no_native_task_and_cannot_archive(self):
        self.assertEqual(self.submit(execution_mode="debug", plan="", build="", environment="").status_code, 302)
        run = APIRun.objects.get()
        self.assertEqual(run.execution_mode, "debug")
        self.assertIsNone(run.test_run_id)
        self.finish(run)
        self.assertEqual(self.client.get(reverse("ai_assistant:automation_archive", args=["api", run.pk])).status_code, 409)
        with self.assertRaisesMessage(ValueError, "调试执行不进入正式报告"):
            self.archive(run)
        self.assertFalse(TestRun.objects.exists())

    def test_repeat_is_idempotent_but_changed_selection_is_rejected(self):
        token = str(uuid.uuid4())
        self.submit(token=token)
        self.assertEqual(self.submit(token=token).status_code, 302)
        self.assertContains(self.submit(token=token, execution_mode="debug"), "已提交过其他选择")
        self.assertEqual(APIRun.objects.count(), 1)
        self.assertEqual(TestRun.objects.count(), 1)

    def test_environment_override_does_not_edit_suite(self):
        other = APIEnvironment.objects.create(owner=self.owner, product=self.product, name="Another QA",
            base_url=self.env.base_url, timeout=7)
        self.submit(environment=other.pk)
        self.suite.refresh_from_db()
        run = APIRun.objects.get()
        self.assertEqual(self.suite.environment_id, self.env.pk)
        self.assertEqual(run.environment_name, other.name)
        self.assertEqual(json.loads(decrypt_api_key(run.snapshot_encrypted))["environment"]["timeout"], 7)

    def test_missing_link_or_case_outside_plan_blocks_formal(self):
        self.config.test_case = None
        self.config.save()
        self.assertContains(self.submit(), "关联到用例库")
        self.config.test_case = self.business
        self.config.save()
        other_plan = TestPlanFactory(product=self.product, product_version=self.version, is_active=True)
        self.assertContains(self.submit(plan=other_plan.pk), "加入所选测试计划")
        self.assertFalse(APIRun.objects.exists())
        self.assertFalse(TestRun.objects.exists())

    def test_missing_environment_and_mismatching_build_are_rejected(self):
        self.assertEqual(self.submit(environment="").status_code, 200)
        other = BuildFactory(version=VersionFactory(product=self.product), is_active=True)
        self.assertContains(self.submit(build=other.pk), "构建版本必须与测试计划版本一致")
        self.assertFalse(TestRun.objects.exists())

    def test_other_account_cannot_access_or_select_environment(self):
        other = UserFactory()
        foreign = APIEnvironment.objects.create(owner=other, product=self.product, name="private",
                                                base_url=self.env.base_url)
        self.assertEqual(self.submit(environment=foreign.pk).status_code, 200)
        self.client.force_login(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.submit().status_code, 404)
        self.assertFalse(APIRun.objects.exists())

    def test_import_review_and_invalid_request_roll_back_native_task(self):
        self.config.import_review_required = True
        self.config.save()
        self.assertContains(self.submit(), "待复核")
        self.assertFalse(TestRun.objects.exists())
        self.config.import_review_required = False
        self.config.path = "/{{missing_variable}}"
        self.config.save()
        self.assertContains(self.submit(), "缺少变量")
        self.assertFalse(TestRun.objects.exists())
        self.assertFalse(APIRun.objects.exists())

    def test_suite_overlap_does_not_create_orphan_native_task(self):
        self.submit()
        self.assertContains(self.submit(), "已有排队或执行中的任务")
        self.assertEqual(TestRun.objects.count(), 1)

    def test_datasets_and_order_map_to_distinct_execution_instances(self):
        second = APICase.objects.create(owner=self.owner, product=self.product, name="Second",
                                       path="/second", test_case=self.business)
        self.suite.case_ids = [second.pk, self.config.pk]
        self.suite.datasets_encrypted = encrypt_api_key('[{"row":1},{"row":2}]')
        self.suite.save()
        self.assertEqual(self.submit().status_code, 302)
        run = APIRun.objects.get()
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual([r["case_id"] for r in snapshot["cases"]], [second.pk, self.config.pk] * 2)
        self.assertEqual(len(set(r["execution_id"] for r in snapshot["cases"])), 4)
        self.assertEqual(run.test_run.executions.count(), 4)
        self.finish(run)
        self.assertEqual(self.archive(run).test_run.executions.count(), 4)

    def test_archive_retains_submitted_case_version_after_case_edit(self):
        self.submit()
        run = APIRun.objects.get()
        version = run.test_run.executions.get().case_text_version
        self.business.summary = "Edited after submission"
        self.business.save()
        self.finish(run)
        self.assertEqual(self.archive(run).results[0]["business_case_version"], version)

    def test_archive_disallows_rebinding_plan_build_and_changed_native_context(self):
        self.submit()
        run = APIRun.objects.get()
        self.finish(run)
        other = BuildFactory(version=self.version, is_active=True)
        with self.assertRaisesMessage(ValueError, "提交时选定的计划和构建"):
            self.archive(run, build=other)
        run.test_run.build = other
        run.test_run.save()
        with self.assertRaisesMessage(ValueError, "关联已变更"):
            self.archive(run)
        self.assertFalse(AutomationArchive.objects.exists())

    def test_archive_form_locks_native_binding(self):
        self.submit()
        run = APIRun.objects.get()
        form = ArchiveForm(owner=self.owner, product=self.product, rows=[], source=run)
        self.assertTrue(all(form.fields[key].disabled for key in ("plan", "build", "target_run")))

    def test_request_errors_are_not_published_as_passed(self):
        self.submit()
        run = APIRun.objects.get()
        self.finish(run, "error")
        self.assertEqual(self.archive(run).test_run.executions.get().status.weight, 0)

    def test_historical_rerun_is_debug_and_cannot_write_to_target(self):
        self.submit()
        source = APIRun.objects.get()
        self.finish(source)
        data = dict(environment=self.env, cases=[self.config], submission_token=uuid.uuid4())
        rerun = submit_run(self.owner, self.product, data, source_run=source)
        self.assertEqual(rerun.execution_mode, "debug")
        with self.assertRaisesMessage(ValueError, "仅用于调试"):
            submit_run(self.owner, self.product, dict(data, submission_token=uuid.uuid4(),
                       test_run=source.test_run, passed_status=self.states["passed"], failed_status=self.states["failed"]),
                       source_run=source)

    def test_legacy_execute_action_opens_preview_without_queuing(self):
        response = self.client.post(reverse("ai_assistant:api_suite_action", args=[self.suite.pk, "execute"]))
        self.assertRedirects(response, self.url)
        self.assertFalse(APIRun.objects.exists())

    def test_legacy_metadata_remains_compatible(self):
        run = submit_run(self.owner, self.product, dict(environment=self.env, cases=[self.config],
                                                       submission_token=uuid.uuid4()))
        self.assertEqual(run.execution_mode, "legacy")

    def test_invalid_mode_metadata_cannot_be_archived(self):
        self.submit()
        run = APIRun.objects.get()
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        snapshot["execution_mode"] = "incorrect"
        run.snapshot_encrypted = encrypt_api_key(json.dumps(snapshot))
        self.finish(run)
        run.save(update_fields=("snapshot_encrypted",))
        with self.assertRaisesMessage(ValueError, "无法确认执行方式"):
            self.archive(run)

    def test_read_only_account_cannot_submit_and_form_is_disabled(self):
        self.owner.is_superuser = False
        self.owner.save()
        self.owner.groups.add(Group.objects.get_or_create(name="AI 只读")[0])
        self.assertContains(self.client.get(self.url), "fieldset disabled")
        self.assertEqual(self.submit(execution_mode="debug").status_code, 403)
        self.assertFalse(APIRun.objects.exists())

    def test_non_privileged_account_can_debug_but_not_create_formal_task(self):
        self.owner.is_superuser = False
        self.owner.save()
        response = self.submit()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TestRun.objects.exists())
        self.assertEqual(self.submit(execution_mode="debug", plan="", build="").status_code, 302)

    def test_account_queue_limit_prevents_orphan_native_task(self):
        APIRun.objects.bulk_create([APIRun(owner=self.owner, product=self.product,
            submission_token=uuid.uuid4(), snapshot_encrypted=encrypt_api_key('{}')) for _ in range(20)])
        self.assertContains(self.submit(), "待执行任务过多")
        self.assertFalse(TestRun.objects.exists())

    def test_rerun_form_of_new_execution_has_no_formal_writeback_fields(self):
        self.submit()
        run = APIRun.objects.get()
        self.finish(run)
        response = self.client.get(reverse("ai_assistant:api_rerun", args=[run.pk]))
        self.assertNotContains(response, 'name="test_run"')
        self.assertNotContains(response, 'name="passed_status"')
        self.assertContains(response, "调试")
