from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import ProductFactory, TestCaseFactory
from . import roles, test_directory_tools
from .models import ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment


class ProductCaseTreeTests(TestCase):
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def test_product_creation_and_rename_automatically_update_root_without_folder_rows(self):
        product = ProductFactory(name="自动生成根目录")
        page = self.library()
        roots = [
            node
            for node in self.nodes(page)
            if node["kind"] == "product" and node["pk"] == product.pk
        ]
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["path"], "自动生成根目录")
        self.assertFalse(Folder.objects.filter(product=product).exists())
        product.name = "项目已改名"
        product.save()
        page = self.library()
        root = next(
            node
            for node in self.nodes(page)
            if node["kind"] == "product" and node["pk"] == product.pk
        )
        self.assertEqual(root["name"], "项目已改名")
        self.assertEqual(root["path"], "项目已改名")
        self.assertFalse(Folder.objects.filter(product=product).exists())

    def library(self, **params):
        return self.client.get(reverse("ai_assistant:scenario_library"), params, secure=True)

    def nodes(self, page):
        return page.wsgi_request.case_hub_browser["nodes"]

    def nested(self):
        child = Folder.objects.create(
            product=self.product, resource_type="case_group", parent=self.folder, name="验证码"
        )
        Assignment.objects.create(resource_type="case", object_id=self.manual.pk, folder=child)
        return child

    def test_products_are_roots_and_unassigned_cases_are_direct_children_without_writes(self):
        self.manual.save()
        history, text = self.manual.history.count(), self.manual.text
        page = self.library(product=self.product.pk)
        node = next(node for node in self.nodes(page) if node["kind"] == "case")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk}")
        self.assertEqual(
            node["path"], f"{self.product.name} / TC-{self.manual.pk} · {self.manual.summary}"
        )
        self.assertEqual(page.context["cases"][0].directory_path, self.product.name)
        self.assertFalse(
            Assignment.objects.filter(resource_type="case", object_id=self.manual.pk).exists()
        )
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, text)
        self.assertEqual(self.manual.history.count(), history)
        self.assertContains(page, "product-tree-heading")
        self.assertContains(page, "输入编号或者名称筛选")
        self.assertContains(page, "<th>路径</th>", html=True)
        self.assertNotContains(page, "未归档")
        self.assertNotContains(page, "当前列表中的用例")

    def test_library_keeps_complete_path_without_duplicate_directory_column(self):
        self.nested()
        for params, expected_count in [({}, 8), ({"product": self.product.pk}, 7)]:
            page = self.library(**params)
            table = page.content.decode().split('class="table scenario-table"', 1)[1].split("</table>", 1)[0]
            header = table.split("<thead>", 1)[1].split("</thead>", 1)[0]
            self.assertNotIn("<th>目录</th>", header)
            self.assertEqual(header.count("<th>路径</th>"), 1)
            self.assertEqual(header.count("<th>"), expected_count)
            self.assertIn(f"{self.product.name} / 登录目录 / 验证码", table)
            self.assertIn(f'data-toggle="modal">{self.manual.summary}</a>', table)
            self.assertContains(page, 'data-tree-command="copy-path"')
            self.assertContains(page, 'data-tree-drop="root"')
            empty = self.library(q="NO-MATCH-COLUMNS-QA", **params)
            self.assertContains(empty, f'colspan="{expected_count}" class="api-empty"')


    def test_nested_folder_path_and_tree_ancestry(self):
        child = self.nested()
        page = self.library(product=self.product.pk)
        node = next(node for node in self.nodes(page) if node["kind"] == "case")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk},f:{self.folder.pk},f:{child.pk}")
        self.assertEqual(node["depth"], 3)
        self.assertEqual(
            page.context["cases"][0].directory_path, f"{self.product.name} / 登录目录 / 验证码"
        )
        self.assertContains(page, 'data-tree-command="copy-path"')
        self.assertContains(page, 'data-tree-drop="root"')
        self.assertContains(page, 'data-drop-accept-case="true"')

    def test_tree_spans_products_but_table_is_product_filtered(self):
        other_product = ProductFactory()
        case = TestCaseFactory(author=self.user, category__product=other_product)
        assign_perm("view_testcase", self.user, case)
        page = self.library(product=self.product.pk)
        self.assertEqual(page.context["cases"].paginator.count, 1)
        node = next(
            node for node in self.nodes(page) if node["kind"] == "case" and node["pk"] == case.pk
        )
        self.assertEqual(node["ancestors"], f"p:{other_product.pk}")
        self.assertEqual(node["modal_id"], "")
        self.assertFalse(node["can_move"])
        self.assertContains(page, f'data-case-preview-url="{node["preview_url"]}"')

    def test_filter_by_name_number_and_prefixed_number(self):
        for term in (
            self.manual.summary,
            str(self.manual.pk),
            f"tc-{self.manual.pk}",
            f"TC-{self.manual.pk:05d}",
        ):
            page = self.library(q=term)
            self.assertEqual(page.context["cases"].paginator.count, 1)
        self.assertEqual(
            self.library(q="TC-99999999999999999999999999").context["cases"].paginator.count, 0
        )

    def test_readonly_can_copy_visible_paths_and_preview_but_not_write(self):
        self.nested()
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        page = self.library(product=self.product.pk)
        self.assertContains(page, 'data-tree-type="case"')
        self.assertContains(page, "data-tree-path=")
        self.assertNotContains(page, "data-directory-node=")
        self.assertNotContains(page, "data-case-node=")
        self.assertNotContains(page, "data-tree-create-url=")
        self.assertNotContains(page, 'draggable="true"')
        response = self.client.get(
            reverse("ai_assistant:scenario_preview", args=[self.manual.pk]), secure=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.manual.summary)

    def test_preview_is_get_only_private_safe_and_not_a_full_page(self):
        url = reverse("ai_assistant:scenario_preview", args=[self.manual.pk])
        page = self.client.get(url, secure=True)
        self.assertContains(page, self.manual.summary)
        self.assertNotContains(page, "UNCHANGED-SECRET")
        self.assertNotContains(page, "UNCHANGED-CIPHER")
        self.assertNotContains(page, "<html")
        self.assertIn("no-store", page["Cache-Control"])
        self.assertEqual(self.client.post(url, secure=True).status_code, 405)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url, secure=True).status_code, 404)

    def test_tree_contains_off_page_cases_without_embedding_all_bodies(self):
        self.manual.text = "DO-NOT-EMBED-OFFPAGE-DESIGN"
        self.manual.save()
        for i in range(31):
            case = TestCaseFactory(
                author=self.user, category=self.manual.category, summary=f"分页测试{i}"
            )
            assign_perm("view_testcase", self.user, case)
        page = self.library(product=self.product.pk)
        self.assertEqual(len(page.context["cases"]), 30)
        self.assertEqual(len([node for node in self.nodes(page) if node["kind"] == "case"]), 32)
        node = next(
            node
            for node in self.nodes(page)
            if node["kind"] == "case" and node["pk"] == self.manual.pk
        )
        self.assertEqual(node["modal_id"], "")
        self.assertNotContains(page, "DO-NOT-EMBED-OFFPAGE-DESIGN")

    def test_capped_tree_explicitly_reports_limit_and_server_search_finds_omitted_case(self):
        case = TestCaseFactory(
            author=self.user, category=self.manual.category, summary="ZZZZ分页末尾"
        )
        assign_perm("view_testcase", self.user, case)
        with patch("tcms.ai_assistant.product_case_tree.TREE_LIMIT", 1):
            page = self.library()
            self.assertTrue(page.wsgi_request.case_hub_browser["has_more"])
            self.assertContains(page, "目录显示前 1 条匹配用例")
            page = self.library(q=f"TC-{case.pk}")
            self.assertEqual(page.context["cases"][0].pk, case.pk)
            self.assertFalse(page.wsgi_request.case_hub_browser["has_more"])

    def test_wrong_product_assignment_does_not_contribute_to_path(self):
        folder = Folder.objects.create(
            product=ProductFactory(), resource_type="case_group", name="错误项目文件夹"
        )
        Assignment.objects.create(resource_type="case", object_id=self.manual.pk, folder=folder)
        page = self.library()
        self.assertEqual(page.context["cases"][0].directory_path, self.product.name)
        node = next(node for node in self.nodes(page) if node["kind"] == "case")
        self.assertNotIn("错误项目文件夹", node["path"])

    def test_move_case_to_root_only_clears_folder_relation(self):
        self.nested()
        text, history = self.manual.text, self.manual.history.count()
        response = self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            dict(resource_type="case", object_id=self.manual.pk, folder=""),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(
            Assignment.objects.filter(resource_type="case", object_id=self.manual.pk).exists()
        )
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, text)
        self.assertEqual(self.manual.history.count(), history)
        self.assertEqual(self.library().context["cases"][0].directory_path, self.product.name)

    def test_renaming_and_moving_folder_recomputes_path_without_case_write(self):
        child = self.nested()
        history = self.manual.history.count()
        self.folder.name = "新登录目录"
        self.folder.save()
        self.assertEqual(
            self.library().context["cases"][0].directory_path,
            f"{self.product.name} / 新登录目录 / 验证码",
        )
        response = self.client.post(
            reverse("ai_assistant:move_resource_folder", args=[child.pk]), {"parent": ""}, secure=True
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            self.library().context["cases"][0].directory_path, f"{self.product.name} / 验证码"
        )
        self.assertEqual(self.manual.history.count(), history)

    def test_delete_folder_keeps_case_and_returns_it_to_product_root(self):
        self.nested()
        response = self.client.post(
            reverse("ai_assistant:delete_resource_folder", args=[self.folder.pk]), secure=True
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Folder.objects.filter(pk=self.folder.pk).exists())
        self.assertEqual(self.library().context["cases"][0].directory_path, self.product.name)

    def test_legacy_unfiled_link_displays_product_instead_of_removed_bucket(self):
        self.nested()
        page = self.library(product=self.product.pk, folder="unfiled")
        self.assertEqual(page.context["cases"].paginator.count, 1)
        self.assertNotContains(page, "未归档")

    def test_foreign_case_names_never_enter_tree_or_preview(self):
        case = TestCaseFactory(author=self.other, summary="PRIVATE-CASE-NAME")
        page = self.library()
        self.assertNotContains(page, "PRIVATE-CASE-NAME")
        self.assertEqual(
            self.client.get(
                reverse("ai_assistant:scenario_preview", args=[case.pk]), secure=True
            ).status_code,
            404,
        )
