"""发布门禁：产品级规则、硬门禁审批、测试经理风险放行。

这一组测试守住三条边界：
1. 门禁规则挂产品，一个产品一条，没配规则时明确回落到「内置默认门禁」；
2. 门禁未通过时**任何人都不能直接审批通过**，报告作者也不行——否则硬门禁形同虚设；
3. 只有测试经理能填理由做风险放行，理由与时间进版本历史，发布结论由门禁推导。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from tcms.management.models import Build, Classification, Priority, Product, Version
from tcms.testcases.models import TestCase as FormalTestCase
from tcms.testcases.models import TestCaseStatus
from tcms.testplans.models import PlanType, TestPlan
from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun

from . import roles
from .engineering import BUILTIN_GATE_NAME, evaluate_release_gate
from .models import AIReleaseGateRule, AITestReport
from .services import build_test_run_snapshot


class ReleaseGateTests(TestCase):
    """一个 50% 成功率的执行任务：内建门禁必然阻断，是硬门禁的最小复现场景。"""

    def setUp(self):
        # 审批与查看都要先过 _require_run_permission（testruns.view_testrun）；
        # 经理额外拿「审批 AI 报告」权限，也就是角色矩阵里的测试经理。
        self.author = self._make_user("gate-author", permission="testruns.view_testrun")
        self.manager = self._make_user(
            "gate-manager",
            permission=(
                "testruns.view_testrun",
                roles.PERM_APPROVE_REPORT,
            ),
        )
        self.product = Product.objects.create(
            name="门禁产品", classification=Classification.objects.create(name="门禁分类")
        )
        version = Version.objects.create(value="1.0", product=self.product)
        build = Build.objects.create(name="门禁构建", version=version)
        plan = TestPlan.objects.create(
            name="门禁计划",
            text="发布门禁验证计划",
            product_version=version,
            author=self.author,
            product=self.product,
            type=PlanType.objects.create(name="门禁计划类型"),
        )
        self.test_run = TestRun.objects.create(
            summary="门禁执行任务",
            plan=plan,
            build=build,
            manager=self.author,
            default_tester=self.author,
        )
        passed_status = TestExecutionStatus.objects.create(
            name="门禁通过", weight=1, icon="fa fa-check", color="#00FF00"
        )
        failed_status = TestExecutionStatus.objects.create(
            name="门禁失败", weight=-1, icon="fa fa-times", color="#FF0000"
        )
        priority, _created = Priority.objects.get_or_create(value="P1")
        case_status = TestCaseStatus.objects.filter(is_confirmed=True).first()
        if case_status is None:
            case_status = TestCaseStatus.objects.create(
                name="门禁已确认", is_confirmed=True
            )
        category = self.product.category.get(name="--default--")
        for summary, status in (("通过用例", passed_status), ("失败用例", failed_status)):
            case = FormalTestCase.objects.create(
                summary=summary,
                requirement="门禁需求",
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
            title="门禁测试报告",
            summary="一条失败",
            metrics_snapshot=build_test_run_snapshot(self.test_run),
            snapshot_hash="gate-snapshot-hash",
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

    def test_builtin_gate_is_used_and_named_when_no_rule_exists(self):
        result = evaluate_release_gate(
            self.product, self.report.metrics_snapshot, [self.test_run.pk]
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["rule"], BUILTIN_GATE_NAME)
        self.assertEqual(result["rule_scope"], "builtin")
        self.assertIn("成功率", "".join(check["name"] for check in result["checks"]))

    def test_author_cannot_approve_a_blocked_report(self):
        response = self.approve(self.author)

        self.assertContains(response, "发布门禁未通过")
        self.report.refresh_from_db()
        self.assertEqual(self.report.approval_status, "pending")
        self.assertEqual(self.report.release_decision, "no_go")
        self.assertIsNone(self.report.gate_waived_at)
        self.assertTrue(self.report.gate_result)
        self.assertFalse(self.report.revisions.exists())

    def test_manager_needs_a_reason_to_waive(self):
        response = self.approve(self.manager, waive_reason="  ")

        self.assertContains(response, "风险放行必须填写理由")
        self.report.refresh_from_db()
        self.assertEqual(self.report.approval_status, "pending")
        self.assertIsNone(self.report.gate_waived_at)

    def test_manager_waives_with_a_reason_and_it_lands_in_the_history(self):
        response = self.approve(self.manager, waive_reason="已知问题，客户同意先上线")

        self.assertEqual(response.status_code, 200)
        self.report.refresh_from_db()
        self.assertEqual(self.report.approval_status, "approved")
        self.assertEqual(self.report.release_decision, "go")
        self.assertEqual(self.report.gate_waived_by, self.manager)
        self.assertEqual(self.report.gate_waive_reason, "已知问题，客户同意先上线")
        self.assertIsNotNone(self.report.gate_waived_at)
        revision = self.report.revisions.get()
        self.assertTrue(revision.change_reason.startswith("风险放行："))
        self.assertIn("客户同意先上线", revision.change_reason)
        page = self.client.get(
            reverse("ai_assistant:edit_report", args=[self.report.pk]), secure=True
        )
        self.assertContains(page, "已风险放行")
        exported = self.client.get(
            reverse("ai_assistant:export_report_html", args=[self.report.pk]), secure=True
        )
        self.assertContains(exported, "风险放行")

    def test_gate_pass_lets_the_author_approve_directly(self):
        AIReleaseGateRule.objects.create(
            product=self.product,
            min_success_rate=0,
            require_all_executed=False,
            max_open_defects=10,
        )

        self.approve(self.author)

        self.report.refresh_from_db()
        self.assertEqual(self.report.approval_status, "approved")
        self.assertEqual(self.report.release_decision, "go")
        self.assertIsNone(self.report.gate_waived_at)

    def test_release_decision_is_derived_and_not_editable(self):
        # 先让门禁跑一次，把发布结论落到「不建议发布」——结论是推导出来的，不是人填的。
        self.approve(self.author)
        self.report.refresh_from_db()
        self.assertEqual(self.report.release_decision, "no_go")

        self.client.force_login(self.author)
        self.client.post(
            reverse("ai_assistant:edit_report", args=[self.report.pk]),
            {
                "title": "门禁测试报告",
                "summary": "一条失败",
                "scope": "",
                "conclusion": "",
                "release_decision": "go",
                "recommendations_text": "",
                "change_reason": "试图改发布结论",
            },
            secure=True,
        )

        self.report.refresh_from_db()
        self.assertEqual(self.report.release_decision, "no_go")

    def test_only_the_manager_can_open_gate_settings(self):
        self.client.force_login(self.author)
        denied = self.client.get(reverse("ai_assistant:release_gate_settings"), secure=True)
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.manager)
        page = self.client.get(reverse("ai_assistant:release_gate_settings"), secure=True)
        self.assertContains(page, "内置默认门禁")
        self.assertContains(page, "门禁产品")

    def test_saving_a_rule_twice_updates_the_same_row(self):
        self.client.force_login(self.manager)
        payload = {
            "product": self.product.pk,
            "name": "发布门槛",
            "block_priority": "P2",
            "min_success_rate": "90",
            "max_open_defects": "2",
            "is_active": "on",
        }
        self.client.post(reverse("ai_assistant:release_gate_settings"), payload, secure=True)
        # 第二次保存改的是阈值：50% 成功率在 40% 的门槛下应当放行。
        payload["min_success_rate"] = "40"
        self.client.post(reverse("ai_assistant:release_gate_settings"), payload, secure=True)

        self.assertEqual(AIReleaseGateRule.objects.count(), 1)
        rule = AIReleaseGateRule.objects.get()
        self.assertEqual(rule.product, self.product)
        self.assertEqual(rule.min_success_rate, 40)
        self.assertEqual(rule.updated_by, self.manager)
        result = evaluate_release_gate(
            self.product, self.report.metrics_snapshot, [self.test_run.pk]
        )
        self.assertEqual(result["rule"], "发布门槛")
        self.assertEqual(result["rule_scope"], "product")
        self.assertTrue(result["passed"])

    def test_gate_settings_entry_is_hidden_from_non_managers(self):
        self.client.force_login(self.author)
        author_page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertNotContains(author_page, "门禁设置")

        self.client.force_login(self.manager)
        manager_page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(manager_page, "门禁设置")
