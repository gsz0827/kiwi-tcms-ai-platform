"""Structured documents preserve source text, access scope, snapshots and AI context."""

import uuid
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from tcms.management.models import Version
from tcms.tests.factories import ProductFactory
from . import roles, test_document_layout
from .models import AIRequest, AIDevTask, AIJob, AITestCaseDraft
from .forms import AIRequestForm, RequirementChangeForm, AIDevTaskForm
from .jobs import (
    submit_requirement,
    _requirement_fingerprint,
    _execute_requirement_analysis,
    _execute_test_case_generation,
    _execute_dev_task_breakdown,
)
from .case_design_context import context_snapshot, validate_context
from .document_sections import item_markdown


from .edit_test_client import EditClient


class DocumentSectionsTests(TestCase):
    client_class = EditClient
    def setUp(self):
        test_document_layout.DocumentLayoutTests.setUp(self)
        self.release = Version.objects.create(product=self.product, value="1.2.0")
        self.foreign_product = ProductFactory()
        self.foreign_release = Version.objects.create(product=self.foreign_product, value="9.9.0")
        self.sections = {
            "background": "支持运营人员查询报表",
            "impact_scope": "运营管理 → 报表查询",
            "business_rules": "仅可查看自己项目的数据\n最多导出 1000 条",
            "exceptions": "超时提示重试",
            "acceptance_criteria": "正常查询得到正确结果\n越权查询被拒绝",
            "references": "https://example.test/prototype",
        }

    def url(self, name, pk=None):
        return reverse("ai_assistant:" + name, args=[pk] if pk else [])

    def payload(self, **changes):
        return {
            "title": self.requirement.title,
            "requirement": self.requirement.requirement,
            "change_summary": "完善业务规则",
            "target_version": self.release.pk,
            "assigned_to": self.member.pk,
            "priority": "P2",
            "status": "confirmed",
            **self.sections,
            **changes,
        }

    def test_new_and_edit_forms_group_every_field_once(self):
        for route, pk in [("requirement_new", None), ("edit_requirement", self.requirement.pk)]:
            page = self.client.get(self.url(route, pk), secure=True)
            for name in [
                "background",
                "impact_scope",
                "requirement",
                "business_rules",
                "exceptions",
                "acceptance_criteria",
                "nonfunctional",
                "references",
                "target_version",
                "assigned_to",
                "priority",
                "status",
            ]:
                self.assertContains(page, f'name="{name}"', count=1)
            self.assertContains(page, "document-form-extra")
            self.assertContains(page, 'aria-label="需求属性"')
        page = self.client.get(self.url("edit_dev_task", self.task.pk), secure=True)
        for name in [
            "objective",
            "impact_scope",
            "description",
            "business_rules",
            "exceptions",
            "acceptance",
            "interfaces",
            "data_changes",
            "deployment",
            "test_notes",
            "references",
            "target_version",
        ]:
            self.assertContains(page, f'name="{name}"', count=1)

    def test_plain_lines_become_numbered_items_without_damaging_markdown(self):
        self.assertEqual(item_markdown("规则 A\n\n规则 B"), "1. 规则 A\n2. 规则 B")
        for original in [
            "- 规则 A\n- 规则 B",
            "1. A\n2. B",
            "## 规则\n\n正文",
            "| 字段 | 值 |\n| --- | --- |\n| a | b |",
            "```\nprint(1)\n```",
        ]:
            self.assertEqual(item_markdown(original), original)

    def test_legacy_text_is_not_split_or_rewritten_by_reading(self):
        original = "## 业务规则\n\n- 原始正文\n\n## 验收标准\n\n保留全部内容"
        self.requirement.requirement = original
        self.requirement.save()
        form = RequirementChangeForm(instance=self.requirement)
        self.assertEqual(form["requirement"].value(), original)
        self.assertEqual(form["business_rules"].value(), "")
        self.assertEqual(self.requirement.requirement_document, original)
        self.client.get(self.url("requirement_trace", self.requirement.pk), secure=True)
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.requirement, original)
        self.assertEqual(self.requirement.document_sections, {})

    def test_legacy_requirement_displays_all_core_sections_without_changing_content(self):
        expected = ["background", "impact_scope", "requirement", "business_rules", "exceptions", "acceptance_criteria"]
        original = self.requirement.requirement
        self.assertEqual([row["name"] for row in self.requirement.document_blocks], expected)
        for route, pk in [("requirement_trace", self.requirement.pk), ("index", None), ("case_design", self.requirement.pk)]:
            page = self.client.get(self.url(route, pk), secure=True)
            for name in expected:
                self.assertContains(page, f'data-section="{name}"')
            self.assertContains(page, '<p class="document-section-empty">未填写</p>', html=True)
            self.assertNotContains(page, 'data-section="nonfunctional"')
            self.assertNotContains(page, 'data-section="references"')
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.document_sections, {})
        self.assertEqual(self.requirement.requirement, original)
        self.assertEqual(self.requirement.requirement_document, original)
        self.assertFalse(AIJob.objects.exists())

    def test_partially_filled_sections_keep_placeholders_out_of_ai_context(self):
        self.requirement.document_sections = {"background": "明确的业务目标"}
        self.requirement.save()
        page = self.client.get(self.url("requirement_trace", self.requirement.pk), secure=True)
        self.assertContains(page, "明确的业务目标")
        self.assertContains(page, "未填写")
        context = self.requirement.requirement_document
        self.assertIn("明确的业务目标", context)
        self.assertNotIn("未填写", context)
        for heading in ["影响范围", "业务规则", "异常处理", "验收标准"]:
            self.assertNotIn("## " + heading, context)
        snapshot = self.requirement.versions.first()
        if snapshot:
            self.assertEqual(len(snapshot.document_blocks), 6)


    def test_edit_persists_sections_and_immutable_version_snapshot(self):
        draft = AITestCaseDraft.objects.create(
            request=self.requirement, case_number="TC-QA", summary="查询报表"
        )
        response = self.client.post(
            self.url("edit_requirement", self.requirement.pk), self.payload(), secure=True
        )
        self.assertEqual(response.status_code, 302)
        self.requirement.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(self.requirement.document_sections, self.sections)
        self.assertEqual(self.requirement.target_version_id, self.release.pk)
        self.assertEqual(self.requirement.assigned_to_id, self.member.pk)
        self.assertTrue(draft.needs_update)
        snapshot = self.requirement.versions.get(version=2)
        self.assertEqual(snapshot.document_sections, self.sections)
        self.assertEqual(snapshot.attributes["target_version"], "1.2.0")
        self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            self.payload(business_rules="新规则"),
            secure=True,
        )
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.document_sections, self.sections)
        self.assertFalse(AIJob.objects.exists())

    def test_section_only_change_creates_revision_and_noop_does_not(self):
        self.client.post(
            self.url("edit_requirement", self.requirement.pk), self.payload(), secure=True
        )
        self.client.post(
            self.url("edit_requirement", self.requirement.pk), self.payload(), secure=True
        )
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.version, 2)
        self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            self.payload(acceptance_criteria="新增验收项"),
            secure=True,
        )
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.version, 3)

    def test_legacy_post_keeps_missing_new_fields_but_explicit_empty_clears(self):
        self.requirement.document_sections = self.sections
        self.requirement.target_version = self.release
        self.requirement.priority = "P2"
        self.requirement.save()
        form = RequirementChangeForm(
            {"title": self.requirement.title, "requirement": "新正文", "change_summary": "旧页面"},
            instance=self.requirement,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["document_sections"], self.sections)
        self.assertEqual(form.cleaned_data["target_version"], self.release)
        self.assertEqual(form.cleaned_data["priority"], "P2")
        form = RequirementChangeForm(
            {
                "title": self.requirement.title,
                "requirement": "正文",
                "change_summary": "清空",
                "business_rules": "",
            },
            instance=self.requirement,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("business_rules", form.cleaned_data["document_sections"])

    def test_wrong_product_version_and_member_cannot_be_submitted(self):
        form = RequirementChangeForm(
            self.payload(target_version=self.foreign_release.pk, assigned_to=self.outsider.pk),
            instance=self.requirement,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("target_version", form.errors)
        self.assertIn("assigned_to", form.errors)
        data = {
            "request": self.requirement.pk,
            "title": "开发文档",
            "priority": "P3",
            "status": "todo",
            "target_version": self.foreign_release.pk,
        }
        form = AIDevTaskForm(data, instance=self.task, user=self.author)
        self.assertFalse(form.is_valid())
        self.assertIn("target_version", form.errors)

    def test_submission_saves_complete_content_and_replay_detects_section_changes(self):
        data = {
            "category": self.requirement.category_id,
            "submission_token": str(uuid.uuid4()),
            **self.payload(),
        }
        form = AIRequestForm(data, user=self.author)
        self.assertTrue(form.is_valid(), form.errors)
        job, created = submit_requirement(
            self.author, form.cleaned_data, "requirement_analysis", self.config
        )
        self.assertTrue(created)
        source = AIRequest.objects.get(pk=job.payload["request_id"])
        self.assertEqual(source.document_sections, self.sections)
        self.assertEqual(source.versions.get(version=1).attributes["target_version"], "1.2.0")
        repeated, is_new = submit_requirement(
            self.author, form.cleaned_data, "requirement_analysis", self.config
        )
        self.assertEqual(job.pk, repeated.pk)
        self.assertFalse(is_new)
        changed = dict(
            form.cleaned_data, document_sections={**self.sections, "acceptance_criteria": "changed"}
        )
        with self.assertRaises(ValueError):
            submit_requirement(self.author, changed, "requirement_analysis", self.config)

    def test_complete_document_used_by_ai_workers_and_fingerprint(self):
        self.requirement.document_sections = self.sections
        self.requirement.target_version = self.release
        self.requirement.save()
        complete = self.requirement.requirement_document
        for value in self.sections.values():
            for line in value.splitlines():
                self.assertIn(line, complete)
        before = _requirement_fingerprint(self.requirement)
        self.requirement.document_sections = {**self.sections, "business_rules": "changed"}
        self.assertNotEqual(before, _requirement_fingerprint(self.requirement))
        self.requirement.refresh_from_db()
        methods = [
            (_execute_requirement_analysis, "analyze_requirement", ({}, self.config, "raw")),
            (_execute_test_case_generation, "generate_test_cases", []),
            (_execute_dev_task_breakdown, "break_down_dev_tasks", []),
        ]
        for method, name, result in methods:
            job = AIJob.objects.create(
                owner=self.author,
                model_config=self.config,
                operation="requirement_analysis",
                status='running',
                payload={"request_id": self.requirement.pk},
            )
            if name == "break_down_dev_tasks":
                self.task.delete()
            with patch("tcms.ai_assistant.jobs." + name, return_value=result) as call:
                method(job)
                self.assertEqual(call.call_args.args[1], complete)

    def test_design_context_contains_requirement_and_optional_task_sections(self):
        self.requirement.document_sections = self.sections
        self.requirement.save()
        self.task.document_sections = {"interfaces": "POST /login", "deployment": "回退配置"}
        self.task.save()
        context = context_snapshot(self.requirement, [self.task])
        self.assertIn("正常查询得到正确结果", context["requirement"])
        self.assertEqual(context["dev_tasks"][0]["document_sections"], self.task.document_sections)
        self.task.document_sections = {"interfaces": "POST /login/v2"}
        self.task.save()
        with self.assertRaises(ValueError):
            validate_context(self.author, self.requirement, context)

    def test_task_sections_survive_status_only_or_old_post(self):
        self.task.document_sections = {"interfaces": "GET /reports", "test_notes": "测试数据准备"}
        self.task.assignee = self.outsider
        self.task.save()
        self.client.force_login(self.outsider)
        response = self.client.post(
            self.url("edit_dev_task", self.task.pk),
            {"status": "doing", "interfaces": "forged", "target_version": self.foreign_release.pk},
            secure=True,
        )
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(
            self.task.document_sections, {"interfaces": "GET /reports", "test_notes": "测试数据准备"}
        )
        self.assertIsNone(self.task.target_version_id)

    def test_details_and_modal_render_sections_as_safe_content(self):
        self.requirement.document_sections = {
            **self.sections,
            "business_rules": '<img src=x onerror="alert(1)">\n另一个规则',
        }
        self.requirement.save()
        for name, pk in [
            ("requirement_trace", self.requirement.pk),
            ("index", None),
            ("case_design", self.requirement.pk),
        ]:
            page = self.client.get(self.url(name, pk), secure=True)
            self.assertContains(page, 'data-section="acceptance_criteria"')
            self.assertNotContains(page, 'onerror="alert(1)"')
        page = self.client.get(self.url("requirement_trace", self.requirement.pk), secure=True)
        self.assertContains(page, "<li>正常查询得到正确结果</li>", html=True)

    def test_options_are_readonly_and_match_version_membership(self):
        url = self.url("document_options")
        response = self.client.get(url, {"category": self.requirement.category_id}, secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn({'id':self.release.pk,'name':'1.2.0'}, response.json()['versions'])
        self.assertNotIn(self.foreign_release.pk,[row['id'] for row in response.json()['versions']])
        self.assertNotIn(self.outsider.pk, [row["id"] for row in response.json()["assignees"]])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.post(url, secure=True).status_code, 405)
        for value in ["", "bad", "²", "0", "9" * 100]:
            self.assertEqual(self.client.get(url, {"category": value}, secure=True).status_code, 400)
        roles.set_user_roles(self.member, [roles.ROLE_VIEWER])
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get(url, {"category": self.requirement.category_id}, secure=True).status_code,
            403,
        )
        self.client.logout()
        self.assertEqual(
            self.client.get(url, {"category": self.requirement.category_id}, secure=True).status_code,
            302,
        )
