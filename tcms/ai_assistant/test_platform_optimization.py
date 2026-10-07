import csv
import io
import uuid

from django.contrib.auth.models import Group, Permission
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from guardian.shortcuts import assign_perm

from tcms.tests.factories import (
    ProductFactory,
    UserFactory,
    TestCaseFactory,
    TestPlanFactory,
    TestRunFactory,
    VersionFactory,
    BuildFactory,
)
from tcms.web_testing.models import WebCase, WebAIRequest, WebAIDraft
from . import roles
from .models import (
    APICase,
    AIRequest,
    AITestCaseDraft,
    AIJob,
    APIAIRequest,
    APIAIDraft,
    ProjectResourceFolder as Folder,
    ProjectResourceAssignment as Assignment,
)


class CaseInventoryTests(TestCase):
    def setUp(self):
        self.user, self.other = UserFactory(), UserFactory()
        self.product, self.alien = ProductFactory(), ProductFactory()
        self.manual = TestCaseFactory(
            author=self.user, category__product=self.product, is_automated=False
        )
        for perm in ("view_testcase", "change_testcase"):
            assign_perm(perm, self.user, self.manual)
        self.web = WebCase.objects.create(
            owner=self.user, product=self.product, name="Web 登录", steps_encrypted="SECRET-STEPS"
        )
        self.api = APICase.objects.create(
            owner=self.user,
            product=self.product,
            name="接口登录",
            path="/login",
            headers={"Authorization": "SECRET-AUTH"},
            body={"password": "SECRET-BODY"},
        )
        self.private = WebCase.objects.create(
            owner=self.other, product=self.product, name="OTHER-PRIVATE", steps_encrypted="SECRET"
        )
        self.folder = Folder.objects.create(
            product=self.product, resource_type="case_group", name="登录模块"
        )
        self.foreign = Folder.objects.create(
            product=self.alien, resource_type="case_group", name="外项目录"
        )
        self.tokens = [f"manual:{self.manual.pk}", f"web:{self.web.pk}", f"api:{self.api.pk}"]
        self.url = reverse("ai_assistant:case_hub_batch")
        self.client.force_login(self.user)

    def post(self, **changes):
        return self.client.post(
            self.url,
            {
                "action": "move",
                "selected": self.tokens,
                "folder": self.folder.pk,
                "next": reverse("ai_assistant:case_hub") + f"?product={self.product.pk}&q=登录",
            }
            | changes,
            secure=True,
        )

    def test_mixed_types_move_atomically_with_typed_identity(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Assignment.objects.filter(folder=self.folder).count(), 3)
        self.assertEqual(
            set(Assignment.objects.values_list("resource_type", flat=True)),
            {"case", "web_case", "api_case"},
        )
        self.assertIn("q=", response.url)
        self.assertTrue(all(a.assigned_by_id == self.user.pk for a in Assignment.objects.all()))

    def test_unfile_never_deletes_cases(self):
        self.post()
        self.assertEqual(self.post(action="unfile").status_code, 302)
        self.assertFalse(Assignment.objects.exists())
        self.assertTrue(WebCase.objects.filter(pk=self.web.pk).exists())
        self.assertTrue(APICase.objects.filter(pk=self.api.pk).exists())

    def test_one_foreign_case_rejects_entire_batch(self):
        self.assertEqual(
            self.post(selected=self.tokens + [f"web:{self.private.pk}"]).status_code, 404
        )
        self.assertFalse(Assignment.objects.exists())

    def test_foreign_product_and_legacy_type_folder_reject_all(self):
        # Invisible folders intentionally return 404 rather than disclose metadata.
        self.assertEqual(self.post(folder=self.foreign.pk).status_code, 404)
        roles.add_product_member(self.user, self.alien)
        self.assertEqual(self.post(folder=self.foreign.pk).status_code, 403)
        legacy = Folder.objects.create(product=self.product, resource_type="case", name="旧手工目录")
        self.assertEqual(self.post(folder=legacy.pk).status_code, 403)
        self.assertFalse(Assignment.objects.exists())

    def test_cross_project_batch_changes_nothing(self):
        extra = WebCase.objects.create(
            owner=self.user, product=self.alien, name="其他项目", steps_encrypted="SECRET"
        )
        self.assertEqual(self.post(selected=self.tokens + [f"web:{extra.pk}"]).status_code, 302)
        self.assertFalse(Assignment.objects.exists())

    def test_view_only_manual_case_blocks_whole_batch(self):
        read_case = TestCaseFactory(category__product=self.product, is_automated=False)
        assign_perm("view_testcase", self.user, read_case)
        self.assertEqual(
            self.post(selected=self.tokens + [f"manual:{read_case.pk}"]).status_code, 403
        )
        self.assertFalse(Assignment.objects.exists())

    def test_readonly_cannot_move_but_can_export_visible_inventory(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post(action="export").status_code, 200)
        self.assertFalse(Assignment.objects.exists())

    def test_export_excludes_credentials_and_formula_is_escaped(self):
        self.web.name = '  =HYPERLINK("https://bad.invalid")'
        self.web.save(update_fields=["name"])
        response = self.post(action="export")
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        for secret in ("SECRET-AUTH", "SECRET-BODY", "SECRET-STEPS"):
            self.assertNotIn(secret, text)
        records = list(csv.reader(io.StringIO(text)))
        self.assertEqual(len(records), 4)
        self.assertTrue(records[2][2].startswith("'"))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_private_export_is_rejected(self):
        response = self.post(action="export", selected=[f"web:{self.private.pk}"])
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"OTHER-PRIVATE", response.content)

    def test_invalid_selection_and_limits_never_mutate(self):
        for tokens in ([], ["manual:no"], ["suite:1"], ["api:" + "9" * 30], self.tokens * 34):
            with self.subTest(tokens=tokens[:2]):
                self.assertEqual(self.post(selected=tokens).status_code, 302)
                self.assertFalse(Assignment.objects.exists())
        self.assertEqual(self.post(action="oops").status_code, 400)

    def test_duplicates_are_deduplicated(self):
        self.post(selected=self.tokens * 2)
        self.assertEqual(Assignment.objects.count(), 3)

    def test_post_only_csrf_and_login_required(self):
        self.assertEqual(self.client.get(self.url, secure=True).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, {"action": "move"}, secure=True).status_code, 403)
        self.client.logout()
        self.assertEqual(self.post().status_code, 302)

    def test_safe_redirect_rejects_external_and_backslash_hosts(self):
        for target in ("https://evil.invalid/", "/\\evil.invalid/"):
            self.assertEqual(self.post(next=target).url, reverse("core-views-index"))

    def test_all_page_paginates_more_than_eight_cases_and_stays_private(self):
        for number in range(36):
            WebCase.objects.create(
                owner=self.user,
                product=self.product,
                name=f"分页用例 {number}",
                steps_encrypted="unused",
            )
        url = reverse("ai_assistant:case_hub")
        seen = set()
        for page_number in (1, 2, 3):
            response = self.client.get(
                url, {"product": self.product.pk, "page_size": 15, "page": page_number}, secure=True
            )
            self.assertEqual(response.context["page"].paginator.count, 39)
            rows = response.context["directory_rows"]
            ids = {(r["kind"], r["pk"]) for r in rows}
            self.assertFalse(seen & ids)
            self.assertLessEqual(len(rows), 15)
            seen |= ids
            self.assertNotContains(response, self.private.name)
        self.assertEqual(len(seen), 39)

    def test_type_search_pagination_and_page_size_are_bounded(self):
        response = self.client.get(
            reverse("ai_assistant:case_hub"),
            {
                "product": self.product.pk,
                "type": "web",
                "q": "登录",
                "page_size": "1000000",
                "page": "bad",
            },
            secure=True,
        )
        self.assertEqual(response.context["page_size"], 30)
        self.assertEqual(response.context["page"].paginator.count, 1)
        self.assertEqual(response.context["directory_rows"][0]["pk"], self.web.pk)
        self.assertContains(response, "批量归档")


