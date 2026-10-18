"""Read-only release eligibility for one authenticated CI execution.

No report selection, approval, waiver, archive or deployment is performed here.
"""

import json
import uuid

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from tcms.testruns.models import TestExecution

from . import audit, roles
from .api_suite_views import authenticate_ci
from .automation_archive import source_rows
from .crypto import decrypt_api_key
from .engineering import canonical_hash, evaluate_release_gate
from .models import APIRun, APISuite, AutomationArchive, AITestReport, AIReleaseGateRule
from .services import build_test_run_snapshot


def _positive_id(value):
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdecimal():
        raise ValueError
    number = int(value)
    if not 0 < number < 2**63:
        raise ValueError
    return number


def _decision(run, kind, plan_id, build_id, submission_key):
    base = {
        "allowed": False,
        "status": "blocked",
        "reason": "",
        "kind": kind,
        "suite_id": run.suite_id,
        "run_id": str(run.pk),
        "product_id": run.product_id,
        "plan_id": plan_id,
        "build_id": build_id,
    }

    def deny(reason, *, waiting=False):
        return dict(base, reason=reason, status="waiting" if waiting else "blocked")

    if str(run.submission_token) != submission_key:
        return deny("ci_submission_mismatch")
    if run.execution_mode != "formal" or not run.test_run_id:
        return deny("formal_execution_required")
    target = run.test_run
    if (
        target.plan_id != plan_id
        or target.build_id != build_id
        or target.plan.product_id != run.product_id
        or target.build.version.product_id != run.product_id
        or target.plan.product_version_id != target.build.version_id
    ):
        return deny("execution_context_mismatch")
    base["version_id"] = target.build.version_id
    snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
    context = snapshot.get("execution_context", {})
    expected = dict(
        product_id=run.product_id,
        plan_id=plan_id,
        build_id=build_id,
        version_id=target.build.version_id,
        test_run_id=target.pk,
    )
    if any(context.get(key) != value for key, value in expected.items()):
        return deny("submission_context_mismatch")
    if run.status in {"queued", "running", "cancel_requested"}:
        return deny("execution_pending", waiting=True)
    if run.status not in ({"passed", "failed"} if kind == "web" else {"completed"}):
        return deny("execution_incomplete")
    rows = source_rows(run, kind)
    if (
        not rows
        or len(rows) != run.results.count()
        or (kind == "web" and (run.total != len(rows) or run.completed_count != len(rows)))
        or any(row["status"] not in {"passed", "failed"} for row in rows)
    ):
        return deny("execution_incomplete")
    archive = AutomationArchive.objects.filter(
        kind=kind, source_id=run.pk, owner=run.owner, test_run=target
    ).first()
    if archive is None:
        return deny("archive_required", waiting=True)
    report = AITestReport.objects.select_related("approved_by", "gate_waived_by").get(
        pk=archive.report_id
    )
    if report.owner_id != run.owner_id or report.test_run_id != target.pk:
        return deny("report_context_mismatch")
    base.update(
        report_id=report.pk, report_version=report.version, snapshot_hash=report.snapshot_hash
    )
    if (
        not report.is_current
        or AITestReport.objects.filter(
            owner=run.owner, test_run=target, created__gt=report.created
        ).exists()
    ):
        return deny("report_superseded")
    # The run was prefetched before the archive query. Under READ COMMITTED,
    # archive publication may have committed in between: cached start/stop
    # dates can still describe the pre-archive task. Refresh only after the
    # archive becomes visible; never rewrite the signed report snapshot.
    target.refresh_from_db()
    if (
        target.plan_id != plan_id
        or target.build_id != build_id
        or target.plan.product_id != run.product_id
        or target.build.version.product_id != run.product_id
        or target.plan.product_version_id != target.build.version_id
    ):
        return deny("execution_context_mismatch")
    captured = report.metrics_snapshot
    if (
        not isinstance(captured, dict)
        or not report.snapshot_hash
        or canonical_hash(captured) != report.snapshot_hash
    ):
        return deny("report_snapshot_invalid")
    if captured.get("automation_source") != {"kind": kind, "id": str(run.pk), "results": rows}:
        return deny("execution_evidence_changed")
    if len(archive.results) != len(rows) or any(
        {key: value for key, value in mapping.items() if key != "execution_id"} != row
        for row, mapping in zip(rows, archive.results)
    ):
        return deny("archive_evidence_changed")
    mapping_ids = [item["execution_id"] for item in archive.results]
    if (
        len(set(mapping_ids)) != len(rows)
        or set(mapping_ids) != set(target.executions.values_list("pk", flat=True))
        or TestExecution.history.filter(id__in=mapping_ids, history_date__gt=report.created).exists()
    ):
        return deny("report_execution_changed")
    # Compare the approved frozen evidence to current native execution data.
    # Changes require a new execution/report, never silently rewriting the snapshot.
    frozen = {key: value for key, value in captured.items() if key != "automation_source"}
    if frozen != build_test_run_snapshot(target):
        return deny("report_execution_changed")
    if (
        not target.stop_date
        or target.executions.count() == 0
        or target.executions.filter(status__weight=0).exists()
    ):
        return deny("execution_incomplete")
    if report.approval_status == "pending":
        return deny("approval_required", waiting=True)
    approver = report.approved_by
    if (
        report.approval_status != "approved"
        or not report.approved_at
        or not report.signature
        or not report.signature.endswith("/ " + report.snapshot_hash[:12])
        or approver is None
        or not approver.is_active
        or not roles.can_approve_report(approver, report)
    ):
        return deny("approval_invalid")
    gate = evaluate_release_gate(run.product, captured, [target.pk])
    rule = AIReleaseGateRule.objects.filter(pk=gate["rule_id"]).first() if gate["rule_id"] else None
    rule_revision = rule.history.first() if rule else None
    gate["rule_version"] = rule_revision.history_id if rule_revision else "builtin-v1"
    base["gate"] = gate
    if not gate["passed"]:
        # A waiver must still describe exactly these checks and be held by an
        # active authorized manager. A stale waiver cannot waive newly found risk.
        fields = ("rule", "rule_scope", "rule_id", "checks")
        waiver_current = all(report.gate_result.get(key) == gate.get(key) for key in fields) and (
            not rule_revision
            or (report.gate_waived_at and rule_revision.history_date <= report.gate_waived_at)
        )
        manager = report.gate_waived_by
        if not (
            waiver_current
            and report.gate_waived_at
            and report.gate_waive_reason.strip()
            and manager
            and roles.can_manage_release_gate(manager)
        ):
            return deny("release_gate_failed")
        base.update(waived=True, waived_by=manager.username, waive_reason=report.gate_waive_reason)
    else:
        base["waived"] = False
    return dict(base, allowed=True, status="allowed", reason="approved_release")


