"""审计留痕：字段级历史 + 动作级审计。

这一组测试守住四条底线，也是内审会直接追问的四件事：

1. **查得到人。** 改门禁规则、审批报告、放行、改成员、删目录，都要能回答
   「谁、什么时候、把什么改成了什么」；
2. **被拒绝的尝试也要留痕。** 越权审批不会产生任何字段变更，只靠历史表查不到；
3. **审计不可篡改。** 记录只增不改不删，账号被删掉之后仍能靠用户名快照追溯；
4. **审计不能反过来搞坏业务。** 写审计失败时审批该成功还是成功。

来源 IP 只认 ``X-Real-IP`` / ``REMOTE_ADDR``：``etc/nginx.conf`` 只设前者，
``X-Forwarded-For`` 若出现必是客户端伪造，专门有一条用例钉住这个口径。
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import DatabaseError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from tcms.management.models import Build, Classification, Priority, Product, Version
from tcms.testcases.models import TestCase as FormalTestCase
from tcms.testcases.models import TestCaseStatus
from tcms.testplans.models import PlanType, TestPlan
from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun

from . import audit, roles
from .models import AIAuditLog, AIReleaseGateRule, AITestReport
from .services import build_test_run_snapshot


class AuditModelTests(TestCase):
    """审计表在模型层就拒绝被改写——这是「不可篡改」的第一道闸。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="audit-owner", password="pw-12345"
        )
        audit.record(None, "credential_update", actor=self.user, reason="首次保存密钥")
        self.entry = AIAuditLog.objects.latest("pk")

    def test_record_keeps_a_username_snapshot(self):
        self.assertEqual(self.entry.actor, self.user)
        self.assertEqual(self.entry.actor_username, "audit-owner")

    def test_entry_cannot_be_updated(self):
        self.entry.reason = "被改过的理由"
        with self.assertRaises(RuntimeError):
            self.entry.save()

    def test_entry_cannot_be_deleted(self):
        with self.assertRaises(RuntimeError):
            self.entry.delete()

    def test_username_survives_account_deletion(self):
        """账号删除后外键置空，历史口令必须还能追溯。"""
        self.user.delete()
        self.entry.refresh_from_db()

        self.assertIsNone(self.entry.actor)
        self.assertEqual(self.entry.actor_username, "audit-owner")

    def test_unregistered_action_still_lands_but_warns(self):
        with self.assertLogs("tcms.ai_assistant.audit", level="WARNING") as captured:
            audit.record(None, "something_not_registered", actor=self.user)

        self.assertTrue(
            any("not registered" in line for line in captured.output),
            captured.output,
        )
        self.assertTrue(
            AIAuditLog.objects.filter(action="something_not_registered").exists()
        )


class ClientIpTests(TestCase):
    """来源 IP 口径：只信反向代理覆盖过的 X-Real-IP 与 REMOTE_ADDR。"""

    def setUp(self):
        self.factory = RequestFactory()

    def test_uses_real_ip_set_by_our_proxy(self):
        request = self.factory.post(
            "/ai/x/", REMOTE_ADDR="10.1.2.3", HTTP_X_REAL_IP="203.0.113.9"
        )
        self.assertEqual(audit.client_ip(request), "203.0.113.9")

    def test_forged_forwarded_for_is_ignored(self):
        """nginx 不设 X-Forwarded-For，出现即伪造，绝不能被写进审计。"""
        request = self.factory.post(
            "/ai/x/",
            REMOTE_ADDR="10.1.2.3",
            HTTP_X_FORWARDED_FOR="198.51.100.66",
        )
        self.assertEqual(audit.client_ip(request), "10.1.2.3")

    def test_falls_back_to_remote_addr(self):
        request = self.factory.post("/ai/x/", REMOTE_ADDR="10.9.9.9")
        self.assertEqual(audit.client_ip(request), "10.9.9.9")


