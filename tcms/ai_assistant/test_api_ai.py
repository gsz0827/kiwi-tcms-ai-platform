"""覆盖 AI 生成接口用例（api_ai）的完整链路。

这组用例的存在理由：该功能的模型、视图与业务逻辑曾经全部就位，但缺少
数据库迁移、URL 路由与模板，导致功能整体不可用，而原有测试完全覆盖不到。
下面钉住路由注册、权限、生成、复核与导入这几个关键环节。
"""
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from tcms.management.models import Priority
from tcms.testcases.models import TestCase as KiwiTestCase
from tcms.testcases.models import TestCaseStatus
from tcms.tests.factories import CategoryFactory, ProductFactory, UserFactory

from .api_ai import execute_generation, import_drafts
from .models import APIAIDraft, APIAIRequest, APICase, AIJob, AIModelConfig


EVIDENCE = "GET /users/{id} 返回用户详情，成功时状态码 200。"
DOCUMENTATION = "接口文档：\n" + EVIDENCE + "\n路径参数 id 为整数。"
MODEL_REPLY = json.dumps(
    {
        "questions": [],
        "cases": [
            {
                "name": "按用户 ID 查询详情",
                "description": "步骤 1：请求 /users/{{user_id}}。预期结果：状态码 200，data.id 等于 1。",
                "evidence": EVIDENCE,
                "configuration": {
                    "method": "GET",
                    "path": "/users/{{user_id}}",
                    "expected_status": 200,
                    "sequence": 10,
                    "assertions": [{"path": "data.id", "operator": "equals", "expected": 1}],
                },
            }
        ],
    },
    ensure_ascii=False,
)


def generation_form_data(category, model_config, token=None, **changes):
    data = {
        "submission_token": str(token or uuid.uuid4()),
        "title": "用户中心接口用例",
        "category": category.pk,
        "model_config": model_config.pk,
        "documentation": DOCUMENTATION,
        "requirements": "需覆盖正常查询与参数边界。",
        "environment_variables": "user_id",
        "count": 3,
    }
    data.update(changes)
    return data


class APIAIRouteTests(SimpleTestCase):
    """路由缺失曾让整个功能无法访问，这里逐个钉住。"""

    def test_generation_routes_are_registered(self):
        expected = [
            ("api_ai_generate", [7], "/api-testing/products/7/ai-requests/new/"),
            ("api_ai_detail", [7], "/api-testing/ai-requests/7/"),
            ("api_ai_import", [7], "/api-testing/ai-requests/7/import/"),
            ("api_ai_review", [7], "/api-testing/ai-drafts/7/review/"),
        ]
        for name, args, suffix in expected:
            with self.subTest(route=name):
                self.assertTrue(reverse(f"ai_assistant:{name}", args=args).endswith(suffix))


