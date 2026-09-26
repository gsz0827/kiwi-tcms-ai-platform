"""上游搜索页的「新建」入口：测试计划 / 执行任务 / 缺陷。

侧边栏取代上游横向导航（MENU_ITEMS）之后，这三个「新建」入口一度整体丢失，
只能手敲 /plan/new/ 之类 URL。这里守住两件事：入口确实在列表页上，以及只发给
有新增权限的账号（只读账号不该看到新建按钮）。
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

VIEW_PERMISSIONS = ("testplans.view_testplan", "testruns.view_testrun", "bugs.view_bug")
ADD_PERMISSIONS = ("testplans.add_testplan", "testruns.add_testrun", "bugs.add_bug")


def grant(user, permissions):
    for permission in permissions:
        app_label, codename = permission.split(".", 1)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label=app_label, codename=codename
            )
        )


class CreateEntryTests(TestCase):
    PAGES = (
        ("plans-search", "plans-new", "新建测试计划"),
        ("testruns-search", "testruns-new", "新建执行任务"),
        ("bugs-search", "bugs-new", "新建缺陷"),
    )

    def setUp(self):
        self.viewer = get_user_model().objects.create_user(
            username="entry-viewer", password="pw-12345"
        )
        grant(self.viewer, VIEW_PERMISSIONS)
        self.editor = get_user_model().objects.create_user(
            username="entry-editor", password="pw-12345"
        )
        grant(self.editor, VIEW_PERMISSIONS + ADD_PERMISSIONS)

    def test_search_pages_offer_the_new_button(self):
        self.client.force_login(self.editor)

        for page, new_url, label in self.PAGES:
            with self.subTest(page=page):
                response = self.client.get(reverse(page), secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, label)
                self.assertContains(response, reverse(new_url))

    def test_read_only_accounts_do_not_get_the_new_button(self):
        self.client.force_login(self.viewer)

        for page, _new_url, label in self.PAGES:
            with self.subTest(page=page):
                response = self.client.get(reverse(page), secure=True)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, label)

    def test_new_pages_open_the_forms(self):
        self.client.force_login(self.editor)

        for _page, new_url, _label in self.PAGES:
            with self.subTest(new_url=new_url):
                response = self.client.get(reverse(new_url), secure=True)
                self.assertEqual(response.status_code, 200)
