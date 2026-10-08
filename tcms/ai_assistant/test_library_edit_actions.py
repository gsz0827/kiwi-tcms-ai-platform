from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import remove_perm

from tcms.testcases.models import TestCase as NativeCase
from . import roles, test_scenario_design
from .models import ProjectResourceAssignment


from .edit_test_client import EditClient


class LibraryEditActionTests(TestCase):
    client_class = EditClient
    setUp = test_scenario_design.DesignPageTests.setUp
    payload = test_scenario_design.DesignPageTests.payload

    def library(self):
        return self.client.get(
            reverse("ai_assistant:scenario_library"), {"product": self.product.pk}, secure=True
        )

    def edit_url(self):
        return reverse("ai_assistant:scenario_edit", args=[self.manual.pk])

    def preview(self):
        return self.client.get(
            reverse("ai_assistant:scenario_preview", args=[self.manual.pk]), secure=True
        )

    def test_library_drops_ai_generation_but_requirement_entry_is_preserved(self):
        page = self.library()
        self.assertNotContains(page, "AI 生成测试用例")
        self.assertContains(page, 'class="scenario-list-toolbar"')
        self.assertContains(
            self.client.get(reverse("ai_assistant:index"), secure=True), "新建需求"
        )

    def test_edit_links_are_in_table_modal_and_offpage_preview(self):
        page = self.library()
        self.assertContains(page, self.edit_url(), count=2)
        self.assertContains(page, f'aria-label="编辑用例 TC-{self.manual.pk}"')
        self.assertTrue(page.context["cases"][0].can_edit)
        self.assertContains(self.preview(), self.edit_url())
        self.assertEqual(self.client.get(self.edit_url(), secure=True).status_code, 200)

    def test_view_only_object_permission_hides_links_and_rejects_edit(self):
        remove_perm("change_testcase", self.user, self.manual)
        self.assertNotContains(self.library(), self.edit_url())
        self.assertFalse(self.library().context["cases"][0].can_edit)
        self.assertNotContains(self.preview(), self.edit_url())
        self.assertEqual(self.client.get(self.edit_url(), secure=True).status_code, 403)
        self.assertEqual(
            self.client.post(self.edit_url(), self.payload(), secure=True).status_code, 403
        )

    def test_readonly_role_blocks_links_even_with_object_edit_permission(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertNotContains(self.library(), self.edit_url())
        self.assertNotContains(self.preview(), self.edit_url())
        self.assertEqual(
            self.client.post(self.edit_url(), self.payload(), secure=True).status_code, 403
        )

    def test_library_editor_updates_same_case_and_preserves_history_and_associations(self):
        self.manual.save()
        ProjectResourceAssignment.objects.create(
            resource_type="case", object_id=self.manual.pk, folder=self.folder
        )
        count, history = NativeCase.objects.count(), self.manual.history.count()
        api_body, cipher = self.api.body.copy(), self.web.steps_encrypted
        page = self.client.post(
            self.edit_url(), self.payload(summary="编辑后的业务用例"), secure=True
        )
        self.assertEqual(page.status_code, 302)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.summary, "编辑后的业务用例")
        self.assertEqual(NativeCase.objects.count(), count)
        self.assertEqual(self.manual.history.count(), history + 1)
        self.assertEqual(self.manual.history.latest().history_user_id, self.user.pk)
        self.assertEqual(
            ProjectResourceAssignment.objects.get(
                resource_type="case", object_id=self.manual.pk
            ).folder_id,
            self.folder.pk,
        )
        self.api.refresh_from_db()
        self.web.refresh_from_db()
        self.assertEqual(self.api.test_case_id, self.manual.pk)
        self.assertEqual(self.api.body, api_body)
        self.assertEqual(self.web.steps_encrypted, cipher)
        self.assertContains(self.library(), "编辑后的业务用例")
