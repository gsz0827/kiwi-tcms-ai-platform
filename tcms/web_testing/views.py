import json
import uuid
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.views.decorators.cache import never_cache
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.ai_assistant.roles import is_read_only
from tcms.management.models import Product
from .forms import CaseForm, SuiteForm
from .models import WebCase, WebSuite, WebRun, WebResult
from .validation import validate_steps, validate_url


def write_guard(view):
    @wraps(view)
    def guarded(request, *args, **kwargs):
        if request.method == "POST" and is_read_only(request.user):
            return HttpResponse("只读账号不能修改或执行测试。", status=403)
        return view(request, *args, **kwargs)
    return guarded


@login_required
def cases(request):
    from tcms.ai_assistant.case_library import selected_product
    from tcms.ai_assistant.automation_folders import automation_cases
    products, product = selected_product(request)
    query = automation_cases(request.user, "web_case", product, request.GET)
    filters = request.GET.copy()
    filters.pop("page", None)
    return render(request, "web_testing/cases.html", {"cases": Paginator(query, 30).get_page(request.GET.get("page")), "products": products, "product": product, "query": filters.urlencode(), "title": "用例管理"})


@login_required
@write_guard
@never_cache
def edit_case(request, pk=None):
    instance = get_object_or_404(WebCase, pk=pk, owner=request.user) if pk else WebCase(owner=request.user)
    form = CaseForm(request.POST or None, instance=instance, owner=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Web 测试用例已保存")
        return redirect("web_testing:cases")
    return render(request, "web_testing/form.html", {"form": form, "title": "编辑 Web 用例" if pk else "新建 Web 用例", "case_editor": True,
        "delete_url": reverse("web_testing:case_delete", args=[pk]) if pk else ""})


@login_required
def environments(request):
    from .models import WebEnvironment
    return render(request, "web_testing/environments.html", {"environments": WebEnvironment.objects.filter(owner=request.user).select_related("product", "setup_case"), "title": "测试环境"})


@login_required
@write_guard
def edit_environment(request, pk=None):
    from .models import WebEnvironment
    from .execution_config import EnvironmentForm
    instance = get_object_or_404(WebEnvironment, pk=pk, owner=request.user) if pk else WebEnvironment(owner=request.user)
    form = EnvironmentForm(request.POST if request.method == "POST" else None, instance=instance, owner=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "测试环境已保存")
        return redirect("web_testing:environments")
    response = render(request, "web_testing/form.html", {"form":form, "title":"编辑测试环境" if pk else "新建测试环境", "environment_editor":True})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def suites(request):
    items = WebSuite.objects.filter(owner=request.user).select_related("product").order_by("-updated")
    return render(request, "web_testing/suites.html", {"suites": Paginator(items, 30).get_page(request.GET.get("page")), "title": "测试套件"})


@login_required
@write_guard
@never_cache
def edit_suite(request, pk=None):
    instance = get_object_or_404(WebSuite, pk=pk, owner=request.user) if pk else WebSuite(owner=request.user)
    form = SuiteForm(request.POST or None, instance=instance, owner=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Web 测试套件已保存")
        return redirect("web_testing:suites")
    return render(request, "web_testing/form.html", {"form": form, "title": "编辑测试套件" if pk else "新建测试套件",
        "delete_url": reverse("web_testing:suite_delete", args=[pk]) if pk else ""})


@login_required
@require_POST
@write_guard
def delete_case(request, pk):
    try:
        get_object_or_404(WebCase, pk=pk, owner=request.user).delete()
    except ProtectedError:
        messages.error(request, "该用例被环境用作公共登录步骤，请先修改环境配置再删除。")
        return redirect("web_testing:case_edit", pk=pk)
    messages.success(request, "用例已删除，历史执行快照保留；引用该用例的套件需要重新选择用例。")
    return redirect("web_testing:cases")


@login_required
@require_POST
@write_guard
def delete_suite(request, pk):
    get_object_or_404(WebSuite, pk=pk, owner=request.user).delete()
    messages.success(request, "套件已删除，历史执行记录保留。")
    return redirect("web_testing:suites")


from .execution_config import suite_snapshot


@login_required
@write_guard
def submit(request, pk):
    suite = get_object_or_404(WebSuite, pk=pk, owner=request.user)
    if request.method == "POST":
        try:
            token = uuid.UUID(request.POST.get("token", ""))
            previous = WebRun.objects.filter(owner=request.user, submission_token=token).first()
            if previous:
                return redirect("web_testing:run", pk=previous.pk)
            if WebRun.objects.filter(owner=request.user, status__in=("queued", "running")).count() >= 20:
                raise ValueError("待执行任务过多，请等待当前任务结束。")
            snapshot = suite_snapshot(suite)
            run, _ = WebRun.objects.get_or_create(owner=request.user, submission_token=token, defaults={
                "product": suite.product, "suite": suite, "name": suite.name,
                "snapshot_encrypted": encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)), "total": len(snapshot["cases"]),
            })
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
        else:
            return redirect("web_testing:run", pk=run.pk)
    return render(request, "web_testing/submit.html", {"suite": suite, "token": uuid.uuid4(), "title": "执行测试套件"})


