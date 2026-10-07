"""Plan library scope, summaries, reuse, and the existing edit workflow."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from guardian.shortcuts import assign_perm

from tcms.ai_assistant.models import ProjectResourceAssignment, ProjectResourceFolder
from tcms.tests.factories import (
    ProductFactory,
    VersionFactory,
    TestPlanFactory,
    TestCaseFactory,
    TestRunFactory,
    CategoryFactory,
)


class PlanWorkspaceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="plan-viewer")
        cls.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testplans", codename="view_testplan")
        )
        cls.product = ProductFactory(name="Alpha project")
        cls.version = VersionFactory(product=cls.product, value="1.0")
        cls.version2 = VersionFactory(product=cls.product, value="2.0")
        cls.plan = TestPlanFactory(
            product=cls.product, product_version=cls.version, name="Smoke test plan"
        )
        cls.second = TestPlanFactory(
            product=cls.product, product_version=cls.version2, name="Regression test plan"
        )
        cls.other_product = ProductFactory(name="Other project")
        cls.other = TestPlanFactory(
            product=cls.other_product, product_version=VersionFactory(product=cls.other_product)
        )
        cls.inactive = TestPlanFactory(
            product=cls.product, product_version=cls.version, is_active=False
        )
        category = CategoryFactory(product=cls.product)
        cls.case1 = TestCaseFactory(category=category, default_tester=cls.user)
        cls.case2 = TestCaseFactory(category=category)
        cls.plan.add_case(cls.case1)
        cls.plan.add_case(cls.case2)
        cls.visible_run = TestRunFactory(plan=cls.plan, summary="Visible execution")
        cls.hidden_run = TestRunFactory(plan=cls.plan, summary="Hidden execution")
        assign_perm("testruns.view_testrun", cls.user, cls.visible_run)
        cls.folder = ProjectResourceFolder.objects.create(
            product=cls.product,
            resource_type="plan",
            name="Release",
            created_by=cls.user,
            updated_by=cls.user,
        )
        cls.subfolder = ProjectResourceFolder.objects.create(
            product=cls.product,
            resource_type="plan",
            name="Smoke",
            parent=cls.folder,
            created_by=cls.user,
            updated_by=cls.user,
        )
        ProjectResourceAssignment.objects.create(
            folder=cls.subfolder, resource_type="plan", object_id=cls.plan.pk, assigned_by=cls.user
        )

    def setUp(self):
        self.client.force_login(self.user)

    def search(self, **params):
        return self.client.get(reverse("plans-search"), params, secure=True)

    def ids(self, response):
        self.assertEqual(response.status_code, 200)
        return [plan.pk for plan in response.context["plans"]]

    def test_default_active_newest_first(self):
        response = self.search()
        self.assertEqual(self.ids(response), [self.other.pk, self.second.pk, self.plan.pk])
        self.assertNotContains(response, 'id="resultsTable"')
        self.assertNotContains(response, "新建测试计划")

    def test_project_version_and_tree_are_consistent(self):
        response = self.search(product=self.product.pk, product_version=self.version.pk)
        self.assertEqual(self.ids(response), [self.plan.pk])
        self.assertEqual(
            [obj.pk for obj in response.wsgi_request.plan_browser["items"]], [self.plan.pk]
        )
        self.assertTrue(
            all(
                item.product_id == self.product.pk
                for item in response.wsgi_request.plan_browser["items"]
            )
        )

    def test_session_project_and_version(self):
        session = self.client.session
        session["ai_product_id"] = str(self.product.pk)
        session["ai_version_id"] = str(self.version.pk)
        session.save()
        self.assertEqual(self.ids(self.search()), [self.plan.pk])
        self.assertEqual(len(self.ids(self.search(product="", product_version="", status="all"))), 4)

    def test_top_bar_version_alias(self):
        self.assertEqual(
            self.ids(self.search(product=self.product.pk, version=self.version2.pk)), [self.second.pk]
        )

    def test_invalid_and_cross_product_filters_fail_closed(self):
        for params in [
            {"product": "unknown"},
            {"product": "²"},
            {"product": "9" * 60},
            {"product": self.other_product.pk, "product_version": self.version.pk},
            {"product_version": "²"},
            {"folder": "²"},
            {"folder": "99999999"},
        ]:
            with self.subTest(params=params):
                self.assertEqual(self.ids(self.search(**params)), [])

    def test_id_and_name_search(self):
        self.assertEqual(self.ids(self.search(name=f"TP-{self.plan.pk}")), [self.plan.pk])
        self.assertEqual(self.ids(self.search(name="Regression")), [self.second.pk])

    def test_inactive_plan_not_mislabeled_as_finished(self):
        response = self.search(product=self.product.pk, status="inactive")
        self.assertEqual(self.ids(response), [self.inactive.pk])
        self.assertContains(response, "停用")

    def test_folder_includes_descendants(self):
        self.assertEqual(self.ids(self.search(folder=self.folder.pk)), [self.plan.pk])
        self.assertEqual(
            self.ids(self.search(product=self.other_product.pk, folder=self.folder.pk)), []
        )

    def test_summary_counts_independent_of_case_filter_and_visibility(self):
        response = self.search(name=f"TP-{self.plan.pk}", default_tester=self.user.username)
        plan = response.context["plans"][0]
        self.assertEqual(plan.case_total, 2)
        self.assertEqual(plan.run_total, 1)

    def test_detail_preserves_native_hooks_and_visible_runs(self):
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible execution")
        self.assertNotContains(response, "Hidden execution")
        self.assertEqual(response.context["plan_run_count"], 1)
        for marker in [
            "plan-panel-cases",
            "plan-panel-runs",
            "plan-panel-description",
            "plan-panel-more",
            "test_case_row",
            "test_plan_pk",
            "testcases-list",
        ]:
            self.assertContains(response, marker)
        self.assertNotContains(
            response, 'href="' + reverse("plan-edit", args=[self.plan.pk]) + '">编辑计划'
        )

    def test_run_states_use_execution_dates(self):
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, "未开始")
        self.visible_run.start_date = timezone.now()
        self.visible_run.save()
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, "执行中")
        self.visible_run.stop_date = timezone.now()
        self.visible_run.save()
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, "已结束")

    def test_pagination_keeps_filter(self):
        for n in range(22):
            TestPlanFactory(
                product=self.product, product_version=self.version, name=f"Paged plan {n}"
            )
        first = self.search(product=self.product.pk, name="Paged plan")
        second = self.search(product=self.product.pk, name="Paged plan", page=2)
        self.assertEqual(len(self.ids(first)), 20)
        self.assertEqual(len(self.ids(second)), 2)
        self.assertContains(first, "name=Paged+plan")
        self.assertFalse(set(self.ids(first)) & set(self.ids(second)))

    def test_invalid_dates_are_reported(self):
        response = self.search(after="2026-02-31")
        self.assertEqual(self.ids(response), [])
        self.assertContains(response, "创建日期格式不正确")

    def test_plan_new_prefills_project_and_version(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testplans", codename="add_testplan")
        )
        response = self.client.get(
            reverse("plans-new"),
            {"product": self.product.pk, "product_version": self.version.pk},
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(response.context["form"]["product"].value()), self.product.pk)
        self.assertEqual(int(response.context["form"]["product_version"].value()), self.version.pk)
        self.assertContains(response, 'name="email_settings-TOTAL_FORMS"')

    def test_empty_plan_create_run_disabled(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testruns", codename="add_testrun")
        )
        response = self.client.get(self.second.get_absolute_url(), secure=True)
        self.assertContains(response, "请先关联已确认的测试用例")

    def test_object_plan_edit_permission(self):
        assign_perm("testplans.change_testplan", self.user, self.plan)
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, '">编辑计划</a>')

    def test_view_permission_required(self):
        stranger = get_user_model().objects.create_user(username="plan-stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.search().status_code, 302)

    def test_script_name_escaped(self):
        self.plan.name = '<img src=x onerror="alert(1)">'
        self.plan.save()
        response = self.search(name=f"TP-{self.plan.pk}")
        self.assertContains(response, "&lt;img")
        self.assertNotContains(response, '<img src=x onerror="alert(1)">')

    def test_plan_pages_do_not_load_grappelli_without_admin_configuration(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testplans", codename="add_testplan")
        )
        for path in (
            reverse("plans-search"),
            self.plan.get_absolute_url(),
            reverse("plans-new"),
            reverse("plans-clone", args=[self.plan.pk]),
        ):
            with self.subTest(path=path):
                response = self.client.get(path, secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'src="/static/grappelli/js/grappelli.min.js"')

    def test_clone_reuses_case_references_by_default_without_touching_runs(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testplans", codename="add_testplan")
        )
        response = self.client.post(
            reverse("plans-clone", args=[self.plan.pk]),
            {"name": "Reused plan", "product": self.product.pk, "version": self.version2.pk},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        from tcms.testplans.models import TestPlan

        clone = TestPlan.objects.get(name="Reused plan")
        self.assertEqual(
            set(clone.cases.values_list("pk", flat=True)), {self.case1.pk, self.case2.pk}
        )
        self.assertEqual(clone.run.count(), 0)
        self.assertEqual(self.plan.run.count(), 2)

    def test_directory_preview_uses_full_case_count(self):
        response = self.search(name=f"TP-{self.plan.pk}", default_tester=self.user.username)
        self.assertEqual(response.wsgi_request.plan_browser["items"][0].case_total, 2)

    def test_new_form_related_popup_dependencies_are_ordered(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testplans", codename="add_testplan")
        )
        response = self.client.get(reverse("plans-new"), secure=True)
        content = response.content.decode()
        self.assertLess(
            content.index("admin/js/vendor/jquery/jquery.min.js"),
            content.index("admin/js/jquery.init.js"),
        )
        self.assertLess(
            content.index("admin/js/jquery.init.js"),
            content.index("admin/js/admin/RelatedObjectLookups.js"),
        )

    def test_removing_from_plan_never_deletes_shared_case(self):
        self.second.add_case(self.case1)
        self.plan.delete_case(self.case1)
        self.assertFalse(self.plan.cases.filter(pk=self.case1.pk).exists())
        self.assertTrue(self.second.cases.filter(pk=self.case1.pk).exists())

    def test_run_dates_follow_display_timezone(self):
        from datetime import datetime
        from tcms.kiwi_auth.preferences_model import UserPreference

        self.visible_run.start_date = datetime(2026, 10, 4, 2, 22)
        self.visible_run.save()
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, "2026-10-04 10:22")
        UserPreference.objects.create(user=self.user, time_zone="Etc/UTC")
        response = self.client.get(self.plan.get_absolute_url(), secure=True)
        self.assertContains(response, "2026-10-04 02:22")

    def test_creation_date_filter_uses_display_day(self):
        from datetime import datetime
        from tcms.testplans.models import TestPlan
        from tcms.kiwi_auth.preferences_model import UserPreference

        TestPlan.objects.filter(pk=self.plan.pk).update(
            create_date=datetime(2026, 10, 3, 18, 0)
        )
        params = {"name": f"TP-{self.plan.pk}", "after": "2026-10-04", "before": "2026-10-04"}
        self.assertEqual(self.ids(self.search(**params)), [self.plan.pk])
        UserPreference.objects.create(user=self.user, time_zone="Etc/UTC")
        self.assertEqual(self.ids(self.search(**params)), [])
