"""Defect-driven formal regression of the exact failed Web snapshot."""

import copy
import json

from django import forms
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.contrib import messages
from guardian.shortcuts import assign_perm

from tcms.ai_assistant.crypto import decrypt_api_key, encrypt_api_key
from tcms.ai_assistant.execution_identity import resolve_archive_case
from tcms.ai_assistant.models import AIDefectDraft, AIRegressionVerification, AutomationArchive
from tcms.ai_assistant.regression_tasks import RegressionTaskForm, can_view_run
from tcms.management.models import Build
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestRun, TestExecution, TestExecutionStatus, TestExecutionProperty
from .models import WebRun
from .run_actions import locked_owner, check_quota
from .validation import validate_url, validate_steps
from .workflow import FORMAL_PERMISSIONS, validate_archive_binding


def require_permission(user):
    from tcms.ai_assistant.roles import is_read_only

    if not user.is_active or is_read_only(user) or not user.has_perms(FORMAL_PERMISSIONS):
        raise PermissionDenied


def origin_snapshot(user, defect, lock=False):
    if not can_view_run(user, defect.execution.run):
        raise PermissionDenied
    archive = get_object_or_404(
        AutomationArchive, owner=user, kind="web", test_run=defect.execution.run
    )
    query = WebRun.objects.select_for_update() if lock else WebRun.objects
    source = get_object_or_404(query, pk=archive.source_id, owner=user, execution_mode="formal")
    validate_archive_binding(source, {"plan": source.test_run.plan, "build": source.test_run.build})
    mapping = [row for row in archive.results if row.get("execution_id") == defect.execution_id]
    if len(mapping) != 1 or mapping[0].get("status") != "failed":
        raise ValueError("缺陷未对应唯一的已归档 Web 失败项。")
    snapshot = json.loads(decrypt_api_key(source.snapshot_encrypted))
    position = mapping[0]["position"]
    if not isinstance(position, int) or not 1 <= position <= len(snapshot.get("cases", [])):
        raise ValueError("原失败项的执行快照不完整。")
    row = copy.deepcopy(snapshot["cases"][position - 1])
    # The shared identity resolver locks the case; previews also need a
    # short transaction on MariaDB (SQLite unit tests do not enforce this).
    with transaction.atomic():
        case, version = resolve_archive_case(user, source, row)
    if case.pk != defect.execution.case_id or version != defect.execution.case_text_version:
        raise ValueError("缺陷的业务用例或执行版本已变更，请核对原失败记录。")
    if not source.results.filter(position=position, status="failed").exists():
        raise ValueError("原 Web 失败证据已变更。")
    validate_url(snapshot["base_url"])
    validate_steps(row["steps"])
    if row.get("setup_steps"):
        validate_steps(row["setup_steps"])
    snapshot["cases"] = [row]
    snapshot["stop_on_failure"] = False
    snapshot.pop("retry_context", None)
    return source, snapshot, case, position


class WebRegressionForm(RegressionTaskForm):
    confirm = forms.BooleanField(label="确认在原测试站点执行失败项快照，并记录所选构建的修复版本")

    def __init__(self, *args, user, source, **kwargs):
        super().__init__(*args, user=user, source=source, **kwargs)
        self.fields["build"].label = "修复构建"
        self.fields["build"].queryset = self.fields["build"].queryset.exclude(pk=source.build_id)
        self.fields["confirm"].widget.attrs.pop("class", None)


