"""Shared navigation must never change plans, run results, or access rules."""

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import (
    BuildFactory,
    ProductFactory,
    TestPlanFactory,
    TestRunFactory,
    UserFactory,
    VersionFactory,
)
from tcms.rpc.api.testrun import filter as run_filter
from . import roles
from .models import ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment


class PlanRunDirectoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = UserFactory()
        for permission in (
            "testplans.view_testplan",
            "testplans.change_testplan",
            "testruns.view_testrun",
            "testruns.change_testrun",
        ):
            assign_perm(permission, cls.user)
        cls.product = ProductFactory(name="目录项目")
        cls.empty = ProductFactory(name="空项目")
        cls.version = VersionFactory(product=cls.product)
        cls.build = BuildFactory(version=cls.version)
        cls.plan = TestPlanFactory(product=cls.product, product_version=cls.version, name="回归计划")
        cls.execution = TestRunFactory(
            plan=cls.plan, build=cls.build, summary="登录回归执行", start_date=None, stop_date=None
        )
        cls.root_run = TestRunFactory(
            plan=cls.plan, build=cls.build, summary="项目根任务", start_date=None, stop_date=None
        )
        cls.plan_folder = Folder.objects.create(
            product=cls.product, resource_type="plan", name="版本计划"
        )
        cls.execution_folder = Folder.objects.create(
            product=cls.product, resource_type="run", name="版本执行"
        )
        cls.sub = Folder.objects.create(
            product=cls.product, resource_type="run", name="冒烟", parent=cls.execution_folder
        )
        Assignment.objects.create(resource_type="plan", object_id=cls.plan.pk, folder=cls.plan_folder)
        Assignment.objects.create(resource_type="run", object_id=cls.execution.pk, folder=cls.sub)

    def setUp(self):
        self.client.force_login(self.user)

    def page(self, kind, **params):
        response = self.client.get(
            reverse("plans-search" if kind == "plan" else "testruns-search"), params, secure=True
        )
        self.assertEqual(response.status_code, 200)
        return response, getattr(response.wsgi_request, kind + "_browser")

    def post(self, view, data=None, args=None):
        return self.client.post(reverse("ai_assistant:" + view, args=args), data or {}, secure=True)

    def test_plan_tree_uses_product_roots_and_preserves_old_assignments(self):
        response, tree = self.page("plan", product=self.product.pk)
        self.assertContains(response, "data-product-tree")
        self.assertContains(response, 'data-tree-number-prefix="tp"')
        self.assertNotContains(response, "未归档")
        roots = [node["label"] for node in tree["nodes"] if node["type"] == "product"]
        self.assertIn(self.product.name, roots)
        self.assertIn(self.empty.name, roots)
        node = next(node for node in tree["nodes"] if node["key"] == f"t:{self.plan.pk}")
        self.assertIn(f"f:{self.plan_folder.pk}", node["ancestors"])
        self.assertIn("目录项目 / 版本计划 / TP-", node["path"])
        self.assertEqual(
            Assignment.objects.get(resource_type="plan", object_id=self.plan.pk).folder_id,
            self.plan_folder.pk,
        )

    def test_run_tree_places_unassigned_tasks_at_product_root(self):
        response, tree = self.page("run", product=self.product.pk, version=self.version.pk)
        self.assertNotContains(response, "未归档")
        self.assertContains(response, 'id="resultsTable"')  # Native AJAX/export table is retained.
        node = next(node for node in tree["nodes"] if node["key"] == f"t:{self.root_run.pk}")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk}")
        self.assertEqual(node["url"], reverse("testruns-get", args=[self.root_run.pk]))
        assigned = next(node for node in tree["nodes"] if node["key"] == f"t:{self.execution.pk}")
        self.assertIn(f"f:{self.sub.pk}", assigned["ancestors"])
        self.assertIn("版本执行 / 冒烟 / TR-", assigned["path"])

    def test_root_navigation_clears_other_products_version_and_folder(self):
        _, tree = self.page(
            "run", product=self.product.pk, version=self.version.pk, folder=self.sub.pk
        )
        node = next(node for node in tree["nodes"] if node["key"] == f"p:{self.empty.pk}")
        self.assertIn(f"product={self.empty.pk}", node["url"])
        self.assertNotIn("version=", node["url"])
        self.assertNotIn("folder=", node["url"])

    def test_invalid_project_or_cross_project_version_fails_closed(self):
        other_version = VersionFactory(product=self.empty)
        for data in (
            {"product": "bad"},
            {"product": "9" * 30},
            {"product": self.empty.pk, "version": self.version.pk},
            {"product": self.product.pk, "version": other_version.pk},
        ):
            _, tree = self.page("run", **data)
            self.assertEqual(tree["items"], [])

    def test_session_scope_is_shared_with_native_run_form(self):
        session = self.client.session
        session["ai_product_id"] = self.product.pk
        session["ai_version_id"] = self.version.pk
        session.save()
        response, tree = self.page("run")
        self.assertEqual(response.context["directory_product"], str(self.product.pk))
        self.assertEqual({obj.pk for obj in tree["items"]}, {self.execution.pk, self.root_run.pk})

    def test_run_product_is_build_product_even_for_legacy_mismatched_plan(self):
        other_plan = TestPlanFactory(
            product=self.empty, product_version=VersionFactory(product=self.empty)
        )
        self.execution.plan = other_plan
        self.execution.save()
        _, tree = self.page("run", product=self.product.pk)
        self.assertIn(self.execution.pk, {obj.pk for obj in tree["items"]})
        response = self.post(
            "assign_resource_folder",
            dict(resource_type="run", object_id=self.execution.pk, folder=self.sub.pk),
        )
        self.assertEqual(response.status_code, 302)

    def test_folder_filter_includes_descendants_and_preserves_query(self):
        data = {"_resource_folder": str(self.execution_folder.pk), "stop_date__isnull": True}
        self.assertEqual([row["id"] for row in run_filter(data)], [self.execution.pk])
        self.assertIn("_resource_folder", data)
        self.assertEqual(
            [row["id"] for row in run_filter({"plan": self.plan.pk})],
            [self.execution.pk, self.root_run.pk],
        )

    def test_invalid_folder_ids_and_wrong_resource_types_return_no_runs(self):
        for folder in ("bad", "9" * 30, "unfiled", str(self.plan_folder.pk), "-1", ""):
            self.assertEqual(run_filter({"_resource_folder": folder}), [])

    def test_cross_product_assignments_are_not_shown_or_matched(self):
        wrong = Folder.objects.create(product=self.empty, resource_type="run", name="错误归档")
        Assignment.objects.filter(resource_type="run", object_id=self.execution.pk).update(
            folder=wrong
        )
        self.assertEqual(run_filter({"_resource_folder": str(wrong.pk)}), [])
        _, tree = self.page("run", product=self.product.pk)
        node = next(node for node in tree["nodes"] if node["key"] == f"t:{self.execution.pk}")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk}")

    def test_create_and_rename_run_folder(self):
        self.assertEqual(
            self.post(
                "create_resource_folder",
                dict(
                    resource_type="run",
                    product=self.product.pk,
                    parent=self.execution_folder.pk,
                    name="专项",
                ),
            ).status_code,
            302,
        )
        folder = Folder.objects.get(product=self.product, resource_type="run", name="专项")
        self.assertEqual(folder.parent_id, self.execution_folder.pk)
        self.assertEqual(
            self.post("rename_resource_folder", {"name": "专项测试"}, [folder.pk]).status_code, 302
        )
        folder.refresh_from_db()
        self.assertEqual(folder.name, "专项测试")

    def test_delete_run_folder_returns_records_to_root_without_deleting_results(self):
        self.assertEqual(
            self.post("delete_resource_folder", args=[self.execution_folder.pk]).status_code, 302
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.plan_id, self.plan.pk)
        self.assertEqual(self.execution.build_id, self.build.pk)
        self.assertFalse(
            Assignment.objects.filter(resource_type="run", object_id=self.execution.pk).exists()
        )
        _, tree = self.page("run", product=self.product.pk)
        node = next(node for node in tree["nodes"] if node["key"] == f"t:{self.execution.pk}")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk}")

    def test_move_run_record_keeps_native_plan_and_history_unchanged(self):
        before = self.execution.history.count()
        self.assertEqual(
            self.post(
                "assign_resource_folder",
                dict(
                    resource_type="run", object_id=self.execution.pk, folder=self.execution_folder.pk
                ),
            ).status_code,
            302,
        )
        self.execution.refresh_from_db()
        self.assertEqual(self.execution.plan_id, self.plan.pk)
        self.assertEqual(self.execution.history.count(), before)
        self.assertEqual(
            self.post(
                "assign_resource_folder",
                dict(resource_type="run", object_id=self.execution.pk, folder=""),
            ).status_code,
            302,
        )
        self.assertFalse(
            Assignment.objects.filter(resource_type="run", object_id=self.execution.pk).exists()
        )

    def test_move_folder_and_reject_self_or_descendants(self):
        for kind, folder in (("plan", self.plan_folder), ("run", self.execution_folder)):
            child = Folder.objects.create(
                product=self.product, resource_type=kind, name="下级", parent=folder
            )
            self.assertEqual(
                self.post("move_resource_folder", {"parent": child.pk}, [folder.pk]).status_code, 302
            )
            folder.refresh_from_db()
            self.assertIsNone(folder.parent_id)
            self.assertEqual(
                self.post("move_resource_folder", {"parent": ""}, [child.pk]).status_code, 302
            )
            child.refresh_from_db()
            self.assertIsNone(child.parent_id)

    def test_cross_product_or_type_moves_are_forbidden(self):
        other = Folder.objects.create(product=self.empty, resource_type="run", name="其他")
        for folder in (other, self.plan_folder):
            self.assertEqual(
                self.post(
                    "assign_resource_folder",
                    dict(resource_type="run", object_id=self.execution.pk, folder=folder.pk),
                ).status_code,
                403,
            )
            self.assertEqual(
                self.post(
                    "move_resource_folder", {"parent": folder.pk}, [self.execution_folder.pk]
                ).status_code,
                403,
            )

    def test_view_only_user_cannot_manage_directories_or_move_records(self):
        reader = UserFactory()
        assign_perm("testplans.view_testplan", reader)
        assign_perm("testruns.view_testrun", reader)
        self.client.force_login(reader)
        for kind, resource, folder in (
            ("plan", self.plan, self.plan_folder),
            ("run", self.execution, self.execution_folder),
        ):
            response, tree = self.page(kind, product=self.product.pk)
            self.assertFalse(
                any(node.get("can_manage") or node.get("can_move") for node in tree["nodes"])
            )
            self.assertNotContains(response, 'data-tree-drag="resource"')
            self.assertEqual(
                self.post(
                    "create_resource_folder",
                    dict(product=self.product.pk, resource_type=kind, name="拒绝"),
                ).status_code,
                403,
            )
            self.assertEqual(
                self.post(
                    "assign_resource_folder",
                    dict(resource_type=kind, object_id=resource.pk, folder=folder.pk),
                ).status_code,
                403,
            )

    def test_explicit_read_only_role_overrides_edit_permissions(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        for kind, folder, resource in (
            ("plan", self.plan_folder, self.plan),
            ("run", self.execution_folder, self.execution),
        ):
            _, tree = self.page(kind, product=self.product.pk)
            self.assertFalse(
                any(node.get("can_manage") or node.get("can_move") for node in tree["nodes"])
            )
            self.assertEqual(
                self.post("rename_resource_folder", {"name": "拒绝"}, [folder.pk]).status_code, 403
            )
            self.assertEqual(
                self.post("move_resource_folder", {"parent": ""}, [folder.pk]).status_code, 403
            )
            self.assertEqual(
                self.post(
                    "assign_resource_folder",
                    dict(resource_type=kind, object_id=resource.pk, folder=""),
                ).status_code,
                403,
            )

    def test_object_edit_permission_allows_only_that_record_to_move(self):
        user = UserFactory()
        assign_perm("testruns.view_testrun", user)
        assign_perm("testruns.change_testrun", user, self.execution)
        self.client.force_login(user)
        _, tree = self.page("run", product=self.product.pk)
        self.assertEqual(
            {node["id"] for node in tree["nodes"] if node.get("can_move")}, {self.execution.pk}
        )
        self.assertEqual(
            self.post(
                "assign_resource_folder",
                dict(
                    resource_type="run", object_id=self.execution.pk, folder=self.execution_folder.pk
                ),
            ).status_code,
            302,
        )
        self.assertEqual(
            self.post(
                "assign_resource_folder",
                dict(
                    resource_type="run", object_id=self.root_run.pk, folder=self.execution_folder.pk
                ),
            ).status_code,
            403,
        )

    def test_corrupt_folder_cycle_does_not_hide_records_or_loop(self):
        Folder.objects.filter(pk=self.execution_folder.pk).update(parent=self.sub)
        _, tree = self.page("run", product=self.product.pk)
        keys = [node["key"] for node in tree["nodes"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn(f"t:{self.execution.pk}", keys)
