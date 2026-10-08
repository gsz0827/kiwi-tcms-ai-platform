from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse
from guardian.shortcuts import assign_perm

from tcms.tests.factories import ProductFactory, TestCaseFactory, UserFactory
from tcms.web_testing.models import WebCase

from .models import APICase


class CaseHubTests(TestCase):
    def setUp(self):
        self.owner = UserFactory()
        self.other = UserFactory()
        self.product = ProductFactory()
        self.other_product = ProductFactory()
        self.manual = TestCaseFactory(category__product=self.product, summary="Hub 手工登录", is_automated=False)
        assign_perm("view_testcase", self.owner, self.manual)
        self.web = WebCase.objects.create(owner=self.owner, product=self.product, name="Hub Web 登录", steps_encrypted="unused")
        self.api = APICase.objects.create(owner=self.owner, product=self.product, name="Hub API 登录", path="/login")
        WebCase.objects.create(owner=self.other, product=self.product, name="其他账号的 Web 秘密", steps_encrypted="unused")
        APICase.objects.create(owner=self.other, product=self.product, name="其他账号的 API 秘密", path="/private")
        WebCase.objects.create(owner=self.owner, product=self.other_product, name="别的项目 Web", steps_encrypted="unused")
        self.client.force_login(self.owner)

    def test_hub_shows_three_types_without_exposing_other_accounts(self):
        response = self.client.get(reverse("ai_assistant:case_hub"), {"product": self.product.pk}, secure=True)
        self.assertEqual(response.status_code, 200)
        for name in (self.manual.summary, self.web.name, self.api.name):
            self.assertContains(response, name)
        self.assertNotContains(response, "其他账号的 Web 秘密")
        self.assertNotContains(response, "其他账号的 API 秘密")
        self.assertNotContains(response, "别的项目 Web")
        self.assertNotContains(response, '<span class="badge"></span>')

    def test_type_and_search_filters_use_real_case_models(self):
        response = self.client.get(reverse("ai_assistant:case_hub"), {"product": self.product.pk, "type": "web", "q": "登录"}, secure=True)
        self.assertContains(response, self.web.name)
        self.assertNotContains(response, self.manual.summary)
        self.assertNotContains(response, self.api.name)
        self.assertEqual(response.context["sections"][0]["count"], 1)

    def test_all_projects_and_session_product_scope(self):
        url = reverse("ai_assistant:case_hub")
        self.assertContains(self.client.get(url, secure=True), "别的项目 Web")
        session = self.client.session
        session["ai_product_id"] = self.product.pk
        session.save()
        self.assertNotContains(self.client.get(url, secure=True), "别的项目 Web")
        self.assertContains(self.client.get(url, {"product": ""}, secure=True), "别的项目 Web")

    def test_workbench_and_navigation_point_to_business_library(self):
        url = reverse("ai_assistant:scenario_library")
        response = self.client.get(reverse("core-views-index"), secure=True)
        self.assertContains(response, f'<a href="{url}"><span class="fa fa-list-alt"')
        response = self.client.get(url, secure=True)
        self.assertContains(response, "测试管理")
        self.assertNotContains(response, 'id="kiwi-nav-manual"')
        html = response.content.decode()
        group = html[html.index('id="kiwi-nav-testing"'):html.index('id="kiwi-nav-web"')]
        self.assertIn(f'href="{url}" aria-current="page"', group)

    def test_project_switch_opens_workbench_and_clears_stale_filters(self):
        next_url = reverse("ai_assistant:api_home") + "?tab=cases&folder=99&page=3&q=登录"
        response = self.client.post(reverse("ai_assistant:set_project_context"), {
            "product": self.other_product.pk, "next": next_url,
        }, secure=True)
        target = urlsplit(response.url)
        self.assertEqual(target.path, reverse("core-views-index"))
        params = parse_qs(target.query)
        self.assertEqual(params["product"], [str(self.other_product.pk)])
        self.assertNotIn("tab", params)
        self.assertNotIn("q", params)
        self.assertNotIn("folder", params)
        self.assertNotIn("page", params)

    def test_project_switch_rejects_external_redirect(self):
        response = self.client.post(reverse("ai_assistant:set_project_context"), {
            "product": self.product.pk, "next": "https://evil.example/path",
        }, secure=True)
        self.assertEqual(urlsplit(response.url).path, reverse("core-views-index"))
        self.assertEqual(urlsplit(response.url).netloc, "")
