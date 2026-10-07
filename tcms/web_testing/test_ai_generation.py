import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase, SimpleTestCase, RequestFactory
from django.urls import reverse, resolve

from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.ai_assistant.jobs import claim_next_job, execute_job
from tcms.ai_assistant.models import AIJob, AIModelConfig, AIUsageLog
from tcms.ai_assistant.services import _request_config_content
from tcms.ai_assistant.roles import ROLE_VIEWER
from tcms.ai_assistant.templatetags.ai_navigation import platform_navigation
from tcms.tests.factories import ProductFactory, UserFactory
from .ai_generation import import_drafts, inputs, parse_response, draft_errors, execute_generation
from .models import WebAIRequest, WebAIDraft, WebCase, WebRun

EVIDENCE = "登录页面 /login/ 的 #login-form 显示用户名和密码输入区域。"
STEPS = [{"action": "goto", "value": "/login/"}, {"action": "assert_visible", "selector": "#login-form"}]
REPLY = json.dumps({"questions": [], "cases": [dict(name="登录页表单展示", description="打开页面，表单可见",
    evidence=EVIDENCE, steps=STEPS)]}, ensure_ascii=False)


class NavigationTests(TestCase):
    def test_sections_and_active_pages(self):
        user = UserFactory()
        factory = RequestFactory()
        cases = [
            ("ai_assistant:index", [], "requirements", "需求管理"),
            ("ai_assistant:dev_task_list", [], "requirements", "开发任务"),
            ("ai_assistant:case_hub", [], "testing", "用例库"),
            ("plans-search", [], "testing", "测试计划"),
            ("testruns-search", [], "testing", "执行任务"),
            ("web_testing:ai_generate", [1], "web", "自动化脚本"),
            ("ai_assistant:api_ai_generate", [1], "api", "自动化脚本"),
        ]
        for name, args, section, label in cases:
            with self.subTest(name=name):
                request = factory.get(reverse(name, args=args))
                request.user, request.resolver_match = user, resolve(request.path)
                nav = platform_navigation({"request": request})
                self.assertNotIn("manual", [s["key"] for s in nav["sections"]])
                self.assertEqual([s["key"] for s in nav["sections"] if s["is_current"]], [section])
                self.assertEqual([i["label"] for s in nav["sections"] for i in s["items"] if i.get("is_active")], [label])


class ResponseValidationTests(SimpleTestCase):
    def test_rejects_scripts_unknown_fields_and_bad_types(self):
        for step in [{"action": "evaluate", "value": "alert(1)"}, {"action": "click", "selector": "#x", "code": "hack"},
                     {"action": ["click"]}, {"action": "fill", "value": 42}]:
            reply = json.loads(REPLY)
            reply["cases"][0]["steps"] = [step]
            with self.subTest(step=step), self.assertRaises(ValueError):
                parse_response(json.dumps(reply), 3)

    def test_empty_cases_keep_questions_but_cannot_be_imported(self):
        case = parse_response('{"cases":[],"questions":["请提供实际定位器"]}', 3)[0]
        draft = WebAIDraft(**case)
        self.assertTrue(draft_errors(draft, dict(documentation=EVIDENCE, requirements="", environment_variables=[])))

    def test_draft_requires_evidence_variables_assertions_and_relative_paths(self):
        context = dict(documentation=EVIDENCE, requirements="", environment_variables=[])
        for steps, evidence in [
            (STEPS, "不存在于资料中的伪造文字"),
            ([{"action": "goto", "value": "/login/"}], EVIDENCE),
            (STEPS + [{"action": "fill", "selector": "#username", "value": "{{secret}}"}], EVIDENCE),
            ([{"action": "goto", "value": "https://kiwi-web:8443/"}] + STEPS[1:], EVIDENCE),
        ]:
            with self.subTest(steps=steps):
                self.assertTrue(draft_errors(WebAIDraft(steps=steps, evidence=evidence), context))


