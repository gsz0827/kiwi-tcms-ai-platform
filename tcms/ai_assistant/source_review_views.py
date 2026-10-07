"""Explicitly acknowledge current requirements; preserve original source snapshots."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from . import roles
from .case_library import visible_cases
from .case_design_context import context_snapshot
from .edit_safety import edit_state, fingerprint
from .models import AIRequest, AITestCaseDraft, DocumentSourceReview
from .scenario_design import parse_design, inline_test_data
from .scenario_permissions import can_edit_scenario


class SourceReviewForm(forms.Form):
    baseline = forms.CharField(widget=forms.HiddenInput)
    confirmed = forms.BooleanField(label="我已核对当前需求及参考开发文档，确认内容已跟进")


def task_content(task):
    return {
        name: getattr(task, name)
        for name in (
            "request_id",
            "title",
            "description",
            "acceptance",
            "document_sections",
            "module",
            "target_version_id",
        )
    }


def mark_task_cases(task, previous):
    if previous is not None and previous != task_content(task):
        linked = task.case_designs.all()
        parent_ids = list(linked.values_list("request_id", flat=True))
        linked.update(needs_update=True)
        AIRequest.objects.filter(pk__in=parent_ids).update(needs_case_review=True)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def recheck(request, pk, kind):
    with transaction.atomic():
        case = task = draft = None
        if kind == "task":
            target = get_object_or_404(roles.visible_dev_tasks(request.user), pk=pk)
            if not roles.can_edit_dev_task(request.user, target):
                raise PermissionDenied
            source_id = target.request_id
            back_url = reverse("ai_assistant:dev_task_detail", args=[pk])
        else:
            case = get_object_or_404(visible_cases(request.user), pk=pk)
            if not can_edit_scenario(request.user, case):
                raise PermissionDenied
            draft = get_object_or_404(
                AITestCaseDraft.objects.filter(request__in=roles.visible_requests(request.user)),
                imported_case=case,
            )
            source_id = draft.request_id
            back_url = reverse("ai_assistant:scenario_detail", args=[pk])
        source = get_object_or_404(
            roles.visible_requests(request.user).select_for_update(), pk=source_id
        )
        if kind == "task":
            task = get_object_or_404(
                roles.visible_dev_tasks(request.user).select_for_update(), pk=pk, request_id=source.pk
            )
            if not roles.can_edit_dev_task(request.user, task):
                raise PermissionDenied
            tasks = [task]
            snapshot = {"source": context_snapshot(source, []), "target": edit_state(task)}
            outdated = task.needs_update or task.requirement_version != source.version
        else:
            draft = get_object_or_404(
                AITestCaseDraft.objects.select_for_update(),
                pk=draft.pk,
                request_id=source.pk,
                imported_case_id=pk,
            )
            tasks = list(draft.dev_tasks.select_for_update().order_by("pk"))
            case = get_object_or_404(visible_cases(request.user).select_for_update(), pk=pk)
            if not can_edit_scenario(request.user, case):
                raise PermissionDenied
            if any(task.request_id != source.pk for task in tasks):
                raise PermissionDenied
            snapshot = {
                "source": context_snapshot(source, tasks),
                "target": edit_state(case),
                "draft": edit_state(draft),
            }
            outdated = draft.needs_update or draft.requirement_version != source.version
        identity = {"kind": kind, "pk": pk, "user": request.user.pk, "hash": fingerprint(snapshot)}
        token = signing.dumps(identity, salt="source-recheck-v1", compress=True)
        form = SourceReviewForm(
            request.POST if request.method == "POST" else None, initial={"baseline": token}
        )
        if request.method == "POST" and form.is_valid():
            try:
                baseline = signing.loads(
                    form.cleaned_data["baseline"], salt="source-recheck-v1", max_age=86400
                )
            except (signing.BadSignature, ValueError, TypeError):
                baseline = None
            if baseline != identity:
                form.add_error(
                    None, "需求、开发文档或用例已变化，未确认复核。请重新打开此页面核对最新内容。"
                )
            elif kind == "case" and any(
                task.needs_update or task.requirement_version != source.version for task in tasks
            ):
                form.add_error(None, "参考开发文档尚未跟进当前需求，请先完成相关开发任务复核。")
            elif not outdated:
                messages.info(request, "当前内容已经跟进此需求修订，无需重复确认。")
                return redirect(back_url)
            else:
                if kind == "task":
                    task.requirement_version = source.version
                    task.needs_update = False
                    task.save(update_fields=["requirement_version", "needs_update", "updated"])
                else:
                    design = inline_test_data(parse_design(case.text))
                    draft.summary = case.summary
                    draft.priority = case.priority.value
                    draft.test_type = design.test_type
                    draft.preconditions = design.preconditions.splitlines()
                    draft.steps = design.steps
                    draft.requirement_version = source.version
                    draft.needs_update = False
                    # Original source_context remains untouched; current evidence is append-only.
                    draft.save(
                        update_fields=[
                            "summary",
                            "priority",
                            "test_type",
                            "preconditions",
                            "steps",
                            "requirement_version",
                            "needs_update",
                        ]
                    )
                    if (
                        not source.drafts.filter(needs_update=True).exclude(pk=draft.pk).exists()
                        and not source.drafts.exclude(requirement_version=source.version).exists()
                    ):
                        AIRequest.objects.filter(pk=source.pk).update(needs_case_review=False)
                # JSON encoder makes dates from native model fields JSON-compatible.
                import json
                from django.core.serializers.json import DjangoJSONEncoder

                DocumentSourceReview.objects.create(
                    request=source,
                    task=task if kind == "task" else None,
                    case=case if kind == "case" else None,
                    requirement_version=source.version,
                    snapshot=json.loads(json.dumps(snapshot, cls=DjangoJSONEncoder)),
                    reviewed_by=request.user,
                )
                messages.success(request, f"已确认跟进需求 V{source.version}，复核记录已保存。")
                return redirect(back_url)
        return render(
            request,
            "ai_assistant/source_recheck.html",
            {
                "form": form,
                "source": source,
                "task": task if kind == "task" else None,
                "case": case,
                "tasks": tasks if kind == "case" else [],
                "back_url": back_url,
                "title": "开发任务复核" if kind == "task" else "用例来源复核",
            },
        )
