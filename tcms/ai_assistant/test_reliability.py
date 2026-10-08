import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from tcms.management.models import Classification, Product

from .jobs import submit_requirement
from .models import AIJob, AIModelConfig, AIRequest, AIRequirementVersion


class SubmissionFixtures:
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="submission-owner")
        self.config = AIModelConfig.objects.create(
            owner=self.owner, name="test", model="test", is_active=True,
            api_base="https://example.test/v1",
        )
        product = Product.objects.create(
            name="Submission", classification=Classification.objects.create(name="Test")
        )
        self.category = product.category.get(name="--default--")
        self.data = {
            "title": "验证码登录", "requirement": "验证码五分钟有效",
            "category": self.category.pk, "action": "analyze",
            "submission_token": str(uuid.uuid4()),
        }
        self.client.force_login(self.owner)

    def post(self, **changes):
        return self.client.post(
            reverse("ai_assistant:index"), self.data | changes, secure=True
        )


class RequirementSubmissionTests(SubmissionFixtures, TestCase):
    def test_page_supplies_a_new_token_and_external_script(self):
        first = self.client.get(reverse("ai_assistant:requirement_new"), secure=True)
        second = self.client.get(reverse("ai_assistant:requirement_new"), secure=True)
        self.assertNotEqual(
            first.context["form"]["submission_token"].value(),
            second.context["form"]["submission_token"].value(),
        )
        self.assertContains(first, 'name="submission_token"')
        self.assertContains(first, "ai_assistant/requirement_form.js")

    def test_replay_returns_original_job_even_after_completion(self):
        first = self.post()
        self.assertEqual(first.status_code, 302)
        job = AIJob.objects.get()
        self.assertEqual(job.operation, "requirement_analysis")
        self.assertEqual(self.post().url, first.url)
        job.status = "completed"
        job.save(update_fields=("status",))
        self.assertEqual(self.post().url, first.url)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIRequirementVersion.objects.count(), 1)
        self.assertEqual(AIJob.objects.count(), 1)

    def test_new_form_can_intentionally_submit_the_same_content(self):
        self.post()
        second = self.post(submission_token=str(uuid.uuid4()))
        self.assertEqual(second.status_code, 302)
        self.assertEqual(AIRequest.objects.count(), 2)
        self.assertEqual(AIJob.objects.count(), 2)

    def test_token_cannot_be_reused_for_changed_content_or_action(self):
        self.post()
        for changes in ({"requirement": "改为十分钟"}, {"action": "generate"}):
            with self.subTest(changes=changes):
                response = self.post(**changes)
                self.assertContains(response, "这份表单已提交过其他内容")
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIJob.objects.count(), 1)

    def test_identical_tokens_are_scoped_to_the_account(self):
        first = self.post()
        other = get_user_model().objects.create_user(username="another-owner")
        AIModelConfig.objects.create(
            owner=other, name="test", model="test", is_active=True,
            api_base="https://example.test/v1",
        )
        self.client.force_login(other)
        second = self.post()
        self.assertEqual(second.status_code, 302)
        self.assertNotEqual(first.url, second.url)
        self.assertEqual(AIJob.objects.filter(owner=other).count(), 1)
        self.assertEqual(AIJob.objects.filter(owner=self.owner).count(), 1)

    def test_invalid_submission_never_creates_partial_records(self):
        for changes in (
            {"submission_token": ""}, {"submission_token": "invalid"},
            {"action": "unsupported"}, {"title": ""},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(**changes).status_code, 200)
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIRequirementVersion.objects.exists())
        self.assertFalse(AIJob.objects.exists())

    @patch("tcms.ai_assistant.jobs.enqueue_ai_job", side_effect=RuntimeError("入队失败"))
    def test_enqueue_failure_rolls_back_requirement_and_version(self, _enqueue):
        self.assertContains(self.post(), "入队失败")
        self.assertFalse(AIRequest.objects.exists())
        self.assertFalse(AIRequirementVersion.objects.exists())
        self.assertFalse(AIJob.objects.exists())


class WorkerRecoveryTests(TransactionTestCase):
    # The command closes old connections, just as a real standalone worker does.
    # An outer TestCase transaction would be invalidated on MariaDB.
    def setUp(self):
        owner = get_user_model().objects.create_user(username="worker-owner")
        self.jobs = {
            status: AIJob.objects.create(owner=owner, operation="connection_test", status=status)
            for status in ("running", "cancel_requested", "completed", "queued")
        }

    @patch("tcms.ai_assistant.management.commands.ai_worker.execute_next_job", return_value=False)
    def test_starting_another_worker_leaves_active_jobs_untouched(self, execute):
        call_command("ai_worker", once=True, stdout=StringIO())
        execute.assert_called_once()
        for status, job in self.jobs.items():
            job.refresh_from_db()
            self.assertEqual(job.status, status)

    def test_recovery_defaults_to_preview(self):
        output = StringIO()
        call_command("ai_recover_jobs", str(self.jobs["running"].pk), stdout=output)
        self.jobs["running"].refresh_from_db()
        self.assertEqual(self.jobs["running"].status, "running")
        self.assertIn("未修改", output.getvalue())

    @patch("tcms.ai_assistant.management.commands.ai_worker.execute_next_job")
    def test_stop_signal_finishes_current_iteration_without_claiming_again(self, execute):
        previous = signal.getsignal(signal.SIGTERM)

        def stop_during_execution():
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return True

        execute.side_effect = stop_during_execution
        call_command("ai_worker", stdout=StringIO())
        execute.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_recovery_requires_workers_to_be_stopped(self):
        with self.assertRaisesMessage(CommandError, "请先停止所有 AI worker"):
            call_command("ai_recover_jobs", str(self.jobs["running"].pk), apply=True)

    def test_recovery_only_changes_selected_interrupted_jobs(self):
        call_command(
            "ai_recover_jobs", str(self.jobs["running"].pk),
            str(self.jobs["completed"].pk), str(self.jobs["queued"].pk),
            apply=True, workers_stopped=True, stdout=StringIO(),
        )
        for original, job in self.jobs.items():
            job.refresh_from_db()
            self.assertEqual(job.status, "failed" if original == "running" else original)
        call_command(
            "ai_recover_jobs", str(self.jobs["cancel_requested"].pk),
            apply=True, workers_stopped=True, stdout=StringIO(),
        )
        self.jobs["cancel_requested"].refresh_from_db()
        self.assertEqual(self.jobs["cancel_requested"].status, "cancelled")


class ConcurrentSubmissionTests(SubmissionFixtures, TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_two_connections_create_one_requirement_and_one_job(self):
        barrier = Barrier(2)

        def submit():
            close_old_connections()
            try:
                owner = get_user_model().objects.get(pk=self.owner.pk)
                config = AIModelConfig.objects.get(pk=self.config.pk)
                cleaned = self.data | {
                    "category": self.category,
                    "submission_token": uuid.UUID(self.data["submission_token"]),
                }
                barrier.wait(timeout=10)
                job, created = submit_requirement(owner, cleaned, "requirement_analysis", config)
                return job.pk, created
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual(results[0][0], results[1][0])
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(AIRequest.objects.count(), 1)
        self.assertEqual(AIRequirementVersion.objects.count(), 1)
        self.assertEqual(AIJob.objects.count(), 1)
