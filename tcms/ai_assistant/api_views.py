import json
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ObjectDoesNotExist
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.decorators.cache import never_cache

from tcms.management.models import Product

from .api_forms import APICaseForm, APISubmitForm, EnvironmentForm
from .api_runner import submit_run
from .crypto import decrypt_api_key
from .models import APICase, APIEnvironment, APIRun, APISuite
from .automation_ui import api_list_context, home_url, return_url, write_guard


@login_required
@never_cache
def home(request):
    return render(request, 'ai_assistant/api/home.html', api_list_context(request))


@login_required
@write_guard
@never_cache
def environment_edit(request, product_id, pk=None):
    product = get_object_or_404(Product, pk=product_id)
    instance = get_object_or_404(APIEnvironment, pk=pk, owner=request.user, product=product) if pk else APIEnvironment(owner=request.user, product=product)
    form = EnvironmentForm(request.POST if request.method == "POST" else None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "接口测试环境已保存")
        return redirect(return_url(request, home_url(product)))
    # Credentials never reappear, even on a rejected form submission.
    if form.is_bound:
        form.data = form.data.copy()
        form.data["secret_headers"] = ""
    return render(request, "ai_assistant/api/form.html", {
        "form": form, "product": product, "back_url": return_url(request, home_url(product)),
        "title": "编辑接口环境" if pk else "新建接口环境",
    })


@login_required
@write_guard
@never_cache
def case_edit(request, product_id, pk=None):
    product = get_object_or_404(Product, pk=product_id)
    instance = get_object_or_404(APICase, pk=pk, owner=request.user, product=product) if pk else APICase(owner=request.user, product=product)
    form = APICaseForm(
        request.POST if request.method == "POST" else None,
        instance=instance, owner=request.user, product=product,
        initial={"test_case": request.GET.get("test_case")},
    )
    if request.method == "POST" and form.is_valid():
        from .case_library import attach_case
        with transaction.atomic():
            config = form.save()
            case = attach_case(config)
            if not case.is_automated:
                case.is_automated = True
                case._history_user = request.user
                case.save(update_fields=("is_automated",))
        messages.success(request, "接口自动化脚本已保存")
        return redirect(return_url(request, home_url(product, 'cases')))
    return render(request, "ai_assistant/api/form.html", {
        "form": form, "product": product, "back_url": return_url(request, home_url(product, 'cases')),
        "title": "编辑接口脚本" if pk else "新建接口脚本",
    })


