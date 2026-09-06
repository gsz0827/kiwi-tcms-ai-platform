import hashlib
import json
import re
from difflib import SequenceMatcher

from django.db import transaction
from django.utils import timezone
from django.utils.module_loading import import_string

from tcms.testcases.models import BugSystem
from tcms.testruns.models import TestExecution

from .models import (
    AIDefectDraft,
    AIDefectStatusHistory,
    AIReleaseGateRule,
)
from .services import build_test_run_snapshot


OPEN_DEFECT_STATUSES = (
    "pending_submission",
    "in_progress",
    "fixed",
    "pending_verification",
)


def canonical_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def defect_fingerprint(draft):
    text = " ".join(
        [draft.title, draft.actual_result, draft.expected_result]
        + list(draft.evidence or [])
        + list(draft.likely_causes or [])
    ).lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def duplicate_candidates(draft, limit=5):
    target = " ".join([draft.title, draft.actual_result]).lower()
    candidates = []
    queryset = AIDefectDraft.objects.filter(owner=draft.owner).exclude(pk=draft.pk)
    queryset = queryset.filter(status__in=OPEN_DEFECT_STATUSES).select_related(
        "execution", "execution__run"
    )
    for other in queryset[:200]:
        other_text = " ".join([other.title, other.actual_result]).lower()
        score = SequenceMatcher(None, target, other_text).ratio()
        if draft.fingerprint and draft.fingerprint == other.fingerprint:
            score = 1.0
        if score >= 0.58:
            candidates.append((round(score * 100), other))
    candidates.sort(key=lambda item: (-item[0], -item[1].pk))
    return candidates[:limit]


def transition_defect(defect, to_status, user=None, source="manual", reason=""):
    valid = {choice[0] for choice in AIDefectDraft.STATUS_CHOICES}
    if to_status not in valid:
        raise ValueError("不支持的缺陷状态")
    if to_status == "closed" and not (defect.closure_reason or reason).strip():
        raise ValueError("关闭缺陷时必须填写关闭原因")
    old_status = defect.status
    if old_status == to_status:
        return False
    defect.status = to_status
    defect.save(update_fields=("status", "updated"))
    AIDefectStatusHistory.objects.create(
        defect=defect,
        from_status=old_status,
        to_status=to_status,
        source=source,
        reason=reason,
        changed_by=user,
    )
    return True


def _map_external_status(status):
    value = (status or "").strip().lower()
    if any(word in value for word in ("closed", "done", "resolved", "已关闭")):
        return "closed"
    if any(word in value for word in ("fixed", "已修复")):
        return "fixed"
    if any(word in value for word in ("verify", "qa", "待验证")):
        return "pending_verification"
    if any(word in value for word in ("open", "progress", "reopen", "处理中")):
        return "in_progress"
    return None


def sync_external_defect(defect, request):
    if not defect.linked_reference_id:
        raise ValueError("请先关联外部缺陷地址")
    url = defect.linked_reference.url
    systems = [
        system
        for system in BugSystem.objects.exclude(base_url__isnull=True).exclude(base_url="")
        if url.startswith(system.base_url.rstrip("/"))
    ]
    if not systems:
        raise ValueError("没有找到与该地址匹配的 Kiwi 缺陷系统配置")
    system = max(systems, key=lambda item: len(item.base_url or ""))
    tracker = import_string(system.tracker_type)(system, request)
    details = tracker.details(url) or {}
    external_status = str(details.get("status") or "").strip()
    defect.external_status = external_status
    defect.last_synced_at = timezone.now()
    defect.sync_note = f"来自 {system.name}"
    defect.save(update_fields=("external_status", "last_synced_at", "sync_note", "updated"))
    mapped = _map_external_status(external_status)
    if mapped:
        if mapped == "closed" and not defect.closure_reason:
            defect.closure_reason = f"外部缺陷系统状态：{external_status}"
            defect.save(update_fields=("closure_reason", "updated"))
        transition_defect(
            defect,
            mapped,
            user=request.user,
            source="external_sync",
            reason=f"同步外部状态：{external_status}",
        )
    return details


def report_snapshot(report):
    return {
        "title": report.title,
        "summary": report.summary,
        "scope": report.scope,
        "conclusion": report.conclusion,
        "release_decision": report.release_decision,
        "recommendations": report.recommendations,
        "metrics_snapshot": report.metrics_snapshot,
        "defect_summary": report.defect_summary,
    }


def evaluate_release_gate(owner, product, metrics_snapshot, test_run_ids):
    rule = AIReleaseGateRule.objects.filter(
        owner=owner, product=product, is_active=True
    ).first()
    if rule is None:
        rule = AIReleaseGateRule.objects.filter(
            owner=owner, product__isnull=True, is_active=True
        ).first()
    metrics = metrics_snapshot.get("metrics", metrics_snapshot)
    open_defects = AIDefectDraft.objects.filter(
        owner=owner,
        execution__run_id__in=test_run_ids,
        status__in=OPEN_DEFECT_STATUSES,
    )
    block_priority = rule.block_priority if rule else "P1"
    max_open = rule.max_open_defects if rule else 0
    min_success = float(rule.min_success_rate) if rule else 95.0
    require_all = rule.require_all_executed if rule else True
    open_count = open_defects.count()
    blocking_count = open_defects.filter(priority=block_priority).count()
    checks = [
        {
            "name": f"不存在未关闭 {block_priority} 缺陷",
            "passed": blocking_count == 0,
            "actual": blocking_count,
        },
        {
            "name": f"未关闭缺陷不超过 {max_open} 个",
            "passed": open_count <= max_open,
            "actual": open_count,
        },
        {
            "name": f"成功率不低于 {min_success:g}%",
            "passed": float(metrics.get("success_rate") or 0) >= min_success,
            "actual": float(metrics.get("success_rate") or 0),
        },
    ]
    if require_all:
        checks.append(
            {
                "name": "所有用例均已执行",
                "passed": int(metrics.get("pending") or 0) == 0,
                "actual": int(metrics.get("pending") or 0),
            }
        )
    return {
        "passed": all(check["passed"] for check in checks),
        "rule": rule.name if rule else "内置默认门禁",
        "evaluated_at": timezone.now().isoformat(),
        "checks": checks,
    }


