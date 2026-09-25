import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tcms.ai_assistant.models import AIJob


class Command(BaseCommand):
    help = "停止所有 AI worker 后，按任务 ID 恢复中断任务；默认只预览"

    def add_arguments(self, parser):
        parser.add_argument("job_ids", nargs="+", type=uuid.UUID)
        parser.add_argument("--apply", action="store_true", help="实际更新选中的任务")
        parser.add_argument(
            "--workers-stopped", action="store_true",
            help="确认所有 AI worker 已停止，避免与正在保存的业务结果竞争",
        )

    def handle(self, *args, **options):
        if options["apply"] and not options["workers_stopped"]:
            raise CommandError("请先停止所有 AI worker，再使用 --apply --workers-stopped")
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