@login_required
@never_cache
@write_guard
def submit(request, product_id=None, pk=None):
    source = None
    initial = {}
    if pk:
        source = get_object_or_404(APIRun, pk=pk, owner=request.user)
        if not source.is_terminal:
            return HttpResponse("原任务尚未结束，请结束后再重新执行。", status=409)
        product_id = source.product_id
        original = json.loads(decrypt_api_key(source.snapshot_encrypted))
        initial = {
            "environment": original["selection"]["environment_id"],
            "cases": original["selection"]["case_ids"],
            "stop_on_failure": original.get("stop_on_failure", False),
            "share_cookies": original.get("share_cookies", False),
            "datasets": original.get("selection", {}).get("datasets", []),
        }
    else:
        initial["cases"] = [value for value in request.GET.getlist("case") if value.isdigit()][:20]
    product = get_object_or_404(Product, pk=product_id)
    form = APISubmitForm(
        request.POST if request.method == "POST" else None, owner=request.user, product=product,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            run = submit_run(request.user, product, form.cleaned_data, source_run=source)
        except (ValueError, RuntimeError, ObjectDoesNotExist) as exc:
            form.add_error(None, str(exc) if not isinstance(exc, ObjectDoesNotExist) else "环境或运行已删除，请重新选择。")
        else:
            return redirect("ai_assistant:api_report", pk=run.pk)
    return render(request, "ai_assistant/api/form.html", {
        "form": form, "product": product, "back_url": return_url(request, home_url(product, 'runs')),
        "title": "重新执行接口测试" if source else "执行接口测试", "is_execution": True,
        "source_run": source,
        "environment_previews": {str(env.pk): {"name": env.name, "base_url": env.base_url, "timeout": env.timeout}
                                 for env in form.fields["environment"].queryset},
    })


@login_required
@never_cache
def report(request, pk):
    run = get_object_or_404(APIRun.objects.select_related("product", "test_run"), pk=pk, owner=request.user)
    results = list(run.results.all())
    counts = {key: sum(item.status == key for item in results)
              for key in ("passed", "failed", "error", "pending", "skipped")}
    for result in results:
        result.request_display = json.dumps(result.request_summary, ensure_ascii=False, indent=2)
    from .execution_results import result_context
    from .roles import is_read_only
    return render(request, "ai_assistant/api/report.html", {
        **result_context('api', run, results), "can_write": not is_read_only(request.user),
        "run": run, "results": results, "counts": counts,
        "finished": len(results) - counts["pending"], "total": len(results),
        "back_url": return_url(request, home_url(run.product, 'runs')),
        "reruns": run.reruns.filter(owner=request.user)[:10],
    })


@login_required
def export_report(request, pk):
    run = get_object_or_404(APIRun, pk=pk, owner=request.user)
    if not run.is_terminal:
        return HttpResponse("请等待任务结束后导出完整报告。", status=409)
    results = list(run.results.values(
        "position", "name", "status", "status_code", "elapsed_ms", "checks",
        "request_summary", "response_summary", "error", "writeback", "started", "completed",
    ))
    response = JsonResponse({
        "schema_version": 1, "run_id": run.pk, "source_run_id": run.source_run_id,
        "product": run.product.name, "environment": run.environment_name,
        "status": run.status, "created": run.created, "completed": run.completed,
        "error": run.error, "results": results,
    }, json_dumps_params={"ensure_ascii": False, "indent": 2})
    response["Content-Disposition"] = f'attachment; filename="api-report-{run.pk}.json"'
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def status(request, pk):
    run = get_object_or_404(APIRun, pk=pk, owner=request.user)
    response = JsonResponse({"status": run.status, "terminal": run.is_terminal,
                             "finished": run.results.exclude(status="pending").count()})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
@write_guard
def cancel(request, pk):
    with transaction.atomic():
        run = get_object_or_404(APIRun.objects.select_for_update(), pk=pk, owner=request.user)
        if run.status == "queued":
            run.status, run.completed = "cancelled", timezone.now()
            run.save(update_fields=("status", "completed"))
            run.results.filter(status="pending").update(status="skipped")
        elif run.status == "running":
            run.status = "cancel_requested"
            run.save(update_fields=("status",))
    return redirect("ai_assistant:api_report", pk=run.pk)


@login_required
@require_POST
@permission_required("testcases.add_testcase", raise_exception=True)
@write_guard
def demo(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        APIEnvironment.objects.get_or_create(
            owner=request.user, product=product, name="本地接口演示",
            defaults={"base_url": "http://api-demo:8080", "variables": {"user_id": 1}},
        )
        examples = [
            {"name": "演示：服务健康检查", "path": "/health",
             "assertions": [{"path": "status", "operator": "equals", "expected": "ok"}]},
            {"name": "演示：查询用户", "path": "/users/{{user_id}}",
             "assertions": [{"path": "data.id", "operator": "equals", "expected": 1}]},
            {"name": "演示：登录", "path": "/login", "method": "POST", "send_body": True,
             "body": {"username": "demo", "password": "demo-password"},
             "assertions": [{"path": "token", "operator": "exists"}]},
            {"name": "演示：失败断言（预期 200，实际 404）", "path": "/users/999"},
        ]
        for example in examples:
            name = example.pop("name")
            config, _ = APICase.objects.get_or_create(owner=request.user, product=product, name=name, defaults=example)
            from .case_library import attach_case
            attach_case(config)
    messages.success(request, "演示环境和 4 条用例已准备好；启动演示服务后即可选择它们执行")
    return redirect(return_url(request, home_url(product)))


@login_required
@require_POST
@permission_required("testcases.add_testcase", raise_exception=True)
@write_guard
def chain_demo(request, product_id):
    product = get_object_or_404(Product, pk=product_id)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        APIEnvironment.objects.get_or_create(
            owner=request.user, product=product, name="本地接口演示",
            defaults={"base_url": "http://api-demo:8080"},
        )
        examples = [
            {"name": "链路演示：登录并提取令牌", "sequence": 10, "path": "/login", "method": "POST",
             "send_body": True, "body": {"username": "demo", "password": "demo-password"},
             "extracts": {"session_token": "token"}},
            {"name": "链路演示：查询登录用户", "sequence": 20, "path": "/me",
             "headers": {"Authorization": "Bearer {{session_token}}"},
             "assertions": [{"path": "data.id", "operator": "equals", "expected": 1}],
             "extracts": {"account_id": "data.id"}},
            {"name": "链路演示：查询用户详情", "sequence": 30, "path": "/users/{{account_id}}",
             "assertions": [{"path": "data.name", "operator": "equals", "expected": "demo"}]},
        ]
        for example in examples:
            name = example.pop("name")
            config, _ = APICase.objects.get_or_create(owner=request.user, product=product, name=name, defaults=example)
            from .case_library import attach_case
            attach_case(config)
    messages.success(request, "已添加 3 条链路用例，请按 10 → 20 → 30 的顺序一起执行")
    return redirect(return_url(request, home_url(product)))
