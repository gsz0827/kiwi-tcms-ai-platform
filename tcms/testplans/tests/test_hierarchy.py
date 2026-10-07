"""Plan nesting, permission boundaries, distinct cases and historical run rollups."""

from django.test import TestCase, RequestFactory
from django.urls import reverse
from guardian.shortcuts import assign_perm
from tcms.tests.factories import (
    UserFactory,
    ProductFactory,
    VersionFactory,
    TestPlanFactory,
    CategoryFactory,
    TestCaseFactory,
    TestRunFactory,
    BuildFactory,
    TestExecutionFactory,
)
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestExecutionStatus
from tcms.testplans.plan_hierarchy import PlanHierarchy, rollup_context
from tcms.ai_assistant.models import ProjectResourceAssignment, ProjectResourceFolder


class PlanHierarchyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = UserFactory(is_superuser=True)
        cls.product = ProductFactory(name="Hierarchy product")
        cls.v1 = VersionFactory(product=cls.product, value="1.0")
        cls.v2 = VersionFactory(product=cls.product, value="2.0")
        cls.parent = TestPlanFactory(product=cls.product, product_version=cls.v1, name="Release")
        cls.child = TestPlanFactory(
            product=cls.product, product_version=cls.v1, parent=cls.parent, name="Login"
        )
        cls.grandchild = TestPlanFactory(
            product=cls.product,
            product_version=cls.v2,
            parent=cls.child,
            name="Regression",
            is_active=False,
        )
        cls.sibling = TestPlanFactory(product=cls.product, product_version=cls.v1, name="Independent")
        category = CategoryFactory(product=cls.product)
        cls.case1 = TestCaseFactory(category=category)
        cls.case2 = TestCaseFactory(category=category)
        cls.parent.add_case(cls.case1)
        cls.child.add_case(cls.case1)
        cls.child.add_case(cls.case2)
        cls.grandchild.add_case(cls.case1)
        cls.pending = TestExecutionStatus.objects.create(name="Hierarchy pending", weight=0)
        cls.passed = TestExecutionStatus.objects.create(name="Hierarchy passed", weight=10)
        cls.failed = TestExecutionStatus.objects.create(name="Hierarchy failed", weight=-10)
        cls.runs = []
        for plan, case, status in (
            (cls.parent, cls.case1, cls.pending),
            (cls.child, cls.case1, cls.passed),
            (cls.grandchild, cls.case1, cls.failed),
        ):
            build = BuildFactory(version=plan.product_version)
            run = TestRunFactory(plan=plan, build=build, summary="Run " + plan.name)
            TestExecutionFactory(run=run, case=case, build=build, status=status)
            cls.runs.append(run)
        cls.folder = ProjectResourceFolder.objects.create(
            product=cls.product, resource_type="plan", name="Releases"
        )
        cls.other_folder = ProjectResourceFolder.objects.create(
            product=cls.product, resource_type="plan", name="Legacy child folder"
        )
        ProjectResourceAssignment.objects.create(
            resource_type="plan", object_id=cls.parent.pk, folder=cls.folder
        )
        ProjectResourceAssignment.objects.create(
            resource_type="plan", object_id=cls.child.pk, folder=cls.other_folder
        )

    def setUp(self):
        self.client.force_login(self.owner)

    def context(self, plan=None, user=None):
        request = RequestFactory().get("/")
        request.user = user or self.owner
        return rollup_context(plan or self.parent, request)

    def test_rollup_deduplicates_cases_not_executions(self):
        result = self.context()["plan_rollup"]
        self.assertEqual(result["subplans"], 2)
        self.assertEqual(result["cases"], 2)
        self.assertEqual(result["runs"], 3)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(result["passed"], 1)
        self.assertEqual(result["unsuccessful"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["completion_rate"], 66.7)
        self.assertEqual(result["pass_rate"], 50.0)

    def test_empty_and_all_pending_do_not_show_success(self):
        empty = self.context(self.sibling)["plan_rollup"]
        self.assertIsNone(empty["completion_rate"])
        self.assertIsNone(empty["pass_rate"])
        self.runs[1].executions.update(status=self.pending)
        self.runs[2].executions.update(status=self.pending)
        pending = self.context()["plan_rollup"]
        self.assertEqual(pending["completion_rate"], 0)
        self.assertIsNone(pending["pass_rate"])

    def test_multiple_versions_and_inactive_descendants_remain_in_history(self):
        rows = self.context()["plan_descendants"]
        self.assertEqual([row["plan"].pk for row in rows], [self.child.pk, self.grandchild.pk])
        self.assertEqual([row["indent"] for row in rows], [0, 18])

    def test_rollup_read_does_not_copy_or_modify_records(self):
        from tcms.testcases.models import TestCase, TestCasePlan
        from tcms.testruns.models import TestRun, TestExecution

        models = [TestPlan, TestCase, TestCasePlan, TestRun, TestExecution, ProjectResourceAssignment]
        before = [model.objects.count() for model in models]
        status = list(TestExecution.objects.values_list("pk", "status_id", "case_text_version"))
        self.context()
        self.assertEqual(before, [model.objects.count() for model in models])
        self.assertEqual(
            status, list(TestExecution.objects.values_list("pk", "status_id", "case_text_version"))
        )

    def test_permissions_hide_plans_runs_cases_and_relationship_names(self):
        user = UserFactory()
        assign_perm("testplans.view_testplan", user, self.parent)
        assign_perm("testplans.view_testplan", user, self.child)
        assign_perm("testruns.view_testrun", user, self.runs[0])
        assign_perm("testcases.view_testcase", user, self.case1)
        self.client.force_login(user)
        response = self.client.get(self.parent.get_absolute_url(), secure=True)
        self.assertEqual(response.status_code, 200)
        result = response.context["plan_rollup"]
        self.assertEqual(
            (result["subplans"], result["cases"], result["runs"], result["total"]), (1, 1, 1, 1)
        )
        self.assertNotContains(response, "Regression")
        self.assertNotContains(response, "Run Login")
        assign_perm("testplans.view_testplan", user, self.grandchild)
        self.client.get(self.parent.get_absolute_url(), secure=True)
        self.assertEqual(self.context(user=user)["plan_rollup"]["subplans"], 2)

    def test_hidden_parent_is_not_exposed(self):
        user = UserFactory()
        assign_perm("testplans.view_testplan", user, self.child)
        self.assertIsNone(self.context(self.child, user)["plan_parent"])
        self.client.force_login(user)
        response = self.client.get(self.child.get_absolute_url(), secure=True)
        self.assertNotContains(response, "Release")

    def test_new_child_prefills_parent_project_version_type(self):
        response = self.client.get(reverse("plans-new"), {"parent": self.grandchild.pk}, secure=True)
        form = response.context["form"]
        for key, value in (
            ("parent", self.grandchild.pk),
            ("product", self.product.pk),
            ("product_version", self.v2.pk),
            ("type", self.grandchild.type_id),
        ):
            self.assertEqual(int(form[key].value()), value)
        self.assertContains(response, "上级计划（可选）")
        self.assertContains(response, "无上级计划")

    def test_invalid_or_private_parent_get_fails_closed(self):
        user = UserFactory()
        assign_perm("testplans.add_testplan", user)
        self.client.force_login(user)
        for pk in (self.parent.pk, "bad", "99999999999999999999"):
            self.assertEqual(
                self.client.get(reverse("plans-new"), {"parent": pk}, secure=True).status_code, 404
            )

    def test_directory_nests_once_and_inherits_root_folder(self):
        response = self.client.get(
            reverse("plans-search"), {"product": self.product.pk, "status": "all"}, secure=True
        )
        tree = response.wsgi_request.plan_browser
        nodes = [node for node in tree["nodes"] if node["type"] == "resource"]
        self.assertEqual(len(nodes), 4)
        child = next(node for node in nodes if node["id"] == self.child.pk)
        grandchild = next(node for node in nodes if node["id"] == self.grandchild.pk)
        self.assertIn(f"t:{self.parent.pk}", child["ancestors"])
        self.assertIn(f"t:{self.child.pk}", grandchild["ancestors"])
        self.assertIn(f"f:{self.folder.pk}", grandchild["ancestors"])
        self.assertTrue(next(node for node in nodes if node["id"] == self.parent.pk)["has_children"])
        self.assertFalse(child["can_move"])
        self.assertContains(response, "data-tree-child-plan-url=")
        self.assertEqual(
            ProjectResourceAssignment.objects.get(
                resource_type="plan", object_id=self.child.pk
            ).folder_id,
            self.other_folder.pk,
        )

    def test_folder_filter_matches_tree_and_includes_descendants(self):
        response = self.client.get(
            reverse("plans-search"), {"folder": self.folder.pk, "status": "all"}, secure=True
        )
        self.assertEqual(
            {p.pk for p in response.context["plans"]},
            {self.parent.pk, self.child.pk, self.grandchild.pk},
        )
        response = self.client.get(
            reverse("plans-search"), {"folder": self.other_folder.pk, "status": "all"}, secure=True
        )
        self.assertEqual(list(response.context["plans"]), [])

    def test_search_and_version_filters_preserve_matching_child(self):
        response = self.client.get(
            reverse("plans-search"), {"name": "Regression", "status": "all"}, secure=True
        )
        nodes = [
            node for node in response.wsgi_request.plan_browser["nodes"] if node["type"] == "resource"
        ]
        self.assertEqual([node["id"] for node in nodes], [self.grandchild.pk])
        self.assertIn(f"f:{self.folder.pk}", nodes[0]["ancestors"])
        self.assertNotIn(f"t:{self.parent.pk}", nodes[0]["ancestors"])

    def test_execution_scope_switch_and_pagination(self):
        response = self.client.get(self.parent.get_absolute_url(), secure=True)
        self.assertEqual(response.context["plan_runs"].paginator.count, 1)
        for n in range(20):
            TestRunFactory(plan=self.child, build=self.runs[1].build, summary=f"Paged {n}")
        response = self.client.get(
            self.parent.get_absolute_url(), {"run_scope": "family"}, secure=True
        )
        self.assertEqual(response.context["plan_runs"].paginator.count, 23)
        self.assertEqual(response.context["plan_run_count"], 1)
        self.assertContains(response, "run_scope=family&amp;run_page=2")

    def test_cross_product_legacy_relationship_not_aggregated(self):
        foreign = TestPlanFactory(parent=self.parent, name="Foreign legacy")
        self.assertNotIn(
            foreign.pk, [p.pk for p, _ in PlanHierarchy(self.owner).family(self.parent.pk)]
        )

    def test_cycle_is_bounded_without_mutating_legacy_data(self):
        TestPlan.objects.filter(pk=self.parent.pk).update(parent=self.grandchild)
        graph = PlanHierarchy(self.owner)
        self.assertLessEqual(len(graph.family(self.parent.pk)), 3)
        self.assertIsNone(graph.parents[min(self.parent.pk, self.child.pk, self.grandchild.pk)])
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.parent_id, self.grandchild.pk)

    def test_detail_renders_tabs_and_no_unauthorized_tree_html(self):
        response = self.client.get(self.parent.get_absolute_url(), secure=True)
        for text in ("含下级计划汇总", "plan-panel-hierarchy", "新建子计划", "已执行通过率", "66.7%"):
            self.assertContains(response, text)
