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
from tcms.ai_assistant.automation_ui import return_url
from tcms.management.models import Product
from .forms import CaseForm, SuiteForm
from .models import WebCase, WebSuite, WebRun, WebResult
from .validation import validate_steps, validate_url, editor_schema


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
    return render(request, "web_testing/cases.html", {"cases": Paginator(query, 30).get_page(request.GET.get("page")), "products": products, "product": product, "query": filters.urlencode(), "title": "自动化脚本", "create_url": reverse("web_testing:case_new") + ("?product=" + str(product.pk) if product else ""), "create_label": "新建脚本", "can_write": not is_read_only(request.user), "ai_url": reverse('web_testing:ai_generate', args=[product.pk]) if product else '', "ai_label": 'AI 生成 Web 脚本'})


@login_required
@write_guard
@never_cache
def edit_case(request, pk=None):
    instance = get_object_or_404(WebCase, pk=pk, owner=request.user) if pk else WebCase(owner=request.user)
    from .workflow import initial_product
    product = initial_product(request) if not pk else instance.product
    initial = {"product": product.pk} if product else {}
    if not pk and request.GET.get("test_case"):
        from tcms.ai_assistant.scenario_permissions import editable_scenarios
        target = get_object_or_404(editable_scenarios(request.user), pk=request.GET["test_case"])
        initial = {"test_case":target.pk, "product":target.category.product_id, "name":target.summary[:200]}
    form = CaseForm(request.POST or None, instance=instance, owner=request.user, initial=initial)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, "Web 自动化脚本已保存；不会自动启动执行。")
        fallback = reverse('ai_assistant:scenario_detail', args=[saved.test_case_id]) if saved.test_case_id else reverse('web_testing:cases')
        return redirect(return_url(request, fallback))
    return render(request, "web_testing/form.html", {"form": form, "title": "编辑 Web 脚本" if pk else "新建 Web 脚本", "case_editor": True,
        "delete_url": reverse("web_testing:case_delete", args=[pk]) if pk else "",
        "debug_url": reverse("web_testing:case_debug", args=[pk]) if pk and not is_read_only(request.user) else "",
        "step_schema": editor_schema(), "back_url": return_url(request, reverse('web_testing:cases'))})


@login_required
def environments(request):
    from .models import WebEnvironment
    from .workflow import list_context
    context = list_context(request, WebEnvironment, "environments")
    context.update(title="测试环境", create_url=reverse("web_testing:environment_new") + ("?product=" + str(context["product"].pk) if context["product"] else ""), create_label="新建环境")
    return render(request, "web_testing/environments.html", context)


@login_required
@write_guard
def edit_environment(request, pk=None):
    from .models import WebEnvironment
    from .execution_config import EnvironmentForm
    instance = get_object_or_404(WebEnvironment, pk=pk, owner=request.user) if pk else WebEnvironment(owner=request.user)
    from .workflow import initial_product
    product = initial_product(request) if not pk else instance.product
    form = EnvironmentForm(request.POST if request.method == "POST" else None, instance=instance, owner=request.user, initial={"product": product.pk} if product else {})
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "测试环境已保存")
        return redirect(return_url(request, reverse('web_testing:environments')))
    response = render(request, "web_testing/form.html", {"form":form, "title":"编辑测试环境" if pk else "新建测试环境", "environment_editor":True, "back_url":return_url(request, reverse('web_testing:environments'))})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def suites(request):
    from .workflow import list_context
    context = list_context(request, WebSuite, "suites")
    context.update(title="测试套件", create_url=reverse("web_testing:suite_new") + ("?product=" + str(context["product"].pk) if context["product"] else ""), create_label="新建套件")
    return render(request, "web_testing/suites.html", context)


@login_required
@write_guard
@never_cache
def edit_suite(request, pk=None):
    instance = get_object_or_404(WebSuite, pk=pk, owner=request.user) if pk else WebSuite(owner=request.user)
    from .workflow import initial_product
    product = initial_product(request) if not pk else instance.product
    form = SuiteForm(request.POST or None, instance=instance, owner=request.user, initial={"product": product.pk} if product else {})
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Web 测试套件已保存")
        return redirect(return_url(request, reverse('web_testing:suites')))
    return render(request, "web_testing/form.html", {"form": form, "title": "编辑测试套件" if pk else "新建测试套件",
        "environment_summary": True, "environment_url_label": "测试站点",
        "environment_previews": {str(env.pk): {"base_url": env.base_url, "ignore_https_errors": env.ignore_https_errors}
                                 for env in form.fields["environment"].queryset},
        "delete_url": reverse("web_testing:suite_delete", args=[pk]) if pk else "", "back_url":return_url(request, reverse('web_testing:suites'))})


@login_required
@require_POST
@write_guard
def delete_case(request, pk):
    try:
        get_object_or_404(WebCase, pk=pk, owner=request.user).delete()
    except ProtectedError:
        messages.error(request, "该脚本被环境用作前置执行脚本，请先修改环境配置再删除。")
        return redirect("web_testing:case_edit", pk=pk)
    messages.success(request, "用例已删除，历史执行快照保留；引用该用例的套件需要重新选择用例。")
    return redirect("web_testing:cases")


@login_required
@require_POST
@write_guard
def delete_suite(request, pk):
    get_object_or_404(WebSuite, pk=pk, owner=request.user).delete()
    messages.success(request, "套件已删除，历史执行记录保留。")
    return redirect(return_url(request, reverse('web_testing:suites')))


from .execution_config import suite_snapshot


@login_required
@write_guard
def submit(request, pk):
    from .workflow import submit as submit_workflow
    return submit_workflow(request, pk)


@login_required
def runs(request):
    from .workflow import list_context
    context = list_context(request, WebRun, "runs")
    context.update(title="执行任务", create_url=reverse("web_testing:suites") + ("?product=" + str(context["product"].pk) if context["product"] else ""), create_label="选择套件执行")
    return render(request, "web_testing/runs.html", context)


@login_required
def run_detail(request, pk):
    run = get_object_or_404(WebRun.objects.select_related("test_run", "environment"), pk=pk, owner=request.user)
    from .workflow import run_context
    from django.db.models import BooleanField, Case, Value, When
    results = run.results.defer("screenshot").annotate(has_screenshot=Case(
        When(screenshot__isnull=True, then=Value(False)), default=Value(True), output_field=BooleanField()))
    from tcms.ai_assistant.execution_results import result_context
    results = list(results)
    response = render(request, "web_testing/run.html", {**result_context('web', run, results), "run": run, "results": results, "title": "执行结果", "token": uuid.uuid4(), "failed_token": uuid.uuid4(), "failed_count": sum(item.status == 'failed' for item in results), "can_write": not is_read_only(request.user), **run_context(run), "back_url":return_url(request, reverse('web_testing:runs'))})
    response["Cache-Control"] = "private, no-store"
    return response


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
    from .run_actions import retry as retry_snapshot
    return retry_snapshot(request, pk)



@login_required
def screenshot(request, pk):
    result = get_object_or_404(WebResult, pk=pk, run__owner=request.user)
    if not result.screenshot:
        return HttpResponse(status=404)
    response = HttpResponse(bytes(result.screenshot), content_type="image/png")
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
