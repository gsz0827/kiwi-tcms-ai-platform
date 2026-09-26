"""后台任务的租约与心跳。

worker 领取任务后会持续刷新 ``AIJob.heartbeat`` / ``APIRun.heartbeat``。进程被强制结束
（断电、``docker kill``、OOM、宿主机重启）之后心跳就停了，任务会永远停在「执行中」，
只能靠人工恢复命令救。本模块把「租约过期」翻译成一个明确的状态：**中断**。

不自动重放是刻意的：模型调用和接口请求都可能已经产生副作用，是否重来必须由人决定。
所以这里只做三件事——刷新心跳、把失去心跳的任务标成中断、让中断的任务可以重试。
"""

import threading
from datetime import timedelta

from django.db import close_old_connections, connections, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import AIJob, APIRun

#: 心跳线程的刷新间隔（秒）。远小于租约时长，允许进程短暂卡顿。
HEARTBEAT_INTERVAL_SECONDS = 20
#: 超过这个时间没有心跳，就认为执行进程已经不在了。
AI_JOB_LEASE_SECONDS = 600
API_RUN_LEASE_SECONDS = 600
#: worker 空闲时巡检过期任务的间隔。
SWEEP_INTERVAL_SECONDS = 60

ACTIVE_STATUSES = ("running", "cancel_requested")

JOB_INTERRUPTED_STAGE = "任务已中断，请核对已有结果后决定是否重试"
JOB_INTERRUPTED_ERROR = (
    "执行进程在任务完成前停止（断电、被强制结束或崩溃），任务没有跑完。"
    "已经产生的数据不会回滚，也不会自动重放，请核对后决定是否重试。"
)
JOB_CANCELLED_ERROR = "取消请求已记录，但执行进程在确认前已停止"
RUN_INTERRUPTED_ERROR = "执行中断。已发送的请求不会自动重放，请核对已有结果后再创建新任务。"
RUN_SKIPPED_ERROR = "任务中断，未取得执行结果；请求可能已发送，请核对目标服务。"


def heartbeat_job(job_id):
    """刷新一条 AI 任务的心跳；任务已经结束或不存在时返回 0。"""
    return AIJob.objects.filter(pk=job_id, status__in=ACTIVE_STATUSES).update(
        heartbeat=timezone.now()
    )


def heartbeat_run(run_id):
    """刷新一次接口自动化执行的心跳；执行已经结束时返回 0。"""
    return APIRun.objects.filter(pk=run_id, status__in=ACTIVE_STATUSES).update(
        heartbeat=timezone.now()
    )


def _lease_deadline(lease_seconds, now):
    return now - timedelta(seconds=lease_seconds)


def stale_jobs(lease_seconds=None, now=None):
    """心跳已经超时的 AI 任务（只读，用于预览）。"""
    now = now or timezone.now()
    lease_seconds = AI_JOB_LEASE_SECONDS if lease_seconds is None else lease_seconds
    return (
        AIJob.objects.annotate(last_seen=Coalesce("heartbeat", "started", "created"))
        .filter(status__in=ACTIVE_STATUSES)
        .filter(last_seen__lt=_lease_deadline(lease_seconds, now))
        .order_by("created")
    )


def stale_runs(lease_seconds=None, now=None):
    """心跳已经超时的接口自动化执行（只读，用于预览）。"""
    now = now or timezone.now()
    lease_seconds = API_RUN_LEASE_SECONDS if lease_seconds is None else lease_seconds
    return (
        APIRun.objects.annotate(last_seen=Coalesce("heartbeat", "started", "created"))
        .filter(status__in=ACTIVE_STATUSES)
        .filter(last_seen__lt=_lease_deadline(lease_seconds, now))
        .order_by("created")
    )


