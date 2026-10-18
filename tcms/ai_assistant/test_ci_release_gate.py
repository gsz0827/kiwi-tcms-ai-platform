import json
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import BuildFactory, TestPlanFactory, VersionFactory, UserFactory
from tcms.testruns.models import TestExecutionStatus, TestRun
from tcms.web_testing.models import WebRun, WebResult, WebSuite
from tcms.web_testing import test_ci as web_fixtures
from .api_scheduling import rotate_token
from .automation_archive import publish
from .crypto import decrypt_api_key, encrypt_api_key
from .models import (
    APICase,
    APIEnvironment,
    APIRun,
    APISuite,
    AutomationArchive,
    AITestReport,
    AIDefectDraft,
    AIAuditLog,
    AIReleaseGateRule,
)


@override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["http://127.0.0.1:8080"])
class ReleaseGateTests(TestCase):
    def setUp(self):
        web_fixtures.WebCITests.setUp(self)
        self.version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(product=self.product, product_version=self.version)
        self.build = BuildFactory(version=self.version, is_active=True)
        self.plan.add_case(self.case)
        self.pending = TestExecutionStatus.objects.create(name="release pending", weight=0)
        self.passed = TestExecutionStatus.objects.create(name="release passed", weight=1)
        self.failed = TestExecutionStatus.objects.create(name="release failed", weight=-1)
        self.api_env = APIEnvironment.objects.create(
            owner=self.owner, product=self.product, name="API QA", base_url="http://127.0.0.1:8080"
        )
        self.api_case = APICase.objects.create(
            owner=self.owner, product=self.product, name="API", path="/health", test_case=self.case
        )
        self.api_suite = APISuite.objects.create(
            owner=self.owner,
            product=self.product,
            name="API",
            environment=self.api_env,
            case_ids=[self.api_case.pk],
        )
        self.api_token = rotate_token(self.api_suite)

    def start(self, kind="web", *, mode="formal"):
        suite, token = (self.suite, self.token) if kind == "web" else (self.api_suite, self.api_token)
        name = "web_testing:ci_submit" if kind == "web" else "ai_assistant:api_ci_submit"
        response = self.client.post(
            reverse(name, args=[suite.pk]),
            json.dumps(dict(execution_mode=mode, plan=self.plan.pk, build=self.build.pk)),
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION="Bearer " + token,
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 202, response.content)
        return (WebRun if kind == "web" else APIRun).objects.get(pk=response.json()["run_id"])

    def complete(self, run, kind):
        run.started = timezone.now()
        if kind == "web":
            run.status, run.finished, run.completed_count = "passed", timezone.now(), 1
            WebResult.objects.create(run=run, position=1, name=self.case.summary, status="passed")
        else:
            run.status, run.completed = "completed", timezone.now()
            run.results.update(status="passed", elapsed_ms=1, status_code=200)
        run.save()

    def archive(self, run, kind):
        self.complete(run, kind)
        saved = publish(
            self.owner,
            kind,
            run.pk,
            dict(
                confirm=True,
                plan=self.plan,
                build=self.build,
                passed=self.passed,
                failed=self.failed,
                pending=self.pending,
                defects=[],
                target_run=run.test_run,
            ),
        )
        self.assertEqual(saved.test_run_id, run.test_run_id)
        return saved.report

    def approve(self, report, **extra):
        response = self.client.post(
            reverse("ai_assistant:approve_report", args=[report.pk]),
            dict(decision="approved", comment="release test", **extra),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.approval_status, "approved")

    def gate(self, run, kind="web", *, token=None, **extra):
        name = "web_testing:ci_release_gate" if kind == "web" else "ai_assistant:api_ci_release_gate"
        credential = self.token if kind == "web" else self.api_token
        data = dict(
            plan_id=self.plan.pk, build_id=self.build.pk, submission_key=str(run.submission_token)
        )
        data.update(extra)
        return self.client.post(
            reverse(name, args=[run.suite_id, run.pk]),
            json.dumps(data),
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION="Bearer " + (token if token is not None else credential),
        )

    def test_approved_formal_web_and_api_allow_and_audit(self):
        for kind in ("web", "api"):
            with self.subTest(kind=kind):
                run = self.start(kind)
                report = self.archive(run, kind)
                self.approve(report)
                result = self.gate(run, kind).json()
                self.assertTrue(result["allowed"], result)
                self.assertEqual(result["report_id"], report.pk)
                self.assertEqual(result["build_id"], self.build.pk)
                self.assertFalse(result["waived"])
                log = AIAuditLog.objects.first()
                self.assertEqual(log.detail["source"], "jenkins_release_check")
                self.assertEqual(log.actor_id, self.owner.pk)

    def test_pending_execution_archive_and_approval_never_allow(self):
        run = self.start()
        self.assertEqual(self.gate(run).json()["reason"], "execution_pending")
        self.complete(run, "web")
        self.assertEqual(self.gate(run).json()["reason"], "archive_required")
        # publish without completing twice
        saved = publish(
            self.owner,
            "web",
            run.pk,
            dict(
                confirm=True,
                plan=self.plan,
                build=self.build,
                passed=self.passed,
                failed=self.failed,
                pending=self.pending,
                defects=[],
            ),
        )
        before = saved.report.approval_status
        result = self.gate(run).json()
        self.assertEqual(result["reason"], "approval_required")
        saved.report.refresh_from_db()
        self.assertEqual(saved.report.approval_status, before)
        self.assertFalse(result["allowed"])

    def test_debug_web_and_api_never_allow(self):
        for kind in ("web", "api"):
            run = self.start(kind, mode="debug")
            result = self.gate(run, kind).json()
            self.assertEqual(result["reason"], "formal_execution_required")
            self.assertFalse(result["allowed"])
        self.assertFalse(AutomationArchive.objects.exists())
        self.assertFalse(AITestReport.objects.exists())

    def test_wrong_build_or_plan_never_selects_another_report(self):
        run = self.start()
        other = BuildFactory(version=self.version)
        self.assertEqual(
            self.gate(run, build_id=other.pk).json()["reason"], "execution_context_mismatch"
        )
        self.assertEqual(
            self.gate(run, plan_id=self.plan.pk + 1000).json()["reason"], "execution_context_mismatch"
        )

    def test_rejected_report_blocks(self):
        run = self.start()
        report = self.archive(run, "web")
        report.approval_status = "rejected"
        report.save()
        self.assertEqual(self.gate(run).json()["reason"], "approval_invalid")

    def test_changed_hash_or_native_result_blocks(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        original = report.snapshot_hash
        report.snapshot_hash = "bad"
        report.save()
        self.assertEqual(self.gate(run).json()["reason"], "report_snapshot_invalid")
        report.snapshot_hash = original
        report.save()
        item = run.test_run.executions.get()
        item.status = self.failed
        item.save()
        self.assertEqual(self.gate(run).json()["reason"], "report_execution_changed")

    def test_changed_source_evidence_blocks(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        row = run.results.get()
        row.elapsed_ms = 999
        row.save()
        self.assertEqual(self.gate(run).json()["reason"], "execution_evidence_changed")

    def test_old_report_version_blocks(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        report.is_current = False
        report.save()
        self.assertEqual(self.gate(run).json()["reason"], "report_superseded")

    def test_new_defect_blocks_and_authorized_waiver_is_scoped(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        execution = run.test_run.executions.get()
        AIDefectDraft.objects.create(
            owner=self.owner,
            execution=execution,
            title="release blocker",
            priority="P1",
            status="in_progress",
        )
        self.assertEqual(self.gate(run).json()["reason"], "release_gate_failed")
        self.approve(report, waive_reason="本次构建已评估风险")
        result = self.gate(run).json()
        self.assertTrue(result["allowed"], result)
        self.assertTrue(result["waived"])
        AIDefectDraft.objects.create(
            owner=self.owner,
            execution=execution,
            title="another blocker",
            priority="P1",
            status="in_progress",
        )
        self.assertEqual(self.gate(run).json()["reason"], "release_gate_failed")

    def test_changed_rule_reevaluates_without_rewriting_report(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        AIReleaseGateRule.objects.create(product=self.product, name="strict", min_success_rate=100)
        before = report.metrics_snapshot
        self.assertTrue(self.gate(run).json()["allowed"])
        report.refresh_from_db()
        self.assertEqual(report.metrics_snapshot, before)

    def test_revoked_token_inactive_owner_and_other_suite_denied(self):
        run = self.start()
        self.assertEqual(self.gate(run, token="invalid").status_code, 401)
        rotate_token(self.suite)
        self.assertEqual(self.gate(run).status_code, 401)
        self.token = rotate_token(self.suite)
        other = WebSuite.objects.create(owner=self.owner, product=self.product, name="other")
        response = self.client.post(
            reverse("web_testing:ci_release_gate", args=[other.pk, run.pk]),
            '{"plan_id":1,"build_id":1}',
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION="Bearer " + rotate_token(other),
        )
        self.assertEqual(response.status_code, 404)
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.gate(run).status_code, 401)

    def test_bad_payloads_fail_closed_and_get_is_not_supported(self):
        run = self.start()
        url = reverse("web_testing:ci_release_gate", args=[run.suite_id, run.pk])
        self.assertEqual(self.client.get(url, secure=True).status_code, 405)
        for payload in (
            "[]",
            "null",
            "{}",
            '{"plan_id":true,"build_id":1}',
            '{"plan_id":1,"build_id":1,"report_id":99}',
            "{",
        ):
            response = self.client.post(
                url,
                payload,
                content_type="application/json",
                secure=True,
                HTTP_AUTHORIZATION="Bearer " + self.token,
            )
            self.assertFalse(response.json()["allowed"])

    def test_api_formal_retries_are_idempotent_and_validate_plan_cases(self):
        url = reverse("ai_assistant:api_ci_submit", args=[self.api_suite.pk])
        key = str(uuid.uuid4())
        body = json.dumps(dict(execution_mode="formal", plan=self.plan.pk, build=self.build.pk))
        headers = dict(
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION="Bearer " + self.api_token,
            HTTP_IDEMPOTENCY_KEY=key,
        )
        first = self.client.post(url, body, **headers)
        self.assertEqual(first.status_code, 202, first.content)
        second = self.client.post(url, body, **headers)
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])
        self.assertEqual(TestRun.objects.count(), 1)
        run = APIRun.objects.get(pk=first.json()["run_id"])
        self.assertEqual(run.execution_mode, "formal")
        self.assertEqual(run.trigger, "ci")
        self.assertEqual(run.test_run.executions.count(), 1)

    def test_api_invalid_formal_does_not_create_partial_run(self):
        self.plan.delete_case(self.case)
        response = self.client.post(
            reverse("ai_assistant:api_ci_submit", args=[self.api_suite.pk]),
            json.dumps(dict(execution_mode="formal", plan=self.plan.pk, build=self.build.pk)),
            content_type="application/json",
            secure=True,
            HTTP_AUTHORIZATION="Bearer " + self.api_token,
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(TestRun.objects.exists())
        self.assertFalse(APIRun.objects.exists())

    def test_wrong_jenkins_submission_key_blocks(self):
        run = self.start()
        result = self.gate(run, submission_key=str(uuid.uuid4())).json()
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "ci_submission_mismatch")

    def test_cancelled_or_incomplete_results_block(self):
        run = self.start()
        run.status = "cancelled"
        run.save()
        self.assertEqual(self.gate(run).json()["reason"], "execution_incomplete")

    def test_invalid_signature_blocks(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        report.signature = "not-the-approved-snapshot"
        report.save()
        self.assertEqual(self.gate(run).json()["reason"], "approval_invalid")

    def test_approval_by_disabled_manager_is_no_longer_valid(self):
        run = self.start()
        report = self.archive(run, "web")
        manager = UserFactory(is_superuser=True, is_active=True)
        self.client.force_login(manager)
        self.approve(report)
        self.client.force_login(self.owner)
        manager.is_active = False
        manager.save()
        self.assertEqual(self.gate(run).json()["reason"], "approval_invalid")

    def test_readonly_owner_cannot_use_release_endpoint(self):
        from django.contrib.auth.models import Group
        from .roles import ROLE_VIEWER

        run = self.start()
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        response = self.gate(run)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()["allowed"])

    def test_native_edit_with_unchanged_counts_invalidates_evidence(self):
        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        item = run.test_run.executions.get()
        item.assignee = UserFactory()
        item.save()
        self.assertEqual(self.gate(run).json()["reason"], "report_execution_changed")

    def test_policy_edit_requires_new_waiver_even_if_thresholds_unchanged(self):
        rule = AIReleaseGateRule.objects.create(product=self.product, name="strict")
        run = self.start()
        report = self.archive(run, "web")
        AIDefectDraft.objects.create(
            owner=self.owner,
            execution=run.test_run.executions.get(),
            title="known blocker",
            priority="P1",
            status="in_progress",
        )
        self.approve(report, waive_reason="本构建风险确认")
        self.assertTrue(self.gate(run).json()["allowed"])
        rule.updated_by = self.owner
        rule.save()
        self.assertEqual(self.gate(run).json()["reason"], "release_gate_failed")

    def test_release_client_to_real_view_allows_then_blocks_new_risk(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import MagicMock, patch
        from urllib.parse import urlsplit
        from deployment.kiwi_release_gate import main

        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        with TemporaryDirectory() as directory:
            source, output = Path(directory) / "run.json", Path(directory) / "gate.json"
            source.write_text(
                json.dumps(
                    {
                        "run_id": str(run.pk),
                        "terminal": True,
                        "ci_context": {
                            "type": "web",
                            "suite": self.suite.pk,
                            "mode": "formal",
                            "plan": self.plan.pk,
                            "build": self.build.pk,
                            "submission_key": str(run.submission_token),
                        },
                    }
                ),
                encoding="utf-8",
            )

            def transport(req, **kwargs):
                response = self.client.post(
                    urlsplit(req.full_url).path,
                    req.data,
                    content_type="application/json",
                    secure=True,
                    HTTP_AUTHORIZATION=req.get_header("Authorization"),
                )
                self.assertEqual(response.status_code, 200)
                wrapper = MagicMock()
                wrapper.__enter__.return_value.read.return_value = response.content
                return wrapper

            opener = MagicMock()
            opener.open.side_effect = transport
            arguments = [
                "--type",
                "web",
                "--suite",
                str(self.suite.pk),
                "--plan",
                str(self.plan.pk),
                "--build",
                str(self.build.pk),
                "--key",
                str(run.submission_token),
                "--run-report",
                str(source),
                "--output",
                str(output),
                "--timeout",
                "1",
                "--simulate",
            ]
            with patch.dict(
                "os.environ",
                {"KIWI_BASE_URL": "https://platform.example.test", "KIWI_CI_TOKEN": self.token},
                clear=True,
            ), patch("deployment.kiwi_release_gate.build_opener", return_value=opener), patch(
                "builtins.print"
            ):
                self.assertEqual(main(arguments), 0)
                self.assertTrue(json.loads(output.read_text())["allowed"])
                AIDefectDraft.objects.create(
                    owner=self.owner,
                    execution=run.test_run.executions.get(),
                    title="new risk",
                    priority="P1",
                    status="in_progress",
                )
                self.assertEqual(main(arguments), 1)
                receipt = json.loads(output.read_text())
                self.assertFalse(receipt["allowed"])
                self.assertFalse(receipt["deployment_performed"])

    def test_polling_reader_does_not_take_writer_locks(self):
        from unittest.mock import patch

        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)
        with patch(
            "django.db.models.query.QuerySet.select_for_update",
            side_effect=AssertionError("Polling must not invert archive writer locks"),
        ):
            self.assertTrue(self.gate(run).json()["allowed"])

    def test_revoked_during_check_is_revalidated_before_return(self):
        from unittest.mock import patch

        run = self.start()
        report = self.archive(run, "web")
        self.approve(report)

        def revoke(*args):
            rotate_token(self.suite)
            return {"allowed": True, "status": "allowed", "reason": "approved_release"}

        with patch("tcms.ai_assistant.ci_release_gate._decision", side_effect=revoke):
            response = self.gate(run)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.json()["allowed"])

    def test_archive_commit_boundary_refreshes_prefetched_task(self):
        from unittest.mock import patch
        from .automation_archive import source_rows

        run = self.start()
        self.complete(run, "web")
        created = []

        def publish_between_reads(source, kind):
            rows = source_rows(source, kind)
            if not created:
                saved = publish(
                    self.owner,
                    kind,
                    source.pk,
                    dict(
                        confirm=True,
                        plan=self.plan,
                        build=self.build,
                        passed=self.passed,
                        failed=self.failed,
                        pending=self.pending,
                        defects=[],
                    ),
                )
                created.append(saved.report_id)
            return rows

        with patch(
            "tcms.ai_assistant.ci_release_gate.source_rows", side_effect=publish_between_reads
        ):
            response = self.gate(run)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "approval_required")
        self.assertEqual(response.json()["status"], "waiting")
        self.assertEqual(response.json()["report_id"], created[0])
