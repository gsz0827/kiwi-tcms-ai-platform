"""Regression for stale editors and explicit, snapshot-preserving source rechecks."""

from django.test import TestCase
from django.urls import reverse
from tcms.testcases.models import TestCase as NativeCase
from . import test_document_sections, test_directory_tools, test_scenario_design
from .models import AIRequest, AIDevTask, AITestCaseDraft, DocumentSourceReview, AIJob
from .edit_safety import edit_token


class RequirementSafetyTests(TestCase):
    setUp = test_document_sections.DocumentSectionsTests.setUp

    def url(self, name, pk):
        return reverse("ai_assistant:" + name, args=[pk])

    def req_data(self, **changes):
        return {
            "title": self.requirement.title,
            "requirement": self.requirement.requirement,
            "change_summary": "验证变更",
            "edit_token": edit_token(self.requirement, self.author),
            **changes,
        }

    def task_data(self, **changes):
        return {
            "request": self.requirement.pk,
            "title": self.task.title,
            "description": self.task.description,
            "acceptance": self.task.acceptance,
            "status": "todo",
            "priority": "P3",
            "edit_token": edit_token(self.task, self.author),
            **changes,
        }

    def baseline(self, route, pk):
        page = self.client.get(self.url(route, pk), secure=True)
        self.assertEqual(page.status_code, 200)
        return page.context["form"]["baseline"].value()

    def test_stale_requirement_cannot_overwrite_latest_and_keeps_input(self):
        endpoint = self.url("edit_requirement", self.requirement.pk)
        page = self.client.get(endpoint, secure=True)
        original_token = page.wsgi_request.document_edit_token
        first = self.req_data(title="第一个编辑者的新标题", edit_token=original_token)
        self.assertEqual(self.client.post(endpoint, first, secure=True).status_code, 302)
        stale = self.req_data(requirement="第二个编辑者未保存的输入", edit_token=original_token)
        page = self.client.post(endpoint, stale, secure=True)
        self.assertContains(page, "未保存本次修改")
        self.assertContains(page, "第二个编辑者未保存的输入")
        self.assertContains(page, 'data-document-unsaved="true"')
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.title, "第一个编辑者的新标题")
        self.assertEqual(self.requirement.version, 2)
        self.assertEqual(page.wsgi_request.document_edit_token, original_token)

    def test_missing_or_tampered_edit_token_cannot_save(self):
        before = self.requirement.title
        for token in ("", "forged"):
            page = self.client.post(
                self.url("edit_requirement", self.requirement.pk),
                self.req_data(title="不能覆盖", edit_token=token),
                secure=True,
            )
            self.assertContains(page, "未保存本次修改")
        self.requirement.refresh_from_db()
        self.assertEqual(self.requirement.title, before)

    def test_metadata_change_preserves_revision_analysis_and_review_flags(self):
        self.requirement.analysis = {"saved": True}
        self.requirement.save()
        draft = AITestCaseDraft.objects.create(
            request=self.requirement, case_number="SAFE", summary="原用例"
        )
        page = self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            self.req_data(priority="P1", status="confirmed", assigned_to=self.member.pk),
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        self.requirement.refresh_from_db()
        self.task.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(self.requirement.version, 1)
        self.assertEqual(self.requirement.analysis, {"saved": True})
        self.assertEqual(self.requirement.priority, "P1")
        self.assertFalse(self.requirement.needs_case_review)
        self.assertFalse(self.task.needs_update)
        self.assertFalse(draft.needs_update)

    def test_content_change_marks_tasks_and_cases_and_preserves_source_revision(self):
        draft = AITestCaseDraft.objects.create(
            request=self.requirement, case_number="SAFE", summary="原用例"
        )
        page = self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            self.req_data(requirement="新增必须验证的业务规则"),
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        self.requirement.refresh_from_db()
        self.task.refresh_from_db()
        draft.refresh_from_db()
        self.assertEqual(self.requirement.version, 2)
        self.assertTrue(self.task.needs_update)
        self.assertTrue(draft.needs_update)
        self.assertEqual(self.task.requirement_version, 1)
        self.assertFalse(AIJob.objects.exists())

    def test_target_product_version_is_a_business_revision(self):
        page = self.client.post(
            self.url("edit_requirement", self.requirement.pk),
            self.req_data(target_version=self.release.pk),
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        self.requirement.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.requirement.version, 2)
        self.assertTrue(self.task.needs_update)

    def test_task_save_does_not_implicitly_recheck_and_stale_save_is_blocked(self):
        AIRequest.objects.filter(pk=self.requirement.pk).update(version=2)
        self.requirement.refresh_from_db()
        self.task.needs_update = True
        self.task.save()
        endpoint = self.url("edit_dev_task", self.task.pk)
        stale = self.task_data(description="已经修订说明")
        self.assertEqual(self.client.post(endpoint, stale, secure=True).status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.needs_update)
        self.assertEqual(self.task.requirement_version, 1)
        page = self.client.post(endpoint, {**stale, "title": "旧页面覆盖"}, secure=True)
        self.assertContains(page, "未保存本次修改")
        self.task.refresh_from_db()
        self.assertNotEqual(self.task.title, "旧页面覆盖")

    def test_explicit_task_recheck_records_snapshot_and_does_not_rewrite_document(self):
        AIRequest.objects.filter(pk=self.requirement.pk).update(version=2)
        self.task.needs_update = True
        self.task.save()
        original = self.task.description
        token = self.baseline("dev_task_recheck", self.task.pk)
        page = self.client.post(
            self.url("dev_task_recheck", self.task.pk),
            {"baseline": token, "confirmed": "on"},
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.requirement_version, 2)
        self.assertFalse(self.task.needs_update)
        self.assertEqual(self.task.description, original)
        record = self.task.source_reviews.get()
        self.assertEqual(record.snapshot["target"]["requirement_version"], 1)
        self.assertEqual(record.reviewed_by, self.author)
        self.client.post(
            self.url("dev_task_recheck", self.task.pk),
            {"baseline": self.baseline("dev_task_recheck", self.task.pk), "confirmed": "on"},
            secure=True,
        )
        self.assertEqual(self.task.source_reviews.count(), 1)
        self.task.description = "后来修改"
        self.task.save()
        record.refresh_from_db()
        self.assertEqual(record.snapshot["target"]["description"], original)

    def test_task_recheck_rejects_changed_source_or_unconfirmed_submission(self):
        self.task.needs_update = True
        self.task.save()
        token = self.baseline("dev_task_recheck", self.task.pk)
        page = self.client.post(
            self.url("dev_task_recheck", self.task.pk), {"baseline": token}, secure=True
        )
        self.assertEqual(page.status_code, 200)
        AIRequest.objects.filter(pk=self.requirement.pk).update(version=2)
        page = self.client.post(
            self.url("dev_task_recheck", self.task.pk),
            {"baseline": token, "confirmed": "on"},
            secure=True,
        )
        self.assertContains(page, "未确认复核")
        self.task.refresh_from_db()
        self.assertTrue(self.task.needs_update)
        self.assertFalse(DocumentSourceReview.objects.exists())

    def test_assignee_status_permission_does_not_grant_source_recheck(self):
        self.task.assignee = self.member
        self.task.save()
        self.client.force_login(self.member)
        for method in (self.client.get, self.client.post):
            page = method(self.url("dev_task_recheck", self.task.pk), secure=True)
            self.assertEqual(page.status_code, 403)
        self.assertFalse(DocumentSourceReview.objects.exists())


