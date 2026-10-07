from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import remove_perm
from tcms.tests.factories import TestCaseFactory, ProductFactory
from tcms.web_testing.models import WebCase
from . import roles, test_directory_tools
from .models import APICase, ProjectResourceFolder as Folder, ProjectResourceAssignment as Assignment


class InheritedAutomationDirectoryTests(TestCase):
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def link(self):
        self.web.test_case = self.manual
        self.web.save()
        Assignment.objects.update_or_create(
            resource_type="case", object_id=self.manual.pk, defaults={"folder": self.folder}
        )

    def page(self, kind, **params):
        return self.client.get(
            reverse("web_testing:cases" if kind == "web" else "ai_assistant:api_home"),
            dict(product=self.product.pk, **({"tab": "cases"} if kind == "api" else {})) | params,
            secure=True,
        )

    def configs(self, kind, **params):
        return list(self.page(kind, **params).context["cases"])

    def test_both_lists_follow_business_folder_not_configuration_folder(self):
        self.link()
        legacy = Folder.objects.create(
            product=self.product, resource_type="web_case", name="OLD-CONFIG-FOLDER"
        )
        Assignment.objects.update_or_create(
            resource_type="web_case", object_id=self.web.pk, defaults={"folder": legacy}
        )
        for kind, config in (("web", self.web), ("api", self.api)):
            self.assertEqual(
                [item.pk for item in self.configs(kind, folder=self.folder.pk)], [config.pk]
            )
            self.assertEqual(self.configs(kind, folder=legacy.pk), [])
            page = self.page(kind)
            self.assertNotContains(page, "OLD-CONFIG-FOLDER")
            self.assertContains(page, f"{self.product.name} / {self.folder.name}")
            self.assertNotContains(page, "data-tree-drag=")
            self.assertNotContains(page, "data-tree-rename-url=")
            self.assertNotContains(page, "data-tree-create-url=")
            self.assertNotContains(page, "data-tree-drop=")
        self.assertTrue(
            Assignment.objects.filter(
                resource_type="web_case", object_id=self.web.pk, folder=legacy
            ).exists()
        )

    def test_multiple_configurations_for_one_case_all_filter_together(self):
        self.link()
        extra = WebCase.objects.create(
            owner=self.user, product=self.product, test_case=self.manual, name="第二实现"
        )
        self.assertEqual(
            {item.pk for item in self.configs("web", business_case=self.manual.pk)},
            {self.web.pk, extra.pk},
        )
        page = self.page("web")
        self.assertEqual(
            page.content.decode().count(f'data-product-tree-node="c:{self.manual.pk}"'), 1
        )

    def test_moving_business_case_updates_both_lists_without_configuration_write(self):
        self.link()
        web_time, api_time, cipher, body = (
            self.web.updated,
            self.api.updated,
            self.web.steps_encrypted,
            self.api.body.copy(),
        )
        new = Folder.objects.create(product=self.product, resource_type="case_group", name="新位置")
        Assignment.objects.filter(resource_type="case", object_id=self.manual.pk).update(folder=new)
        for kind in ("web", "api"):
            self.assertEqual(self.configs(kind, folder=self.folder.pk), [])
            self.assertEqual(len(self.configs(kind, folder=new.pk)), 1)
            self.assertEqual(self.configs(kind)[0].business_path, f"{self.product.name} / 新位置")
        self.web.refresh_from_db()
        self.api.refresh_from_db()
        self.assertEqual((self.web.updated, self.api.updated), (web_time, api_time))
        self.assertEqual((self.web.steps_encrypted, self.api.body), (cipher, body))

    def test_folder_rename_move_and_delete_are_reflected_without_touching_config(self):
        self.link()
        parent = Folder.objects.create(
            product=self.product, resource_type="case_group", name="父目录"
        )
        self.folder.name = "新目录名"
        self.folder.parent = parent
        self.folder.save()
        for kind in ("web", "api"):
            self.assertEqual(
                self.configs(kind)[0].business_path, f"{self.product.name} / 父目录 / 新目录名"
            )
            self.assertEqual(len(self.configs(kind, folder=parent.pk)), 1)
        self.folder.delete()
        for kind in ("web", "api"):
            self.assertEqual(self.configs(kind)[0].business_path, self.product.name)
            self.assertEqual(len(self.configs(kind, folder="unfiled")), 1)
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk, test_case=self.manual).exists())
        self.assertTrue(APICase.objects.filter(pk=self.api.pk, test_case=self.manual).exists())

    def test_unlinked_is_null_link_only_not_root_business_cases(self):
        self.link()
        web = WebCase.objects.create(owner=self.user, product=self.product, name="未关联Web")
        api = APICase.objects.create(
            owner=self.user, product=self.product, name="未关联API", path="/"
        )
        Assignment.objects.filter(resource_type="case", object_id=self.manual.pk).delete()
        for kind, config in (("web", web), ("api", api)):
            self.assertEqual(
                [item.pk for item in self.configs(kind, association="unlinked")], [config.pk]
            )
            self.assertEqual(len(self.configs(kind, folder="unfiled")), 1)
            self.assertEqual(len(self.configs(kind)), 2)

    def test_search_configuration_name_and_business_id_or_name_independently(self):
        self.link()
        for kind in ("web", "api"):
            for term in (self.manual.summary, str(self.manual.pk), f"TC-{self.manual.pk:05d}"):
                self.assertEqual(len(self.configs(kind, case_q=term)), 1)
            self.assertEqual(len(self.configs(kind, q="登录")), 1)
            self.assertEqual(self.configs(kind, q="没有这个配置"), [])
            self.assertEqual(self.configs(kind, case_q="没有这个业务用例"), [])

    def test_private_owner_names_payloads_and_native_invisible_cases_are_not_in_tree(self):
        self.link()
        hidden = TestCaseFactory(
            author=self.other, category__product=self.product, summary="HIDDEN-BUSINESS"
        )
        foreign = WebCase.objects.create(
            owner=self.other,
            product=self.product,
            test_case=self.manual,
            name="FOREIGN-CONFIG",
            steps_encrypted="SECRET-CIPHER",
        )
        owned_hidden = WebCase.objects.create(
            owner=self.user, product=self.product, test_case=hidden, name="权限撤回后的配置"
        )
        page = self.page("web")
        self.assertNotContains(page, "HIDDEN-BUSINESS")
        self.assertNotContains(page, foreign.name)
        self.assertNotContains(page, "SECRET-CIPHER")
        item = next(item for item in page.context["cases"] if item.pk == owned_hidden.pk)
        self.assertFalse(item.business_case_visible)
        self.assertEqual(item.business_path, "关联不可访问")
        self.assertEqual(self.configs("web", business_case=hidden.pk), [])

    def test_readonly_has_copy_paths_without_directory_write_targets(self):
        self.link()
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        for kind in ("web", "api"):
            page = self.page(kind)
            self.assertContains(page, "data-tree-path=")
            self.assertContains(page, 'data-tree-command="copy-path"')
            self.assertNotContains(page, 'draggable="true"')
            self.assertNotContains(page, "data-directory-node=")

    def test_invalid_huge_ids_and_cross_product_filters_are_empty(self):
        self.link()
        foreign = Folder.objects.create(
            product=ProductFactory(), resource_type="case_group", name="外部目录"
        )
        for kind in ("web", "api"):
            for params in (
                {"folder": str(10**30)},
                {"folder": "invalid"},
                {"folder": foreign.pk},
                {"business_case": str(10**30)},
                {"business_case": "invalid"},
            ):
                self.assertEqual(self.configs(kind, **params), [])

    def test_revoke_business_permission_hides_tree_and_path_but_preserves_owned_config(self):
        self.link()
        remove_perm("view_testcase", self.user, self.manual)
        for kind in ("web", "api"):
            page = self.page(kind)
            self.assertNotContains(page, f'data-product-tree-node="c:{self.manual.pk}"')
            self.assertEqual(page.context["cases"].paginator.count, 1)
            self.assertEqual(page.context["cases"][0].business_path, "关联不可访问")
            self.assertEqual(self.configs(kind, folder=self.folder.pk), [])
