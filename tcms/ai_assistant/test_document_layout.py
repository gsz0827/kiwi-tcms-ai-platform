"""Document layouts retain role scope and existing AI actions without calling a model."""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from django.urls import reverse
from tcms.kiwi_auth.models import UserPreference

from . import roles, test_shared_folders
from .crypto import encrypt_api_key
from .models import AIDevTask, AIJob, AIModelConfig, AIRequest, AITestCaseDraft


class DocumentLayoutTests(TestCase):
    def setUp(self):
        test_shared_folders.RequirementSharedFolderTests.setUp(self)
        self.client.force_login(self.author)
        self.config = AIModelConfig.objects.create(
            owner=self.author,
            name="own-model",
            model="test-model",
            api_base="https://model.example.test/v1",
            is_active=True,
            api_key_encrypted=encrypt_api_key("fake-key"),
        )
        self.task = AIDevTask.objects.create(
            request=self.requirement,
            owner=self.author,
            title="开发文档",
            description="## 接口约定\n\n| 参数 | 含义 |\n| --- | --- |\n| code | 验证码 |",
            acceptance="- 无效验证码拒绝登录\n- 不创建会话",
        )

    def get(self, route, pk=None, **params):
        return self.client.get(
            reverse("ai_assistant:" + route, args=[pk] if pk else []), params, secure=True
        )

    def test_index_has_compact_table_and_collapsed_creation_form(self):
        page = self.get("index")
        self.assertContains(page, reverse("ai_assistant:requirement_new"))
        self.assertContains(page, "data-requirement-list")
        self.assertContains(page, 'scope="col">需求名称')
        self.assertNotContains(page, 'id="generation-form"')
        self.assertNotContains(page, 'id="analyze-button"')
        self.assertNotContains(page, 'id="generate-button"')
        self.assertNotContains(page, "<h3>历史请求</h3>")
        self.assertContains(page, "data-legacy-detail-url=")

    @patch("tcms.ai_assistant.views.submit_requirement")
    def test_invalid_creation_reopens_form_and_keeps_input_without_ai_call(self, submit):
        page = self.client.post(
            reverse("ai_assistant:index"),
            {
                "title": "保留输入",
                "requirement": "",
                "category": self.requirement.category_id,
                "submission_token": str(uuid.uuid4()),
                "action": "analyze",
            },
            secure=True,
        )
        self.assertTemplateUsed(page, "ai_assistant/requirement_new.html")
        self.assertContains(page, "保留输入")
        submit.assert_not_called()
        self.assertFalse(AIJob.objects.exists())

    def test_pagination_keeps_product_filter_and_visibility(self):
        AIRequest.objects.bulk_create(
            [
                AIRequest(
                    created_by=self.author,
                    category=self.requirement.category,
                    title=f"需求-{n}",
                    requirement="正文",
                )
                for n in range(21)
            ]
        )
        AIRequest.objects.create(
            created_by=self.outsider, title="PRIVATE-REQUIREMENT", requirement="hidden"
        )
        page = self.get("index", product=self.product.pk)
        self.assertEqual(page.context["ai_requests"].paginator.count, 22)
        self.assertEqual(len(page.context["ai_requests"]), 20)
        self.assertContains(page, f"?product={self.product.pk}&amp;page=2")
        self.assertNotContains(page, "PRIVATE-REQUIREMENT")
        last = self.get("index", product=self.product.pk, page=2)
        self.assertEqual(len(last.context["ai_requests"]), 2)
        self.assertEqual(last.context["pagination_query"], f"product={self.product.pk}")

    def test_requirement_default_body_is_markdown_and_related_tabs_start_hidden(self):
        self.requirement.requirement = (
            "## 业务规则\n\n- 验证码有效期一分钟\n\n| 输入 | 结果 |\n| --- | --- |\n| 过期 | 拒绝 |"
        )
        self.requirement.save()
        page = self.get("requirement_trace", self.requirement.pk)
        self.assertContains(page, "<h2>业务规则</h2>", html=True)
        self.assertContains(page, "<li>验证码有效期一分钟</li>", html=True)
        self.assertContains(page, "<th>输入</th>", html=True)
        self.assertContains(page, 'id="requirement-document" role="tabpanel"')
        self.assertContains(page, "data-document-panel hidden")
        self.assertContains(page, 'aria-label="需求属性"')
        self.assertContains(page, "需求修订")

    def test_task_documents_render_sections_and_safe_markdown(self):
        page = self.get("dev_task_detail", self.task.pk)
        self.assertContains(page, "<h2>接口约定</h2>", html=True)
        self.assertContains(page, "<th>参数</th>", html=True)
        self.assertContains(page, "<li>不创建会话</li>", html=True)
        self.assertContains(page, 'id="task-related"')
        self.assertContains(page, 'aria-label="开发任务属性"')
        self.assertContains(page, reverse("ai_assistant:assign_dev_task", args=[self.task.pk]))
        self.assertContains(page, self.member.username)

    def test_document_markdown_removes_script_and_event_attributes(self):
        self.requirement.requirement = '<script>alert(1)</script>\n<img src=x onerror="alert(2)">'
        self.requirement.save()
        self.task.description = self.requirement.requirement
        self.task.save()
        for route, pk in [
            ("requirement_trace", self.requirement.pk),
            ("dev_task_detail", self.task.pk),
        ]:
            page = self.get(route, pk)
            self.assertNotContains(page, "<script>alert(1)</script>")
            self.assertNotContains(page, "onerror=")

    def test_task_list_does_not_expand_documents_or_assignment_forms(self):
        page = self.get("dev_task_list")
        self.assertContains(page, self.task.title)
        self.assertNotContains(page, "无效验证码拒绝登录")
        self.assertNotContains(page, 'id="task-assignee"')
        self.assertNotContains(page, reverse("ai_assistant:assign_dev_task", args=[self.task.pk]))
        self.assertContains(
            page,
            reverse("ai_assistant:case_design", args=[self.requirement.pk]) + f"?task={self.task.pk}",
        )

    def test_readonly_member_sees_documents_not_mutation_controls(self):
        self.member.groups.add(Group.objects.get(name=roles.ROLE_VIEWER))
        self.client.force_login(self.member)
        self.assertNotContains(self.get("index"), 'id="new-requirement"')
        listing = self.get("dev_task_list")
        self.assertNotContains(listing, reverse("ai_assistant:edit_dev_task", args=[self.task.pk]))
        self.assertNotContains(listing, reverse("ai_assistant:delete_dev_task", args=[self.task.pk]))
        detail = self.get("dev_task_detail", self.task.pk)
        self.assertNotContains(detail, 'id="task-assignee"')
        self.assertContains(detail, "<h2>接口约定</h2>", html=True)

    def test_assignee_without_requirement_visibility_does_not_get_parent_shortcuts(self):
        self.task.assignee = self.outsider
        self.task.save()
        self.client.force_login(self.outsider)
        listing = self.get("dev_task_list")
        self.assertContains(listing, self.task.title)
        self.assertNotContains(listing, self.requirement.title)
        self.assertNotContains(
            listing, reverse("ai_assistant:case_design", args=[self.requirement.pk])
        )
        self.assertNotContains(self.get("dev_task_detail", self.task.pk), 'id="task-assignee"')

    def test_ai_results_are_retained_in_auxiliary_tab_with_own_model(self):
        self.requirement.analysis = {"summary": "风险分析结果", "risk_level": "high"}
        self.requirement.save()
        self.author.user_permissions.add(
            Permission.objects.get(content_type__app_label="testcases", codename="add_testcase")
        )
        AITestCaseDraft.objects.create(
            request=self.requirement, case_number="TC-1", summary="过期拒绝", priority="P2"
        )
        page = self.get("requirement_trace", self.requirement.pk)
        self.assertContains(page, 'id="analysis"')
        self.assertContains(page, "风险分析结果")
        self.assertContains(page, "过期拒绝")
        self.assertContains(
            page, reverse("ai_assistant:analyze_coverage", args=[self.requirement.pk])
        )
        self.assertContains(page, 'name="draft_ids"')
        self.assertEqual(page.context["active_config"], self.config)

    def test_readonly_cannot_confirm_drafts_even_with_native_add_permission(self):
        self.member.groups.add(Group.objects.get(name=roles.ROLE_VIEWER))
        self.member.user_permissions.add(
            Permission.objects.get(content_type__app_label="testcases", codename="add_testcase")
        )
        AITestCaseDraft.objects.create(
            request=self.requirement, case_number="TC-1", summary="只读草稿", priority="P2"
        )
        self.client.force_login(self.member)
        page = self.get("requirement_trace", self.requirement.pk)
        self.assertContains(page, "只读草稿")
        self.assertNotContains(page, 'name="draft_ids"')
        self.assertNotContains(page, "确认并保存选中的用例")
        self.assertIsNone(page.context["active_config"])

    def test_get_pages_do_not_change_business_records(self):
        before = list(AIRequest.objects.values()), list(AIDevTask.objects.values())
        for route, pk in [
            ("index", None),
            ("dev_task_list", None),
            ("requirement_trace", self.requirement.pk),
            ("dev_task_detail", self.task.pk),
        ]:
            self.assertEqual(self.get(route, pk).status_code, 200)
        self.assertEqual(before, (list(AIRequest.objects.values()), list(AIDevTask.objects.values())))
        self.assertFalse(AIJob.objects.exists())

    @override_settings(USE_TZ=True)
    def test_document_dates_follow_display_timezone_without_changing_stored_timestamp(self):
        stored = datetime(2026, 10, 3, 9, 10, tzinfo=timezone.utc)
        self.assert_display_dates(stored)

    @override_settings(USE_TZ=False)
    def test_legacy_naive_dates_also_follow_display_timezone(self):
        self.assert_display_dates(datetime(2026, 10, 3, 9, 10))

    def assert_display_dates(self, stored):
        AIRequest.objects.filter(pk=self.requirement.pk).update(created=stored)
        AIDevTask.objects.filter(pk=self.task.pk).update(updated=stored)
        for zone, expected in [
            ("Asia/Shanghai", "2026-10-03 17:10"),
            ("Etc/UTC", "2026-10-03 09:10"),
        ]:
            UserPreference.objects.update_or_create(user=self.author, defaults={"time_zone": zone})
            self.assertContains(self.get("requirement_trace", self.requirement.pk), expected)
            self.assertContains(self.get("dev_task_detail", self.task.pk), expected)
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.created, stored)
