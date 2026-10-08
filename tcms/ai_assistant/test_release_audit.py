from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase, RequestFactory
from django.urls import reverse
from tcms.tests.factories import ProductFactory, UserFactory
from . import audit, roles
from .models import AIAuditLog, AIReleaseGateRule


class ReleaseAuditTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        audit.record(None, "credential_update", actor=self.user)
        self.entry = AIAuditLog.objects.latest("pk")

    def test_queryset_updates_and_deletes_are_rejected(self):
        with self.assertRaises(RuntimeError):
            AIAuditLog.objects.filter(pk=self.entry.pk).update(reason="篡改")
        with self.assertRaises(RuntimeError):
            AIAuditLog.objects.filter(pk=self.entry.pk).delete()
        self.assertTrue(AIAuditLog.objects.filter(pk=self.entry.pk).exists())

    def test_new_instance_with_existing_id_cannot_overwrite(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            AIAuditLog(pk=self.entry.pk, action="permission_denied", result="denied").save()
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.action, "credential_update")

    def test_failed_insert_does_not_poison_surrounding_transaction(self):
        with transaction.atomic():
            with patch.object(AIAuditLog.objects, "create", side_effect=DatabaseError("simulated outage")):
                audit.record(None, "credential_update", actor=self.user)
            self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_actor_deletion_keeps_username(self):
        username = self.user.username
        self.user.delete()
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.actor_id)
        self.assertEqual(self.entry.actor_username, username)

    def test_fake_client_headers_do_not_change_source_address(self):
        request = RequestFactory().post("/", REMOTE_ADDR="10.0.0.9",
            HTTP_X_REAL_IP="1.2.3.4", HTTP_X_FORWARDED_FOR="5.6.7.8")
        self.assertEqual(audit.client_ip(request), "10.0.0.9")

    def test_request_id_is_generated_without_middleware(self):
        request = RequestFactory().post("/")
        request.user = self.user
        audit.record(request, "permission_denied")
        self.assertEqual(AIAuditLog.objects.latest("pk").request_id, request.request_id)
        self.assertTrue(request.request_id)

    def test_manager_cannot_edit_foreign_project_gate(self):
        own, foreign = ProductFactory(), ProductFactory()
        from django.contrib.auth.models import Permission
        self.user.user_permissions.add(Permission.objects.get(
            content_type__app_label="ai_assistant", codename="approve_aireport"))
        roles.add_product_member(self.user, own)
        self.client.force_login(self.user)
        response = self.client.post(reverse("ai_assistant:release_gate_settings"), {
            "product": foreign.pk, "name": "非法门禁", "block_priority": "P1",
            "min_success_rate": 95, "max_open_defects": 0, "is_active": "on"}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(AIReleaseGateRule.objects.filter(product=foreign).exists())

    def test_admin_has_readonly_audit_access(self):
        self.client.force_login(UserFactory(is_superuser=True, is_staff=True))
        url = reverse("admin:ai_assistant_aiauditlog_changelist")
        self.assertEqual(self.client.get(url, secure=True).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:ai_assistant_aiauditlog_add"), secure=True).status_code, 403)