def create_regression(user, defect_id, data):
    with transaction.atomic():
        owner = locked_owner(user)
        require_permission(owner)
        defect = get_object_or_404(
            AIDefectDraft.objects.select_for_update().select_related(
                "execution__run__plan", "execution__run__build"
            ),
            pk=defect_id,
            owner=owner,
        )
        previous = WebRun.objects.filter(
            owner=owner, submission_token=data["submission_token"]
        ).first()
        if previous:
            context = json.loads(decrypt_api_key(previous.snapshot_encrypted)).get(
                "regression_context", {}
            )
            if (
                previous.execution_mode != "formal"
                or context.get("defect_id") != defect.pk
                or (
                    previous.test_run.plan_id != data["plan"].pk
                    or previous.test_run.build_id != data["build"].pk
                )
            ):
                raise PermissionDenied("提交标识已用于其他执行。")
            return previous
        if not data.get("confirm"):
            raise ValueError("请先确认正式复测执行。")
        if defect.status not in ("fixed", "pending_verification"):
            raise ValueError("请先将缺陷更新为已修复或待验证，再发起正式复测。")
        source, snapshot, case, position = origin_snapshot(owner, defect, lock=True)
        plan = get_object_or_404(
            TestPlan.objects.select_for_update(),
            pk=data["plan"].pk,
            product_id=source.product_id,
            is_active=True,
        )
        if not (
            owner.has_perm("testplans.change_testplan")
            or owner.has_perm("testplans.change_testplan", plan)
        ):
            raise PermissionDenied
        build = get_object_or_404(
            Build.objects.select_for_update(),
            pk=data["build"].pk,
            version_id=plan.product_version_id,
            version__product_id=source.product_id,
            is_active=True,
        )
        if build.pk == source.test_run.build_id:
            raise ValueError("请选择不同于原失败任务的修复构建。")
        if not plan.cases.filter(pk=case.pk).exists():
            raise ValueError(f"请先将 TC-{case.pk} 加入所选测试计划。")
        if not case.case_status.is_confirmed:
            raise ValueError("关联业务用例尚未通过正式评审。")
        pending = TestExecutionStatus.objects.filter(weight=0).order_by("pk").first()
        if not pending:
            raise ValueError("请先配置未执行状态。")
        check_quota(owner)
        target = TestRun.objects.create(
            plan=plan,
            build=build,
            manager=owner,
            default_tester=owner,
            summary=f"Web 缺陷复测 · 缺陷 #{defect.pk}",
            notes=f"原失败任务 TR-{source.test_run_id}；原失败项 {position}。\n{data.get('notes', '')}",
        )
        for permission in ("view_testrun", "change_testrun"):
            assign_perm("testruns." + permission, owner, target)
        row = snapshot["cases"][0]
        execution = TestExecution.objects.create(
            run=target,
            case=case,
            build=build,
            status=pending,
            assignee=owner,
            case_text_version=row["business_case_version"],
            sortkey=1,
        )
        for prop in defect.execution.properties():
            TestExecutionProperty.objects.create(
                execution=execution, name=prop.name, value=prop.value
            )
        snapshot["regression_context"] = {
            "defect_id": defect.pk,
            "defect_title": defect.title,
            "source_execution_id": defect.execution_id,
            "source_web_run_id": str(source.pk),
            "source_position": position,
            "fix_version": build.version.value,
        }
        context = snapshot.setdefault("execution_context", {})
        context.update(
            product_id=source.product_id,
            plan_id=plan.pk,
            plan_name=plan.name,
            build_id=build.pk,
            build_name=build.name,
            version_id=build.version_id,
            version_name=build.version.value,
            test_run_id=target.pk,
        )
        run = WebRun.objects.create(
            owner=owner,
            product=source.product,
            source=source,
            environment=source.environment,
            name=f"Web 缺陷复测 · 缺陷 #{defect.pk}",
            submission_token=data["submission_token"],
            execution_mode="formal",
            test_run=target,
            total=1,
            snapshot_encrypted=encrypt_api_key(json.dumps(snapshot, ensure_ascii=False)),
        )
        AIRegressionVerification.objects.create(
            owner=owner,
            defect_draft=defect,
            regression_run=target,
            status="incomplete",
            notes=data.get("notes", ""),
            result={
                "task_created": True,
                "submission_token": str(data["submission_token"]),
                "web_run_id": str(run.pk),
                "source_run_id": source.test_run_id,
                "regression_run_id": target.pk,
                "outcome": "pending",
            },
        )
        defect.fix_version = build.version.value
        defect.save(update_fields=("fix_version", "updated"))
        return run


def validate_verification(defect, target):
    """Also guard the existing manual-ID verification endpoint against debug results."""
    runs = list(WebRun.objects.filter(test_run=target))
    if not runs:
        return
    if len(runs) != 1:
        raise ValueError("复测任务关联了多个 Web 执行，请核对来源。")
    run = runs[0]
    context = json.loads(decrypt_api_key(run.snapshot_encrypted)).get("regression_context", {})
    if (
        run.owner_id != defect.owner_id
        or run.execution_mode != "formal"
        or context.get("defect_id") != defect.pk
        or context.get("source_execution_id") != defect.execution_id
    ):
        raise ValueError("请从当前缺陷发起对应的 Web 正式复测，不能使用调试或其他缺陷的结果。")
    if (
        not run.terminal
        or not AutomationArchive.objects.filter(
            owner=defect.owner, kind="web", source_id=run.pk, test_run=target
        ).exists()
    ):
        raise ValueError("请等待 Web 缺陷复测结束，并先确认归档结果。")
    validate_archive_binding(run, {"plan": target.plan, "build": target.build})
    if defect.fix_version != context.get("fix_version"):
        raise ValueError("缺陷修复版本已变更，请发起对应版本的新复测。")
    latest = (
        AIRegressionVerification.objects.filter(owner=defect.owner, defect_draft=defect)
        .order_by("-created", "-pk")
        .first()
    )
    if not latest or latest.regression_run_id != target.pk:
        raise ValueError("该缺陷已有更新的复测任务，请确认最新复测结果，不能用旧结果覆盖。")
    archive = AutomationArchive.objects.get(
        owner=defect.owner, kind="web", source_id=run.pk, test_run=target
    )
    executions = list(target.executions.select_related("status"))
    if len(archive.results) != 1 or len(executions) != 1:
        raise ValueError("复测任务范围已变更，请重新执行。")
    row, execution = archive.results[0], executions[0]
    if (
        row.get("execution_id") != execution.pk
        or row.get("business_case_id") != execution.case_id
        or row.get("business_case_version") != execution.case_text_version
    ):
        raise ValueError("复测执行关联已变更，请重新执行。")
    state = row.get("status")
    weight = execution.status.weight
    if (
        (state == "passed" and weight <= 0)
        or (state == "failed" and weight >= 0)
        or (state not in ("passed", "failed") and weight != 0)
    ):
        raise ValueError("任务状态与已归档的 Web 证据不一致，不能验证。")


