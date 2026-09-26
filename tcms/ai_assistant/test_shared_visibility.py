"""共享资产的可见性一致性：列表里看得到的，点开也要打得开。

角色与权限把「同一产品的成员」当作可见范围（``roles.visible_*``）。列表页用了这些
queryset，详情页如果还按作者过滤，就会出现「列表里看得到、点开 403/404」的分裂——
造演示数据时就撞上了：经理在「测试质量趋势」里看得到同事的报告，点开却是 403。

这一组测试钉住五条路径：测试报告详情、执行任务的报告列表、测试质量趋势、
迭代报告详情、需求下草稿用例的编辑与导入。三个账号的 Django 权限完全一样，
差别只在产品成员关系，这样断言才只反映可见范围本身。
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
from .models import AIIterationReport, AIRequest, AITestCaseDraft, AITestReport
from .services import build_test_run_snapshot


class SharedAssetVisibilityTests(TestCase):
    """同一产品里两个同事 + 一个局外人；权限相同，只有产品成员关系不同。"""

    def setUp(self):
        self.author = self._make_user("visibility-author")
        self.colleague = self._make_user("visibility-colleague")
        self.outsider = self._make_user("visibility-outsider")

        self.product = Product.objects.create(
            name="可见性产品",
            classification=Classification.objects.create(name="可见性分类"),
        )
        version = Version.objects.create(value="1.0", product=self.product)
        build = Build.objects.create(name="可见性构建", version=version)
        plan = TestPlan.objects.create(
            name="可见性计划",
            text="可见性验证计划",
            product_version=version,
            author=self.author,
            product=self.product,
            type=PlanType.objects.create(name="可见性计划类型"),
        )
        self.test_run = TestRun.objects.create(
            summary="可见性执行任务",
            plan=plan,
            build=build,
            manager=self.author,
            default_tester=self.author,
        )
        passed = TestExecutionStatus.objects.create(
            name="可见性通过", weight=1, icon="fa fa-check", color="#00FF00"
        )
        failed = TestExecutionStatus.objects.create(
            name="可见性失败", weight=-1, icon="fa fa-times", color="#FF0000"
        )
        priority, _created = Priority.objects.get_or_create(value="P1")
        case_status = TestCaseStatus.objects.filter(is_confirmed=True).first()
        if case_status is None:
            case_status = TestCaseStatus.objects.create(
                name="可见性已确认", is_confirmed=True
            )
        category = self.product.category.get(name="--default--")
        for summary, status in (("可见性通过用例", passed), ("可见性失败用例", failed)):
            case = FormalTestCase.objects.create(
                summary=summary,
                requirement="可见性需求",
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

        self.report = AITestReport.objects.create(
            owner=self.author,
            test_run=self.test_run,
            title="可见性测试报告",
            summary="一条失败",
            metrics_snapshot=build_test_run_snapshot(self.test_run),
            snapshot_hash="visibility-snapshot-hash",
        )
        self.iteration = AIIterationReport.objects.create(
            owner=self.author,
            title="可见性迭代报告",
            product=self.product,
            metrics_snapshot={"metrics": {"success_rate": 50}},
            conclusion="迭代结论",
            release_decision="no_go",
            snapshot_hash="visibility-iteration-hash",
        )
        self.ai_request = AIRequest.objects.create(
            title="可见性需求",
            requirement="同一产品的同事之间应当能互相协作。",
            created_by=self.author,
            category=category,
        )
        self.draft = AITestCaseDraft.objects.create(
            request=self.ai_request,
            case_number="TC-1",
            summary="可见性草稿用例",
        )
        roles.add_product_member(self.colleague, self.product, granted_by=self.author)

    def _make_user(self, username, with_run_permission=True):
        user = get_user_model().objects.create_user(username=username, password="pw-12345")
        permissions = ["testcases.add_testcase", "testcases.change_testcase"]
        if with_run_permission:
            permissions.append("testruns.view_testrun")
        for item in permissions:
            app_label, codename = item.split(".", 1)
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label=app_label, codename=codename
                )
            )
        return user

    def _get(self, user, name, *args):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args), secure=True)

    def _post(self, user, name, payload, *args):
        self.client.force_login(user)
        return self.client.post(reverse(name, args=args), payload, secure=True)

    def test_colleague_can_open_the_report_detail(self):
        page = self._get(self.colleague, "ai_assistant:edit_report", self.report.pk)

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "可见性测试报告")

    def test_outsider_cannot_open_the_report_detail(self):
        page = self._get(self.outsider, "ai_assistant:edit_report", self.report.pk)

        self.assertEqual(page.status_code, 404)

    def test_product_member_opens_the_run_pages_without_the_run_permission(self):
        """造演示数据时撞上的那条路：产品成员没有 testruns.view_testrun，也该进得去。"""
        member = self._make_user("visibility-member", with_run_permission=False)
        roles.add_product_member(member, self.product, granted_by=self.author)

        run_page = self._get(member, "ai_assistant:run_report", self.test_run.pk)
        report_page = self._get(member, "ai_assistant:edit_report", self.report.pk)

        self.assertEqual(run_page.status_code, 200)
        self.assertContains(run_page, "可见性测试报告")
        self.assertEqual(report_page.status_code, 200)

    def test_stranger_is_refused_on_the_run_page(self):
        stranger = self._make_user("visibility-stranger", with_run_permission=False)

        run_page = self._get(stranger, "ai_assistant:run_report", self.test_run.pk)

        self.assertEqual(run_page.status_code, 403)

    def test_run_page_lists_the_report_for_colleagues_only(self):
        colleague_page = self._get(
            self.colleague, "ai_assistant:run_report", self.test_run.pk
        )
        outsider_page = self._get(self.outsider, "ai_assistant:run_report", self.test_run.pk)

        self.assertContains(colleague_page, "可见性测试报告")
        self.assertNotContains(outsider_page, "可见性测试报告")

    def test_trends_list_the_report_for_colleagues_only(self):
        colleague_page = self._get(self.colleague, "ai_assistant:report_trends")
        outsider_page = self._get(self.outsider, "ai_assistant:report_trends")

        self.assertContains(colleague_page, "可见性测试报告")
        self.assertNotContains(outsider_page, "可见性测试报告")

    def test_colleague_can_open_the_iteration_report(self):
        colleague_page = self._get(
            self.colleague, "ai_assistant:iteration_report_detail", self.iteration.pk
        )
        outsider_page = self._get(
            self.outsider, "ai_assistant:iteration_report_detail", self.iteration.pk
        )

        self.assertEqual(colleague_page.status_code, 200)
        self.assertEqual(outsider_page.status_code, 404)

    def test_colleague_can_open_a_case_draft(self):
        colleague_page = self._get(self.colleague, "ai_assistant:edit_draft", self.draft.pk)
        outsider_page = self._get(self.outsider, "ai_assistant:edit_draft", self.draft.pk)

        self.assertEqual(colleague_page.status_code, 200)
        self.assertEqual(outsider_page.status_code, 404)

    def test_colleague_can_import_a_case_draft(self):
        before = FormalTestCase.objects.count()

        response = self._post(
            self.colleague,
            "ai_assistant:import",
            {"draft_ids": [self.draft.pk]},
            self.ai_request.pk,
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(FormalTestCase.objects.count(), before + 1)

    def test_outsider_cannot_import_a_case_draft(self):
        before = FormalTestCase.objects.count()

        response = self._post(
            self.outsider,
            "ai_assistant:import",
            {"draft_ids": [self.draft.pk]},
            self.ai_request.pk,
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(FormalTestCase.objects.count(), before)
