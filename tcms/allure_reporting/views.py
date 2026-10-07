import gzip
import hashlib
import io
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from tcms.ai_assistant.models import APIRun
from tcms.ai_assistant.roles import is_read_only
from tcms.web_testing.models import WebRun
from .models import AllureReport
from .worker import MAX_HTML


def source(user, kind, pk, lock=False):
    if kind not in ("web", "api"):
        raise Http404
    query = (WebRun if kind == "web" else APIRun).objects
    if lock:
        query = query.select_for_update()
    return get_object_or_404(query, pk=pk, owner=user)


def relation(kind, run):
    return {"web_run" if kind == "web" else "api_run": run}


def context(user, kind, run):
    report = AllureReport.objects.defer("artifact").filter(owner=user, **relation(kind, run)).first()
    terminal = run.terminal if kind == "web" else run.is_terminal
    label = report.get_status_display() if report else "尚未生成"
    if not terminal:
        label = "执行结束后自动生成"
    return {
        "report": report,
        "allure_kind": kind,
        "allure_run": run,
        "allure_state": report.status if report else "missing",
        "allure_label": label,
        "allure_poll": terminal and bool(report) and report.status in ("queued", "generating"),
        "allure_can_generate": terminal
        and not is_read_only(user)
        and (not report or report.status == "error"),
        "allure_url": reverse("allure_reporting:view", args=[kind, run.pk]),
        "allure_status_url": reverse("allure_reporting:status", args=[kind, run.pk]),
        "allure_generate_url": reverse("allure_reporting:generate", args=[kind, run.pk]),
    }


@login_required
@never_cache
def view(request, kind, pk):
    run = source(request.user, kind, pk)
    data = context(request.user, kind, run)
    data["back_url"] = reverse(
        "web_testing:run" if kind == "web" else "ai_assistant:api_report", args=[pk]
    )
    return render(request, "allure_reporting/view.html", data)


@login_required
@never_cache
def status(request, kind, pk):
    run = source(request.user, kind, pk)
    data = context(request.user, kind, run)
    return JsonResponse(
        {
            "status": data["allure_state"],
            "label": data["allure_label"],
            "ready": data["allure_state"] == "ready",
        }
    )


@require_POST
@login_required
@never_cache
def generate(request, kind, pk):
    if not request.user.is_active or is_read_only(request.user):
        raise PermissionDenied
    with transaction.atomic():
        run = source(request.user, kind, pk, lock=True)
        if not (run.terminal if kind == "web" else run.is_terminal):
            return HttpResponse("请等待执行结束后生成报告。", status=409)
        report, _ = AllureReport.objects.get_or_create(
            **relation(kind, run), defaults={"owner": request.user}
        )
        if report.owner_id != request.user.pk:
            raise PermissionDenied
        if report.status == "error":
            if report.heartbeat and (timezone.now() - report.heartbeat).total_seconds() < 30:
                return HttpResponse("请稍后再重试生成。", status=429)
            report.status, report.error = "queued", ""
            report.save(update_fields=("status", "error"))
    return redirect("allure_reporting:view", kind=kind, pk=pk)


@login_required
@never_cache
def artifact(request, kind, pk):
    run = source(request.user, kind, pk)
    report = get_object_or_404(
        AllureReport, owner=request.user, **relation(kind, run), status="ready"
    )
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(bytes(report.artifact))) as compressed:
            html = compressed.read(MAX_HTML + 1)
        if len(html) > MAX_HTML or hashlib.sha256(html).hexdigest() != report.checksum:
            raise ValueError
    except (ValueError, TypeError, OSError, EOFError):
        return HttpResponse("报告文件损坏，请联系维护人员核对备份。", status=409)
    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["X-Content-Type-Options"] = "nosniff"
    # Opaque sandbox: report scripts cannot read the platform's cookies or DOM.
    response["Content-Security-Policy"] = (
        "sandbox allow-scripts allow-downloads; default-src 'none'; "
        "script-src 'unsafe-inline' data: blob:; style-src 'unsafe-inline' data: blob:; img-src data: blob:; "
        "font-src data: blob:; connect-src blob: data:; worker-src blob:; frame-ancestors 'self'; "
        "base-uri 'none'; form-action 'none'"
    )
    response["Referrer-Policy"] = "no-referrer"
    if request.GET.get("download") == "1":
        response["Content-Disposition"] = f'attachment; filename="allure-{kind}-{pk}.html"'
    return response
