from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tcms.ai_assistant.models import APIRun


class Command(BaseCommand):
    help = "确认所有 Worker 已停止后，将指定接口任务标记为中断；不会重新发送请求"

    def add_arguments(self, parser):
        parser.add_argument("run_id")
        parser.add_argument("--workers-stopped", action="store_true")

    def handle(self, *args, **options):
        if not options["workers_stopped"]:
            raise CommandError("先停止所有连接该数据库的 Worker，再添加 --workers-stopped。")
        with transaction.atomic():
            run = APIRun.objects.select_for_update().filter(
                pk=options["run_id"], status__in=("running", "cancel_requested")
            ).first()
            if not run:
                raise CommandError("未找到指定的执行中任务。")
            run.status = "interrupted"
            run.completed = timezone.now()
            run.error = "Worker 中断。未保存结果的请求可能已经发送，请人工核对目标服务；不会自动重放。"
            run.save(update_fields=("status", "completed", "error"))
        self.stdout.write("已标记中断，保留已有结果，没有重新发送任何请求。")
