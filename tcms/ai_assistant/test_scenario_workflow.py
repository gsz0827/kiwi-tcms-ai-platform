import json
import uuid
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, Client, override_settings, SimpleTestCase
from .scenario_trace import published_execution_ids
from django.urls import reverse
from django.utils import timezone
from guardian.shortcuts import assign_perm, remove_perm
from tcms.tests.factories import TestCaseFactory, ProductFactory
from tcms.web_testing.models import WebCase, WebRun, WebSuite, WebAIRequest, WebAIDraft
from tcms.web_testing.forms import CaseForm
from tcms.web_testing.ai_generation import import_drafts, check_access
from tcms.web_testing.execution_config import suite_snapshot
from . import roles, test_directory_tools
from .crypto import encrypt_api_key
from .models import AIRequest, AITestCaseDraft

STEPS = [
    {"action": "goto", "value": "/login/"},
    {"action": "assert_visible", "selector": "#login-form"},
]
EVIDENCE = "登录页面 /login/ 的 #login-form 显示用户名和密码输入区域。"


from .edit_test_client import EditClient


class ScenarioWorkflowTests(TestCase):
    client_class = EditClient
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def library(self, **params):
        return self.client.get(
            reverse("ai_assistant:scenario_library"),
            {"product": self.product.pk} | params,
            secure=True,
        )

    def detail(self):
        return self.client.get(
            reverse("ai_assistant:scenario_detail", args=[self.manual.pk]), secure=True
        )

    def grant_add(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testcases", codename="add_testcase")
        )

    def web_form(self, **overrides):
        data = (
            dict(
                product=self.product.pk,
                test_case=self.manual.pk,
                name="Web 执行实现",
                steps=json.dumps(STEPS),
                description="配置说明",
            )
            | overrides
        )
        return CaseForm(data, owner=self.user)

    def source(self):
        source = AIRequest.objects.create(
            created_by=self.user,
            category=self.manual.category,
            title="登录需求",
            requirement="输入正确账号密码后登录成功。",
        )
        draft = AITestCaseDraft.objects.create(
            request=source,
            case_number="TC-AI-1",
            summary="AI 登录业务场景",
            priority="P3",
            preconditions=["账号存在"],
            steps=[{"action": "输入账号并登录", "expected": "进入首页"}],
        )
        return source, draft

    def adopt(self, source, draft, **changes):
        return self.client.post(
            reverse("ai_assistant:scenario_confirm", args=[source.pk]),
            dict(draft_ids=[draft.pk], confirmed="on") | changes,
            secure=True,
        )

    def batch(self, target=None, reviewed=True):
        payload = dict(
            documentation=EVIDENCE,
            requirements="",
            environment_variables=[],
            count=3,
            target_case_id=(target or self.manual).pk,
        )
        batch = WebAIRequest.objects.create(
            owner=self.user,
            product=self.product,
            title="Web配置",
            submission_token=uuid.uuid4(),
            fingerprint="f" * 64,
            generated=True,
            input_encrypted=encrypt_api_key(json.dumps(payload)),
        )
        draft = WebAIDraft.objects.create(
            request=batch,
            position=0,
            name="Web 登录",
            description="实现",
            evidence=EVIDENCE,
            steps=STEPS,
            reviewed_at=timezone.now() if reviewed else None,
        )
        return batch, draft

    def test_one_business_row_with_api_not_hidden_by_automated_flag(self):
        self.manual.is_automated = True
        self.manual.save()
        self.web.test_case = self.manual
        self.web.save()
        page = self.library()
        self.assertEqual(page.context["cases"].paginator.count, 1)
        self.assertTrue(page.context["cases"][0].owned_api)
        self.assertTrue(page.context["cases"][0].owned_web)
        self.assertNotContains(page, "data-case-kind=")
        self.assertNotContains(page, "进入用例管理")
        self.assertNotContains(page, "管理共享目录")
        self.assertContains(page, 'data-case-node="manual:')

    def test_project_name_folder_filters_and_config_filters(self):
        from .models import ProjectResourceAssignment

        ProjectResourceAssignment.objects.create(
            resource_type="case", object_id=self.manual.pk, folder=self.folder
        )
        self.assertEqual(self.library(folder=self.folder.pk).context["cases"].paginator.count, 1)
        self.assertEqual(self.library(folder="unfiled").context["cases"].paginator.count, 1)
        self.assertEqual(self.library(automation="api").context["cases"].paginator.count, 1)
        self.assertEqual(self.library(automation="web").context["cases"].paginator.count, 0)
        self.assertEqual(self.library(automation="none").context["cases"].paginator.count, 0)
        self.assertEqual(self.library(q="不存在").context["cases"].paginator.count, 0)
        self.assertEqual(self.library(folder="invalid").context["cases"].paginator.count, 0)

    def test_config_badges_do_not_disclose_other_owners(self):
        self.api.owner = self.other
        self.api.save()
        WebCase.objects.create(
            owner=self.other,
            product=self.product,
            test_case=self.manual,
            name="PRIVATE WEB",
            steps_encrypted="PRIVATE",
        )
        page = self.library()
        self.assertFalse(page.context["cases"][0].owned_api)
        self.assertFalse(page.context["cases"][0].owned_web)
        self.assertNotContains(self.detail(), "PRIVATE WEB")

    def test_detail_never_embeds_private_steps_parameters_or_foreign_names(self):
        self.web.test_case = self.manual
        self.web.save()
        page = self.detail()
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Web 登录")
        self.assertNotContains(page, "UNCHANGED-CIPHER")
        self.assertNotContains(page, "UNCHANGED-SECRET")
        self.assertContains(page, "添加 Web 脚本")
        self.assertContains(page, "添加接口脚本")

    def test_foreign_business_case_is_404(self):
        self.client.force_login(self.other)
        self.assertEqual(self.detail().status_code, 404)

    def test_visible_readonly_detail_has_no_write_actions(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        page = self.detail()
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, "添加 Web 脚本")
        self.assertNotContains(page, "编辑业务用例")
        self.assertEqual(
            self.client.post(
                reverse("ai_assistant:scenario_edit", args=[self.manual.pk]), secure=True
            ).status_code,
            403,
        )

    def test_web_configuration_links_without_changing_business_body_or_name(self):
        text, summary, count = self.manual.text, self.manual.summary, self.manual.history.count()
        form = self.web_form()
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.test_case_id, self.manual.pk)
        self.manual.refresh_from_db()
        self.assertEqual(
            (self.manual.text, self.manual.summary, self.manual.history.count()),
            (text, summary, count),
        )

    def test_web_link_rejects_cross_project_and_noneditable(self):
        self.assertFalse(self.web_form(product=ProductFactory().pk).is_valid())
        alien = TestCaseFactory(author=self.other, category__product=self.product)
        self.assertFalse(self.web_form(test_case=alien.pk).is_valid())
        remove_perm("change_testcase", self.user, self.manual)
        self.assertFalse(self.web_form().is_valid())

    def test_web_create_prefills_from_business_case_and_returns_to_detail(self):
        url = reverse("web_testing:case_new")
        page = self.client.get(url, {"test_case": self.manual.pk}, secure=True)
        self.assertEqual(page.context["form"].initial["test_case"], self.manual.pk)
        response = self.client.post(
            url,
            dict(
                product=self.product.pk,
                test_case=self.manual.pk,
                name="保存配置",
                steps=json.dumps(STEPS),
            ),
            secure=True,
        )
        self.assertRedirects(
            response,
            reverse("ai_assistant:scenario_detail", args=[self.manual.pk]),
            fetch_redirect_response=False,
        )
        self.assertFalse(WebRun.objects.exists())

    def test_web_ai_initial_target_and_context(self):
        page = self.client.get(
            reverse("web_testing:ai_generate", args=[self.product.pk]),
            {"test_case": self.manual.pk},
            secure=True,
        )
        self.assertEqual(page.context["form"].initial["target_case"], self.manual.pk)
        self.assertEqual(page.context["form"].initial["requirements"], self.manual.text[:12000])

    def test_web_ai_import_preserves_business_and_no_execution(self):
        batch, draft = self.batch()
        text = self.manual.text
        self.assertEqual(import_drafts(self.user, batch.pk, [draft.pk]), 1)
        draft.refresh_from_db()
        self.assertEqual(draft.web_case.test_case_id, self.manual.pk)
        self.assertEqual(import_drafts(self.user, batch.pk, [draft.pk]), 0)
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.text, text)
        self.assertFalse(WebRun.objects.exists())

    def test_unreviewed_web_ai_is_not_imported(self):
        batch, draft = self.batch(reviewed=False)
        with self.assertRaises(ValueError):
            import_drafts(self.user, batch.pk, [draft.pk])
        self.assertFalse(WebCase.objects.filter(test_case=self.manual).exists())

    def test_revoked_or_moved_business_blocks_pending_web_import(self):
        batch, draft = self.batch()
        remove_perm("change_testcase", self.user, self.manual)
        with self.assertRaises(ValueError):
            check_access(batch, self.user)
        assign_perm("change_testcase", self.user, self.manual)
        self.manual.category.product = ProductFactory()
        self.manual.category.save()
        with self.assertRaises(ValueError):
            import_drafts(self.user, batch.pk, [draft.pk])

    @override_settings(WEB_TEST_ALLOWED_ORIGINS=["https://kiwi-web:8443"])
    def test_new_snapshot_identity_is_immutable_on_relink(self):
        self.web.test_case = self.manual
        self.web.steps_encrypted = encrypt_api_key(json.dumps(STEPS))
        self.web.save()
        suite = WebSuite.objects.create(
            owner=self.user,
            product=self.product,
            name="快照",
            base_url="https://kiwi-web:8443",
            case_ids=[self.web.pk],
        )
        snapshot = suite_snapshot(suite)
        self.assertEqual(snapshot["cases"][0]["business_case_id"], self.manual.pk)
        self.web.test_case = None
        self.web.save()
        self.assertEqual(snapshot["cases"][0]["business_case_id"], self.manual.pk)
        self.assertIsNone(suite_snapshot(suite)["cases"][0]["business_case_id"])

    def test_old_web_history_not_inferred_from_current_binding(self):
        self.web.test_case = self.manual
        self.web.save()
        for name, body in (
            ("旧历史", dict(id=self.web.pk)),
            ("带业务快照", dict(business_case_id=self.manual.pk)),
        ):
            WebRun.objects.create(
                owner=self.user,
                product=self.product,
                name=name,
                submission_token=uuid.uuid4(),
                status="passed",
                snapshot_encrypted=encrypt_api_key(json.dumps({"cases": [body]})),
            )
        page = self.detail()
        self.assertContains(page, "带业务快照")
        self.assertNotContains(page, ">旧历史<")

    def test_business_creation_requires_human_confirmation(self):
        self.grant_add()
        url = reverse("ai_assistant:scenario_new", args=[self.product.pk])
        data = dict(
            summary="新业务用例",
            category=self.manual.category_id,
            priority=self.manual.priority_id,
            case_status=self.manual.case_status_id,
            text="前置：账号存在；操作：登录；预期：进入首页",
        )
        self.assertEqual(self.client.post(url, data, secure=True).status_code, 200)
        self.assertFalse(TestCaseFactory._meta.model.objects.filter(summary="新业务用例").exists())
        self.assertEqual(
            self.client.post(url, data | {"confirmed": "on"}, secure=True).status_code, 302
        )

    def test_ai_adoption_requires_confirmation_then_links_source(self):
        self.grant_add()
        source, draft = self.source()
        self.adopt(source, draft, confirmed="")
        draft.refresh_from_db()
        self.assertIsNone(draft.imported_case_id)
        response = self.adopt(source, draft)
        draft.refresh_from_db()
        self.assertTrue(draft.imported_case_id)
        self.assertIn(reverse("ai_assistant:scenario_library"), response.url)
        page = self.client.get(
            reverse("ai_assistant:scenario_detail", args=[draft.imported_case_id]), secure=True
        )
        self.assertContains(page, f"来源需求 #{source.pk}")
        self.assertFalse(WebRun.objects.exists())

    def test_stale_or_unrelated_drafts_not_adopted(self):
        self.grant_add()
        source, draft = self.source()
        for changes in ({"needs_update": True}, {"requirement_version": 2, "needs_update": False}):
            for key, value in changes.items():
                setattr(draft, key, value)
            draft.save()
            self.adopt(source, draft)
            draft.refresh_from_db()
            self.assertIsNone(draft.imported_case_id)
        self.adopt(source, draft, draft_ids=[10**15])
        self.assertIsNone(draft.imported_case_id)

    def test_adoption_readonly_post_login_csrf(self):
        self.grant_add()
        source, draft = self.source()
        url = reverse("ai_assistant:scenario_confirm", args=[source.pk])
        self.assertEqual(self.client.get(url, secure=True).status_code, 405)
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.user)
        self.assertEqual(strict.post(url, secure=True).status_code, 403)
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertEqual(self.adopt(source, draft).status_code, 403)

    def test_pagination_has_single_list_and_keeps_filters(self):
        for i in range(31):
            case = TestCaseFactory(
                author=self.user, category=self.manual.category, summary=f"分页业务 {i}"
            )
            assign_perm("view_testcase", self.user, case)
        page = self.library(q="分页业务", page=2)
        self.assertEqual(page.context["cases"].paginator.count, 31)
        self.assertEqual(len(page.context["cases"]), 1)
        self.assertContains(page, "q=%E5%88%86%E9%A1%B5%E4%B8%9A%E5%8A%A1")


class PublishedTraceTests(SimpleTestCase):
    def test_only_matching_snapshot_positions_belong_to_business_case(self):
        rows = [
            {"position": 1, "execution_id": 11},
            {"position": 2, "execution_id": 22},
            {"position": 3, "execution_id": 33},
        ]
        self.assertEqual(published_execution_ids(rows, {1, 3}), {11, 33})

    def test_missing_or_invalid_execution_identity_is_not_guessed(self):
        rows = [
            {"position": 0},
            {"position": 1, "execution_id": "22"},
            {"position": 2, "execution_id": True},
            {"position": 3, "execution_id": -1},
        ]
        self.assertEqual(published_execution_ids(rows, {0, 1, 2, 3}), set())
