"""租约与心跳：Worker 被强制结束（断电、崩溃）后，任务不该永远停在「执行中」。"""

import time
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from tcms.tests.factories import ProductFactory

from . import leases
from .api_runner import submit_run
from .jobs import execute_next_job
from .leases import AI_JOB_LEASE_SECONDS, WorkerHeartbeat, heartbeat_job
from .leases import reclaim_stale_jobs, reclaim_stale_runs, stale_runs
from .models import AIJob, AIModelConfig, APICase, APIEnvironment, APIRun


class LeaseFixtures:
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="lease-owner")
        self.config = AIModelConfig.objects.create(
            owner=self.owner, name="lease", model="lease", is_active=True,
            api_base="https://example.test/v1",
        )

    def job(self, status="running", age_seconds=7200, **extra):
        """造一个 status 为 status、心跳在 age_seconds 之前的任务单。"""
        return AIJob.objects.create(
            owner=self.owner, model_config=self.config, operation="connection_test",
            status=status, heartbeat=timezone.now() - timedelta(seconds=age_seconds),
            **extra,
        )


class StaleJobReclaimTests(LeaseFixtures, TestCase):
    def test_fresh_heartbeat_is_left_alone(self):
        job = self.job(age_seconds=1)
        self.assertEqual(reclaim_stale_jobs(), [])
        job.refresh_from_db()
        self.assertEqual(job.status, "running")

    def test_stale_running_job_becomes_interrupted(self):
        job = self.job(age_seconds=AI_JOB_LEASE_SECONDS + 60)
        reclaimed = reclaim_stale_jobs()
        self.assertEqual([item.pk for item in reclaimed], [job.pk])
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")
        self.assertTrue(job.is_terminal)
        self.assertIsNotNone(job.completed)
        self.assertIn("不会自动重放", job.error_message)
        self.assertIn("核对", job.stage)

    def test_stale_cancel_request_becomes_cancelled(self):
        job = self.job(status="cancel_requested")
        reclaim_stale_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")

    def test_open_and_finished_jobs_are_never_touched(self):
        queued = self.job(status="queued")
        completed = self.job(status="completed")
        failed = self.job(status="failed")
        self.assertEqual(reclaim_stale_jobs(), [])
        for job in (queued, completed, failed):
            job.refresh_from_db()
            self.assertIn(job.status, ("queued", "completed", "failed"))

    def test_job_without_heartbeat_falls_back_to_started(self):
        old = AIJob.objects.create(
            owner=self.owner, operation="connection_test", status="running",
            started=timezone.now() - timedelta(hours=3),
        )
        fresh = AIJob.objects.create(
            owner=self.owner, operation="connection_test", status="running",
            started=timezone.now(),
        )
        reclaimed = reclaim_stale_jobs()
        self.assertEqual([item.pk for item in reclaimed], [old.pk])
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, "running")

    def test_job_without_any_timestamp_is_judged_by_created(self):
        job = AIJob.objects.create(
            owner=self.owner, operation="connection_test", status="running"
        )
        AIJob.objects.filter(pk=job.pk).update(
            created=timezone.now() - timedelta(hours=3)
        )
        self.assertEqual([item.pk for item in reclaim_stale_jobs()], [job.pk])


class HeartbeatTests(LeaseFixtures, TestCase):
    def test_heartbeat_job_refreshes_only_open_jobs(self):
        running = self.job(age_seconds=3600)
        finished = self.job(status="completed", age_seconds=3600)
        self.assertEqual(heartbeat_job(running.pk), 1)
        running.refresh_from_db()
        self.assertLess((timezone.now() - running.heartbeat).total_seconds(), 5)
        self.assertEqual(heartbeat_job(finished.pk), 0)

    def test_worker_heartbeat_context_manager_beats_immediately(self):
        job = self.job(age_seconds=3600)
        with WorkerHeartbeat(job=job, interval=0):
            pass
        job.refresh_from_db()
        self.assertLess((timezone.now() - job.heartbeat).total_seconds(), 5)
        self.assertEqual(reclaim_stale_jobs(), [])

    @patch("tcms.ai_assistant.jobs.execute_job")
    def test_execute_next_job_runs_under_a_heartbeat(self, execute):
        job = AIJob.objects.create(
            owner=self.owner, model_config=self.config, operation="connection_test",
            status="queued",
        )
        seen = {}

        def capture(target):
            seen["heartbeat"] = AIJob.objects.get(pk=target.pk).heartbeat

        execute.side_effect = capture
        self.assertTrue(execute_next_job(heartbeat_interval=0))
        self.assertIsNotNone(seen["heartbeat"])
        job.refresh_from_db()
        self.assertEqual(job.status, "running")

    def test_execute_next_job_returns_false_on_an_empty_queue(self):
        self.assertFalse(execute_next_job(heartbeat_interval=0))


