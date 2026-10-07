from unittest.mock import patch
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm
from tcms.testcases.models import TestCase as NativeCase
from tcms.tests.factories import ProductFactory, UserFactory
from . import roles, test_scenario_design
from .models import ProjectResourceAssignment, ProjectResourceFolder


class ScenarioCreationTests(TestCase):
    setUp = test_scenario_design.DesignPageTests.setUp
    payload = test_scenario_design.DesignPageTests.payload

    def grant_add(self):
        assign_perm("testcases.add_testcase", self.user)

    def new_url(self):
        return reverse("ai_assistant:scenario_new", args=[self.product.pk])

    def library(self, **params):
        return self.client.get(reverse("ai_assistant:scenario_library"), params, secure=True)

    def test_button_visible_in_all_projects_and_selected_product(self):
        self.grant_add()
        response = self.library(product="")
        self.assertContains(response, 'id="scenario-create-button"')
        self.assertContains(response, 'id="scenario-create-product"')
        response = self.library(product=self.product.pk)
        self.assertContains(response, 'id="scenario-create-button"')
        self.assertContains(response, self.new_url())
        self.assertNotContains(response, 'id="scenario-create-product"')

    def test_toolbar_and_context_menu_preserve_selected_folder(self):
        self.grant_add()
        response = self.library(product=self.product.pk, folder=self.folder.pk)
        self.assertEqual(response.context["create_url"], self.new_url() + f"?folder={self.folder.pk}")
        self.assertContains(response, "data-tree-new-case-url=")
        self.assertContains(response, self.new_url() + f"?folder={self.folder.pk}")

    def test_new_form_prefills_directory_and_only_same_product_folders(self):
        self.grant_add()
        response = self.client.get(self.new_url(), {"folder": self.folder.pk}, secure=True)
        self.assertEqual(int(response.context["form"]["folder"].value()), self.folder.pk)
        self.assertContains(response, "保存目录")
        self.assertContains(response, f"{self.product.name} / {self.folder.name}")
        foreign = ProjectResourceFolder.objects.create(
            product=ProductFactory(), resource_type="case", name="Foreign folder"
        )
        field = response.context["form"].fields["folder"]
        self.assertFalse(field.queryset.filter(pk=foreign.pk).exists())

    def test_create_saves_one_case_in_selected_folder_and_detail_back_link(self):
        self.grant_add()
        count = NativeCase.objects.count()
        response = self.client.post(
            self.new_url(),
            self.payload(summary="目录中新建", folder=self.folder.pk, requirement=""),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        case = NativeCase.objects.get(summary="目录中新建")
        self.assertEqual(NativeCase.objects.count(), count + 1)
        self.assertEqual(case.author_id, self.user.pk)
        self.assertFalse(case.is_automated)
        self.assertEqual(
            ProjectResourceAssignment.objects.get(resource_type="case", object_id=case.pk).folder_id,
            self.folder.pk,
        )
        self.assertTrue(self.user.has_perm("testcases.view_testcase", case))
        page = self.client.get(response.url, secure=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"folder={self.folder.pk}", page.context["back_url"])
        page = self.library(product=self.product.pk, folder=self.folder.pk)
        self.assertIn(case.pk, [c.pk for c in page.context["cases"]])

    def test_root_creation_does_not_create_unfiled_assignment(self):
        self.grant_add()
        response = self.client.post(
            self.new_url(), self.payload(summary="根目录新建", folder=""), secure=True
        )
        self.assertEqual(response.status_code, 302)
        case = NativeCase.objects.get(summary="根目录新建")
        self.assertFalse(
            ProjectResourceAssignment.objects.filter(resource_type="case", object_id=case.pk).exists()
        )

    def test_invalid_form_keeps_chosen_folder_and_steps(self):
        self.grant_add()
        response = self.client.post(
            self.new_url(), self.payload(summary="", folder=self.folder.pk), secure=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("summary", response.context["form"].errors)
        self.assertEqual(str(response.context["form"]["folder"].value()), str(self.folder.pk))
        self.assertContains(response, "操作步骤")

    def test_forged_folder_cross_product_or_type_is_rejected_without_saving(self):
        self.grant_add()
        wrong_product = ProjectResourceFolder.objects.create(
            product=ProductFactory(), resource_type="case", name="Foreign"
        )
        wrong_kind = ProjectResourceFolder.objects.create(
            product=self.product, resource_type="plan", name="Plans"
        )
        count = NativeCase.objects.count()
        for value in (wrong_product.pk, wrong_kind.pk, "bad", "9" * 60):
            response = self.client.post(self.new_url(), self.payload(folder=value), secure=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("folder", response.context["form"].errors)
            self.assertEqual(NativeCase.objects.count(), count)
            self.assertEqual(
                self.client.get(self.new_url(), {"folder": value}, secure=True).status_code, 404
            )

    def test_folder_changed_during_save_rolls_back_new_case_and_history(self):
        self.grant_add()
        count, history = NativeCase.objects.count(), NativeCase.history.count()
        with patch(
            "tcms.ai_assistant.scenario_workflow.assign_created_case",
            side_effect=ValueError("目录已删除"),
        ):
            response = self.client.post(
                self.new_url(), self.payload(folder=self.folder.pk), secure=True
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "目录已删除")
        self.assertEqual(NativeCase.objects.count(), count)
        self.assertEqual(NativeCase.history.count(), history)

    def test_choose_product_redirects_only_to_internal_editor(self):
        self.grant_add()
        response = self.client.get(
            reverse("ai_assistant:scenario_new_choose"),
            {"product": self.product.pk, "next": "https://evil.invalid"},
            secure=True,
        )
        self.assertRedirects(response, self.new_url(), fetch_redirect_response=False)
        for value in ("", "bad", "9" * 60):
            self.assertEqual(
                self.client.get(
                    reverse("ai_assistant:scenario_new_choose"), {"product": value}, secure=True
                ).status_code,
                404,
            )

    def test_read_only_or_no_permission_has_no_entry_and_cannot_create(self):
        self.assertNotContains(self.library(product=self.product.pk), 'id="scenario-create-button"')
        self.assertNotContains(self.library(product=self.product.pk), "data-tree-new-case-url=")
        self.assertEqual(self.client.get(self.new_url(), secure=True).status_code, 403)
        self.grant_add()
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertNotContains(self.library(product=self.product.pk), 'id="scenario-create-button"')
        self.assertNotContains(self.library(product=self.product.pk), "data-tree-new-case-url=")
        self.assertEqual(
            self.client.post(
                self.new_url(), self.payload(folder=self.folder.pk), secure=True
            ).status_code,
            403,
        )

    def test_edit_does_not_change_directory_through_new_creation_field(self):
        from .edit_test_client import EditClient

        client = EditClient()
        client.force_login(self.user)
        ProjectResourceAssignment.objects.create(
            resource_type="case", object_id=self.manual.pk, folder=self.folder
        )
        response = client.post(
            reverse("ai_assistant:scenario_edit", args=[self.manual.pk]),
            self.payload(folder=""),
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ProjectResourceAssignment.objects.get(
                resource_type="case", object_id=self.manual.pk
            ).folder_id,
            self.folder.pk,
        )

    def test_hidden_shared_folder_is_not_accepted_by_add_only_user(self):
        self.grant_add()
        hidden = ProjectResourceFolder.objects.create(
            product=ProductFactory(), resource_type="case_group", name="Hidden"
        )
        actor = UserFactory()
        assign_perm("testcases.add_testcase", actor)
        self.client.force_login(actor)
        url = reverse("ai_assistant:scenario_new", args=[hidden.product_id])
        response = self.client.get(url, secure=True)
        self.assertNotContains(response, "Hidden")
        self.assertEqual(self.client.get(url, {"folder": hidden.pk}, secure=True).status_code, 404)
