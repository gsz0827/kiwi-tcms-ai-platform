from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from tcms.ai_assistant.leases import (
    API_RUN_LEASE_SECONDS,
    reclaim_stale_runs,
    stale_runs,
)
from tcms.ai_assistant.models import APIRun


class Command(BaseCommand):
    help = (
        "确认所有 Worker 已停止后，将指定接口任务标记为中断；不会重新发送请求。"
        "用 --stale 时改为巡检所有心跳过期的执行"
    )

    def add_arguments(self, parser):
        parser.add_argument("run_id", nargs="?")
        parser.add_argument("--stale", action="store_true", help="巡检心跳早已停止的执行")
        parser.add_argument("--lease-seconds", type=int, default=None,
                            help="配合 --stale：多久没有心跳算过期（默认 600 秒）")
        parser.add_argument("--workers-stopped", action="store_true")

    def handle(self, *args, **options):
        if not options["workers_stopped"]:
            raise CommandError("先停止所有连接该数据库的 Worker，再添加 --workers-stopped。")
        if not options["run_id"] and not options["stale"]:
            raise CommandError("请给出执行 ID，或用 --stale 巡检心跳过期的执行")
        if options["stale"]:
            return self.handle_stale(options)
        return self.handle_run_id(options)

    def handle_stale(self, options):
        lease = options["lease_seconds"]
        expired = list(stale_runs(lease_seconds=lease))
        if not expired:
            self.stdout.write("没有心跳过期的执行，未修改任何数据。")
            return
        reclaimed = reclaim_stale_runs(lease_seconds=lease)
        for run in reclaimed:
            self.stdout.write(f"{run.pk}: {run.status} -> 中断")
        self.stdout.write(
            f"已标记中断 {len(reclaimed)} 次执行（心跳超过 "
            f"{lease if lease is not None else API_RUN_LEASE_SECONDS} 秒未刷新），"
            "保留已有结果，没有重新发送任何请求。"
        )

    def handle_run_id(self, options):
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
            run.results.filter(status="pending").update(
                status="skipped", completed=run.completed,
                error="任务中断，未取得执行结果；请求可能已发送，请核对目标服务。",
            )
        self.stdout.write("已标记中断，保留已有结果，没有重新发送任何请求。")
