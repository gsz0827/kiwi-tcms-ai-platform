"""Document authoring is presentation-only; saving and access rules remain unchanged."""

import html
import json

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from . import roles, test_document_layout
from .models import AIJob, AIRequest, AIDevTask, AITestCaseDraft


from .edit_test_client import EditClient


class DocumentEditorTests(TestCase):
    client_class = EditClient
    def setUp(self):
        test_document_layout.DocumentLayoutTests.setUp(self)

    def url(self, route, pk=None):
        return reverse("ai_assistant:" + route, args=[pk] if pk else [])

    def get(self, route, pk=None, **params):
        return self.client.get(self.url(route, pk), params, secure=True)

    def test_requirement_edit_retains_fields_errors_and_revision_history(self):
        page = self.get("edit_requirement", self.requirement.pk)
        self.assertContains(page, "data-requirement-editor")
        self.assertContains(page, "data-document-form")
        for field in ("title", "requirement", "change_summary"):
            self.assertContains(page, f'name="{field}"', count=1)
            self.assertContains(page, f'for="id_{field}"')
        self.assertContains(page, "data-editor-preview-button", count=8)
        self.assertContains(page, "需求修订记录")
        self.assertContains(page, "标记为待复核")
        self.assertContains(page, self.url("requirement_trace", self.requirement.pk))

    def test_task_form_groups_document_and_metadata_without_duplicating_fields(self):
        page = self.get("edit_dev_task", self.task.pk)
        self.assertContains(page, "data-task-editor")
        for field in (
            "request",
            "task_number",
            "title",
            "module",
            "description",
            "acceptance",
            "priority",
            "estimate_hours",
            "assignee",
            "status",
        ):
            self.assertContains(page, f'name="{field}"', count=1)
            self.assertContains(page, f'for="id_{field}"')
        self.assertContains(page, "data-editor-preview-button", count=11)
        self.assertNotContains(page, "两份都要能被人直接执行")
        self.assertContains(page, self.url("dev_task_detail", self.task.pk))

    def test_new_task_preserves_requirement_prefill(self):
        page = self.get("dev_task_create", request=self.requirement.pk)
        self.assertEqual(page.context["form"]["request"].value(), self.requirement.pk)
        self.assertContains(page, "新建开发任务")
        self.assertContains(page, self.url("dev_task_list"))

    def test_new_requirement_editor_keeps_ai_submission_actions_and_token(self):
        page = self.get("index")
        self.assertContains(page, "data-document-editor")
        for name in ("submission_token", "title", "requirement"):
            self.assertContains(page, f'name="{name}"', count=1)
        self.assertContains(page, 'name="action" value="analyze"')
        self.assertContains(page, 'name="action" value="generate"')

    def test_invalid_requirement_preserves_document_input_and_does_not_increment_revision(self):
        page = self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            {"title": "暂存标题", "requirement": "## 保留正文", "change_summary": ""},
            secure=True,
        )
        self.assertContains(page, "暂存标题")
        self.assertContains(page, "## 保留正文")
        self.assertContains(page, "document-field-error")
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.version, 1)
        self.assertEqual(self.requirement.title, "导出月度报表")

    def test_valid_requirement_edit_still_marks_case_review_and_creates_snapshot(self):
        draft = AITestCaseDraft.objects.create(
            request=self.requirement, case_number="TC-1", summary="导出报表", priority="P2"
        )
        page = self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            {
                "title": "新版需求",
                "requirement": "## 新版规则\n\n按季度导出",
                "change_summary": "扩展季度",
            },
            secure=True,
        )
        self.assertRedirects(
            page, self.url("requirement_trace", self.requirement.pk), fetch_redirect_response=False
        )
        self.requirement.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(self.requirement.version, 2)
        self.assertTrue(self.requirement.needs_case_review)
        self.assertTrue(draft.needs_update)
        self.assertEqual(self.requirement.versions.get(version=2).change_summary, "扩展季度")
        self.assertFalse(AIJob.objects.exists())

    def test_task_validation_keeps_input_and_existing_record(self):
        page = self.client.post(
            self.url("edit_dev_task", self.task.pk),
            {
                "request": self.requirement.pk,
                "title": "",
                "description": "未保存的开发说明",
                "acceptance": "待校验",
                "priority": "P2",
                "status": "todo",
            },
            secure=True,
        )
        self.assertContains(page, "未保存的开发说明")
        self.assertContains(page, "document-field-error")
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "开发文档")

    def test_assignee_only_status_field_and_same_post_boundary(self):
        self.task.assignee = self.outsider
        self.task.save()
        self.client.force_login(self.outsider)
        page = self.get("edit_dev_task", self.task.pk)
        self.assertContains(page, "更新任务状态")
        self.assertContains(page, 'name="status"', count=1)
        for field in ("request", "title", "description", "acceptance", "assignee", "priority"):
            self.assertNotContains(page, f'name="{field}"')
        self.assertNotContains(page, "data-document-editor")
        self.assertNotContains(page, self.requirement.title)
        result = self.client.post(
            self.url("edit_dev_task", self.task.pk),
            {"status": "doing", "title": "tampered"},
            secure=True,
        )
        self.assertEqual(result.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "doing")
        self.assertEqual(self.task.title, "开发文档")

    def test_readonly_and_unrelated_users_cannot_edit_documents(self):
        self.member.groups.add(Group.objects.get(name=roles.ROLE_VIEWER))
        self.client.force_login(self.member)
        for route, pk in [("edit_requirement", self.requirement.pk), ("edit_dev_task", self.task.pk)]:
            self.assertEqual(self.get(route, pk).status_code, 403)
        self.client.force_login(self.outsider)
        for route, pk in [("edit_requirement", self.requirement.pk), ("edit_dev_task", self.task.pk)]:
            self.assertEqual(self.get(route, pk).status_code, 404)

    def test_preview_uses_existing_sanitizer_and_does_not_save_or_queue_jobs(self):
        before = list(AIRequest.objects.values()), list(AIDevTask.objects.values())
        response = self.client.post(
            "/json-rpc/",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "Markdown.render",
                    "params": [
                        '## 标题\n\n| 项 | 值 |\n| --- | --- |\n| a | b |\n\n<script>alert(1)</script>\n<img src=x onerror="alert(2)">'
                    ],
                    "id": "document-preview",
                }
            ),
            content_type="application/json",
            secure=True,
        )
        self.assertEqual(response.status_code, 200)
        result = html.unescape(response.json()["result"])
        self.assertIn("<h2>标题</h2>", result)
        self.assertIn("<table>", result)
        self.assertNotIn("<script>", result)
        self.assertNotIn("onerror=", result)
        self.assertEqual(before, (list(AIRequest.objects.values()), list(AIDevTask.objects.values())))
        self.assertFalse(AIJob.objects.exists())

    def test_get_editor_pages_does_not_change_business_records(self):
        before = list(AIRequest.objects.values()), list(AIDevTask.objects.values())
        for route, pk in [
            ("edit_requirement", self.requirement.pk),
            ("edit_dev_task", self.task.pk),
            ("dev_task_create", None),
        ]:
            self.assertEqual(self.get(route, pk).status_code, 200)
        self.assertEqual(before, (list(AIRequest.objects.values()), list(AIDevTask.objects.values())))

    def test_status_save_from_new_form_returns_own_detail_not_invisible_parent_list(self):
        self.task.assignee = self.outsider
        self.task.save()
        self.client.force_login(self.outsider)
        page = self.client.post(
            self.url("edit_dev_task", self.task.pk),
            {"status": "doing", "return_detail": "1"},
            secure=True,
        )
        self.assertRedirects(
            page, self.url("dev_task_detail", self.task.pk), fetch_redirect_response=False
        )
        self.assertNotContains(self.get("dev_task_detail", self.task.pk), self.requirement.title)