class WebAIWorkflowTests(TestCase):
    def setUp(self):
        self.owner, self.other = UserFactory(), UserFactory()
        self.product = ProductFactory()
        self.model = AIModelConfig.objects.create(owner=self.owner, name="模拟模型", api_base="http://127.0.0.1:9/v1",
            model="test", is_active=True)
        self.client.force_login(self.owner)

    def data(self, **overrides):
        data = dict(submission_token=str(uuid.uuid4()), title="登录页测试", model_config=self.model.pk,
            documentation=EVIDENCE, requirements="表单应显示", environment_variables="", count=3)
        return data | overrides

    def submit(self, **overrides):
        return self.client.post(reverse("web_testing:ai_generate", args=[self.product.pk]), self.data(**overrides))

    def generate(self):
        self.assertEqual(self.submit().status_code, 302)
        job = claim_next_job()
        with patch("tcms.web_testing.ai_generation._request_ai_content", return_value=(REPLY, self.model)) as mock:
            execute_job(job)
        self.assertEqual(mock.call_args.kwargs["operation"], "web_case_generation")
        job.refresh_from_db()
        self.assertEqual((job.status, job.progress), ("completed", 100))
        return WebAIRequest.objects.get(), WebAIDraft.objects.get()

    def review(self, draft, **overrides):
        data = dict(revision=draft.revision, name=draft.name, description=draft.description,
            evidence=draft.evidence, steps=json.dumps(draft.steps), review_notes="", confirmed="on")
        return self.client.post(reverse("web_testing:ai_review", args=[draft.pk]), data | overrides)

    def test_forms_buttons_and_no_model_fallback(self):
        response = self.client.get(reverse("web_testing:cases"), {"product": self.product.pk})
        self.assertContains(response, reverse("web_testing:ai_generate", args=[self.product.pk]))
        self.client.force_login(self.other)
        response = self.client.get(reverse("web_testing:ai_generate", args=[self.product.pk]))
        self.assertContains(response, "尚未配置 AI 模型")
        self.assertNotContains(response, "模拟模型")
        self.assertEqual(self.submit().status_code, 200)
        self.assertFalse(AIJob.objects.exists())
        self.assertFalse(WebAIRequest.objects.exists())

    def test_async_draft_progress_then_review_import_edit_without_execution(self):
        batch, draft = self.generate()
        self.assertNotIn(EVIDENCE, batch.input_encrypted)
        self.assertEqual(inputs(batch)["documentation"], EVIDENCE)
        self.assertFalse(WebCase.objects.exists())
        response = self.client.get(reverse("web_testing:ai_detail", args=[batch.pk]))
        self.assertContains(response, "编辑与复核")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        with self.assertRaises(ValueError):
            import_drafts(self.owner, batch.pk, [draft.pk])
        self.assertEqual(self.review(draft, name="人工修改后的标题").status_code, 302)
        self.assertEqual(import_drafts(self.owner, batch.pk, [draft.pk]), 1)
        self.assertEqual(import_drafts(self.owner, batch.pk, [draft.pk]), 0)
        case = WebCase.objects.get()
        self.assertEqual(case.name, "人工修改后的标题")
        self.assertEqual(json.loads(decrypt_api_key(case.steps_encrypted)), STEPS)
        self.assertEqual(self.client.get(reverse("web_testing:case_edit", args=[case.pk])).status_code, 200)
        self.assertFalse(WebRun.objects.exists())

    def test_pending_page_loads_real_progress_poller(self):
        self.submit()
        batch = WebAIRequest.objects.get()
        response = self.client.get(reverse("web_testing:ai_detail", args=[batch.pk]))
        self.assertContains(response, "api_ai_drafts.js")
        self.assertContains(response, 'role="progressbar"')
        self.assertContains(response, reverse("ai_assistant:job_status", args=[AIJob.objects.get().pk]))

    def test_same_form_token_deduplicates_and_changed_input_rejected(self):
        data = self.data()
        url = reverse("web_testing:ai_generate", args=[self.product.pk])
        first = self.client.post(url, data)
        second = self.client.post(url, data)
        self.assertEqual(first.url, second.url)
        self.assertEqual(AIJob.objects.count(), 1)
        self.assertContains(self.client.post(url, data | {"title": "修改后的主题"}), "已提交过不同内容")
        self.assertEqual(WebAIRequest.objects.count(), 1)

    def test_owner_isolation_for_result_review_import_and_job(self):
        batch, draft = self.generate()
        self.client.force_login(self.other)
        for name, pk in [("ai_detail", batch.pk), ("ai_review", draft.pk)]:
            self.assertEqual(self.client.get(reverse("web_testing:" + name, args=[pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("web_testing:ai_import", args=[batch.pk]), {"draft_ids": [draft.pk]}).status_code, 404)
        self.assertEqual(self.client.get(reverse("ai_assistant:job_status", args=[AIJob.objects.get().pk])).status_code, 404)

    def test_readonly_and_revoked_owner_cannot_write_or_generate(self):
        batch, draft = self.generate()
        self.owner.groups.add(Group.objects.get_or_create(name=ROLE_VIEWER)[0])
        self.assertEqual(self.submit().status_code, 403)
        self.assertEqual(self.review(draft).status_code, 403)
        with self.assertRaises(ValueError):
            import_drafts(self.owner, batch.pk, [draft.pk])

    def test_cancel_during_model_response_discards_drafts(self):
        self.submit()
        job = claim_next_job()
        def cancel(*args, **kwargs):
            AIJob.objects.filter(pk=job.pk).update(status="cancel_requested")
            return REPLY, self.model
        with patch("tcms.web_testing.ai_generation._request_ai_content", side_effect=cancel):
            execute_job(job)
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(WebAIDraft.objects.exists())
        self.assertFalse(WebAIRequest.objects.get().generated)

    def test_optimistic_review_lock_and_questions(self):
        batch, draft = self.generate()
        draft.questions = ["定位器是否正确？"]
        draft.save()
        self.assertContains(self.review(draft), "请记录待确认问题")
        self.assertEqual(self.review(draft, review_notes="已在测试环境核对").status_code, 302)
        self.assertContains(self.review(draft, review_notes="另一个标签页"), "草稿已被其他页面修改")
        draft.refresh_from_db()
        self.assertEqual(draft.revision, 2)

    def test_import_atomic_when_one_draft_is_unreviewed(self):
        batch, draft = self.generate()
        self.review(draft)
        extra = WebAIDraft.objects.create(request=batch, position=1, name="未复核", steps=STEPS, evidence=EVIDENCE)
        with self.assertRaises(ValueError):
            import_drafts(self.owner, batch.pk, [draft.pk, extra.pk])
        self.assertFalse(WebCase.objects.exists())

    def test_retry_generated_batch_is_idempotent(self):
        batch, draft = self.generate()
        job = AIJob.objects.get()
        with patch("tcms.web_testing.ai_generation._request_ai_content") as mock:
            url, result = execute_generation(job)
        self.assertEqual(result["generated_count"], 1)
        self.assertEqual(url, reverse("web_testing:ai_detail", args=[batch.pk]))
        mock.assert_not_called()
        self.assertEqual(WebAIDraft.objects.count(), 1)

    def test_provider_errors_do_not_leak_credentials_or_documents_into_logs(self):
        self.model.api_key_encrypted = encrypt_api_key("fake-secret-for-test")
        self.model.save()
        with patch("tcms.ai_assistant.services._open_ai_request", side_effect=RuntimeError("fake-secret-for-test private-document")):
            with self.assertRaisesRegex(RuntimeError, "Web 用例生成调用失败") as error:
                _request_config_content(self.model, "system", "private-document", operation="web_case_generation")
        self.assertNotIn("fake-secret", str(error.exception))
        log = AIUsageLog.objects.get(owner=self.owner, operation="web_case_generation")
        self.assertNotIn("private-document", log.error_message)
        self.assertNotIn("fake-secret", log.error_message)
