import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from tcms.ai_assistant.jobs import execute_next_job
from tcms.ai_assistant.models import AIJob


class Command(BaseCommand):
    help = "运行 AI 助手数据库后台任务 worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="领取至多一个任务后退出")
        parser.add_argument("--interval", type=float, default=1.0, help="空队列轮询间隔秒数")

    def handle(self, *args, **options):
        now = timezone.now()
        AIJob.objects.filter(status="running").update(
            status="failed",
            stage="Worker 重启，原任务已中断，可手动重试",
            error_message="后台 Worker 在任务执行期间重启",
            completed=now,
        )
        AIJob.objects.filter(status="cancel_requested").update(
            status="cancelled",
            progress=100,
            stage="任务已取消",
            completed=now,
        )
        self.stdout.write(self.style.SUCCESS("AI worker 已启动"))

        while True:
            close_old_connections()
            processed = execute_next_job()
            close_old_connections()
            if options["once"]:
                return
            if not processed:
                time.sleep(max(0.2, options["interval"]))
