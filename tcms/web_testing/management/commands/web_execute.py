from django.core.management.base import BaseCommand
from tcms.web_testing.runner import execute


class Command(BaseCommand):
    help = "执行已领取的 Web 自动化任务（供 worker 调用）"

    def add_arguments(self, parser):
        parser.add_argument("run_id")

    def handle(self, *args, **options):
        execute(options["run_id"])
