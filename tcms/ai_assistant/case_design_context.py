"""One requirement-based design context, with optional development-document references."""

from django import forms
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from . import roles
from .engineering import canonical_hash
from .models import AIDevTask


class DesignContextForm(forms.Form):
    dev_tasks = forms.ModelMultipleChoiceField(
        queryset=AIDevTask.objects.none(),
        required=False,
        label="参考开发任务",
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, requirement, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dev_tasks"].queryset = requirement.dev_tasks.order_by("position", "pk")


def context_snapshot(requirement, tasks):
    context = {
        "request_id": requirement.pk,
        "version": requirement.version,
        "title": requirement.title,
        "requirement": requirement.requirement_document,
        "category_id": requirement.category_id,
        "dev_tasks": [
            {
                key: getattr(task, key)
                for key in (
                    "id",
                    "task_number",
                    "title",
                    "module",
                    "description",
                    "acceptance",
                    "requirement_version",
                    "needs_update",
                )
            }
            for task in sorted(tasks, key=lambda task: task.pk)
        ],
    }
    for row, task in zip(context['dev_tasks'], sorted(tasks, key=lambda task:task.pk)):
        if task.document_sections:
            row['document_sections'] = task.document_sections
        if task.target_version_id:
            row['target_product_version'] = task.target_version.value
    context["fingerprint"] = canonical_hash(context)
    return context


def validate_context(user, requirement, context, lock=False):
    if not user.is_active or not roles.can_generate_cases(user, requirement):
        raise PermissionDenied
    if not roles.visible_requests(user).filter(pk=requirement.pk).exists():
        raise PermissionDenied
    task_ids = [row["id"] for row in context.get("dev_tasks", [])]
    tasks = requirement.dev_tasks.filter(pk__in=task_ids).order_by("pk")
    if lock:
        tasks = tasks.select_for_update()
    tasks = list(tasks)
    if len(tasks) != len(task_ids) or context_snapshot(requirement, tasks) != context:
        raise ValueError("需求或开发文档已变更，请刷新设计页面后重新提交。")
    return tasks


def manual_context(request, product):
    params = request.POST if request.method == "POST" else request.GET
    source_id = params.get("design_request", "")
    if not source_id:
        return None, [], None
    source = (
        get_object_or_404(
            roles.visible_requests(request.user).select_related("category__product"), pk=source_id
        )
        if str(source_id).isdigit() and len(str(source_id)) <= 18
        else None
    )
    if source is None or source.category_id is None or source.category.product_id != product.pk:
        raise PermissionDenied
    if not roles.can_generate_cases(request.user, source):
        raise PermissionDenied
    selection = DesignContextForm(params, requirement=source)
    if not selection.is_valid():
        raise PermissionDenied
    tasks = list(selection.cleaned_data["dev_tasks"])
    return source, tasks, context_snapshot(source, tasks)