class WorkbenchProjectTests(TestCase):
    def setUp(self):
        self.owner, self.other = UserFactory(), UserFactory()
        self.product, self.alien = ProductFactory(), ProductFactory()
        self.version = VersionFactory(product=self.product)
        self.other_version = VersionFactory(product=self.product)
        self.plan = TestPlanFactory(
            author=self.owner, product=self.product, product_version=self.version
        )
        self.run = TestRunFactory(
            plan=self.plan,
            manager=self.owner,
            default_tester=self.owner,
            build=BuildFactory(version=self.version),
            stop_date=None,
        )
        other_plan = TestPlanFactory(author=self.owner, product=self.alien)
        TestRunFactory(plan=other_plan, manager=self.owner, default_tester=self.owner, stop_date=None)
        self.req = AIRequest.objects.create(
            created_by=self.owner,
            category=self.product.category.first(),
            title="本项目需求",
            requirement="验证",
        )
        AIRequest.objects.create(
            created_by=self.owner,
            category=self.alien.category.first(),
            title="其他项目需求",
            requirement="验证",
        )
        AITestCaseDraft.objects.create(request=self.req, summary="手工草稿", case_number="TC-1")
        self.job = AIJob.objects.create(
            owner=self.owner, operation="connection_test", status="queued"
        )
        AIJob.objects.create(owner=self.other, operation="connection_test", status="queued")
        self.client.force_login(self.owner)
        session = self.client.session
        session["ai_product_id"] = self.product.pk
        session.save()
        self.url = reverse("core-views-index")

    def test_selected_project_scopes_counts_and_recent_items(self):
        response = self.client.get(self.url, secure=True)
        self.assertEqual(response.context["requirement_count"], 1)
        self.assertEqual(response.context["test_plans_count"], 1)
        self.assertEqual(response.context["test_runs_count"], 1)
        self.assertEqual(response.context["pending_draft_count"], 1)
        self.assertNotContains(response, "其他项目需求")
        self.assertContains(response, "统计范围：")

    def test_process_local_cache_is_not_used_for_shared_user_sessions(self):
        from django.conf import settings

        if settings.CACHES["default"]["BACKEND"].endswith(".LocMemCache"):
            self.assertNotEqual(settings.SESSION_ENGINE, "django.contrib.sessions.backends.cached_db")
        from importlib import import_module

        store_class = import_module(settings.SESSION_ENGINE).SessionStore
        key = self.client.session.session_key
        first = store_class(session_key=key)
        self.assertEqual(first["ai_product_id"], self.product.pk)
        second = store_class(session_key=key)
        second["ai_product_id"] = self.alien.pk
        second.save()
        self.assertEqual(store_class(session_key=key)["ai_product_id"], self.alien.pk)

    def test_jobs_are_explicitly_account_scoped_not_product_scoped(self):
        response = self.client.get(self.url, {"product": self.alien.pk}, secure=True)
        self.assertEqual(response.context["active_job_count"], 1)
        self.assertContains(response, "账号范围")
        self.assertEqual(list(response.context["recent_jobs"]), [self.job])

    def test_all_projects_explicitly_overrides_session(self):
        response = self.client.get(self.url, {"product": ""}, secure=True)
        self.assertEqual(response.context["requirement_count"], 2)
        self.assertEqual(response.context["test_plans_count"], 2)

    def test_stale_product_does_not_widen_to_all(self):
        response = self.client.get(self.url, {"product": "999999999"}, secure=True)
        self.assertTrue(response.context["workbench_invalid_scope"])
        self.assertEqual(response.context["requirement_count"], 0)
        self.assertEqual(response.context["test_runs_count"], 0)
        self.assertContains(response, "当前项目已不存在")

    def test_version_filters_plans_and_runs_not_requirement_revision_numbers(self):
        response = self.client.get(self.url, {"version": self.other_version.pk}, secure=True)
        self.assertEqual(response.context["test_runs_count"], 0)
        self.assertEqual(response.context["test_plans_count"], 0)
        self.assertEqual(response.context["requirement_count"], 1)

    def test_automation_drafts_surface_with_private_project_scope(self):
        web_req = WebAIRequest.objects.create(
            owner=self.owner,
            product=self.product,
            title="Web 草稿需求",
            submission_token=uuid.uuid4(),
            fingerprint="x",
            input_encrypted="SECRET",
        )
        draft = WebAIDraft.objects.create(request=web_req, position=1, name="待处理 Web 草稿")
        WebAIDraft.objects.create(
            request=web_req, position=2, name="已导入 Web 草稿", imported_at=timezone.now()
        )
        api_req = APIAIRequest.objects.create(
            owner=self.owner,
            product=self.product,
            category=self.product.category.first(),
            title="接口草稿需求",
            submission_token=uuid.uuid4(),
            fingerprint="x",
            input_encrypted="SECRET",
        )
        APIAIDraft.objects.create(request=api_req, position=1, name="待处理接口草稿")
        response = self.client.get(self.url, secure=True)
        self.assertEqual(response.context["web_draft_count"], 1)
        self.assertEqual(response.context["api_draft_count"], 1)
        self.assertContains(response, reverse("web_testing:ai_review", args=[draft.pk]))
        response = self.client.get(self.url, {"product": self.alien.pk}, secure=True)
        self.assertEqual(response.context["web_draft_count"], 0)
        self.assertEqual(response.context["api_draft_count"], 0)

    def test_plan_run_search_defaults_follow_project_and_explicit_all_overrides(self):
        for app, codename in (("testplans", "view_testplan"), ("testruns", "view_testrun")):
            self.owner.user_permissions.add(
                Permission.objects.get(content_type__app_label=app, codename=codename)
            )
        session = self.client.session
        session["ai_version_id"] = self.version.pk
        session.save()
        for route, field in (("plans-search", "product_version"), ("testruns-search", "version")):
            response = self.client.get(reverse(route), secure=True)
            self.assertEqual(str(response.context["form"]["product"].value()), str(self.product.pk))
            self.assertEqual(str(response.context["form"][field].value()), str(self.version.pk))
            response = self.client.get(reverse(route), {"product": ""}, secure=True)
            self.assertEqual(response.context["form"]["product"].value(), "")
        response = self.client.get(
            reverse("plans-search"),
            {"product": self.product.pk, "version": self.version.pk},
            secure=True,
        )
        self.assertEqual(
            str(response.context["form"]["product_version"].value()), str(self.version.pk)
        )
