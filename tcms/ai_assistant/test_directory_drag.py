"""Drag/drop reuses the protected POST actions, never changing case payloads."""

from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from tcms.tests.factories import ProductFactory, UserFactory
from . import roles
from .models import ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment
from .test_directory_tools import DirectoryToolsTests


class DirectoryDragTests(TestCase):
    setUp = DirectoryToolsTests.setUp

    def assign(self, kind, case, folder="", **extra):
        return self.client.post(
            reverse("ai_assistant:assign_resource_folder"),
            dict(resource_type=kind, object_id=case.pk, folder=folder, next=self.hub) | extra,
            secure=True,
        )

    def move(self, folder, parent="", **extra):
        return self.client.post(
            reverse("ai_assistant:move_resource_folder", args=[folder.pk]),
            dict(parent=parent, next=self.hub) | extra,
            secure=True,
        )

    def child(self, name="子目录", **extra):
        return Folder.objects.create(
            **(
                dict(product=self.product, resource_type="case_group", name=name, parent=self.folder)
                | extra
            )
        )

    def test_three_cases_move_unfile_and_preserve_payload(self):
        child = self.child()
        original = self.manual.text, self.manual.summary, self.web.steps_encrypted, self.api.body
        for kind, case in (("case", self.manual), ("web_case", self.web), ("api_case", self.api)):
            self.assertEqual(self.assign(kind, case, child.pk).status_code, 302)
            self.assertEqual(
                Assignment.objects.get(resource_type=kind, object_id=case.pk).folder_id, child.pk
            )
            self.assertEqual(self.assign(kind, case).status_code, 302)
            self.assertFalse(
                Assignment.objects.filter(resource_type=kind, object_id=case.pk).exists()
            )
        for case in (self.manual, self.web, self.api):
            case.refresh_from_db()
        self.assertEqual(
            original, (self.manual.text, self.manual.summary, self.web.steps_encrypted, self.api.body)
        )

    def test_case_cross_project_and_incompatible_type_rejected(self):
        wrong_project = self.child(product=ProductFactory(), parent=None)
        wrong_type = self.child(resource_type="api_case")
        for kind, case in (("case", self.manual), ("web_case", self.web)):
            for target in (wrong_project, wrong_type):
                self.assertEqual(self.assign(kind, case, target.pk).status_code, 403)
        self.assertEqual(
            Assignment.objects.get(resource_type="web_case", object_id=self.web.pk).folder_id,
            self.folder.pk,
        )

    def test_automation_ownership_even_for_superuser(self):
        for user in (self.other, UserFactory(is_superuser=True)):
            self.client.force_login(user)
            for kind, case in (("web_case", self.web), ("api_case", self.api)):
                self.assertEqual(self.assign(kind, case, self.folder.pk).status_code, 404)

    def test_readonly_no_write_or_draggable_metadata(self):
        child = self.child()
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        for kind, case in (("case", self.manual), ("web_case", self.web), ("api_case", self.api)):
            self.assertEqual(self.assign(kind, case, child.pk).status_code, 403)
        self.assertEqual(self.move(self.folder, child.pk).status_code, 403)
        page = self.client.get(self.hub, {"product": self.product.pk}, secure=True)
        self.assertNotContains(page, 'data-tree-drag="case"')
        self.assertNotContains(page, 'data-tree-drag="folder"')

    def test_whole_folder_preserves_children_assignments(self):
        child = self.child()
        target = self.child(name="目标目录", parent=None)
        self.assertEqual(self.move(self.folder, target.pk).status_code, 302)
        self.folder.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(self.folder.parent_id, target.pk)
        self.assertEqual(child.parent_id, self.folder.pk)
        self.assertEqual(
            Assignment.objects.get(resource_type="web_case", object_id=self.web.pk).folder_id,
            self.folder.pk,
        )

    def test_self_and_descendant_cycles_rejected(self):
        child = self.child()
        grandchild = self.child(name="孙目录", parent=child)
        for target in (self.folder, child, grandchild):
            response = self.move(self.folder, target.pk)
            self.assertEqual(response.status_code, 302)
            self.folder.refresh_from_db()
            self.assertIsNone(self.folder.parent_id)
            self.assertIn("不能将目录", str(list(response.wsgi_request._messages)))

    def test_folder_cross_project_and_type_rejected(self):
        for target in (
            self.child(product=ProductFactory(), parent=None),
            self.child(resource_type="api_case", parent=None),
        ):
            self.assertEqual(self.move(self.folder, target.pk).status_code, 403)
        self.folder.refresh_from_db()
        self.assertIsNone(self.folder.parent_id)

    def test_folder_root_and_duplicate_name(self):
        child = self.child()
        self.assertEqual(self.move(child).status_code, 302)
        child.refresh_from_db()
        self.assertIsNone(child.parent_id)
        duplicate = self.child(name=child.name)
        self.assertEqual(self.move(duplicate).status_code, 302)
        duplicate.refresh_from_db()
        self.assertEqual(duplicate.parent_id, self.folder.pk)

    def test_post_csrf_login_safe_return(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        for url in (
            reverse("ai_assistant:assign_resource_folder"),
            reverse("ai_assistant:move_resource_folder", args=[self.folder.pk]),
        ):
            self.assertEqual(self.client.get(url, secure=True).status_code, 405)
            self.assertEqual(strict.post(url, secure=True).status_code, 403)
        self.assertEqual(
            self.move(self.folder, next="https://evil.invalid/").url, reverse("core-views-index")
        )
        self.client.logout()
        self.assertEqual(self.move(self.folder).status_code, 302)

    def test_hub_and_business_modules_keep_drag_while_config_pages_only_filter(self):
        for route in ("ai_assistant:case_hub", "ai_assistant:case_library"):
            page = self.client.get(reverse(route), {"product": self.product.pk}, secure=True)
            for value in (
                'data-tree-drag="case"',
                f'data-drag-product="{self.product.pk}"',
                'data-tree-drag="folder"',
                f'data-drop-id="{self.folder.pk}"',
                'data-tree-drop="root"',
                'data-tree-drop="unfiled"',
                'id="directory-move-form"',
            ):
                self.assertContains(page, value)
        for route, params in (("web_testing:cases", {}), ("ai_assistant:api_home", {"tab":"cases"})):
            page = self.client.get(reverse(route), {"product":self.product.pk} | params, secure=True)
            self.assertNotContains(page,'data-tree-drag=')
            self.assertNotContains(page,'data-tree-drop=')
            self.assertContains(page,'在用例库管理目录')
