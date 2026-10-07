from django.test import TestCase
from django.urls import reverse

from tcms.web_testing.models import WebCase
from . import test_directory_tools
from .models import APICase


class AutomationConfigUITests(TestCase):
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def get(self, name, **params):
        return self.client.get(reverse(name), {"product": self.product.pk} | params, secure=True)

    def test_web_entry_is_configuration_not_duplicate_case_library(self):
        self.web.test_case = self.manual
        self.web.save()
        before = self.web.steps_encrypted
        page = self.get("web_testing:cases")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["title"], "自动化脚本")
        self.assertContains(page, "<th>脚本名称</th>", html=True)
        self.assertContains(page, "<th>关联业务用例</th>", html=True)
        self.assertContains(page, reverse("ai_assistant:scenario_detail", args=[self.manual.pk]))
        self.assertContains(page, "用例目录")
        self.assertNotContains(page, "脚本目录")
        self.assertContains(page, "AI 生成 Web 脚本")
        self.assertNotContains(page, "用例管理")
        self.web.refresh_from_db()
        self.assertEqual(self.web.steps_encrypted, before)
        self.assertEqual(self.web.test_case_id, self.manual.pk)

    def test_api_entry_is_configuration_with_business_case_link(self):
        before = self.api.body.copy()
        page = self.get("ai_assistant:api_home", tab="cases")
        self.assertContains(page, "<h1>自动化脚本</h1>", html=True)
        self.assertContains(page, "自动化脚本 · 接口自动化测试")
        self.assertContains(page, "<th>关联业务用例</th>", html=True)
        self.assertContains(page, reverse("ai_assistant:scenario_detail", args=[self.manual.pk]))
        self.assertContains(page, "AI 生成接口脚本")
        self.assertNotContains(page, "用例管理")
        self.api.refresh_from_db()
        self.assertEqual(self.api.body, before)
        self.assertEqual(self.api.test_case_id, self.manual.pk)

    def test_legacy_unlinked_web_config_is_preserved_and_can_be_edited(self):
        page = self.get("web_testing:cases")
        self.assertContains(page, "未关联")
        self.assertContains(page, reverse("web_testing:case_edit", args=[self.web.pk]))
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk, test_case__isnull=True).exists())

    def test_editor_titles_and_bookmarked_routes_remain_accessible(self):
        from .crypto import encrypt_api_key

        self.web.steps_encrypted = encrypt_api_key('[{"action":"goto","value":"/"}]')
        self.web.save()
        for name, args, title in (
            ("web_testing:case_edit", [self.web.pk], "编辑 Web 脚本"),
            ("web_testing:case_new", [], "新建 Web 脚本"),
            ("ai_assistant:api_case_edit", [self.product.pk, self.api.pk], "编辑接口脚本"),
            ("ai_assistant:api_case_new", [self.product.pk], "新建接口脚本"),
        ):
            page = self.client.get(reverse(name, args=args), secure=True)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.context["title"], title)

    def test_owned_configuration_lists_still_hide_other_accounts(self):
        WebCase.objects.create(
            owner=self.other,
            product=self.product,
            name="PRIVATE-WEB-CONFIG",
            steps_encrypted="PRIVATE-CIPHER",
        )
        APICase.objects.create(
            owner=self.other,
            product=self.product,
            name="PRIVATE-API-CONFIG",
            path="/private",
            body={"secret": "PRIVATE-BODY"},
        )
        for name, params in (("web_testing:cases", {}), ("ai_assistant:api_home", {"tab": "cases"})):
            page = self.get(name, **params)
            self.assertNotContains(page, "PRIVATE-WEB-CONFIG")
            self.assertNotContains(page, "PRIVATE-API-CONFIG")
            self.assertNotContains(page, "PRIVATE-CIPHER")
            self.assertNotContains(page, "PRIVATE-BODY")

    def test_other_api_tabs_keep_their_names(self):
        for tab, title in (
            ("environments", "测试环境"),
            ("suites", "测试套件"),
            ("runs", "执行任务"),
        ):
            self.assertContains(
                self.get("ai_assistant:api_home", tab=tab), f"<h1>{title}</h1>", html=True
            )

    def test_compact_library_has_one_body_preview_and_no_hero(self):
        import re

        page = self.get("ai_assistant:scenario_library")
        content = page.content.decode()
        title = re.search(r"<title>(.*?)</title>", content, re.S).group(1)
        self.assertEqual(title.strip(), "Kiwi TCMS - 用例库")
        self.assertEqual(content.count('id="scenario-tree-preview"'), 1)
        self.assertNotIn("<div", title)
        self.assertContains(page, 'class="scenario-library"')
        self.assertNotContains(page, '<header class="api-header">')
        self.assertNotContains(page, "按业务模块管理测试目标")
        self.assertContains(page, f'id="scenario-{self.manual.pk}"')

    def test_library_actions_follow_filters_and_keep_project_context(self):
        page = self.get("ai_assistant:scenario_library")
        content = page.content.decode()
        self.assertLess(
            content.index('class="scenario-filter"'), content.index('class="scenario-list-toolbar"')
        )
        self.assertLess(
            content.index('class="scenario-list-toolbar"'),
            content.index('class="table scenario-table"'),
        )
        self.assertNotContains(page, "AI 生成测试用例")
        create_url = reverse("ai_assistant:scenario_new", args=[self.product.pk])
        self.assertEqual(create_url in content, bool(page.context["can_create"]))

    def test_all_products_library_does_not_offer_ambiguous_creation(self):
        page = self.client.get(reverse("ai_assistant:scenario_library"), secure=True)
        self.assertNotContains(page, "AI 生成测试用例")
        self.assertNotContains(page, reverse("ai_assistant:scenario_new", args=[self.product.pk]))
        self.assertContains(page, "<th>项目</th>", html=True)

    def test_business_library_still_owns_case_design(self):
        page = self.get("ai_assistant:scenario_library")
        self.assertContains(page, "<title>Kiwi TCMS - 用例库</title>", html=True)
        self.assertContains(page, "<h1>用例库</h1>", html=True)
        self.assertEqual(page.context["cases"].paginator.count, 1)
        self.assertNotContains(page, "AI 生成测试用例")
        self.assertContains(page, "用例目录")
