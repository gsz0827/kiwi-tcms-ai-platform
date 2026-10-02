import os
import signal
import subprocess
import sys
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from tcms.web_testing.models import WebRun
from tcms.web_testing.runner import claim_run, recover_stale


class Command(BaseCommand):
    help = "运行独立的 Web 自动化队列，每个任务最长 330 秒"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
        self.stdout.write("Web 自动化 worker 已启动")
        try:
            while not stop.is_set():
                close_old_connections()
                recover_stale()
                pk = claim_run()
                if pk:
                    try:
                        result = subprocess.run([sys.executable, os.path.abspath("manage.py"), "web_execute", str(pk)], timeout=330, check=False)
                        if result.returncode:
                            WebRun.objects.filter(pk=pk, status="running").update(status="interrupted", finished=timezone.now(), error="执行进程异常退出，可手动重新执行。")
                    except subprocess.TimeoutExpired:
                        WebRun.objects.filter(pk=pk, status="running").update(status="interrupted", finished=timezone.now(), error="任务超过 330 秒上限，已终止执行。")
                if options["once"]: return
                if not pk: stop.wait(2)
        finally:
            for sig, handler in previous.items(): signal.signal(sig, handler)
