import json
import uuid
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from tcms.tests.factories import (
    UserFactory,
    ProductFactory,
    VersionFactory,
    BuildFactory,
    TestPlanFactory,
    TestCaseFactory,
)
from tcms.testcases.models import TestCaseStatus
from tcms.testruns.models import TestExecutionStatus, TestExecutionProperty, TestRun
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from tcms.ai_assistant.automation_archive import publish
from tcms.ai_assistant.engineering import verify_defect_regression
from tcms.ai_assistant.models import AIDefectDraft, AIRegressionVerification, AutomationArchive
from .models import WebCase, WebSuite, WebEnvironment, WebRun, WebResult
from .regression import create_regression


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DefectWebRegressionTests(TestCase):
    def setUp(self):
        self.owner = UserFactory(is_superuser=True, is_active=True)
        self.product = ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version)
        self.build = BuildFactory(version=self.version, is_active=True)
        self.fixed = BuildFactory(version=self.version, is_active=True)
        confirmed = TestCaseStatus.objects.create(name="Regression reviewed", is_confirmed=True)
        self.case = TestCaseFactory(category__product=self.product, case_status=confirmed)
        self.case.save()
        self.plan.add_case(self.case)
        steps = [
            {"action": "goto", "value": "/"},
            {"action": "fill", "selector": "input", "value": "{{data}}"},
            {"action": "assert_visible", "selector": "body"},
        ]
        self.config = WebCase.objects.create(
            owner=self.owner,
            product=self.product,
            test_case=self.case,
            name="Source config",
            steps_encrypted=encrypt_api_key(json.dumps(steps)),
        )
        self.env = WebEnvironment.objects.create(
            owner=self.owner,
            product=self.product,
            name="Original env",
            base_url="https://kiwi-web:8443",
            ignore_https_errors=True,
        )
        self.suite = WebSuite.objects.create(
            owner=self.owner,
            product=self.product,
            name="Source suite",
            base_url=self.env.base_url,
            case_ids=[self.config.pk],
            datasets_encrypted=encrypt_api_key('[{"data":"SECRET1"},{"data":"SECRET2"}]'),
        )
        self.states = {
            key: TestExecutionStatus.objects.create(name="Web regression " + key, weight=weight)
            for key, weight in [("passed", 1), ("failed", -1), ("pending", 0)]
        }
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("web_testing:submit", args=[self.suite.pk]),
            dict(
                token=uuid.uuid4(),
                execution_mode="formal",
                plan=self.plan.pk,
                build=self.build.pk,
                environment=self.env.pk,
            ),
        )
        self.assertEqual(
            response.status_code,
            302,
            str(response.context["form"].errors) if response.status_code == 200 else "",
        )
        self.source = WebRun.objects.get()
        self.finish(self.source, "failed")
        archive = self.archive(self.source, defects=["2"])
        self.defect = AIDefectDraft.objects.get(execution__run=archive.test_run)
        self.defect.status = "fixed"
        self.defect.save()

    def finish(self, run, state):
        run.status = state
        run.completed_count = run.total
        run.save()
        for position in range(1, run.total + 1):
            WebResult.objects.create(run=run, position=position, name="QA result", status=state)

    def archive(self, run, **extra):
        return publish(
            self.owner,
            "web",
            run.pk,
            dict(
                self.states,
                plan=run.test_run.plan,
                build=run.test_run.build,
                confirm=True,
                defects=extra.get("defects", []),
            ),
        )

    def data(self, **changes):
        data = dict(
            submission_token=uuid.uuid4(),
            plan=self.plan,
            build=self.fixed,
            confirm=True,
            notes="Fix acceptance",
        )
        data.update(changes)
        return data

    def new(self, **changes):
        return create_regression(self.owner, self.defect.pk, self.data(**changes))

    def snapshot(self, run):
        return json.loads(decrypt_api_key(run.snapshot_encrypted))

    def test_preserves_exact_failed_dataset_and_original_version(self):
        original = self.snapshot(self.source)["cases"][1]
        self.config.steps_encrypted = encrypt_api_key('[{"action":"goto","value":"/changed"}]')
        self.config.save()
        self.case.summary = "Changed later"
        self.case.save()
        self.env.base_url = "https://localhost:9443"
        self.env.save()
        run = self.new()
        self.assertEqual(self.snapshot(run)["cases"], [original])
        self.assertEqual(self.snapshot(run)["base_url"], "https://kiwi-web:8443")
        self.assertEqual(
            run.test_run.executions.get().case_text_version, original["business_case_version"]
        )
        self.assertEqual(run.total, 1)
        self.assertIsNone(run.suite_id)
        self.assertEqual(run.source_id, self.source.pk)
        self.assertEqual(run.test_run.build_id, self.fixed.pk)
        self.assertEqual(run.test_run.executions.get().status.weight, 0)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.fix_version, self.version.value)

    def test_preserves_source_execution_properties(self):
        TestExecutionProperty.objects.create(
            execution=self.defect.execution, name="region", value="QA"
        )
        run = self.new()
        self.assertEqual(
            list(run.test_run.executions.get().properties().values_list("name", "value")),
            [("region", "QA")],
        )

    def test_preserves_login_setup(self):
        snapshot = self.snapshot(self.source)
        setup = [
            {"action": "goto", "value": "/accounts/login/"},
            {"action": "assert_visible", "selector": "body"},
        ]
        snapshot["cases"][1]["setup_steps"] = setup
        self.source.snapshot_encrypted = encrypt_api_key(json.dumps(snapshot))
        self.source.save()
        self.assertEqual(self.snapshot(self.new())["cases"][0]["setup_steps"], setup)

    def test_idempotent_submission_and_token_mismatch(self):
        data = self.data()
        run = create_regression(self.owner, self.defect.pk, data)
        self.assertEqual(create_regression(self.owner, self.defect.pk, data).pk, run.pk)
        self.assertEqual(AIRegressionVerification.objects.count(), 1)
        from django.core.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            create_regression(self.owner, self.defect.pk, dict(data, build=self.build))

    def test_requires_fixed_status_and_explicit_confirmation(self):
        with self.assertRaisesMessage(ValueError, "确认"):
            self.new(confirm=False)
        self.defect.status = "in_progress"
        self.defect.save()
        with self.assertRaisesMessage(ValueError, "已修复或待验证"):
            self.new()
        self.assertEqual(WebRun.objects.count(), 1)

    def test_rejects_old_build_foreign_version_missing_plan_case(self):
        with self.assertRaisesMessage(ValueError, "修复构建"):
            self.new(build=self.build)
        from django.http import Http404

        with self.assertRaises(Http404):
            self.new(build=BuildFactory(version=VersionFactory(product=self.product)))
        self.plan.delete_case(self.case)
        with self.assertRaisesMessage(ValueError, "加入所选测试计划"):
            self.new()
        self.assertEqual(TestRun.objects.count(), 1)

    def test_readonly_and_cross_owner_blocked(self):
        url = reverse("web_testing:regression_new", args=[self.defect.pk])
        self.client.force_login(UserFactory(is_superuser=True, is_active=True))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.owner)
        self.owner.groups.add(Group.objects.get_or_create(name="AI 只读")[0])
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_get_is_readonly_and_no_secrets_exposed(self):
        response = self.client.get(reverse("web_testing:regression_new", args=[self.defect.pk]))
        self.assertContains(response, "新建 Web 缺陷复测")
        self.assertNotContains(response, "SECRET2")
        self.assertEqual(WebRun.objects.count(), 1)
        self.assertEqual(
            response["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private"
        )

    def test_raw_pass_does_not_change_defect_before_archive(self):
        run = self.new()
        self.finish(run, "passed")
        with self.assertRaisesMessage(ValueError, "先确认归档"):
            verify_defect_regression(self.defect, run.test_run)
        self.client.post(reverse("web_testing:regression_verify", args=[run.pk]))
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "fixed")
        self.assertEqual(run.test_run.executions.get().status.weight, 0)

    def test_archived_pass_requires_confirmation_and_remains_idempotent(self):
        run = self.new()
        self.finish(run, "passed")
        self.archive(run)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "fixed")
        url = reverse("web_testing:regression_verify", args=[run.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url)
        self.client.post(url)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "pending_verification")
        self.assertEqual(self.defect.status_history.count(), 1)
        record = AIRegressionVerification.objects.get()
        self.assertEqual(record.status, "passed")
        self.assertEqual(record.result["web_run_id"], str(run.pk))
        self.assertTrue(record.result["task_created"])
        self.defect.status = "closed"
        self.defect.closure_reason = "QA confirmed"
        self.defect.save()
        self.client.post(url)
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "closed")

    def test_failure_reopens_closed_defect(self):
        run = self.new()
        self.finish(run, "failed")
        self.archive(run)
        self.defect.refresh_from_db()
        self.defect.status = "closed"
        self.defect.closure_reason = "Previously closed"
        self.defect.save()
        self.client.post(reverse("web_testing:regression_verify", args=[run.pk]))
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "in_progress")
        self.assertEqual(AIRegressionVerification.objects.get().status, "failed")

    def test_foreign_defect_and_regular_formal_result_cannot_verify(self):
        with self.assertRaisesMessage(ValueError, "当前缺陷"):
            verify_defect_regression(self.defect, self.source.test_run)
        run = self.new()
        self.finish(run, "passed")
        self.archive(run)
        other = AIDefectDraft.objects.create(
            owner=self.owner, execution=self.defect.execution, title="Other defect"
        )
        with self.assertRaisesMessage(ValueError, "当前缺陷"):
            verify_defect_regression(other, run.test_run)

    def test_changed_fix_version_or_native_state_rejected(self):
        run = self.new()
        self.finish(run, "passed")
        self.archive(run)
        self.defect.refresh_from_db()
        self.defect.fix_version = "later fix"
        self.defect.save()
        with self.assertRaisesMessage(ValueError, "修复版本已变更"):
            verify_defect_regression(self.defect, run.test_run)
        self.defect.fix_version = self.version.value
        self.defect.save()
        execution = run.test_run.executions.get()
        execution.status = self.states["failed"]
        execution.save()
        with self.assertRaisesMessage(ValueError, "证据不一致"):
            verify_defect_regression(self.defect, run.test_run)

    def test_existing_native_id_endpoint_preserves_binding(self):
        run = self.new()
        self.finish(run, "passed")
        self.archive(run)
        url = reverse("ai_assistant:create_defect_regression", args=[self.defect.pk])
        self.client.post(url, {"regression_run_id": run.test_run_id, "notes": "Verify"})
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "pending_verification")
        self.assertEqual(AIRegressionVerification.objects.count(), 1)
        other = AIDefectDraft.objects.create(
            owner=self.owner, execution=self.defect.execution, title="Other defect"
        )
        self.client.post(
            reverse("ai_assistant:create_defect_regression", args=[other.pk]),
            {"regression_run_id": run.test_run_id, "notes": "Wrong target"},
        )
        other.refresh_from_db()
        self.assertEqual(other.status, "pending_submission")
        self.assertFalse(AIRegressionVerification.objects.filter(defect_draft=other).exists())

    def test_confirm_form_submission_and_repeated_post(self):
        url = reverse("web_testing:regression_new", args=[self.defect.pk])
        data = dict(
            submission_token=str(uuid.uuid4()), plan=self.plan.pk, build=self.fixed.pk, confirm="on"
        )
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(WebRun.objects.count(), 2)
        self.assertEqual(AIRegressionVerification.objects.count(), 1)

    def test_old_pass_cannot_override_newer_regression(self):
        older = self.new()
        self.finish(older, "passed")
        self.archive(older)
        newer = self.new()
        self.finish(newer, "failed")
        self.archive(newer)
        self.client.post(reverse("web_testing:regression_verify", args=[newer.pk]))
        self.client.post(reverse("web_testing:regression_verify", args=[older.pk]))
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "in_progress")
        self.assertEqual(
            AIRegressionVerification.objects.get(regression_run=newer.test_run).status, "failed"
        )

    def test_pending_archive_never_counts_as_pass(self):
        run = self.new()
        run.status = "cancelled"
        run.save()
        self.archive(run)
        self.client.post(reverse("web_testing:regression_verify", args=[run.pk]))
        self.defect.refresh_from_db()
        self.assertEqual(self.defect.status, "fixed")
        self.assertEqual(AIRegressionVerification.objects.get().status, "incomplete")

    def test_quota_rollback_no_orphan_native_task(self):
        for i in range(20):
            WebRun.objects.create(
                owner=self.owner,
                product=self.product,
                name="Queued",
                submission_token=uuid.uuid4(),
                snapshot_encrypted=self.source.snapshot_encrypted,
            )
        with self.assertRaisesMessage(ValueError, "待执行任务过多"):
            self.new()
        self.assertEqual(TestRun.objects.count(), 1)

    def test_changed_source_shows_helpful_conflict_page(self):
        execution = self.defect.execution
        execution.case_text_version += 99999
        execution.save()
        response = self.client.get(reverse('web_testing:regression_new', args=[self.defect.pk]))
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, '暂时无法发起 Web 缺陷复测', status_code=409)
        self.assertEqual(WebRun.objects.count(), 1)

    def test_archive_returns_to_regression_result_and_report_link(self):
        run = self.new()
        self.finish(run, 'passed')
        response = self.client.post(reverse('ai_assistant:automation_archive', args=['web', run.pk]),
            dict(passed=self.states['passed'].pk, failed=self.states['failed'].pk,
                 pending=self.states['pending'].pk, confirm='on'))
        self.assertRedirects(response, reverse('web_testing:run', args=[run.pk]))
        detail = self.client.get(reverse('web_testing:run', args=[run.pk]))
        self.assertContains(detail, '查看复测报告')
        self.assertContains(detail, '确认缺陷复测结论')
        self.assertNotContains(detail, '确认结果 / 归档报告')