class APIAIGenerationTests(TestCase):
    def setUp(self):
        self.add_testcase = Permission.objects.get(
            content_type__app_label="testcases", codename="add_testcase"
        )
        self.owner = UserFactory()
        self.owner.user_permissions.add(self.add_testcase)
        self.product = ProductFactory()
        self.category = CategoryFactory(product=self.product)
        self.model_config = AIModelConfig.objects.create(
            owner=self.owner,
            name="本地演示模型",
            api_base="http://127.0.0.1:9/v1",
            model="demo-model",
            is_active=True,
        )
        TestCaseStatus.objects.create(name="Proposed", is_confirmed=False)
        Priority.objects.get_or_create(value="P1")
        self.client.force_login(self.owner)

    @property
    def generate_url(self):
        return reverse("ai_assistant:api_ai_generate", args=[self.product.pk])

    def submit(self, **changes):
        return self.client.post(
            self.generate_url,
            generation_form_data(self.category, self.model_config, **changes),
        )

    def generate_drafts(self):
        """走完「提交 → worker 生成草稿」，返回批次对象。"""
        self.submit()
        batch = APIAIRequest.objects.get()
        job = AIJob.objects.get(operation="api_case_generation")
        AIJob.objects.filter(pk=job.pk).update(status="running")
        job.refresh_from_db()
        with patch(
            "tcms.ai_assistant.api_ai._request_ai_content",
            return_value=(MODEL_REPLY, {"elapsed_ms": 1}),
        ):
            url, summary = execute_generation(job)
        batch.refresh_from_db()
        return batch, url, summary

    def test_generate_page_renders_for_authorised_owner(self):
        response = self.client.get(self.generate_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "接口文档")
        self.assertContains(response, 'name="documentation"')

    def test_generate_page_denies_anonymous_and_unauthorised_users(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.generate_url).status_code, 302)

        stranger = UserFactory()
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(self.generate_url).status_code, 403)

    def test_submitting_creates_request_and_background_job(self):
        response = self.submit()
        self.assertEqual(response.status_code, 302)

        batch = APIAIRequest.objects.get()
        self.assertEqual(batch.owner, self.owner)
        self.assertEqual(batch.product, self.product)
        self.assertFalse(batch.generated)

        job = AIJob.objects.get(operation="api_case_generation")
        self.assertEqual(job.owner, self.owner)
        self.assertEqual(job.payload, {"api_request_id": batch.pk})
        self.assertEqual(job.status, "queued")

    def test_replaying_same_submission_reuses_original_request(self):
        token = uuid.uuid4()
        self.submit(token=token)
        self.submit(token=token)

        self.assertEqual(APIAIRequest.objects.count(), 1)
        self.assertEqual(AIJob.objects.filter(operation="api_case_generation").count(), 1)

    def test_worker_turns_model_reply_into_reviewable_drafts(self):
        batch, url, summary = self.generate_drafts()

        self.assertTrue(batch.generated)
        self.assertEqual(summary, {"generated_count": 1})
        self.assertEqual(url, reverse("ai_assistant:api_ai_detail", args=[batch.pk]))

        draft = APIAIDraft.objects.get()
        self.assertEqual(draft.request, batch)
        self.assertEqual(draft.position, 0)
        self.assertIsNone(draft.reviewed_at)
        self.assertIsNone(draft.imported_at)
        self.assertEqual(draft.configuration["method"], "GET")

    def test_detail_and_review_pages_render(self):
        batch, _url, _summary = self.generate_drafts()
        draft = APIAIDraft.objects.get()

        detail = self.client.get(reverse("ai_assistant:api_ai_detail", args=[batch.pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, draft.name)

        review = self.client.get(reverse("ai_assistant:api_ai_review", args=[draft.pk]))
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, "复核确认")
        self.assertContains(review, 'name="confirmed"')

    def test_import_creates_case_and_api_configuration_after_review(self):
        batch, _url, _summary = self.generate_drafts()
        draft = APIAIDraft.objects.get()
        draft.reviewed_at = timezone.now()
        draft.save(update_fields=("reviewed_at",))

        import_drafts(self.owner, batch.pk, [draft.pk])

        draft.refresh_from_db()
        self.assertIsNotNone(draft.imported_at)
        self.assertIsNotNone(draft.api_case_id)

        case = KiwiTestCase.objects.get(category=self.category)
        self.assertEqual(case.summary, draft.name)
        self.assertTrue(case.is_automated)

        config = APICase.objects.get(owner=self.owner)
        self.assertEqual(config.test_case, case)
        self.assertEqual(config.path, "/users/{{user_id}}")
        self.assertEqual(config.expected_status, 200)

    def test_import_rejects_unreviewed_draft(self):
        batch, _url, _summary = self.generate_drafts()
        draft = APIAIDraft.objects.get()

        with self.assertRaises(ValueError):
            import_drafts(self.owner, batch.pk, [draft.pk])

        self.assertEqual(APICase.objects.count(), 0)
        draft.refresh_from_db()
        self.assertIsNone(draft.imported_at)

    def test_detail_page_auto_refreshes_while_generation_is_running(self):
        """生成是异步的：任务未结束时页面得自己刷新，否则用户只能守着按 F5。"""
        self.submit()
        batch = APIAIRequest.objects.get()

        response = self.client.get(reverse("ai_assistant:api_ai_detail", args=[batch.pk]))

        self.assertContains(response, "api_ai_drafts.js")
        self.assertContains(response, "api-ai-job")

    def test_detail_page_stops_auto_refreshing_once_the_job_finished(self):
        """任务已结束时不再轮询，避免无谓的请求。"""
        batch, _url, _summary = self.generate_drafts()
        AIJob.objects.filter(operation="api_case_generation").update(status="completed")

        response = self.client.get(reverse("ai_assistant:api_ai_detail", args=[batch.pk]))

        self.assertNotContains(response, "api_ai_drafts.js")

    def test_generate_page_loads_the_duplicate_submit_guard(self):
        response = self.client.get(self.generate_url)

        self.assertContains(response, "api_submit_once.js")
