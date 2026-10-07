import signal
import threading
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from tcms.allure_reporting.worker import claim_report, generate_report, recover_reports


class Command(BaseCommand):
    help = "独立生成 Web/API 的 Allure 报告，不重新执行测试"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")

    def handle(self, *args, **options):
        stop = threading.Event()
        handlers = {
            sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        self.stdout.write("Allure 报告 worker 已启动")
        try:
            while not stop.is_set():
                close_old_connections()
                recover_reports()
                pk = claim_report()
                if pk:
                    generate_report(pk)
                if options["once"]:
                    return
                if not pk:
                    stop.wait(2)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