def reclaim_stale_jobs(lease_seconds=None, now=None):
    """把心跳超时的 AI 任务标成中断 / 已取消，返回被处理的 AIJob 列表。"""
    now = now or timezone.now()
    lease_seconds = AI_JOB_LEASE_SECONDS if lease_seconds is None else lease_seconds
    reclaimed = []
    with transaction.atomic():
        locked = stale_jobs(lease_seconds=lease_seconds, now=now).select_for_update()
        for job in locked:
            if job.status == "cancel_requested":
                job.status = "cancelled"
                job.stage = "任务已取消"
                AIJob.objects.filter(pk=job.pk).update(
                    status="cancelled", progress=100, stage=job.stage,
                    error_message=JOB_CANCELLED_ERROR, completed=now, heartbeat=now,
                )
            else:
                job.status = "interrupted"
                job.stage = JOB_INTERRUPTED_STAGE
                AIJob.objects.filter(pk=job.pk).update(
                    status="interrupted", stage=job.stage,
                    error_message=JOB_INTERRUPTED_ERROR, completed=now, heartbeat=now,
                )
            job.completed = now
            reclaimed.append(job)
    return reclaimed


def reclaim_stale_runs(lease_seconds=None, now=None):
    """把心跳超时的接口自动化执行标成中断，返回被处理的 APIRun 列表。"""
    now = now or timezone.now()
    lease_seconds = API_RUN_LEASE_SECONDS if lease_seconds is None else lease_seconds
    reclaimed = []
    with transaction.atomic():
        locked = stale_runs(lease_seconds=lease_seconds, now=now).select_for_update()
        for run in locked:
            if run.status == "cancel_requested":
                run.status = "cancelled"
                run.error = JOB_CANCELLED_ERROR
                APIRun.objects.filter(pk=run.pk).update(
                    status="cancelled", error=run.error, completed=now, heartbeat=now,
                )
            else:
                run.status = "interrupted"
                run.error = RUN_INTERRUPTED_ERROR
                APIRun.objects.filter(pk=run.pk).update(
                    status="interrupted", error=run.error, completed=now, heartbeat=now,
                )
            # 进程已经不在，没人会再写这些结果了：明确标成跳过，别让它们留在「未执行」。
            run.results.filter(status="pending").update(
                status="skipped", completed=now, error=RUN_SKIPPED_ERROR,
            )
            run.completed = now
            reclaimed.append(run)
    return reclaimed


def reclaim_all(lease_seconds=None, now=None):
    """巡检一次：返回 {"jobs": [...], "runs": [...]}。"""
    return {
        "jobs": reclaim_stale_jobs(lease_seconds=lease_seconds, now=now),
        "runs": reclaim_stale_runs(lease_seconds=lease_seconds, now=now),
    }


def describe_reclaimed(reclaimed):
    """给日志用的一句话摘要。"""
    jobs, runs = reclaimed["jobs"], reclaimed["runs"]
    parts = []
    if jobs:
        described = ", ".join(
            f"{job.get_operation_display()}#{job.pk}({job.status})" for job in jobs
        )
        parts.append("AI 任务 " + described)
    if runs:
        parts.append("接口执行 " + ", ".join(f"#{run.pk}({run.status})" for run in runs))
    return "；".join(parts)


class WorkerHeartbeat:
    """在任务执行期间持续刷新心跳的上下文管理器。

    ``interval`` 为 0 时只刷新一次、不开线程（测试和短任务用）。
    """

    def __init__(self, job=None, run=None, interval=HEARTBEAT_INTERVAL_SECONDS):
        if (job is None) == (run is None):
            raise ValueError("WorkerHeartbeat 需要且只需要一个 job 或 run")
        self.job = job
        self.run = run
        self.interval = max(0.0, float(interval))
        self._stop = threading.Event()
        self._thread = None

    def beat(self):
        if self.job is not None:
            return heartbeat_job(self.job.pk)
        return heartbeat_run(self.run.pk)

    def _loop(self):
        try:
            close_old_connections()
            while not self._stop.wait(self.interval):
                self.beat()
        finally:
            connections.close_all()

    def __enter__(self):
        self.beat()
        if self.interval > 0:
            self._thread = threading.Thread(
                target=self._loop, name="ai-worker-heartbeat", daemon=True
            )
            self._thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None
        return False
