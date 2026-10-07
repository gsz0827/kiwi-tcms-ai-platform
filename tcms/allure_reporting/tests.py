import gzip
import hashlib
import json
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from tcms.ai_assistant.crypto import encrypt_api_key
from tcms.ai_assistant.models import APIRun, APIResult
from tcms.management.models import Classification, Product
from tcms.web_testing.models import WebRun, WebResult
from .export import make_results, milliseconds
from .models import AllureReport
from .presentation import private_html
from .worker import claim_report, generate_report, recover_reports


class AllureTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="allure-owner")
        self.other = get_user_model().objects.create_user(username="allure-other", is_superuser=True)
        self.product = Product.objects.create(name="Allure project", classification=Classification.objects.create(name="Allure classification"))
        self.client.force_login(self.owner)

    def create_run(self, kind="web", state=None, cases=None):
        cases = cases or [{"name": "Frozen name", "id": 10, "case_id": 10, "dataset": 0, "steps": [{"value": "PRIVATE-input"}], "dataset_values": {"secret": "PRIVATE-variable"}}]
        fields = dict(owner=self.owner, product=self.product, submission_token=uuid.uuid4(), snapshot_encrypted=encrypt_api_key(json.dumps({"cases": cases})), status=state or ("passed" if kind == "web" else "completed"))
        return (WebRun.objects.create(name="Report QA", total=len(cases), **fields) if kind == "web" else APIRun.objects.create(environment_name="QA", **fields))

    def ready(self, kind="web"):
        run = self.create_run(kind)
        report = run.allure_report
        html = b"<!DOCTYPE html><html><body>Frozen report</body></html>"
        report.status, report.artifact, report.checksum = "ready", gzip.compress(html), hashlib.sha256(html).hexdigest()
        report.save()
        return run, report, html

    def url(self, name, run, kind="web"):
        return reverse("allure_reporting:" + name, args=(kind, run.pk))

    def test_submission_automatically_queues_each_kind(self):
        web, api = self.create_run(), self.create_run("api")
        self.assertEqual(AllureReport.objects.count(), 2)
        self.assertEqual(web.allure_report.owner, self.owner)
        self.assertEqual(api.allure_report.kind, "api")

    @override_settings(ALLURE_ENABLED=False)
    def test_disabled_automatic_queue_and_historical_manual_generation(self):
        run = self.create_run()
        self.assertEqual(AllureReport.objects.count(), 0)
        self.assertEqual(self.client.get(self.url("view", run)).status_code, 200)
        self.assertEqual(AllureReport.objects.count(), 0)
        self.assertEqual(self.client.post(self.url("generate", run)).status_code, 302)
        self.assertEqual(AllureReport.objects.count(), 1)

    def test_transaction_rollback_removes_queued_report(self):
        try:
            with transaction.atomic():
                self.create_run()
                raise ValueError
        except ValueError:
            pass
        self.assertFalse(AllureReport.objects.exists())

    def test_resave_does_not_duplicate(self):
        run = self.create_run()
        run.save()
        self.assertEqual(AllureReport.objects.count(), 1)

    def test_one_source_constraint(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AllureReport.objects.create(owner=self.owner)

    def test_worker_waits_for_source_to_finish(self):
        run = self.create_run(state="queued")
        self.assertIsNone(claim_report())
        WebRun.objects.filter(pk=run.pk).update(status="failed")
        self.assertEqual(claim_report(), run.allure_report.pk)
        report = AllureReport.objects.get()
        self.assertEqual((report.status, report.attempts), ("generating", 1))
        self.assertIsNone(claim_report())

    def test_api_cancel_requested_not_terminal(self):
        run = self.create_run("api", "cancel_requested")
        self.assertIsNone(claim_report())
        APIRun.objects.filter(pk=run.pk).update(status="cancelled")
        self.assertEqual(claim_report(), run.allure_report.pk)

    def test_stale_worker_recovery_is_bounded(self):
        run = self.create_run()
        AllureReport.objects.filter(pk=run.allure_report.pk).update(status="generating", attempts=2, heartbeat=timezone.now()-timedelta(minutes=6))
        recover_reports()
        self.assertEqual(AllureReport.objects.get().status, "queued")
        AllureReport.objects.update(status="generating", attempts=3)
        recover_reports()
        self.assertEqual(AllureReport.objects.get().status, "error")

    def test_web_results_and_steps_export_from_snapshot(self):
        run = self.create_run(state="failed")
        WebResult.objects.create(run=run, position=1, name="Later edited name", status="failed", steps=[{"action":"assert_visible", "status":"failed", "selector":"PRIVATE-selector"}], screenshot=b"\x89PNG\r\n\x1a\nfixture", error="PRIVATE-error")
        documents, attachments, summary = make_results("web", run)
        self.assertEqual(documents[0]["name"], "Frozen name")
        self.assertEqual(documents[0]["status"], "failed")
        self.assertEqual(len(attachments), 1)
        self.assertEqual(summary["failed"], 1)
        self.assertNotIn("PRIVATE", json.dumps(documents))

    def test_api_payload_secrets_are_not_exported(self):
        run = self.create_run("api")
        APIResult.objects.create(run=run, position=0, name="API", status="passed", status_code=200, checks=[{"label":"状态码", "passed":True, "expected":"PRIVATE-expect", "actual":"PRIVATE-actual"}], request_summary={"Authorization":"PRIVATE-token"}, response_summary="PRIVATE-body", error="PRIVATE-error")
        documents, attachments, summary = make_results("api", run)
        self.assertEqual(documents[0]["status"], "passed")
        self.assertEqual(documents[0]["steps"][0]["status"], "passed")
        self.assertNotIn("PRIVATE", json.dumps(documents))
        self.assertEqual(attachments, {})
        self.assertEqual(summary["passed"], 1)

    def test_environment_errors_are_broken(self):
        run = self.create_run("api")
        APIResult.objects.create(run=run, position=0, name="API", status="error")
        self.assertEqual(make_results("api", run)[0][0]["status"], "broken")

    def test_cancelled_and_unexecuted_not_counted_passed(self):
        for kind in ("web", "api"):
            run = self.create_run(kind, "cancelled")
            docs, _, summary = make_results(kind, run)
            self.assertEqual(docs[0]["status"], "skipped")
            self.assertEqual((summary["passed"], summary["skipped"]), (0, 1))

    def test_active_source_cannot_be_exported(self):
        with self.assertRaises(ValueError):
            make_results("web", self.create_run(state="running"))

    def test_full_api_dataset_capacity(self):
        run = self.create_run("api", cases=[{"name":f"Row{i}", "case_id":i % 20, "dataset":i // 20} for i in range(200)])
        self.assertEqual(make_results("api", run)[2]["total"], 200)

    def test_same_case_different_dataset_history_identity(self):
        run = self.create_run(cases=[{"id":10,"name":"First","dataset":0},{"id":10,"name":"Second","dataset":1}])
        docs = make_results("web", run)[0]
        self.assertEqual(docs[0]["testCaseId"], docs[1]["testCaseId"])
        self.assertNotEqual(docs[0]["historyId"], docs[1]["historyId"])

    def test_attachment_total_budget(self):
        run = self.create_run(cases=[{"id":i,"name":str(i)} for i in range(5)])
        png = b"\x89PNG\r\n\x1a\n" + b"0"*(2*1024*1024-8)
        for i in range(1,6):
            WebResult.objects.create(run=run, position=i, name=str(i), status="failed", screenshot=png)
        self.assertEqual(len(make_results("web", run)[1]), 3)

    def test_other_account_even_admin_cannot_access_any_report_route(self):
        for kind in ("web", "api"):
            run, _, _ = self.ready(kind)
            self.client.force_login(self.other)
            for name in ("view", "status", "artifact"):
                self.assertEqual(self.client.get(self.url(name, run, kind)).status_code, 404)
            self.assertEqual(self.client.post(self.url("generate", run, kind)).status_code, 404)
            self.client.force_login(self.owner)

    def test_unauthenticated_requests_require_login(self):
        run, _, _ = self.ready()
        self.client.logout()
        for name in ("view", "status", "artifact"):
            self.assertEqual(self.client.get(self.url(name, run)).status_code, 302)

    def test_artifact_is_private_and_opaque_sandboxed(self):
        run, _, html = self.ready()
        response = self.client.get(self.url("artifact", run))
        self.assertEqual(response.content, html)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("sandbox allow-scripts", response["Content-Security-Policy"])
        self.assertNotIn("allow-same-origin", response["Content-Security-Policy"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_authenticated_html_download(self):
        run, _, html = self.ready()
        response = self.client.get(self.url("artifact", run), {"download":"1"})
        self.assertEqual(response.content, html)
        self.assertIn("attachment; filename=", response["Content-Disposition"])

    def test_corrupt_report_refused(self):
        run, report, _ = self.ready()
        AllureReport.objects.filter(pk=report.pk).update(artifact=b"invalid-gzip")
        self.assertEqual(self.client.get(self.url("artifact", run)).status_code, 409)

    def test_wrong_checksum_refused(self):
        run, report, _ = self.ready()
        AllureReport.objects.filter(pk=report.pk).update(checksum="0"*64)
        self.assertEqual(self.client.get(self.url("artifact", run)).status_code, 409)

    def test_pending_report_has_no_artifact(self):
        run = self.create_run()
        self.assertEqual(self.client.get(self.url("artifact", run)).status_code, 404)
        self.assertEqual(self.client.get(self.url("status", run)).json()["status"], "queued")
        self.assertEqual(self.client.get(self.url("view", run)).status_code, 200)

    def test_generation_requires_post_csrf_and_finished_source(self):
        run = self.create_run(state="running")
        self.assertEqual(self.client.get(self.url("generate", run)).status_code, 405)
        self.assertEqual(self.client.post(self.url("generate", run)).status_code, 409)
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.owner)
        self.assertEqual(secure.post(self.url("generate", run)).status_code, 403)

    def test_readonly_can_read_but_not_retry(self):
        self.owner.groups.add(Group.objects.get_or_create(name="AI 只读")[0])
        run, _, _ = self.ready()
        self.assertEqual(self.client.get(self.url("artifact", run)).status_code, 200)
        self.assertEqual(self.client.post(self.url("generate", run)).status_code, 403)

    def test_ready_report_immutable_on_generate(self):
        run, report, html = self.ready()
        self.client.post(self.url("generate", run))
        generate_report(report.pk)
        report.refresh_from_db()
        self.assertEqual((report.status, gzip.decompress(bytes(report.artifact))), ("ready", html))

    def test_failed_report_can_retry_without_reexecuting(self):
        run = self.create_run()
        AllureReport.objects.update(status="error")
        self.assertEqual(self.client.post(self.url("generate", run)).status_code, 302)
        self.assertEqual(AllureReport.objects.get().status, "queued")
        run.refresh_from_db()
        self.assertEqual(run.status, "passed")

    def test_retry_rate_limited(self):
        run = self.create_run()
        AllureReport.objects.update(status="error", heartbeat=timezone.now())
        self.assertEqual(self.client.post(self.url("generate", run)).status_code, 429)

    @patch("tcms.allure_reporting.worker.subprocess.run", side_effect=subprocess.TimeoutExpired("allure", 120))
    def test_cli_timeout_preserves_execution_and_sets_retryable_error(self, cli):
        run = self.create_run()
        generate_report(claim_report())
        run.refresh_from_db()
        self.assertEqual(run.status, "passed")
        self.assertEqual(AllureReport.objects.get().status, "error")
        self.assertFalse(cli.call_args.kwargs.get("shell", False))
        self.assertEqual(cli.call_args.kwargs["timeout"], 120)

    def fake_cli(self, arguments, **kwargs):
        output = Path(arguments[arguments.index("--output")+1])
        output.mkdir()
        (output/"index.html").write_text('<html><head></head><body>Report<script async src="https://www.googletagmanager.com/gtag/js?id=G-LNDJ3J7WT0"></script><script>window.dataLayer = []; gtag("config");</script></body></html>', encoding="utf-8")

    def test_cli_success_publishes_atomically(self):
        run = self.create_run()
        with patch("tcms.allure_reporting.worker.subprocess.run", side_effect=self.fake_cli):
            generate_report(claim_report())
        report = AllureReport.objects.get()
        self.assertEqual(report.status, "ready")
        self.assertEqual(report.engine_version, "3.20.0")
        self.assertEqual(report.summary["skipped"], 1)

    def test_disabled_owner_never_publishes(self):
        run = self.create_run()
        self.owner.is_active = False
        self.owner.save()
        with patch("tcms.allure_reporting.worker.subprocess.run") as cli:
            generate_report(claim_report())
        cli.assert_not_called()
        self.assertEqual(AllureReport.objects.get().status, "error")

    def test_missing_cli_output_sets_error(self):
        self.create_run()
        with patch("tcms.allure_reporting.worker.subprocess.run"):
            generate_report(claim_report())
        self.assertEqual(AllureReport.objects.get().status, "error")

    def test_source_deletion_cleans_private_artifact(self):
        run, _, _ = self.ready()
        run.delete()
        self.assertEqual(AllureReport.objects.count(), 0)

    def test_private_html_removes_external_tracking_and_has_storage_shim(self):
        source = b'<html><head></head><body><script async src="https://www.googletagmanager.com/gtag/js?id=G-LNDJ3J7WT0"></script><script>window.dataLayer = []; gtag("config");</script>Report</body></html>'
        normalized = private_html(source)
        self.assertNotIn(b'googletagmanager', normalized)
        self.assertNotIn(b'gtag("config")', normalized)
        self.assertIn(b'Object.defineProperty(window,name', normalized)
        self.assertIn(b'Report', normalized)

    def test_changed_allure_template_requires_privacy_review(self):
        with self.assertRaises(ValueError):
            private_html(b'<html><head></head><body>Unknown template</body></html>')

    @override_settings(DEBUG=True)
    def test_debug_middleware_preserves_artifact_sandbox(self):
        run, _, _ = self.ready()
        response = self.client.get(self.url('artifact', run))
        self.assertIn('sandbox allow-scripts', response['Content-Security-Policy'])
