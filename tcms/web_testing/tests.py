import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, SimpleTestCase, override_settings, RequestFactory
from django.urls import reverse, resolve
from django.utils import timezone
from tcms.management.models import Product, Classification
from tcms.ai_assistant.crypto import encrypt_api_key, decrypt_api_key
from .models import WebCase, WebSuite, WebRun, WebResult
from .forms import CaseForm, SuiteForm
from .runner import claim_run, recover_stale
from .validation import validate_steps, validate_url


class ValidationTests(SimpleTestCase):
    def test_requires_assertion(self):
        with self.assertRaises(ValueError): validate_steps([{"action": "goto", "value": "/"}])

    def test_rejects_code(self):
        with self.assertRaises(ValueError): validate_steps([{"action": "evaluate", "value": "alert(1)"}, {"action": "assert_visible", "selector": "body"}])

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://web:8443"])
    def test_explicit_origins_only(self):
        self.assertEqual(validate_url("https://web:8443/login"), "https://web:8443/login")
        for url in ["file:///etc/passwd", "https://evil.invalid", "https://user:secret@web:8443", "https://web:443"]:
            with self.assertRaises(ValueError): validate_url(url)


class WorkflowTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="web-owner")
        self.other = get_user_model().objects.create_user(username="web-other")
        self.product = Product.objects.create(name="Web project", classification=Classification.objects.create(name="Web classification"))
        self.steps = [{"action": "goto", "value": "/accounts/login/"}, {"action": "assert_visible", "selector": "body"}]
        self.case = WebCase.objects.create(owner=self.owner, product=self.product, name="Login", steps_encrypted=encrypt_api_key(json.dumps(self.steps)))
        self.suite = WebSuite.objects.create(owner=self.owner, product=self.product, name="Smoke", base_url="https://kiwi-web:8443", case_ids=[self.case.pk])
        self.client.force_login(self.owner)

    def submit(self):
        return self.client.post(reverse("web_testing:submit", args=[self.suite.pk]), {"token": str(uuid.uuid4())})

    def test_pages_render(self):
        for name in ("cases", "suites", "runs", "case_new", "suite_new"):
            self.assertEqual(self.client.get(reverse("web_testing:" + name)).status_code, 200)

    def test_case_form_encrypts_steps_and_suite_accepts_container_address(self):
        response = self.client.post(reverse("web_testing:case_new"), {
            "product": self.product.pk, "name": "Created in UI", "folder": "Login/Smoke", "description": "",
            "steps": json.dumps(self.steps),
        })
        self.assertEqual(response.status_code, 302)
        created = WebCase.objects.get(name="Created in UI")
        self.assertEqual(json.loads(decrypt_api_key(created.steps_encrypted)), self.steps)
        response = self.client.post(reverse("web_testing:suite_new"), {
            "product": self.product.pk, "name": "UI Suite", "base_url": "https://kiwi-web:8443",
            "cases": [created.pk], "ignore_https_errors": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(WebSuite.objects.get(name="UI Suite").case_ids, [created.pk])

    def test_deleting_case_and_suite_preserves_history(self):
        self.submit()
        run = WebRun.objects.get()
        self.client.post(reverse("web_testing:case_delete", args=[self.case.pk]))
        self.client.post(reverse("web_testing:suite_delete", args=[self.suite.pk]))
        run.refresh_from_db()
        self.assertIsNone(run.suite_id)
        self.assertEqual(json.loads(decrypt_api_key(run.snapshot_encrypted))["cases"][0]["name"], "Login")

    def test_other_owner_cannot_delete(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("web_testing:case_delete", args=[self.case.pk])).status_code, 404)
        self.assertTrue(WebCase.objects.filter(pk=self.case.pk).exists())

    def test_api_home_tabs_render(self):
        for tab in ("environments", "cases", "suites", "runs"):
            response = self.client.get(reverse("ai_assistant:api_home"), {"tab": tab})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["tab"], tab)

    def test_submission_is_idempotent_and_snapshots_are_encrypted(self):
        token = str(uuid.uuid4())
        url = reverse("web_testing:submit", args=[self.suite.pk])
        self.client.post(url, {"token": token})
        self.client.post(url, {"token": token})
        self.assertEqual(WebRun.objects.count(), 1)
        run = WebRun.objects.get()
        self.case.name = "Changed"
        self.case.save()
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        self.assertEqual(snapshot["cases"][0]["name"], "Login")
        self.assertNotIn("Login", run.snapshot_encrypted)

    def test_foreign_cases_rejected_in_suite(self):
        alien = WebCase.objects.create(owner=self.other, product=self.product, name="Other", steps_encrypted=self.case.steps_encrypted)
        self.suite.case_ids = [alien.pk]
        self.suite.save()
        self.submit()
        self.assertFalse(WebRun.objects.exists())

    def test_owner_isolation_for_edit_report_and_screenshot(self):
        self.submit()
        run = WebRun.objects.get()
        result = WebResult.objects.create(run=run, position=1, name="Test", status="failed", screenshot=b"png")
        self.client.force_login(self.other)
        for name, pk in [("case_edit", self.case.pk), ("suite_edit", self.suite.pk), ("run", run.pk), ("status", run.pk), ("screenshot", result.pk)]:
            self.assertEqual(self.client.get(reverse("web_testing:" + name, args=[pk])).status_code, 404)

    def test_queued_cancel_and_worker_claim(self):
        self.submit()
        run = WebRun.objects.get()
        self.client.post(reverse("web_testing:cancel", args=[run.pk]))
        self.assertIsNone(claim_run())
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")

    def test_claim_only_once_and_recover_stale(self):
        self.submit()
        run = WebRun.objects.get()
        self.assertEqual(claim_run(), run.pk)
        self.assertIsNone(claim_run())
        self.assertEqual(recover_stale(), 0)
        WebRun.objects.filter(pk=run.pk).update(heartbeat=timezone.now() - timedelta(minutes=11))
        self.assertEqual(recover_stale(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")

    def test_get_cannot_cancel_or_retry(self):
        self.submit()
        run = WebRun.objects.get()
        for name in ("cancel", "retry"):
            self.assertEqual(self.client.get(reverse("web_testing:" + name, args=[run.pk])).status_code, 405)

    def test_readonly_cannot_submit(self):
        self.owner.groups.add(Group.objects.get_or_create(name="AI 只读")[0])
        self.assertEqual(self.submit().status_code, 403)
        self.assertFalse(WebRun.objects.exists())

    def test_retry_retains_snapshot(self):
        self.submit()
        run = WebRun.objects.get()
        run.status = "failed"
        run.save()
        self.client.post(reverse("web_testing:retry", args=[run.pk]), {"token": str(uuid.uuid4())})
        child = WebRun.objects.get(source=run)
        self.assertEqual(child.snapshot_encrypted, run.snapshot_encrypted)
        self.assertNotEqual(child.pk, run.pk)

    def test_web_and_api_menu_highlights_do_not_activate_manual(self):
        from tcms.ai_assistant.templatetags.ai_navigation import platform_navigation
        for url in [reverse("web_testing:cases"), reverse("web_testing:runs"), reverse("ai_assistant:api_home") + "?tab=cases"]:
            request = RequestFactory().get(url)
            request.user = self.owner
            request.resolver_match = resolve(request.path)
            nav = platform_navigation({"request": request})
            active = [section["key"] for section in nav["sections"] if section["is_current"]]
            self.assertEqual(active, ["web"] if url.startswith("/web-testing/") else ["api"])
