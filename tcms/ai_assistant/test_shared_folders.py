"""共享目录：用例库目录栏、项目隔离，以及需求目录的团队可见性。"""
from django.test import TestCase
from django.urls import reverse

from tcms.tests.factories import ProductFactory, TestCaseFactory, UserFactory
from . import roles
from .models import AIRequest, ProjectResourceAssignment, ProjectResourceFolder


def pane_html(response):
    """只取左侧共享目录面板的 HTML，让断言针对目录栏本身。"""
    html = response.content.decode()
    start = html.index('class="kiwi-resource-pane"')
    return html[start:html.index("</aside>", start)]


class CaseLibrarySharedFolderTests(TestCase):
    """用例库是单项目视图：目录栏跟随页面筛选，只列当前项目的目录。"""

    def setUp(self):
        self.owner = UserFactory(is_superuser=True)
        self.product = ProductFactory()
        self.other_product = ProductFactory()
        self.manual = TestCaseFactory(category__product=self.product, is_automated=False)
        self.automated = TestCaseFactory(category__product=self.product, is_automated=True)
        self.folder = ProjectResourceFolder.objects.create(
            product=self.product, resource_type="case", name="冒烟用例", created_by=self.owner
        )
        self.other_folder = ProjectResourceFolder.objects.create(
            product=self.other_product, resource_type="case", name="别的项目目录", created_by=self.owner
        )
        self.client.force_login(self.owner)

    def library(self, **params):
        params.setdefault("product", self.product.pk)
        return self.client.get(reverse("ai_assistant:case_library"), params, secure=True)

    def file_case(self, folder):
        return self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "case",
                "object_id": self.automated.pk,
                "folder": folder.pk if folder else "",
                "next": reverse("ai_assistant:case_library"),
            },
            secure=True,
        )

    def test_library_page_shows_the_folder_pane(self):
        page = self.library()

        self.assertEqual(page.status_code, 200)
        pane = pane_html(page)
        self.assertIn("用例目录", pane)
        self.assertIn("管理共享目录", pane)
        self.assertIn(self.folder.name, pane)
        self.assertIn(self.automated.summary, pane)

    def test_pane_follows_the_page_filters(self):
        automated_pane = pane_html(self.library(type="automated"))
        self.assertIn(self.automated.summary, automated_pane)
        self.assertNotIn(self.manual.summary, automated_pane)

        manual_pane = pane_html(self.library(type="manual"))
        self.assertIn(self.manual.summary, manual_pane)
        self.assertNotIn(self.automated.summary, manual_pane)

    def test_pane_only_lists_the_current_product_folders(self):
        pane = pane_html(self.library())

        self.assertIn(self.folder.name, pane)
        self.assertNotIn(self.other_folder.name, pane)

    def test_move_a_case_into_a_folder_and_back(self):
        self.assertEqual(self.file_case(self.folder).status_code, 302)
        assignment = ProjectResourceAssignment.objects.get(
            resource_type="case", object_id=self.automated.pk
        )
        self.assertEqual(assignment.folder_id, self.folder.pk)

        self.assertEqual(self.file_case(None).status_code, 302)
        self.assertFalse(
            ProjectResourceAssignment.objects.filter(
                resource_type="case", object_id=self.automated.pk
            ).exists()
        )

    def test_a_folder_of_another_product_is_rejected(self):
        response = self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "case",
                "object_id": self.automated.pk,
                "folder": self.other_folder.pk,
                "next": reverse("ai_assistant:case_library"),
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(ProjectResourceAssignment.objects.exists())


class RequirementSharedFolderTests(TestCase):
    """需求目录：项目成员互相可见，目录本身对所有登录用户开放。"""

    def setUp(self):
        roles.ensure_role_groups()
        self.author = UserFactory()
        self.member = UserFactory()
        self.outsider = UserFactory()
        self.product = ProductFactory()
        roles.add_product_member(self.author, self.product)
        roles.add_product_member(self.member, self.product)
        self.requirement = AIRequest.objects.create(
            title="导出月度报表",
            requirement="支持按月份导出报表。",
            created_by=self.author,
            category=self.product.category.get(name="--default--"),
        )
        self.folder = ProjectResourceFolder.objects.create(
            product=self.product,
            resource_type="requirement",
            name="一期需求",
            created_by=self.member,
        )

    def file_requirement(self):
        return self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            {
                "resource_type": "requirement",
                "object_id": self.requirement.pk,
                "folder": self.folder.pk,
                "next": reverse("ai_assistant:index"),
            },
            secure=True,
        )

    def test_a_member_sees_and_files_a_teammates_requirement(self):
        self.client.force_login(self.member)

        pane = pane_html(self.client.get(reverse("ai_assistant:index"), secure=True))
        self.assertIn(self.requirement.title, pane)

        self.assertEqual(self.file_requirement().status_code, 302)
        assignment = ProjectResourceAssignment.objects.get(
            resource_type="requirement", object_id=self.requirement.pk
        )
        self.assertEqual(assignment.folder_id, self.folder.pk)

    def test_project_outsider_cannot_create_a_requirement_folder(self):
        self.client.force_login(self.outsider)

        response = self.client.post(
            reverse("ai_assistant:create_resource_folder"),
            {
                "resource_type": "requirement",
                "product": self.product.pk,
                "parent": "",
                "name": "临时目录",
                "next": reverse("ai_assistant:index"),
            },
            secure=True,
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            ProjectResourceFolder.objects.filter(
                product=self.product, resource_type="requirement", name="临时目录"
            ).exists()
        )

    def test_an_outsider_cannot_see_or_file_the_requirement(self):
        self.client.force_login(self.outsider)

        pane = pane_html(self.client.get(reverse("ai_assistant:index"), secure=True))
        self.assertNotIn(self.requirement.title, pane)
        self.assertEqual(self.file_requirement().status_code, 404)
