import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tcms.ai_assistant.leases import (
    AI_JOB_LEASE_SECONDS,
    API_RUN_LEASE_SECONDS,
    describe_reclaimed,
    reclaim_all,
    stale_jobs,
    stale_runs,
)
from tcms.ai_assistant.models import AIJob


class Command(BaseCommand):
    help = (
        "停止所有 AI worker 后恢复中断任务；默认只预览。"
        "给出任务 ID 时按 ID 恢复；用 --stale 时改为巡检所有心跳过期的任务"
    )

    def add_arguments(self, parser):
        parser.add_argument("job_ids", nargs="*", type=uuid.UUID)
        parser.add_argument("--apply", action="store_true", help="实际更新选中的任务")
        parser.add_argument(
            "--stale", action="store_true",
            help="不按 ID，而是找出心跳早已停止的任务（执行进程已不在）并标成中断",
        )
        parser.add_argument(
            "--lease-seconds", type=int, default=None,
            help="配合 --stale：多久没有心跳算过期（默认 600 秒）",
        )
        parser.add_argument(
            "--workers-stopped", action="store_true",
            help="确认所有 AI worker 已停止，避免与正在保存的业务结果竞争",
        )

    def handle(self, *args, **options):
        if not options["job_ids"] and not options["stale"]:
            raise CommandError("请给出任务 ID，或用 --stale 巡检心跳过期的任务")
        if options["apply"] and not options["workers_stopped"]:
            raise CommandError("请先停止所有 AI worker，再使用 --apply --workers-stopped")
        if options["stale"]:
            self.handle_stale(options)
        else:
            self.handle_job_ids(options)

    def handle_stale(self, options):
        lease = options["lease_seconds"]
        job_lease = lease if lease is not None else AI_JOB_LEASE_SECONDS
        run_lease = lease if lease is not None else API_RUN_LEASE_SECONDS
        if not options["apply"]:
            jobs = list(stale_jobs(lease_seconds=lease))
            runs = list(stale_runs(lease_seconds=lease))
            for job in jobs:
                self.stdout.write(f"{job.pk}: {job.status} -> 中断（心跳停在 {job.last_seen}）")
            for run in runs:
                self.stdout.write(f"接口执行 {run.pk}: {run.status} -> 中断（心跳停在 {run.last_seen}）")
            self.stdout.write(
                f"预览，未修改：{len(jobs)} 个 AI 任务与 {len(runs)} 次接口执行的心跳"
                f"已超过 {job_lease}/{run_lease} 秒；加 --apply --workers-stopped 实际恢复"
            )
            return
        reclaimed = reclaim_all(lease_seconds=lease)
        self.stdout.write(
            f"已恢复 {len(reclaimed['jobs'])} 个 AI 任务、{len(reclaimed['runs'])} 次接口执行"
        )
        summary = describe_reclaimed(reclaimed)
        if summary:
            self.stdout.write(summary)

    def handle_job_ids(self, options):
        with transaction.atomic():
            jobs = list(AIJob.objects.select_for_update().filter(
                pk__in=options["job_ids"], status__in=("running", "cancel_requested")
            ))
            for job in jobs:
                status = "cancelled" if job.status == "cancel_requested" else "failed"
                self.stdout.write(f"{job.pk}: {job.status} -> {status}")
                if options["apply"]:
                    job.status = status
                    job.stage = "中断任务已人工恢复，请核对已有结果后决定是否重试"
                    job.error_message = "管理员在停止所有 Worker 后恢复了中断任务"
                    job.completed = timezone.now()
                    job.save(update_fields=("status", "stage", "error_message", "completed"))
            self.stdout.write(
                f"{'已恢复' if options['apply'] else '预览，未修改'} {len(jobs)} 个任务"
            )