@csrf_exempt
@never_cache
def check(request, kind, pk, run_id):
    if request.method != "POST":
        return JsonResponse({"allowed": False, "error": "method_not_allowed"}, status=405)
    from tcms.web_testing.models import WebRun, WebSuite

    suite_model, run_model = (WebSuite, WebRun) if kind == "web" else (APISuite, APIRun)
    suite = suite_model.objects.filter(pk=pk).first()
    if suite is None:
        return JsonResponse({"allowed": False, "error": "not_found"}, status=404)
    with transaction.atomic():
        # This is a polling reader, not a writer. Owner/source row locks invert
        # the archive writer's source -> owner FK lock order on MariaDB.
        # Keep reads non-locking and revalidate credentials before returning.
        suite = suite_model.objects.select_related("owner").get(pk=pk)
        owner = suite.owner
        if not authenticate_ci(request, suite):
            response = JsonResponse(
                {"allowed": False, "error": "invalid_or_expired_token"}, status=401
            )
            response["WWW-Authenticate"] = "Bearer"
            return response
        if roles.is_read_only(owner):
            return JsonResponse({"allowed": False, "error": "readonly_account"}, status=403)
        run = (
            run_model.objects.select_related("test_run__plan", "test_run__build__version", "product")
            .filter(pk=run_id, suite=suite, owner=owner, trigger="ci")
            .first()
        )
        if run is None:
            return JsonResponse({"allowed": False, "error": "not_found"}, status=404)
        try:
            if len(request.body) > 4096:
                raise ValueError
            data = json.loads(request.body)
            if not isinstance(data, dict) or set(data) != {"plan_id", "build_id", "submission_key"}:
                raise ValueError
            result = _decision(
                run,
                kind,
                _positive_id(data["plan_id"]),
                _positive_id(data["build_id"]),
                str(uuid.UUID(data["submission_key"])),
            )
        except (ValueError, TypeError, KeyError, RuntimeError, AttributeError):
            result = {
                "allowed": False,
                "status": "blocked",
                "reason": "invalid_release_evidence",
                "run_id": str(run.pk),
                "suite_id": suite.pk,
                "kind": kind,
            }
        fresh_suite = suite_model.objects.select_related("owner").get(pk=pk)
        if not authenticate_ci(request, fresh_suite):
            return JsonResponse({"allowed": False, "error": "invalid_or_expired_token"}, status=401)
        if roles.is_read_only(fresh_suite.owner):
            return JsonResponse({"allowed": False, "error": "readonly_account"}, status=403)
        audit.record(
            request,
            "report_gate_evaluate",
            actor=owner,
            target=run,
            product=run.product,
            result=audit.RESULT_SUCCESS if result["allowed"] else audit.RESULT_DENIED,
            reason=result["reason"],
            detail={"source": "jenkins_release_check", **result},
        )
    return JsonResponse(result)