@login_required
def runs(request):
    items = WebRun.objects.filter(owner=request.user).select_related("product")
    return render(request, "web_testing/runs.html", {"runs": Paginator(items, 30).get_page(request.GET.get("page")), "title": "执行任务"})


@login_required
def run_detail(request, pk):
    run = get_object_or_404(WebRun, pk=pk, owner=request.user)
    from django.db.models import BooleanField, Case, Value, When
    results = run.results.defer("screenshot").annotate(has_screenshot=Case(
        When(screenshot__isnull=True, then=Value(False)), default=Value(True), output_field=BooleanField()))
    return render(request, "web_testing/run.html", {"run": run, "results": results, "title": "执行结果", "token": uuid.uuid4()})


@login_required
def status(request, pk):
    run = get_object_or_404(WebRun, pk=pk, owner=request.user)
    response = JsonResponse({"status": run.get_status_display(), "terminal": run.terminal, "completed": run.completed_count, "total": run.total})
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
@write_guard
def cancel(request, pk):
    with transaction.atomic():
        run = get_object_or_404(WebRun.objects.select_for_update(), pk=pk, owner=request.user)
        if not run.terminal:
            run.cancel_requested = True
            if run.status == "queued":
                run.status = "cancelled"
                run.finished = timezone.now()
            run.save(update_fields=("cancel_requested", "status", "finished"))
    return redirect("web_testing:run", pk=pk)


@login_required
@require_POST
@write_guard
def retry(request, pk):
    source = get_object_or_404(WebRun, pk=pk, owner=request.user)
    if not source.terminal:
        return HttpResponse("请等待原任务结束。", status=409)
    try:
        token = uuid.UUID(request.POST.get("token", ""))
    except ValueError:
        return HttpResponse("无效的提交标识。", status=400)
    previous = WebRun.objects.filter(owner=request.user, submission_token=token).first()
    if previous:
        return redirect("web_testing:run", pk=previous.pk)
    if WebRun.objects.filter(owner=request.user, status__in=("queued", "running")).count() >= 20:
        return HttpResponse("待执行任务过多，请等待当前任务结束。", status=409)
    run, _ = WebRun.objects.get_or_create(owner=request.user, submission_token=token, defaults={
        "product": source.product, "suite": source.suite, "source": source, "name": source.name,
        "snapshot_encrypted": source.snapshot_encrypted, "total": source.total,
    })
    return redirect("web_testing:run", pk=run.pk)


@login_required
def screenshot(request, pk):
    result = get_object_or_404(WebResult, pk=pk, run__owner=request.user)
    if not result.screenshot:
        return HttpResponse(status=404)
    response = HttpResponse(bytes(result.screenshot), content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
