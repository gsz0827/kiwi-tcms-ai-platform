import json
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.urls import reverse

from . import roles, test_scenario_design
from .case_design_context import context_snapshot
from .crypto import encrypt_api_key
from .jobs import execute_job
from .models import AIDevTask, AIJob, AIModelConfig, AIRequest, AITestCaseDraft

DRAFT = dict(
    case_number="TC-1",
    summary="验证码过期",
    priority="P2",
    test_type="边界值测试",
    preconditions=["已有验证码"],
    steps=[{"action": "使用过期验证码", "expected": "拒绝登录"}],
)


class CaseDesignEntryTests(TestCase):
    payload = test_scenario_design.DesignPageTests.payload

    def setUp(self):
        test_scenario_design.DesignPageTests.setUp(self)
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="testcases", codename="add_testcase")
        )
        self.source = AIRequest.objects.create(
            created_by=self.user,
            title="验证码登录",
            requirement="验证码 60 秒后失效",
            category=self.manual.category,
        )
        self.task = AIDevTask.objects.create(
            request=self.source,
            owner=self.user,
            task_number="DEV-1",
            title="登录接口",
            description="校验验证码",
            acceptance="过期验证码不能登录",
        )
        self.config = AIModelConfig.objects.create(
            owner=self.user,
            name="mock",
            model="example",
            api_base="https://model.example.test/v1",
            api_key_encrypted=encrypt_api_key("fake-only"),
            is_active=True,
        )

    def url(self):
        return reverse("ai_assistant:case_design", args=[self.source.pk])

    def submit(self, tasks=None, action="generate", **changes):
        data = dict(
            action=action,
            dev_tasks=[t.pk for t in (tasks or [])],
            context_fingerprint=context_snapshot(self.source, list(self.source.dev_tasks.all()))[
                "fingerprint"
            ],
        )
        return self.client.post(self.url(), data | changes, secure=True)

    def run_job(self, call=None):
        job = AIJob.objects.latest("created")
        job.status = "running"
        job.save(update_fields=["status"])
        with patch(
            "tcms.ai_assistant.jobs.generate_test_cases",
            side_effect=call,
            return_value=[DRAFT.copy()],
        ) as mocked:
            execute_job(job)
        job.refresh_from_db()
        return job, mocked

    def test_requirement_and_task_open_one_page_with_optional_selected_task(self):
        page = self.client.get(self.url(), secure=True)
        self.assertContains(page, "人工编写用例")
        self.assertContains(page, self.source.requirement)
        self.assertFalse(page.context["form"].initial.get("dev_tasks"))
        page = self.client.get(self.url(), {"task": self.task.pk}, secure=True)
        self.assertEqual(page.context["form"].initial["dev_tasks"], [self.task.pk])
        self.assertContains(page, 'id="id_dev_tasks_0" checked')
        trace = self.client.get(
            reverse("ai_assistant:requirement_trace", args=[self.source.pk]), secure=True
        )
        self.assertContains(trace, self.url())
        tasks = self.client.get(reverse("ai_assistant:dev_task_list"), secure=True)
        self.assertContains(tasks, self.url() + f"?task={self.task.pk}")

    def test_selected_documents_are_frozen_and_linked_to_drafts(self):
        self.submit([self.task])
        job, mocked = self.run_job()
        self.assertEqual(job.status, "completed", job.error_message)
        self.assertIn("#design-drafts", job.result_url)
        self.assertEqual(mocked.call_args.kwargs["dev_task_context"][0]["description"], "校验验证码")
        draft = self.source.drafts.get()
        self.assertEqual(list(draft.dev_tasks.all()), [self.task])
        self.assertEqual(draft.source_context["requirement"], self.source.requirement)
        self.task.description = "后来修改的开发文档"
        self.task.save()
        draft.refresh_from_db()
        self.assertEqual(draft.source_context["dev_tasks"][0]["description"], "校验验证码")

    def test_no_document_is_required_and_queued_replay_is_idempotent(self):
        first = self.submit()
        second = self.submit()
        self.assertEqual(first.url, second.url)
        self.assertEqual(AIJob.objects.count(), 1)
        job, mocked = self.run_job()
        self.assertEqual(job.status, "completed", job.error_message)
        self.assertEqual(mocked.call_args.kwargs["dev_task_context"], [])
        self.submit()
        self.assertEqual(AIJob.objects.count(), 1)

    def test_new_document_scope_appends_instead_of_replacing_existing_drafts(self):
        old = AITestCaseDraft.objects.create(request=self.source, **DRAFT)
        self.submit([self.task])
        job, _ = self.run_job()
        self.assertEqual(job.status, "completed", job.error_message)
        self.assertEqual(self.source.drafts.count(), 2)
        old.refresh_from_db()
        self.assertEqual(old.summary, DRAFT["summary"])
        self.assertEqual(len(set(self.source.drafts.values_list("case_number", flat=True))), 2)

    def test_foreign_requirement_task_is_rejected_without_queueing(self):
        other_source = AIRequest.objects.create(
            created_by=self.user, title="另一需求", requirement="另一需求"
        )
        other = AIDevTask.objects.create(request=other_source, owner=self.user, title="不同需求任务")
        page = self.submit([other])
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.context["form"].errors)
        self.assertFalse(AIJob.objects.exists())
        self.assertEqual(
            self.client.get(self.url(), {"task": other.pk}, secure=True).status_code, 404
        )

    def test_changed_document_before_execution_never_calls_model(self):
        self.submit([self.task])
        AIDevTask.objects.filter(pk=self.task.pk).update(description="changed")
        job, mocked = self.run_job()
        self.assertEqual(job.status, "failed")
        mocked.assert_not_called()
        self.assertFalse(self.source.drafts.exists())

    def test_changed_document_during_model_call_rejects_result(self):
        self.submit([self.task])

        def change(*args, **kwargs):
            AIDevTask.objects.filter(pk=self.task.pk).update(acceptance="新的验收标准")
            return [DRAFT.copy()]

        job, _ = self.run_job(change)
        self.assertEqual(job.status, "failed")
        self.assertFalse(self.source.drafts.exists())

    def test_shared_requirement_uses_callers_model_not_authors_model(self):
        roles.add_product_member(self.other, self.product)
        self.source.created_by = self.other
        self.source.save()
        self.submit([self.task])
        job, _ = self.run_job()
        self.assertEqual(job.status, "completed", job.error_message)
        self.assertEqual(job.owner_id, self.user.pk)
        self.assertEqual(job.model_config_id, self.config.pk)

    def test_readonly_and_nonmember_cannot_submit(self):
        self.user.groups.add(Group.objects.get_or_create(name=roles.ROLE_VIEWER)[0])
        self.assertEqual(self.submit([self.task]).status_code, 403)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url(), secure=True).status_code, 404)
        self.assertFalse(AIJob.objects.exists())

    def test_stale_page_or_missing_model_does_not_queue(self):
        self.assertEqual(self.submit(context_fingerprint="stale").status_code, 200)
        self.assertFalse(AIJob.objects.exists())
        self.config.delete()
        self.assertEqual(self.submit().url, reverse("ai_assistant:model_settings"))
        self.assertFalse(AIJob.objects.exists())

    def test_human_confirmation_retains_development_links(self):
        self.submit([self.task])
        self.run_job()
        draft = self.source.drafts.get()
        page = self.client.post(
            reverse("ai_assistant:scenario_confirm", args=[self.source.pk]),
            {"draft_ids": [draft.pk], "confirmed": "on"},
            secure=True,
        )
        self.assertEqual(page.status_code, 302)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.imported_case_id)
        self.assertTrue(draft.dev_tasks.filter(pk=self.task.pk).exists())
        self.assertContains(
            self.client.get(
                reverse("ai_assistant:scenario_detail", args=[draft.imported_case_id]), secure=True
            ),
            "DEV-1",
        )

    def test_manual_case_uses_standard_editor_and_keeps_both_links(self):
        page = self.submit([self.task], action="manual")
        self.assertEqual(page.status_code, 302)
        editor = self.client.get(page.url, secure=True)
        self.assertEqual(editor.context["design_source"], self.source)
        self.assertEqual(editor.context["form"].initial["category"], self.source.category_id)
        self.assertIn(self.source.title, editor.context["form"].initial["requirement"])
        data = self.payload(summary="人工设计的过期场景") | dict(
            design_request=self.source.pk,
            dev_tasks=[self.task.pk],
            design_fingerprint=context_snapshot(self.source, [self.task])["fingerprint"],
        )
        saved = self.client.post(page.url, data, secure=True)
        self.assertEqual(saved.status_code, 302)
        draft = self.source.drafts.get()
        self.assertEqual(draft.source_context["origin"], "manual")
        self.assertEqual(draft.imported_case.summary, "人工设计的过期场景")
        self.assertTrue(draft.dev_tasks.filter(pk=self.task.pk).exists())

    def test_manual_save_rejects_document_changed_since_editor_opened(self):
        page = self.submit([self.task], action="manual")
        snapshot = context_snapshot(self.source, [self.task])
        AIDevTask.objects.filter(pk=self.task.pk).update(description="新的文档")
        data = self.payload(summary="不应保存") | dict(
            design_request=self.source.pk,
            dev_tasks=[self.task.pk],
            design_fingerprint=snapshot["fingerprint"],
        )
        saved = self.client.post(page.url, data, secure=True)
        self.assertEqual(saved.status_code, 200)
        self.assertContains(saved, "需求或开发文档已变更")
        self.assertFalse(self.source.drafts.exists())

    def test_revoked_visibility_before_execution_never_calls_model(self):
        roles.add_product_member(self.other, self.product)
        self.source.created_by = self.other
        self.source.save()
        self.submit([self.task])
        roles.remove_product_member(self.user, self.product)
        job, mocked = self.run_job()
        self.assertEqual(job.status, "failed")
        mocked.assert_not_called()
        self.assertFalse(self.source.drafts.exists())


class DesignPromptTests(TestCase):
    @patch("tcms.ai_assistant.services._request_ai_content", return_value=(json.dumps([DRAFT]), None))
    def test_prompt_includes_docs_without_replacing_business_requirement(self, mocked):
        from .services import generate_test_cases

        generate_test_cases(
            "登录",
            "60 秒后验证码过期",
            None,
            dev_task_context=[{"title": "接口", "description": "实现校验"}],
        )
        prompt = mocked.call_args.args[2]
        self.assertIn("60 秒后验证码过期", prompt)
        self.assertIn("实现校验", prompt)
        self.assertIn("不要把实现描述当作预期结果", prompt)
