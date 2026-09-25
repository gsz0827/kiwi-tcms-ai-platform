import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tcms.ai_assistant.jobs import execute_next_job
from tcms.ai_assistant.api_runner import execute_next_api_run


class Command(BaseCommand):
    help = "运行 AI 助手及接口自动化数据库后台任务 worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="领取至多一个任务后退出")
        parser.add_argument("--interval", type=float, default=1.0, help="空队列轮询间隔秒数")

    def handle(self, *args, **options):
        # A different worker may still own these jobs. Recovery is an explicit
        # maintenance operation (ai_recover_jobs), never a startup side effect.
        self.stdout.write(self.style.SUCCESS("Worker 已启动（AI 任务 / 接口自动化）"))

        stopping = False

        def request_stop(_signum, _frame):
            nonlocal stopping
            stopping = True
            self.stdout.write("正在停止 Worker：完成当前任务后退出，不再领取新任务。")

        previous_handlers = {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        try:
            prefer_api = True
            while not stopping:
                close_old_connections()
                queues = (
                    (execute_next_api_run, execute_next_job)
                    if prefer_api
                    else (execute_next_job, execute_next_api_run)
                )
                processed = queues[0]()
                if not processed and not stopping:
                    processed = queues[1]()
                prefer_api = not prefer_api
                close_old_connections()
                if options["once"]:
                    return
                if not processed and not stopping:
                    time.sleep(max(0.2, options["interval"]))
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
