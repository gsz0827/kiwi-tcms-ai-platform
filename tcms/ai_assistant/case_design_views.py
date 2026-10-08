from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import roles
from .case_design_context import DesignContextForm, context_snapshot, validate_context
from .jobs import enqueue_ai_job
from .models import AIModelConfig, AIRequest


@login_required
def design(request, pk):
    source = get_object_or_404(
        roles.visible_requests(request.user).select_related("category__product"), pk=pk
    )
    can_design = request.user.is_active and roles.can_generate_cases(request.user, source)
    initial = {}
    task_id = request.GET.get("task", "")
    if task_id:
        task = (
            get_object_or_404(source.dev_tasks, pk=task_id)
            if (task_id.isdigit() and len(task_id) <= 18)
            else None
        )
        if task is None:
            raise PermissionDenied
        initial["dev_tasks"] = [task.pk]
    form = DesignContextForm(
        request.POST if request.method == "POST" else None, requirement=source, initial=initial
    )
    if request.method == "POST":
        if not can_design:
            raise PermissionDenied
        if form.is_valid():
            tasks = list(form.cleaned_data["dev_tasks"])
            context = context_snapshot(source, tasks)
            if (
                request.POST.get("context_fingerprint")
                != context_snapshot(source, list(source.dev_tasks.order_by("pk")))["fingerprint"]
            ):
                form.add_error(None, "需求或开发文档已变更，请刷新后重新选择。")
            elif request.POST.get("action") == "manual":
                if not request.user.has_perm("testcases.add_testcase"):
                    raise PermissionDenied
                if source.category_id is None:
                    form.add_error(None, "请先为需求选择项目和分类。")
                else:
                    params = [("design_request", source.pk)] + [("dev_tasks", t.pk) for t in tasks]
                    return redirect(
                        reverse("ai_assistant:scenario_new", args=[source.category.product_id])
                        + "?"
                        + urlencode(params)
                    )
            elif request.POST.get("action") == "generate":
                config = AIModelConfig.objects.filter(owner=request.user, is_active=True).first()
                if config is None:
                    messages.warning(request, "请先配置自己的默认 AI 模型。")
                    return redirect("ai_assistant:model_settings")
                with transaction.atomic():
                    locked = AIRequest.objects.select_for_update().get(pk=source.pk)
                    try:
                        validate_context(request.user, locked, context, lock=True)
                    except ValueError as exc:
                        form.add_error(None, str(exc))
                    else:
                        if locked.drafts.filter(
                            source_context__fingerprint=context["fingerprint"],
                            source_context__origin="ai",
                        ).exists():
                            messages.info(request, "相同设计依据已生成过草稿，请查看或编辑已有用例。")
                            return redirect(
                                reverse("ai_assistant:case_design", args=[source.pk])
                                + "#design-drafts"
                            )
                        try:
                            job, _created = enqueue_ai_job(
                                request.user,
                                "test_case_generation",
                                {"request_id": source.pk, "design_context": context,
                                 "additional_instructions": form.cleaned_data["additional_instructions"]},
                                model_config=config,
                                dedupe_key=f"design:{source.pk}:{context['fingerprint']}",
                            )
                        except ValueError as exc:
                            form.add_error(None, str(exc))
                        else:
                            return redirect("ai_assistant:job_detail", pk=job.pk)
            else:
                form.add_error(None, "请选择生成或人工编写用例。")
    return render(
        request,
        "ai_assistant/case_design.html",
        {
            "source": source,
            "form": form,
            "can_design": can_design,
            "can_create": can_design and request.user.has_perm("testcases.add_testcase"),
            "active_config": AIModelConfig.objects.filter(owner=request.user, is_active=True).first(),
            "context_fingerprint": context_snapshot(source, list(source.dev_tasks.order_by("pk")))[
                "fingerprint"
            ],
            "drafts": source.drafts.select_related("imported_case").prefetch_related("dev_tasks"),
        },
    )