def run_context(run):
    context = json.loads(decrypt_api_key(run.snapshot_encrypted)).get("regression_context", {})
    if run.execution_mode != "formal" or not context:
        return {}
    defect = AIDefectDraft.objects.filter(pk=context.get("defect_id"), owner=run.owner).first()
    archive = AutomationArchive.objects.filter(owner=run.owner, kind="web", source_id=run.pk, test_run_id=run.test_run_id).first()
    return {"regression_defect": defect, "regression_archived": bool(archive),
            "regression_report_id": archive.report_id if archive else None}


@login_required
@never_cache
def new(request, pk):
    require_permission(request.user)
    defect = get_object_or_404(
        AIDefectDraft.objects.select_related("execution__run__plan", "execution__run__build"),
        pk=pk,
        owner=request.user,
    )
    try:
        source, snapshot, case, position = origin_snapshot(request.user, defect)
    except (ValueError, RuntimeError) as exc:
        return render(request, "web_testing/regression_unavailable.html",
                      {"defect": defect, "error": str(exc)}, status=409)
    form = WebRegressionForm(
        request.POST if request.method == "POST" else None, user=request.user, source=source.test_run
    )
    if request.method == "POST" and form.is_valid():
        try:
            run = create_regression(request.user, pk, form.cleaned_data)
        except (ValueError, RuntimeError) as exc:
            form.add_error(None, str(exc))
        else:
            return redirect("web_testing:run", pk=run.pk)
    return render(
        request,
        "web_testing/regression.html",
        {
            "form": form,
            "defect": defect,
            "source": source,
            "case": case,
            "position": position,
            "base_url": snapshot["base_url"],
            "title": "新建 Web 缺陷复测",
        },
    )


@require_POST
@login_required
@never_cache
def verify(request, pk):
    require_permission(request.user)
    with transaction.atomic():
        owner = locked_owner(request.user)
        run = get_object_or_404(
            WebRun.objects.select_for_update(), pk=pk, owner=owner, execution_mode="formal"
        )
        context = json.loads(decrypt_api_key(run.snapshot_encrypted)).get("regression_context", {})
        defect = get_object_or_404(
            AIDefectDraft.objects.select_for_update(), pk=context.get("defect_id"), owner=owner
        )
        target = get_object_or_404(TestRun.objects.select_for_update(), pk=run.test_run_id)
        if not can_view_run(owner, target):
            raise PermissionDenied
        list(target.executions.select_for_update())
        try:
            from tcms.ai_assistant.engineering import verify_defect_regression, transition_defect

            status, result = verify_defect_regression(defect, target)
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            verification = get_object_or_404(
                AIRegressionVerification.objects.select_for_update(),
                owner=owner,
                defect_draft=defect,
                regression_run=target,
            )
            verification.status = status
            verification.result = dict(verification.result, **result)
            verification.save(update_fields=("status", "result"))
            if status == "failed":
                transition_defect(
                    defect,
                    "in_progress",
                    user=owner,
                    source="regression",
                    reason=f"TR-{target.pk} Web 复测失败，重新打开",
                )
                messages.warning(request, "复测失败，缺陷已重新打开为处理中。")
            elif status == "passed":
                if defect.status != "closed":
                    transition_defect(
                        defect,
                        "pending_verification",
                        user=owner,
                        source="regression",
                        reason=f"TR-{target.pk} Web 复测通过，等待人工关闭",
                    )
                messages.success(request, "复测通过，已保存验证结论；关闭缺陷仍需人工确认。")
            else:
                messages.info(request, "复测尚未完成，不改变缺陷状态。")
    return redirect("ai_assistant:edit_defect_draft", pk=defect.pk)