class WorkerHeartbeatThreadTests(TransactionTestCase):
    """心跳线程要能在主线程执行任务时持续刷新（需要真实提交，故用事务测试）。"""

    def test_background_thread_keeps_beating(self):
        owner = get_user_model().objects.create_user(username="lease-thread")
        job = AIJob.objects.create(
            owner=owner, operation="connection_test", status="running",
            heartbeat=timezone.now() - timedelta(hours=1),
        )
        beats = []
        real = leases.heartbeat_job

        def counting(job_id):
            beats.append(timezone.now())
            return real(job_id)

        with patch("tcms.ai_assistant.leases.heartbeat_job", side_effect=counting):
            with WorkerHeartbeat(job=job, interval=0.05):
                time.sleep(0.4)

        self.assertGreaterEqual(len(beats), 2, beats)
        job.refresh_from_db()
        self.assertLess((timezone.now() - job.heartbeat).total_seconds(), 5)


class StaleRunReclaimTests(LeaseFixtures, TestCase):
    def setUp(self):
        super().setUp()
        override = override_settings(API_AUTOMATION_ALLOWED_ORIGINS=["https://example.test"])
        override.enable()
        self.addCleanup(override.disable)
        self.product = ProductFactory()
        self.env = APIEnvironment.objects.create(
            owner=self.owner, product=self.product, name="租约环境",
            base_url="https://example.test", timeout=1,
        )
        self.case = APICase.objects.create(
            owner=self.owner, product=self.product, name="健康检查", path="/health"
        )

    def queued_run(self, status="running", age_seconds=7200):
        run = submit_run(self.owner, self.product, {
            "environment": self.env, "cases": [self.case],
            "submission_token": uuid.uuid4(),
        })
        APIRun.objects.filter(pk=run.pk).update(
            status=status,
            started=timezone.now() - timedelta(seconds=age_seconds),
            heartbeat=timezone.now() - timedelta(seconds=age_seconds),
        )
        run.refresh_from_db()
        return run

    def test_stale_run_is_marked_interrupted_and_pending_results_skipped(self):
        run = self.queued_run()
        self.assertEqual([item.pk for item in stale_runs()], [run.pk])
        reclaimed = reclaim_stale_runs()
        self.assertEqual([item.pk for item in reclaimed], [run.pk])
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")
        self.assertTrue(run.is_terminal)
        self.assertIn("不会自动重放", run.error)
        skipped = run.results.get(position=0)
        self.assertEqual(skipped.status, "skipped")
        self.assertIn("核对目标服务", skipped.error)
        self.assertIsNotNone(skipped.completed)

    def test_api_recover_runs_stale_mode_marks_the_run(self):
        run = self.queued_run()
        output = StringIO()
        call_command("api_recover_runs", stale=True, workers_stopped=True, stdout=output)
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")
        self.assertIn("没有重新发送任何请求", output.getvalue())

    def test_api_recover_runs_still_requires_workers_stopped(self):
        run = self.queued_run()
        with self.assertRaises(CommandError):
            call_command("api_recover_runs", str(run.pk), stdout=StringIO())


class WorkerCommandLeaseTests(LeaseFixtures, TransactionTestCase):
    @patch(
        "tcms.ai_assistant.management.commands.ai_worker.execute_next_api_run",
        return_value=False,
    )
    @patch(
        "tcms.ai_assistant.management.commands.ai_worker.execute_next_job",
        return_value=False,
    )
    def test_startup_sweep_marks_only_stale_jobs(self, job_queue, api_queue):
        stale = self.job()
        fresh = self.job(age_seconds=1)
        output = StringIO()
        call_command("ai_worker", once=True, lease_seconds=600, stdout=output)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(stale.status, "interrupted")
        self.assertEqual(fresh.status, "running")
        self.assertIn("启动巡检", output.getvalue())
        self.assertIn("不会自动重跑", output.getvalue())

    def test_recover_command_stale_mode_previews_before_applying(self):
        job = self.job()
        output = StringIO()
        call_command("ai_recover_jobs", stale=True, stdout=output)
        job.refresh_from_db()
        self.assertEqual(job.status, "running")
        self.assertIn("预览，未修改", output.getvalue())

        output = StringIO()
        call_command(
            "ai_recover_jobs", stale=True, apply=True, workers_stopped=True,
            stdout=output,
        )
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")
        self.assertIn("已恢复", output.getvalue())


class InterruptedJobViewTests(LeaseFixtures, TestCase):
    def test_interrupted_job_can_be_retried(self):
        job = self.job(status="interrupted")
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("ai_assistant:retry_job", args=[job.pk]), secure=True
        )
        self.assertEqual(response.status_code, 302)
        retry = AIJob.objects.exclude(pk=job.pk).get()
        self.assertEqual(retry.operation, job.operation)
        self.assertEqual(retry.attempts, 2)
        self.assertEqual(retry.status, "queued")

    def test_running_job_cannot_be_retried(self):
        job = self.job(status="running", age_seconds=1)
        self.client.force_login(self.owner)
        self.client.post(
            reverse("ai_assistant:retry_job", args=[job.pk]), secure=True
        )
        self.assertFalse(AIJob.objects.exclude(pk=job.pk).exists())

    def test_job_detail_page_offers_the_retry_button(self):
        job = self.job(status="interrupted")
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("ai_assistant:job_detail", args=[job.pk]), secure=True
        )
        self.assertContains(response, "中断")
        self.assertContains(response, reverse("ai_assistant:retry_job", args=[job.pk]))