class CaseSourceSafetyTests(TestCase):
    setUp = test_directory_tools.DirectoryToolsTests.setUp

    def create_source(self):
        self.source = AIRequest.objects.create(
            created_by=self.user,
            category=self.manual.category,
            title="测试需求",
            requirement="验证说明",
            version=2,
            needs_case_review=True,
        )
        self.draft = AITestCaseDraft.objects.create(
            request=self.source,
            imported_case=self.manual,
            case_number="SAFE",
            summary="旧标题",
            requirement_version=1,
            needs_update=True,
            source_context={"origin": "manual", "version": 1, "title": "历史依据"},
        )

    def url(self, name):
        return reverse("ai_assistant:" + name, args=[self.manual.pk])

    def baseline(self):
        return (
            self.client.get(self.url("scenario_recheck"), secure=True)
            .context["form"]["baseline"]
            .value()
        )

    def payload(self, **changes):
        return {
            **test_scenario_design.DesignPageTests.payload(self),
            "edit_token": edit_token(self.manual, self.user),
            **changes,
        }

    def test_stale_native_case_is_blocked_without_new_history(self):
        data = self.payload(summary="先保存的名称")
        self.assertEqual(
            self.client.post(self.url("scenario_edit"), data, secure=True).status_code, 302
        )
        self.manual.refresh_from_db()
        history_count = self.manual.history.count()
        page = self.client.post(
            self.url("scenario_edit"), {**data, "summary": "旧页面覆盖"}, secure=True
        )
        self.assertContains(page, "未保存本次修改")
        self.manual.refresh_from_db()
        self.assertEqual(self.manual.summary, "先保存的名称")
        self.assertEqual(self.manual.history.count(), history_count)
        self.assertContains(page, 'data-document-unsaved="true"')

    def test_case_save_requires_separate_recheck_and_original_snapshot_survives(self):
        self.create_source()
        original = dict(self.draft.source_context)
        self.assertEqual(
            self.client.post(
                self.url("scenario_edit"), self.payload(summary="人工修订名称"), secure=True
            ).status_code,
            302,
        )
        self.draft.refresh_from_db()
        self.assertTrue(self.draft.needs_update)
        token = self.baseline()
        page = self.client.post(
            self.url("scenario_recheck"), {"baseline": token, "confirmed": "on"}, secure=True
        )
        self.assertEqual(page.status_code, 302)
        self.draft.refresh_from_db()
        self.source.refresh_from_db()
        self.assertFalse(self.draft.needs_update)
        self.assertFalse(self.source.needs_case_review)
        self.assertEqual(self.draft.requirement_version, 2)
        self.assertEqual(self.draft.summary, "人工修订名称")
        self.assertEqual(self.draft.steps[0]["data"], "wrong-only")
        self.assertEqual(self.draft.source_context, original)
        record = self.manual.source_reviews.get()
        self.assertEqual(record.snapshot["draft"]["summary"], "旧标题")
        self.assertEqual(record.snapshot["source"]["version"], 2)

    def test_case_cannot_recheck_with_outdated_dev_document(self):
        self.create_source()
        task = AIDevTask.objects.create(
            request=self.source,
            owner=self.user,
            title="开发说明",
            requirement_version=1,
            needs_update=True,
        )
        self.draft.dev_tasks.add(task)
        page = self.client.post(
            self.url("scenario_recheck"),
            {"baseline": self.baseline(), "confirmed": "on"},
            secure=True,
        )
        self.assertContains(page, "请先完成相关开发任务复核")
        self.draft.refresh_from_db()
        self.assertTrue(self.draft.needs_update)
        self.assertFalse(DocumentSourceReview.objects.exists())

    def test_case_recheck_stale_case_or_source_cannot_clear_flags(self):
        self.create_source()
        token = self.baseline()
        NativeCase.objects.filter(pk=self.manual.pk).update(summary="另一个编辑者修改")
        page = self.client.post(
            self.url("scenario_recheck"), {"baseline": token, "confirmed": "on"}, secure=True
        )
        self.assertContains(page, "未确认复核")
        self.draft.refresh_from_db()
        self.assertTrue(self.draft.needs_update)
        self.assertFalse(DocumentSourceReview.objects.exists())

    def test_other_outdated_case_keeps_requirement_flag(self):
        self.create_source()
        AITestCaseDraft.objects.create(
            request=self.source,
            case_number="OTHER",
            summary="未复核",
            needs_update=True,
            requirement_version=1,
        )
        self.assertEqual(
            self.client.post(
                self.url("scenario_recheck"),
                {"baseline": self.baseline(), "confirmed": "on"},
                secure=True,
            ).status_code,
            302,
        )
        self.source.refresh_from_db()
        self.assertTrue(self.source.needs_case_review)

    def test_task_content_changes_mark_related_case_but_status_changes_do_not(self):
        self.create_source()
        self.draft.needs_update = False
        self.draft.requirement_version = 2
        self.draft.save()
        task = AIDevTask.objects.create(
            request=self.source, owner=self.user, title="开发说明", requirement_version=2
        )
        self.draft.dev_tasks.add(task)
        endpoint = reverse("ai_assistant:edit_dev_task", args=[task.pk])

        def data(**changes):
            return {
                "request": self.source.pk,
                "title": task.title,
                "description": task.description,
                "acceptance": task.acceptance,
                "priority": "P3",
                "status": "doing",
                "edit_token": edit_token(task, self.user),
                **changes,
            }

        self.assertEqual(self.client.post(endpoint, data(), secure=True).status_code, 302)
        self.draft.refresh_from_db()
        task.refresh_from_db()
        self.assertFalse(self.draft.needs_update)
        self.assertEqual(
            self.client.post(endpoint, data(description="新增接口校验"), secure=True).status_code, 302
        )
        self.draft.refresh_from_db()
        self.assertTrue(self.draft.needs_update)
