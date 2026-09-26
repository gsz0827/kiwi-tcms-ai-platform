import signal
import time

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tcms.ai_assistant.jobs import execute_next_job
from tcms.ai_assistant.api_runner import execute_next_api_run
from tcms.ai_assistant.leases import (
    SWEEP_INTERVAL_SECONDS,
    describe_reclaimed,
    reclaim_all,
)


class Command(BaseCommand):
    help = "运行 AI 助手及接口自动化数据库后台任务 worker"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="领取至多一个任务后退出")
        parser.add_argument("--interval", type=float, default=1.0, help="空队列轮询间隔秒数")
        parser.add_argument(
            "--lease-seconds", type=int, default=None,
            help="多久没有心跳就认为执行进程已消失（默认 600 秒）",
        )
        parser.add_argument(
            "--sweep-interval", type=float, default=SWEEP_INTERVAL_SECONDS,
            help="巡检过期任务的间隔秒数，0 表示只巡检一次",
        )

    def handle(self, *args, **options):
        lease_seconds = options["lease_seconds"]
        sweep_interval = max(0.0, options["sweep_interval"])

        def sweep(label):
            reclaimed = reclaim_all(lease_seconds=lease_seconds)
            summary = describe_reclaimed(reclaimed)
            if summary:
                self.stdout.write(self.style.WARNING(
                    f"{label}：{summary}。中断的任务不会自动重跑，请核对后自行重试。"
                ))
            return summary

        self.stdout.write(self.style.SUCCESS("Worker 已启动（AI 任务 / 接口自动化）"))

        # 启动巡检只处理「心跳早就停了」的任务，即执行进程已经不在（断电、被强杀、崩溃）。
        # 其他 worker 正在处理的任务心跳是新鲜的，不会被碰；这里也从不重放任务：
        # 只把状态标成中断，是否重来由用户在页面上决定。人工按 ID 恢复的命令
        # （ai_recover_jobs）仍然保留，作为 worker 停着时的手工兜底。
        sweep("启动巡检")

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
            last_sweep = time.monotonic()
            while not stopping:
                close_old_connections()
                if sweep_interval and time.monotonic() - last_sweep >= sweep_interval:
                    sweep("巡检")
                    last_sweep = time.monotonic()
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