class ReleaseGateAuditTests(TestCase):
    """用最小可复现的门禁场景（成功率 50%）走完整审计链路。"""

    def setUp(self):
        self.author = self._make_user("audit-author", "testruns.view_testrun")
        self.manager = self._make_user(
            "audit-manager", ("testruns.view_testrun", roles.PERM_APPROVE_REPORT)
        )
        self.product = Product.objects.create(
            name="审计项目",
            classification=Classification.objects.create(name="审计分类"),
        )
        version = Version.objects.create(value="1.0", product=self.product)
        build = Build.objects.create(name="审计构建", version=version)
        plan = TestPlan.objects.create(
            name="审计计划",
            text="审计验证计划",
            product_version=version,
            author=self.author,
            product=self.product,
            type=PlanType.objects.create(name="审计计划类型"),
        )
        self.test_run = TestRun.objects.create(
            summary="审计执行任务",
            plan=plan,
            build=build,
            manager=self.author,
            default_tester=self.author,
        )
        passed_status = TestExecutionStatus.objects.create(
            name="审计通过", weight=1, icon="fa fa-check", color="#00FF00"
        )
        failed_status = TestExecutionStatus.objects.create(
            name="审计失败", weight=-1, icon="fa fa-times", color="#FF0000"
        )
        priority, _created = Priority.objects.get_or_create(value="P1")
        case_status = TestCaseStatus.objects.filter(is_confirmed=True).first()
        if case_status is None:
            case_status = TestCaseStatus.objects.create(
                name="审计已确认", is_confirmed=True
            )
        category = self.product.category.get(name="--default--")
        for summary, status in (("通过用例", passed_status), ("失败用例", failed_status)):
            case = FormalTestCase.objects.create(
                summary=summary,
                requirement="审计需求",
                text="执行并记录",
                author=self.author,
                default_tester=self.author,
                reviewer=self.author,
                priority=priority,
                case_status=case_status,
                category=category,
            )
            history = case.history.first()
            TestExecution.objects.create(
                run=self.test_run,
                build=build,
                case=case,
                case_text_version=history.history_id if history else 0,
                status=status,
                assignee=self.author,
                tested_by=self.author,
            )
        roles.add_product_member(self.manager, self.product, granted_by=self.author)
        self.report = AITestReport.objects.create(
            owner=self.author,
            test_run=self.test_run,
            title="审计测试报告",
            summary="一条失败",
            metrics_snapshot=build_test_run_snapshot(self.test_run),
            snapshot_hash="audit-snapshot-hash",
        )

    def _make_user(self, username, permission=None):
        user = get_user_model().objects.create_user(username=username, password="pw-12345")
        permissions = permission if isinstance(permission, tuple) else (permission,)
        for item in filter(None, permissions):
            app_label, codename = item.split(".", 1)
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        return user

    def approve(self, user, **payload):
        payload.setdefault("decision", "approved")
        payload.setdefault("comment", "按流程审批")
        self.client.force_login(user)
        return self.client.post(
            reverse("ai_assistant:approve_report", args=[self.report.pk]),
            payload,
            secure=True,
            follow=True,
        )

    def latest(self, action):
        return AIAuditLog.objects.filter(action=action).order_by("-pk").first()

    # --- 审批与风险放行 -------------------------------------------------

    def test_waiver_audit_records_reason_actor_ip_and_request_id(self):
        self.approve(self.manager, waive_reason="已知问题，客户同意先上线")

        entry = self.latest("report_gate_waive")
        self.assertIsNotNone(entry, "风险放行必须留下审计记录")
        self.assertEqual(entry.actor, self.manager)
        self.assertEqual(entry.actor_username, "audit-manager")
        self.assertEqual(entry.result, audit.RESULT_SUCCESS)
        self.assertIn("客户同意先上线", entry.reason)
        self.assertEqual(entry.target_id, str(self.report.pk))
        self.assertTrue(entry.request_id, "审计要能对回后端日志的 request_id")
        self.assertIsNotNone(entry.ip)
        self.assertTrue(entry.detail["waived"])
        self.assertEqual(entry.product, self.product)

    def test_plain_approval_is_recorded(self):
        self.approve(self.author)

        entry = self.latest("report_approve")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor, self.author)
        self.assertEqual(entry.result, audit.RESULT_SUCCESS)
        self.assertEqual(entry.detail["decision"], "approved")

    def test_blocked_approval_by_author_is_recorded_as_denied(self):
        """作者审批被门禁拦下：不产生字段变更，只能靠动作审计查到。"""
        self.approve(self.author)

        denied = AIAuditLog.objects.filter(
            action="report_approve", result=audit.RESULT_DENIED
        ).first()
        self.assertIsNotNone(denied, "被门禁拒绝的审批必须留痕")
        self.assertEqual(denied.actor, self.author)
        self.assertEqual(denied.target_id, str(self.report.pk))
        self.assertIn("门禁", denied.reason)

    def test_missing_waive_reason_is_recorded_as_failed(self):
        self.approve(self.manager, waive_reason="   ")

        entry = self.latest("report_gate_waive")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.result, audit.RESULT_FAILED)
        self.assertIn("理由", entry.reason)

    # --- 发布门禁规则 ---------------------------------------------------

    def post_rule(self, **overrides):
        payload = {
            "product": self.product.pk,
            "name": "默认发布门禁",
            "block_priority": "P1",
            "min_success_rate": "95.00",
            "require_all_executed": "on",
            "max_open_defects": "0",
            "is_active": "on",
        }
        payload.update(overrides)
        return self.client.post(
            reverse("ai_assistant:release_gate_settings"), payload, secure=True
        )

    def test_rule_creation_and_change_are_audited_with_a_field_diff(self):
        self.client.force_login(self.manager)
        self.post_rule()

        created = self.latest("release_gate_update")
        self.assertIsNotNone(created)
        self.assertTrue(created.detail["created"])
        self.assertIsNone(created.detail["changes"])

        self.post_rule(min_success_rate="80.00")

        changed = self.latest("release_gate_update")
        self.assertFalse(changed.detail["created"])
        self.assertEqual(
            changed.detail["changes"]["min_success_rate"],
            {"from": "95.00", "to": "80.00"},
        )
        self.assertEqual(changed.product, self.product)

    def test_rule_change_also_lands_in_the_field_level_history(self):
        self.client.force_login(self.manager)
        self.post_rule()
        rule = AIReleaseGateRule.objects.get(product=self.product)
        self.post_rule(min_success_rate="70.00")

        history = rule.history.order_by("-history_date", "-history_id").first()
        self.assertIsNotNone(history, "门禁规则必须留下字段级历史")
        self.assertEqual(str(history.min_success_rate), "70.00")
        self.assertEqual(history.history_user, self.manager)

    def test_non_manager_cannot_touch_the_gate_and_the_attempt_is_recorded(self):
        self.client.force_login(self.author)
        response = self.post_rule()

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AIReleaseGateRule.objects.filter(product=self.product).exists())
        denied = self.latest("permission_denied")
        self.assertIsNotNone(denied)
        self.assertEqual(denied.actor, self.author)
        self.assertEqual(denied.detail["attempted"], "release_gate_update")

    # --- 成员与角色 -----------------------------------------------------

    def test_adding_and_removing_a_member_is_recorded(self):
        member = self._make_user("audit-member")
        self.client.force_login(self.manager)
        self.client.post(
            reverse("ai_assistant:add_product_member"),
            {"product": self.product.pk, "user": member.pk, "role": roles.ROLE_ENGINEER},
            secure=True,
        )

        added = self.latest("member_add")
        self.assertIsNotNone(added)
        self.assertEqual(added.detail["member"], "audit-member")
        self.assertEqual(added.detail["role"], roles.ROLE_ENGINEER)
        self.assertEqual(added.product, self.product)

        self.client.post(
            reverse("ai_assistant:remove_product_member", args=[member.pk]),
            {"product": self.product.pk},
            secure=True,
        )
        removed = self.latest("member_remove")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.detail["member"], "audit-member")

    def test_role_change_records_before_and_after(self):
        self.client.force_login(self.manager)
        self.client.post(
            reverse("ai_assistant:set_member_roles", args=[self.manager.pk]),
            {"product": self.product.pk, "roles": [roles.ROLE_MANAGER]},
            secure=True,
        )

        entry = self.latest("role_change")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.detail["after"], [roles.ROLE_MANAGER])

    # --- 目录与凭据 -----------------------------------------------------

    def test_deleting_a_folder_keeps_what_was_deleted(self):
        from .models import ProjectResourceFolder

        folder = ProjectResourceFolder.objects.create(
            product=self.product,
            resource_type="case",
            name="审计待删目录",
            created_by=self.manager,
            updated_by=self.manager,
        )
        self.client.force_login(self.manager)
        self.client.post(
            reverse("ai_assistant:delete_resource_folder", args=[folder.pk]),
            secure=True,
        )

        entry = self.latest("folder_delete")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.detail["name"], "审计待删目录")
        self.assertEqual(entry.target_id, str(folder.pk))

    def test_model_config_save_records_credential_change_without_the_key(self):
        self.client.force_login(self.author)
        self.client.post(
            reverse("ai_assistant:model_settings"),
            {
                "name": "审计模型",
                "api_base": "https://api.example.com/v1",
                "model": "audit-model",
                "timeout": 300,
                "api_key": "sk-should-never-appear-in-audit",
            },
            secure=True,
        )

        entry = self.latest("credential_update")
        self.assertIsNotNone(entry)
        self.assertIn("has_key_after", entry.detail)
        self.assertNotIn("sk-should-never-appear-in-audit", str(entry.detail))


