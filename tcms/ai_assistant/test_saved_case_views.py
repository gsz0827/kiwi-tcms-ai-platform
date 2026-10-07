from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.template.loader import render_to_string
from guardian.shortcuts import assign_perm

from tcms.tests.factories import ProductFactory, TestCaseFactory, UserFactory
from tcms.web_testing.models import WebCase
from . import roles
from .models import SavedCaseView, ProjectResourceFolder as Folder
from .saved_case_views import context, normalize, LIMIT


class SavedCaseViewTests(TestCase):
    def setUp(self):
        self.user, self.other = UserFactory(), UserFactory()
        self.product, self.alien = ProductFactory(), ProductFactory()
        roles.add_product_member(self.user, self.product)
        roles.add_product_member(self.other, self.product)
        self.folder = Folder.objects.create(
            product=self.product, resource_type="case_group", name="登录模块"
        )
        self.foreign = Folder.objects.create(
            product=self.alien, resource_type="case_group", name="其他项目目录"
        )
        self.web = WebCase.objects.create(
            owner=self.user,
            product=self.product,
            name="可见 Web 登录",
            steps_encrypted="PRIVATE-STEPS",
        )
        WebCase.objects.create(
            owner=self.other, product=self.product, name="OTHER-PRIVATE-WEB", steps_encrypted="SECRET"
        )
        self.manual = TestCaseFactory(category__product=self.product, is_automated=False)
        assign_perm("view_testcase", self.user, self.manual)
        self.filters = dict(
            product=str(self.product.pk),
            type="web",
            folder=str(self.folder.pk),
            q="登录",
            page_size="15",
        )
        self.hub = reverse("ai_assistant:case_hub")
        self.next = self.hub + f"?product={self.product.pk}&type=web&q=登录"
        self.new = reverse("ai_assistant:saved_case_view_new")
        self.client.force_login(self.user)

    def create(self, **changes):
        return self.client.post(
            self.new, self.filters | dict(name="我的登录视图", next=self.next) | changes, secure=True
        )

    def record(self, **changes):
        return SavedCaseView.objects.create(
            owner=self.user, name="我的登录视图", filters=self.filters | changes
        )

    def update(self, item, **changes):
        return self.client.post(
            reverse("ai_assistant:saved_case_view_edit", args=[item.pk]),
            self.filters
            | dict(name=item.name, revision=item.revision, action="replace", next=self.next)
            | changes,
            secure=True,
        )

    def delete(self, item, **changes):
        return self.client.post(
            reverse("ai_assistant:saved_case_view_delete", args=[item.pk]),
            dict(revision=item.revision, next=self.next) | changes,
            secure=True,
        )

    def test_save_whitelists_only_canonical_filter_values(self):
        response = self.create(page="99", token="DO-NOT-SAVE", body="SECRET", name="  登录接口  ")
        self.assertEqual(response.status_code, 302)
        item = SavedCaseView.objects.get(owner=self.user)
        self.assertEqual(item.name, "登录接口")
        self.assertEqual(item.filters, self.filters)
        self.assertEqual(item.revision, 1)
        self.assertNotIn("SECRET", str(item.filters))

    def test_loading_restores_unicode_filters_without_page_or_external_redirect(self):
        item = self.record(q="登录 & 特殊/字符", folder="unfiled")
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]),
            {"next": "https://evil.invalid/", "page": "99"},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        parsed = urlsplit(response.url)
        self.assertEqual(parsed.path, self.hub)
        self.assertFalse(parsed.netloc)
        self.assertEqual(parse_qs(parsed.query)["q"], ["登录 & 特殊/字符"])
        self.assertNotIn("page", parse_qs(parsed.query))
        self.assertNotIn("next", parse_qs(parsed.query))

    def test_all_projects_view_does_not_inherit_current_session_project(self):
        item = self.record(product="", folder="", type="all", q="")
        session = self.client.session
        session["ai_product_id"] = self.product.pk
        session.save()
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
        )
        self.assertEqual(
            parse_qs(urlsplit(response.url).query, keep_blank_values=True)["product"], [""]
        )

    def test_reloading_never_exposes_other_accounts_cases(self):
        item = self.record(folder="", q="")
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]), follow=True, secure=True
        )
        self.assertContains(response, self.web.name)
        self.assertNotContains(response, "OTHER-PRIVATE-WEB")

    def test_listing_is_owner_only_even_for_project_colleagues(self):
        self.record()
        private = SavedCaseView.objects.create(
            owner=self.other, name="OTHER-PRIVATE-VIEW", filters=self.filters
        )
        own = context(self.user, self.filters)["saved_case_views"]
        other = context(self.other, self.filters)["saved_case_views"]
        self.assertEqual(len(own), 1)
        self.assertNotEqual(own[0].pk, private.pk)
        self.assertEqual([item.pk for item in other], [private.pk])

    def test_cross_account_load_update_delete_return_404(self):
        item = self.record()
        self.client.force_login(self.other)
        self.assertEqual(
            self.client.get(
                reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
            ).status_code,
            404,
        )
        self.assertEqual(self.update(item).status_code, 404)
        self.assertEqual(self.delete(item).status_code, 404)
        item.refresh_from_db()
        self.assertEqual(item.revision, 1)

    def test_superuser_still_cannot_load_other_accounts_personal_views(self):
        item = self.record()
        self.client.force_login(UserFactory(is_staff=True, is_superuser=True))
        self.assertEqual(
            self.client.get(
                reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
            ).status_code,
            404,
        )

    def test_readonly_can_manage_personal_preferences_not_business_assets(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertEqual(self.create().status_code, 302)
        item = SavedCaseView.objects.get(owner=self.user)
        self.assertEqual(self.update(item, action="rename", name="只读账号视图").status_code, 302)
        item.refresh_from_db()
        self.assertEqual(self.delete(item).status_code, 302)
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(Folder.objects.filter(pk=self.folder.pk).exists())

    def test_duplicate_name_never_silently_overwrites_existing_conditions(self):
        item = self.record()
        self.create(type="api", q="changed")
        item.refresh_from_db()
        self.assertEqual(item.filters, self.filters)
        self.assertEqual(SavedCaseView.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(item.revision, 1)

    def test_names_are_scoped_per_owner(self):
        self.record()
        self.client.force_login(self.other)
        self.create()
        self.assertEqual(SavedCaseView.objects.count(), 2)

    def test_rename_preserves_filters_even_if_current_page_conditions_are_invalid(self):
        item = self.record()
        self.update(item, action="rename", name="重命名", folder="garbage", type="garbage")
        item.refresh_from_db()
        self.assertEqual(item.name, "重命名")
        self.assertEqual(item.filters, self.filters)
        self.assertEqual(item.revision, 2)

    def test_replace_changes_filters_and_revision(self):
        item = self.record()
        self.update(item, type="api", folder="unfiled", q="新条件", page_size="60")
        item.refresh_from_db()
        self.assertEqual(item.filters["type"], "api")
        self.assertEqual(item.filters["folder"], "unfiled")
        self.assertEqual(item.revision, 2)

    def test_stale_revision_cannot_update_or_delete(self):
        item = self.record()
        self.update(item, name="新版名称")
        self.update(item, name="旧页面覆盖")
        self.delete(item)
        item.refresh_from_db()
        self.assertEqual(item.name, "新版名称")
        self.assertEqual(item.revision, 2)
        self.assertEqual(self.update(item, revision="bad").status_code, 302)

    def test_quota_is_bounded_but_existing_view_can_be_renamed(self):
        SavedCaseView.objects.bulk_create(
            [
                SavedCaseView(owner=self.user, name=f"视图{i}", filters=self.filters)
                for i in range(LIMIT)
            ]
        )
        self.create()
        self.assertEqual(SavedCaseView.objects.filter(owner=self.user).count(), LIMIT)
        item = SavedCaseView.objects.filter(owner=self.user).first()
        self.update(item, action="rename", name="配额满了仍能改名")
        item.refresh_from_db()
        self.assertEqual(item.name, "配额满了仍能改名")

    def test_deleted_folder_is_explicitly_unavailable_not_all_cases(self):
        item = self.record()
        self.folder.delete()
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "本次没有应用筛选", status_code=409)
        self.assertNotContains(response, self.web.name, status_code=409)
        item.refresh_from_db()
        self.assertEqual(item.filters, self.filters)
        data = context(self.user, self.filters)
        self.assertTrue(data["saved_case_views"][0].invalid_reason)

    def test_revoked_project_membership_does_not_restore_invisible_folder(self):
        hidden = ProductFactory()
        roles.add_product_member(self.user, hidden)
        folder = Folder.objects.create(
            product=hidden, resource_type="case_group", name="权限撤销目录"
        )
        item = self.record(product=str(hidden.pk), folder=str(folder.pk))
        roles.remove_product_member(self.user, hidden)
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
        )
        self.assertEqual(response.status_code, 409)

    def test_deleted_project_reference_is_not_converted_to_all_projects(self):
        item = self.record(product="999999999", folder="")
        response = self.client.get(
            reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
        )
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, "项目已不存在", status_code=409)

    def test_cross_project_and_legacy_wrong_type_directories_rejected(self):
        self.create(folder=str(self.foreign.pk))
        legacy = Folder.objects.create(product=self.product, resource_type="case", name="仅手工目录")
        self.create(folder=str(legacy.pk), type="web")
        self.assertFalse(SavedCaseView.objects.exists())

    def test_invalid_filter_values_and_name_lengths_do_not_save(self):
        for changes in [
            dict(name=""),
            dict(name="x" * 81),
            dict(q="x" * 201),
            dict(type="admin"),
            dict(page_size="9999"),
            dict(folder="garbage"),
            dict(product="-1"),
            dict(product="9" * 30),
        ]:
            with self.subTest(changes=changes):
                self.create(**changes)
                self.assertFalse(SavedCaseView.objects.exists())

    def test_corrupted_json_and_unknown_stored_fields_are_not_applied(self):
        for filters in (["bad"], {"type": True}, self.filters | {"next": "https://evil.invalid/"}):
            item = SavedCaseView.objects.create(
                owner=self.user, name=str(SavedCaseView.objects.count()), filters=filters
            )
            response = self.client.get(
                reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
            )
            self.assertEqual(response.status_code, 409)
        data = context(self.user, self.filters)
        self.assertEqual(len(data["saved_case_views"]), 3)
        self.assertTrue(all(item.invalid_reason for item in data["saved_case_views"]))

    def test_retained_view_template_escapes_markup(self):
        self.create(name="<img src=x onerror=alert(1)>", q="<script>alert(1)</script>")
        html = render_to_string(
            "ai_assistant/_saved_case_views.html", context(self.user, self.filters)
        )
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_delete_view_never_deletes_folder_or_cases(self):
        item = self.record()
        self.delete(item)
        self.assertFalse(SavedCaseView.objects.filter(pk=item.pk).exists())
        self.assertTrue(Folder.objects.filter(pk=self.folder.pk).exists())
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(type(self.manual).objects.filter(pk=self.manual.pk).exists())

    def test_post_only_csrf_and_login_required(self):
        item = self.record()
        for url in (
            self.new,
            reverse("ai_assistant:saved_case_view_edit", args=[item.pk]),
            reverse("ai_assistant:saved_case_view_delete", args=[item.pk]),
        ):
            self.assertEqual(self.client.get(url, secure=True).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(
            strict.post(self.new, self.filters | dict(name="CSRF"), secure=True).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                reverse("ai_assistant:saved_case_view_load", args=[item.pk]), secure=True
            ).status_code,
            405,
        )
        self.client.logout()
        self.assertEqual(self.create().status_code, 302)

    def test_safe_return_url_rejects_external_and_backslash_targets(self):
        for number, next_url in enumerate(("https://evil.invalid/", "/\\evil.invalid/")):
            self.assertEqual(
                self.create(name=f"安全{number}", next=next_url).url, reverse("core-views-index")
            )

    def test_context_resolves_references_without_per_view_query_growth(self):
        self.record()
        with CaptureQueriesContext(connection) as first:
            context(self.user, self.filters)
        SavedCaseView.objects.bulk_create(
            [
                SavedCaseView(owner=self.user, name=f"更多{i}", filters=self.filters)
                for i in range(LIMIT - 1)
            ]
        )
        with CaptureQueriesContext(connection) as many:
            data = context(self.user, self.filters)
        self.assertEqual(len(data["saved_case_views"]), LIMIT)
        self.assertLessEqual(len(many), len(first) + 2)

    def test_retained_context_reports_invalid_current_folder(self):
        data = context(self.user, self.filters | {"folder": "bad"})
        self.assertTrue(data["current_case_filter_error"])

    def test_hub_hides_saved_views_without_querying_or_deleting_them(self):
        item = self.record()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.hub, {"product": self.product.pk}, secure=True)
        self.assertEqual(response.status_code, 200)
        for text in ("我的筛选视图", "保存当前筛选", 'id="hub-saved-manager"'):
            self.assertNotContains(response, text)
        for text in ('id="hub-search"', "用例目录", "Web 自动化测试", self.web.name):
            self.assertContains(response, text)
        self.assertNotIn("saved_case_views", response.context)
        table = SavedCaseView._meta.db_table.lower()
        self.assertFalse(any(table in query["sql"].lower() for query in queries))
        item.refresh_from_db()
        self.assertEqual(item.filters, self.filters)
        self.assertTrue(Folder.objects.filter(pk=self.folder.pk).exists())
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk).exists())

    def test_normalization_allows_personal_bookmarks_not_visibility_grants(self):
        values, product, folder = normalize(self.user, self.filters)
        self.assertEqual(values, self.filters)
        self.assertEqual(product, self.product)
        self.assertEqual(folder, self.folder)
