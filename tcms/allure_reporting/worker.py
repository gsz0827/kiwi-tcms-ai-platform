import gzip
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .export import make_results
from .models import AllureReport
from .presentation import private_html

MAX_HTML = 64 * 1024 * 1024
ENGINE_VERSION = "3.20.0"


def claim_report():
    eligible = Q(web_run__status__in=("passed", "failed", "error", "cancelled", "interrupted")) | Q(
        api_run__status__in=("completed", "cancelled", "interrupted")
    )
    with transaction.atomic():
        report = (
            AllureReport.objects.select_for_update()
            .filter(status="queued")
            .filter(eligible)
            .order_by("created", "pk")
            .first()
        )
        if report:
            report.status = "generating"
            report.heartbeat = timezone.now()
            report.attempts += 1
            report.save(update_fields=("status", "heartbeat", "attempts"))
            return report.pk
    return None


def recover_reports():
    stale = AllureReport.objects.filter(
        status="generating", heartbeat__lt=timezone.now() - timedelta(minutes=5)
    )
    stale.filter(attempts__lt=3).update(status="queued", error="")
    stale.filter(attempts__gte=3).update(
        status="error", error="报告进程中断，请重试生成；测试结果保持不变。"
    )


def generate_report(pk):
    report = (
        AllureReport.objects.select_related("owner", "web_run__product", "api_run__product")
        .defer("artifact")
        .filter(pk=pk, status="generating")
        .first()
    )
    if not report:
        return
    try:
        if not report.owner.is_active or report.source.owner_id != report.owner_id:
            raise ValueError("任务账号不可用。")
        results, attachments, summary = make_results(report.kind, report.source)
        with tempfile.TemporaryDirectory(prefix="kiwi-allure-") as temporary:
            root = Path(temporary)
            inputs, output = root / "results", root / "report"
            inputs.mkdir()
            for result in results:
                (inputs / (result["uuid"] + "-result.json")).write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
            for filename, content in attachments.items():
                (inputs / filename).write_bytes(content)
            config = root / "allurerc.json"
            config.write_text(
                json.dumps(
                    {
                        "name": "Kiwi 执行报告",
                        "plugins": {
                            "awesome": {
                                "options": {
                                    "singleFile": True,
                                    "reportLanguage": "zh",
                                    "open": False,
                                    "publish": False,
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            executable = os.environ.get("ALLURE_COMMAND", "/opt/allure/node_modules/.bin/allure")
            subprocess.run(
                [
                    executable,
                    "generate",
                    str(inputs),
                    "--output",
                    str(output),
                    "--config",
                    str(config),
                ],
                cwd=root,
                check=True,
                timeout=120,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            candidates = list(output.rglob("index.html"))
            if (
                len(candidates) != 1
                or candidates[0].is_symlink()
                or candidates[0].stat().st_size > MAX_HTML
            ):
                raise ValueError("报告文件缺失或超过大小限制。")
            html = candidates[0].read_bytes()
            if b"<html" not in html[:1000].lower():
                raise ValueError("报告不是有效 HTML。")
            html = private_html(html)
            compressed = gzip.compress(html, compresslevel=6)
            if len(compressed) > 12 * 1024 * 1024:
                raise ValueError("报告超过保存限制。")
        # Atomic publication: failed generators never expose incomplete artifacts.
        published = AllureReport.objects.filter(
            pk=pk, status="generating", owner__is_active=True
        ).update(
            status="ready",
            artifact=compressed,
            checksum=hashlib.sha256(html).hexdigest(),
            engine_version=ENGINE_VERSION,
            summary=summary,
            generated=timezone.now(),
            error="",
        )
        if not published:
            raise ValueError("任务所属账号已停用或报告任务已改变。")
    except Exception as exc:
        AllureReport.objects.filter(pk=pk, status="generating").update(
            status="error",
            error=f"Allure 报告生成失败（{type(exc).__name__}），可重试生成；原测试结果保持不变。",
        )