class AuditSafetyTests(TestCase):
    """审计是旁路：它坏了不能把主流程带走。"""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="safety-user", password="pw-12345"
        )

    def test_database_error_does_not_propagate(self):
        with mock.patch.object(
            AIAuditLog.objects, "create", side_effect=DatabaseError("库挂了")
        ):
            with self.assertLogs("tcms.ai_assistant.audit", level="ERROR") as captured:
                audit.record(None, "credential_update", actor=self.user)

        self.assertTrue(
            any("audit write failed" in line for line in captured.output),
            captured.output,
        )

    def test_anonymous_actor_is_recorded_as_empty_not_wrong(self):
        from django.contrib.auth.models import AnonymousUser

        request = RequestFactory().post("/ai/x/", REMOTE_ADDR="10.0.0.1")
        request.user = AnonymousUser()
        audit.record(request, "permission_denied")

        entry = AIAuditLog.objects.latest("pk")
        self.assertIsNone(entry.actor)
        self.assertEqual(entry.actor_username, "")


class PermissionDeniedHelperTests(TestCase):
    def test_denied_helper_shape(self):
        request = RequestFactory().post("/ai/x/", REMOTE_ADDR="10.0.0.7")
        request.user = get_user_model().objects.create_user(
            username="denied-user", password="pw-12345"
        )

        audit.denied(request, "report_approve", "没有审批权限")

        entry = AIAuditLog.objects.latest("pk")
        self.assertEqual(entry.action, "permission_denied")
        self.assertEqual(entry.result, audit.RESULT_DENIED)
        self.assertEqual(entry.detail, {"attempted": "report_approve"})
        self.assertEqual(entry.reason, "没有审批权限")
        self.assertEqual(entry.ip, "10.0.0.7")
