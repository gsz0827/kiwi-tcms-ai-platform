"""Common case folders share taxonomy, not private automation payloads."""
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import ProductFactory, UserFactory, TestCaseFactory
from tcms.web_testing.models import WebCase
from . import roles
from .models import APICase, ProjectResourceAssignment as Assignment, ProjectResourceFolder as Folder


class HubDirectoryTests(TestCase):
    def setUp(self):
        self.owner, self.member, self.other = UserFactory(), UserFactory(), UserFactory()
        self.product, self.other_product = ProductFactory(), ProductFactory()
        roles.add_product_member(self.owner, self.product)
        roles.add_product_member(self.member, self.product)
        self.manual = TestCaseFactory(category__product=self.product, summary="目录手工登录", is_automated=False, author=self.owner)
        for perm in ("view_testcase", "change_testcase"):
            assign_perm(perm, self.owner, self.manual)
        self.web = WebCase.objects.create(owner=self.owner, product=self.product, name="目录 Web 登录", description="页面描述", steps_encrypted="unused")
        self.api = APICase.objects.create(owner=self.owner, product=self.product, name="目录 API 登录", path="/login")
        self.secret = WebCase.objects.create(owner=self.other, product=self.product, name="OTHER PRIVATE WEB", steps_encrypted="unused")
        self.folder = Folder.objects.create(product=self.product, resource_type="case_group", name="用户中心", created_by=self.owner)
        self.child = Folder.objects.create(product=self.product, resource_type="case_group", name="登录", parent=self.folder)
        self.alien = Folder.objects.create(product=self.other_product, resource_type="case_group", name="其他项目目录")
        self.url = reverse("ai_assistant:case_hub")
        self.client.force_login(self.owner)

    def page(self, **params):
        return self.client.get(self.url, {"product":self.product.pk} | params, secure=True)

    def assign(self, kind, obj, folder):
        return self.client.post(reverse("ai_assistant:assign_resource_folder"), dict(resource_type=kind,
            object_id=obj.pk, folder=folder.pk if folder else "", next=self.url + f"?product={self.product.pk}"), secure=True)

    def file_all(self):
        for kind, obj in [("case",self.manual),("web_case",self.web),("api_case",self.api)]:
            self.assertEqual(self.assign(kind, obj, self.child).status_code, 302)

    def test_pane_and_modal_details_are_outside_main_navigation(self):
        response = self.page()
        self.assertContains(response, 'class="kiwi-resource-pane"')
        self.assertContains(response, 'data-resource-browser="case_hub"')
        self.assertContains(response, "管理共享目录")
        self.assertContains(response, self.folder.name)
        self.assertNotContains(response, self.alien.name)
        self.assertNotContains(response, self.secret.name)
        self.assertContains(response, f'id="hub-case-web-{self.web.pk}"')
        self.assertContains(response, f'id="hub-case-api-{self.api.pk}"')
        self.assertContains(response, self.web.description)

    def test_common_folder_filters_all_three_kinds_and_parent_includes_children(self):
        self.file_all()
        response = self.page(folder=self.folder.pk)
        self.assertEqual([section["count"] for section in response.context["sections"]], [1,1,1])
        nodes = response.context["directories"]["folders"]
        self.assertEqual(next(node.case_count for node in nodes if node.pk == self.folder.pk), 3)
        self.assertEqual(next(node.case_count for node in nodes if node.pk == self.child.pk), 3)
        self.assertEqual(self.page(folder="unfiled").context["sections"][0]["count"], 0)
        self.assign("web_case", self.web, None)
        self.assertEqual(self.page(type="web", folder="unfiled").context["sections"][0]["count"], 1)

    def test_module_pages_reuse_business_case_directory_membership(self):
        self.file_all()
        self.web.test_case = self.manual
        self.web.save()
        self.api.test_case = self.manual
        self.api.save()
        routes = [("ai_assistant:case_library",{}), ("web_testing:cases",{}), ("ai_assistant:api_home",{"tab":"cases"})]
        for route, params in routes:
            response = self.client.get(reverse(route), {"product":self.product.pk,"folder":self.folder.pk} | params, secure=True)
            self.assertContains(response, self.folder.name)
            self.assertEqual(response.context["cases"].paginator.count, 1)
            empty = self.client.get(reverse(route), {"product":self.product.pk,"folder":"unfiled"} | params, secure=True)
            self.assertEqual(empty.context["cases"].paginator.count, 0)

    def test_legacy_folders_are_preserved_and_visible_in_hub(self):
        legacy = Folder.objects.create(product=self.product, resource_type="web_case", name="原 Web 目录")
        self.assign("web_case", self.web, legacy)
        self.assertContains(self.page(), legacy.name)
        response = self.page(folder=legacy.pk)
        self.assertEqual([s["count"] for s in response.context["sections"]], [0,1,0])
        self.assertEqual(Assignment.objects.get(resource_type="web_case").folder_id, legacy.pk)

    def test_product_search_and_type_scope_keep_counts_private(self):
        self.file_all()
        self.assertEqual(self.page(type="web", q="不存在", folder=self.folder.pk).context["sections"][0]["count"], 0)
        self.assertEqual(self.page(type="api", folder=self.child.pk).context["sections"][0]["count"], 1)
        self.assertEqual(self.page(folder=self.alien.pk).context["sections"][0]["count"], 0)
        self.assertEqual(self.page(folder="bad").context["sections"][0]["count"], 0)

    def test_members_share_folder_names_without_sharing_private_cases(self):
        self.file_all()
        self.client.force_login(self.member)
        response = self.page(folder=self.folder.pk)
        self.assertContains(response, self.folder.name)
        self.assertNotContains(response, self.web.name)
        self.assertNotContains(response, self.api.name)
        self.assertEqual([s["count"] for s in response.context["sections"]], [0,0,0])
        self.assertEqual(self.assign("web_case", self.web, self.child).status_code, 404)

    def create(self, **changes):
        return self.client.post(reverse("ai_assistant:create_resource_folder"), dict(resource_type="case_group",
            product=self.product.pk, parent=self.folder.pk, name="边界场景", next=self.url) | changes, secure=True)

    def test_create_and_rename_business_folders(self):
        self.assertEqual(self.create().status_code, 302)
        new = Folder.objects.get(name="边界场景")
        self.assertEqual(new.parent_id, self.folder.pk)
        self.create()
        self.assertEqual(Folder.objects.filter(name="边界场景").count(), 1)
        self.assertEqual(self.client.post(reverse("ai_assistant:rename_resource_folder", args=[new.pk]),
            {"name":"输入校验","next":self.url}, secure=True).status_code, 302)
        new.refresh_from_db()
        self.assertEqual(new.name, "输入校验")

    def test_outsider_cannot_manage_common_folders(self):
        self.client.force_login(self.other)
        # The other account owns an automation case in this project, so use a true outsider.
        outsider = UserFactory()
        self.client.force_login(outsider)
        self.assertNotContains(self.page(), self.folder.name)
        self.assertEqual(self.create().status_code, 403)
        for route in ("rename_resource_folder","delete_resource_folder","move_resource_folder"):
            self.assertEqual(self.client.post(reverse("ai_assistant:"+route,args=[self.folder.pk]), {"name":"越权"},secure=True).status_code,403)

    def test_readonly_with_global_permissions_cannot_modify_or_assign(self):
        self.owner.user_permissions.add(Permission.objects.get(content_type__app_label="testcases", codename="change_testcase"))
        self.owner.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertNotContains(self.page(), "管理共享目录")
        self.assertEqual(self.create().status_code, 403)
        self.assertEqual(self.assign("case", self.manual, self.child).status_code,403)
        for route in ("rename_resource_folder","delete_resource_folder","move_resource_folder"):
            self.assertEqual(self.client.post(reverse("ai_assistant:"+route,args=[self.folder.pk]), {"name":"只读越权"},secure=True).status_code,403)

    def test_foreign_product_and_legacy_wrong_type_assignments_rejected(self):
        self.assertEqual(self.assign("web_case", self.web, self.alien).status_code,403)
        legacy = Folder.objects.create(product=self.product, resource_type="case", name="手工旧目录")
        self.assertEqual(self.assign("web_case", self.web, legacy).status_code,403)
        self.assertFalse(Assignment.objects.exists())

    def move(self, folder, parent):
        return self.client.post(reverse("ai_assistant:move_resource_folder", args=[folder.pk]),
            {"parent":parent.pk if parent else "", "next":self.url}, secure=True)

    def test_move_keeps_assignments_and_rejects_cycles_cross_product_and_wrong_type(self):
        self.file_all()
        self.move(self.child, None)
        self.child.refresh_from_db()
        self.assertIsNone(self.child.parent_id)
        self.assertEqual(Assignment.objects.count(), 3)
        self.move(self.child, self.folder)
        self.move(self.folder, self.child)
        self.folder.refresh_from_db()
        self.assertIsNone(self.folder.parent_id)
        self.assertEqual(self.move(self.child, self.alien).status_code,403)
        legacy = Folder.objects.create(product=self.product, resource_type="web_case", name="旧分类")
        self.assertEqual(self.move(self.child, legacy).status_code,403)
        self.assertEqual(self.move(legacy, self.folder).status_code,302)

    def test_duplicate_target_sibling_names_are_rejected(self):
        duplicate = Folder.objects.create(product=self.product, resource_type="case_group", name="登录")
        self.move(duplicate, self.folder)
        duplicate.refresh_from_db()
        self.assertIsNone(duplicate.parent_id)

    def test_delete_folder_unfiles_cases_without_deleting_cases(self):
        self.file_all()
        self.client.post(reverse("ai_assistant:delete_resource_folder",args=[self.folder.pk]), {"next":self.url},secure=True)
        self.assertFalse(Assignment.objects.exists())
        self.assertTrue(type(self.manual).objects.filter(pk=self.manual.pk).exists())
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(APICase.objects.filter(pk=self.api.pk).exists())

    def test_writes_are_post_only_and_redirects_are_same_site(self):
        self.assertEqual(self.client.get(reverse("ai_assistant:move_resource_folder", args=[self.folder.pk]),secure=True).status_code,405)
        response = self.create(next="/\\evil.example/")
        self.assertEqual(response.url, reverse("core-views-index"))
