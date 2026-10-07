from django.contrib.auth.models import Group
from django.test import Client, TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import ProductFactory, TestCaseFactory, UserFactory
from tcms.web_testing.models import WebCase
from . import roles
from .models import APICase, ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment


class DirectoryToolsTests(TestCase):
    def setUp(self):
        self.user, self.other = UserFactory(), UserFactory()
        self.product = ProductFactory()
        roles.add_product_member(self.user, self.product)
        self.manual = TestCaseFactory(
            author=self.user, category__product=self.product, is_automated=False
        )
        for permission in ("view_testcase", "change_testcase"):
            assign_perm(permission, self.user, self.manual)
        self.web = WebCase.objects.create(
            owner=self.user,
            product=self.product,
            name="Web 登录",
            description="说明",
            steps_encrypted="UNCHANGED-CIPHER",
        )
        self.api = APICase.objects.create(
            owner=self.user,
            product=self.product,
            name="接口登录",
            path="/login",
            body={"password": "UNCHANGED-SECRET"},
            test_case=self.manual,
        )
        self.folder = Folder.objects.create(
            product=self.product, resource_type="case_group", name="登录目录"
        )
        Assignment.objects.create(
            resource_type="web_case", object_id=self.web.pk, folder=self.folder, assigned_by=self.user
        )
        self.hub = reverse("ai_assistant:case_hub")
        self.client.force_login(self.user)

    def rename(self, kind, case, **changes):
        return self.client.post(
            reverse("ai_assistant:case_tree_rename", args=[kind, case.pk]),
            dict(
                name="新的名称",
                expected_name=case.summary if kind == "manual" else case.name,
                next=self.hub + f"?product={self.product.pk}&type={kind}",
            )
            | changes,
            secure=True,
        )

    def test_manual_rename_preserves_body_and_records_native_history_actor(self):
        self.manual.save()  # Factory creation suppresses native history in the test settings.
        count = self.manual.history.count()
        text, original = self.manual.text, self.manual.summary
        response = self.rename("manual", self.manual)
        self.assertEqual(response.status_code, 302)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.summary, "新的名称")
        self.assertEqual(self.manual.text, text)
        self.assertEqual(self.manual.history.count(), count + 1)
        self.assertEqual(self.manual.history.latest().history_user_id, self.user.pk)
        self.assertEqual(self.manual.history.order_by("-history_id")[1].summary, original)

    def test_web_rename_preserves_steps_and_folder(self):
        self.assertEqual(self.rename("web", self.web).status_code, 302)
        self.web.refresh_from_db()
        self.assertEqual(self.web.name, "新的名称")
        self.assertEqual(self.web.steps_encrypted, "UNCHANGED-CIPHER")
        self.assertEqual(self.web.description, "说明")
        self.assertEqual(
            Assignment.objects.get(object_id=self.web.pk, resource_type="web_case").folder_id,
            self.folder.pk,
        )

    def test_api_rename_does_not_rename_linked_manual_or_modify_request(self):
        summary = self.manual.summary
        self.assertEqual(self.rename("api", self.api).status_code, 302)
        self.api.refresh_from_db()
        self.manual.refresh_from_db()
        self.assertEqual(self.api.name, "新的名称")
        self.assertEqual(self.api.body, {"password": "UNCHANGED-SECRET"})
        self.assertEqual(self.api.path, "/login")
        self.assertEqual(self.manual.summary, summary)

    def test_cross_account_automation_rename_is_404_even_for_superuser(self):
        for user in (self.other, UserFactory(is_superuser=True)):
            self.client.force_login(user)
            for kind, case in (("web", self.web), ("api", self.api)):
                self.assertEqual(self.rename(kind, case).status_code, 404)
        self.web.refresh_from_db()
        self.assertEqual(self.web.name, "Web 登录")

    def test_visible_manual_without_edit_permission_cannot_rename(self):
        assign_perm("view_testcase", self.other, self.manual)
        self.client.force_login(self.other)
        self.assertEqual(self.rename("manual", self.manual).status_code, 403)

    def test_readonly_cannot_rename_or_get_tree_write_targets(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        for kind, case in (("manual", self.manual), ("web", self.web), ("api", self.api)):
            self.assertEqual(self.rename(kind, case).status_code, 403)
        page = self.client.get(self.hub, {"product": self.product.pk}, secure=True)
        self.assertNotContains(page, "data-directory-node=")
        self.assertNotContains(page, "data-case-node=")
        self.assertNotContains(page, "data-directory-create ")

    def test_empty_and_overlong_names_are_rejected_for_each_type(self):
        for kind, case, maximum in (
            ("manual", self.manual, 255),
            ("web", self.web, 200),
            ("api", self.api, 200),
        ):
            for name in ("  ", "x" * (maximum + 1)):
                self.assertEqual(self.rename(kind, case, name=name).status_code, 409)
            self.assertEqual(self.rename(kind, case, name="x" * maximum).status_code, 302)

    def test_stale_form_cannot_overwrite_new_name(self):
        self.rename("web", self.web)
        self.assertEqual(self.rename("web", self.web, name="过期更新").status_code, 409)
        self.web.refresh_from_db()
        self.assertEqual(self.web.name, "新的名称")

    def test_unchanged_name_does_not_add_native_history(self):
        count = self.manual.history.count()
        self.assertEqual(
            self.rename("manual", self.manual, name=self.manual.summary).status_code, 302
        )
        self.assertEqual(self.manual.history.count(), count)

    def test_post_only_login_csrf_and_safe_redirect(self):
        url = reverse("ai_assistant:case_tree_rename", args=["web", self.web.pk])
        self.assertEqual(self.client.get(url, secure=True).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(url, {"name": "CSRF"}, secure=True).status_code, 403)
        self.assertEqual(
            self.rename("web", self.web, next="https://evil.invalid/").url,
            reverse("core-views-index"),
        )
        self.client.logout()
        self.assertEqual(self.client.post(url, secure=True).status_code, 302)

    def test_invalid_type_and_huge_id_return_404(self):
        for kind, pk in (("admin", self.web.pk), ("web", 10**30)):
            self.assertEqual(
                self.client.post(
                    reverse("ai_assistant:case_tree_rename", args=[kind, pk]), secure=True
                ).status_code,
                404,
            )

    def test_hub_exposes_right_click_targets_without_per_node_action_buttons(self):
        page = self.client.get(self.hub, {"product": self.product.pk}, secure=True)
        self.assertContains(page, f'data-directory-node="{self.folder.pk}"')
        self.assertContains(page, f'data-case-node="web:{self.web.pk}"')
        self.assertContains(page, 'id="directory-context-menu"')
        self.assertContains(page, "directory_tree.js")
        self.assertNotContains(page, 'class="directory-actions"')

    def test_business_tree_has_rename_targets_but_config_tree_only_filters(self):
        page = self.client.get(reverse("ai_assistant:case_library"), {"product":self.product.pk},secure=True)
        self.assertContains(page,f'data-case-node="manual:{self.manual.pk}"')
        self.assertContains(page,f'data-directory-node="{self.folder.pk}"')
        for route, params in (("web_testing:cases",{}),("ai_assistant:api_home",{"tab":"cases"})):
            page = self.client.get(reverse(route), {"product":self.product.pk} | params,secure=True)
            self.assertNotContains(page,'data-case-node=')
            self.assertNotContains(page,'data-directory-node=')
            self.assertContains(page,'在用例库管理目录')


    def test_directory_panels_omit_permanent_operation_tutorials(self):
        for route, params in (
            ("ai_assistant:scenario_library", {}),
            ("ai_assistant:case_hub", {}),
            ("ai_assistant:case_library", {}),
            ("web_testing:cases", {}),
            ("ai_assistant:api_home", {"tab": "cases"}),
        ):
            page = self.client.get(reverse(route), {"product": self.product.pk} | params, secure=True)
            self.assertNotContains(page, 'class="directory-help"')
            self.assertNotContains(page, "拖动用例到文件夹；拖动文件夹可调整层级。")
            self.assertContains(page, 'id="directory-context-menu"')
            self.assertContains(page, "directory_tree.js")

    def test_names_are_escaped_in_context_menu_metadata(self):
        self.web.name = "<img src=x onerror=alert(1)>"
        self.web.save(update_fields=("name",))
        page = self.client.get(self.hub, {"product": self.product.pk}, secure=True)
        self.assertNotContains(page, "<img src=x onerror=alert(1)>")
        self.assertContains(page, "&lt;img src=x onerror=alert(1)&gt;")
