"""Presentation parity with the case tree without expanding requirement visibility."""

from django.test import RequestFactory, TestCase
from django.urls import reverse

from tcms.tests.factories import ProductFactory
from . import roles
from .models import AIRequest, ProjectResourceAssignment, ProjectResourceFolder
from .requirement_directory import requirement_browser
from . import test_shared_folders


class RequirementDirectoryTests(TestCase):
    setUp = test_shared_folders.RequirementSharedFolderTests.setUp

    def browser(self, user=None, product=""):
        request = RequestFactory().get(reverse("ai_assistant:index"))
        request.user = user or self.member
        query = roles.visible_requests(request.user).select_related("category__product")
        if product:
            query = query.filter(category__product_id=product)
        return requirement_browser(request, query.order_by("-created"), product)

    def test_product_root_contains_unfiled_requirement_without_writes(self):
        before = ProjectResourceAssignment.objects.count()
        tree = self.browser()
        node = next(node for node in tree["nodes"] if node["kind"] == "requirement")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk}")
        self.assertEqual(
            node["path"], f"{self.product.name} / R-{self.requirement.pk} · {self.requirement.title}"
        )
        self.assertEqual(ProjectResourceAssignment.objects.count(), before)

    def test_nested_folder_and_requirement_keep_full_ancestry(self):
        child = ProjectResourceFolder.objects.create(
            product=self.product, resource_type="requirement", parent=self.folder, name="报表"
        )
        ProjectResourceAssignment.objects.create(
            resource_type="requirement", object_id=self.requirement.pk, folder=child
        )
        node = next(node for node in self.browser()["nodes"] if node["kind"] == "requirement")
        self.assertEqual(node["ancestors"], f"p:{self.product.pk},f:{self.folder.pk},f:{child.pk}")
        self.assertIn(" / 一期需求 / 报表 / R-", node["path"])
        self.assertEqual(node["indent"], 54)

    def test_other_products_are_automatic_roots_and_filter_stays_scoped(self):
        second = ProductFactory()
        AIRequest.objects.create(
            title="其他项目需求", created_by=self.member, category=second.category.first()
        )
        tree = self.browser(product=self.product.pk)
        roots = [node["pk"] for node in tree["nodes"] if node["kind"] == "product"]
        self.assertIn(second.pk, roots)
        self.assertEqual(tree["total"], 1)
        self.assertEqual(tree["selected_product"], str(self.product.pk))
        self.assertNotIn("其他项目需求", str(tree["nodes"]))

    def test_private_requirements_and_descriptions_do_not_leak(self):
        AIRequest.objects.create(
            title="PRIVATE-REQUIREMENT", requirement="PRIVATE-CONTENT", created_by=self.outsider
        )
        self.client.force_login(self.member)
        page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(page, self.requirement.title)
        self.assertNotContains(page, "PRIVATE-REQUIREMENT")
        self.assertNotContains(page, "PRIVATE-CONTENT")
        self.assertEqual(self.browser(self.outsider)["total"], 1)

    def test_uncategorized_own_requirement_is_retained(self):
        item = AIRequest.objects.create(title="待归类", created_by=self.member)
        tree = self.browser()
        node = next(node for node in tree["nodes"] if node["key"] == f"r:{item.pk}")
        self.assertEqual(node["ancestors"], "u")
        self.assertFalse(node["can_move"])
        self.assertIn("未指定项目", node["path"])

    def test_directory_is_not_limited_to_twenty_recent_requirements(self):
        AIRequest.objects.bulk_create(
            [
                AIRequest(
                    title=f"需求 {i}", created_by=self.author, category=self.requirement.category
                )
                for i in range(25)
            ]
        )
        tree = self.browser()
        self.assertEqual(tree["total"], 26)
        self.assertEqual(sum(node["kind"] == "requirement" for node in tree["nodes"]), 26)
        self.assertFalse(tree["has_more"])

    def test_tree_layout_modal_and_folder_actions_render(self):
        self.client.force_login(self.member)
        page = self.client.get(reverse("ai_assistant:index"), secure=True)
        self.assertContains(page, 'data-resource-browser="requirement" data-product-tree')
        self.assertContains(page, 'data-tree-number-prefix="r"')
        self.assertContains(page, 'class="kiwi-resource-browser-header product-tree-heading"')
        self.assertContains(page, f'data-directory-node="{self.folder.pk}"')
        self.assertContains(page, f'id="kiwi-resource-modal-requirement-{self.requirement.pk}"')
        self.assertContains(page, f'id="requirement-folder-{self.requirement.pk}"')
        self.assertContains(page, "项目根目录")
        self.assertNotContains(page, "未归档")

    def test_product_rename_is_reflected_without_reassigning_requirement(self):
        self.product.name = "新的项目名"
        self.product.save()
        tree = self.browser()
        root = next(node for node in tree["nodes"] if node["key"] == f"p:{self.product.pk}")
        self.assertEqual(root["name"], "新的项目名")
        self.assertFalse(ProjectResourceAssignment.objects.exists())
