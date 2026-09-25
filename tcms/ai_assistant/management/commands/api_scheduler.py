import signal
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from tcms.ai_assistant.api_scheduling import dispatch_due_suites


class Command(BaseCommand):
    help = "检查到期自动化套件并提交到 Worker 队列"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stopping = threading.Event()
        previous = {s: signal.signal(s, lambda *_: stopping.set())
                    for s in (signal.SIGTERM, signal.SIGINT)}
        self.stdout.write("自动化调度器已启动")
        try:
            while not stopping.is_set():
                close_old_connections()
                dispatch_due_suites()
                close_old_connections()
                if options["once"]:
                    break
                stopping.wait(10)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