def build_iteration_snapshot(runs):
    run_snapshots = [build_test_run_snapshot(run) for run in runs]
    totals = {key: 0 for key in ("total", "completed", "success", "failure", "pending", "defect_links")}
    for snapshot in run_snapshots:
        metrics = snapshot["metrics"]
        for key in totals:
            totals[key] += int(metrics.get(key) or 0)
    total = totals["total"]
    totals["completion_rate"] = round(totals["completed"] * 100 / total, 1) if total else 0
    totals["success_rate"] = round(totals["success"] * 100 / total, 1) if total else 0
    totals["failure_rate"] = round(totals["failure"] * 100 / total, 1) if total else 0
    return {"metrics": totals}, run_snapshots


def verify_defect_regression(defect, regression_run):
    executions = list(
        TestExecution.objects.filter(
            run=regression_run, case_id=defect.execution.case_id
        ).select_related("status", "case")
    )
    if not executions:
        status, outcome = "incomplete", "missing"
    elif any(item.status.weight < 0 for item in executions):
        status, outcome = "failed", "failed"
    elif all(item.status.weight > 0 for item in executions):
        status, outcome = "passed", "passed"
    else:
        status, outcome = "incomplete", "pending"
    return status, {
        "defect_id": defect.pk,
        "source_execution_id": defect.execution_id,
        "case_id": defect.execution.case_id,
        "regression_run_id": regression_run.pk,
        "outcome": outcome,
        "regression_executions": [
            {
                "execution_id": item.pk,
                "status": item.status.name,
                "status_weight": item.status.weight,
            }
            for item in executions
        ],
    }


def render_report_lines(report):
    metrics = report.metrics_snapshot.get("metrics", report.metrics_snapshot)
    lines = [
        report.title,
        f"报告版本：V{report.version}",
        f"测试运行：TR-{report.test_run_id} {report.test_run.summary}",
        f"生成时间：{report.created:%Y-%m-%d %H:%M:%S}",
        f"审批状态：{report.get_approval_status_display()}",
        "",
        "执行指标",
        f"总数 {metrics.get('total', 0)}；成功 {metrics.get('success', 0)}；失败 {metrics.get('failure', 0)}；待执行 {metrics.get('pending', 0)}",
        f"成功率 {metrics.get('success_rate', 0)}%；缺陷率 {metrics.get('failure_rate', 0)}%",
        "",
        "执行摘要",
        report.summary,
        "",
        "测试范围",
        report.scope,
        "",
        "测试结论",
        report.conclusion,
        f"发布结论：{report.get_release_decision_display()}",
    ]
    if report.signature:
        lines.extend(("", f"签字确认：{report.signature}"))
    if report.recommendations:
        lines.extend(("", "后续建议"))
        lines.extend(f"• {item}" for item in report.recommendations)
    return lines


def _wrap_pdf_line(value, max_units=48):
    result = []
    for paragraph in str(value or "").splitlines() or [""]:
        current = ""
        units = 0
        for char in paragraph:
            width = 1 if ord(char) > 127 else 0.55
            if current and units + width > max_units:
                result.append(current)
                current, units = "", 0
            current += char
            units += width
        result.append(current)
    return result


def make_chinese_pdf(lines):
    wrapped = []
    for line in lines:
        wrapped.extend(_wrap_pdf_line(line))
    pages = [wrapped[index:index + 44] for index in range(0, len(wrapped), 44)] or [[]]
    page_count = len(pages)
    page_numbers = list(range(4, 4 + page_count))
    content_numbers = list(range(4 + page_count, 4 + page_count * 2))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Count {page_count} /Kids ["
            + " ".join(f"{number} 0 R" for number in page_numbers)
            + "] >>"
        ).encode("ascii"),
        3: (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            b"/Encoding /UniGB-UCS2-H /DescendantFonts [<< /Type /Font "
            b"/Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> >>] >>"
        ),
    }
    for index, page_lines in enumerate(pages):
        page_no = page_numbers[index]
        content_no = content_numbers[index]
        objects[page_no] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>"
        ).encode("ascii")
        commands = [b"BT /F1 11 Tf 45 800 Td 0 -17 Td"]
        for line in page_lines:
            encoded = ("\ufeff" + line).encode("utf-16-be").hex().upper()
            commands.append(f"<{encoded}> Tj 0 -17 Td".encode("ascii"))
        commands.append(b"ET")
        stream = b"\n".join(commands)
        objects[content_no] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, max(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode("ascii")
    )
    return bytes(output)
