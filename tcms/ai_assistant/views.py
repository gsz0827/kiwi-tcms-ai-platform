from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Avg, Count, Max, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from guardian.decorators import permission_required as object_permission_required

from tcms.management.forms import ProductForm
from .forms import (
    AIDefectDraftForm,
    AIDevTaskForm,
    AIReleaseGateRuleForm,
    AIInstructionProfileForm,
    AIModelConfigForm,
    AIRequestForm,
    AITestCaseDraftForm,
    AITestReportForm,
    DefectLinkForm,
    IterationReportForm,
    RegressionVerificationForm,
    ReportApprovalForm,
    RequirementChangeForm,
)
from tcms.management.models import Classification, Product, Version
from tcms.core.contrib.linkreference.models import LinkReference
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestExecution, TestRun

from .models import (
    AIDefectDraft,
    AIDefectStatusHistory,
    AIDevTask,
    AIIterationReport,
    AIJob,
    AIInstructionProfile,
    AIModelConfig,
    AIReleaseGateRule,
    AIRegressionVerification,
    AIRequest,
    AIRequirementVersion,
    AITestCaseDraft,
    AITestCaseReview,
    AITestReport,
    AITestReportRevision,
    AITestRunAnalysis,
    AIUsageLog,
    ProjectResourceAssignment,
    ProjectResourceFolder,
)
from . import roles
from .engineering import (
    build_iteration_snapshot,
    canonical_hash,
    defect_fingerprint,
    derived_release_decision,
    duplicate_candidates,
    evaluate_release_gate,
    make_chinese_pdf,
    render_report_lines,
    report_snapshot,
    sync_external_defect,
    transition_defect,
    verify_defect_regression,
)
from .jobs import enqueue_ai_job, submit_requirement
from .services import (
    apply_test_case_review,
    build_test_run_snapshot,
    import_test_case_drafts,
    verify_regression,
)


def _run_product(test_run):
    """执行任务所属的产品；拿不到就返回 None。

    和 ``roles.visible_reports`` 一样先看计划的 product，再回落到构建版本上的产品。
    """
    if test_run is None:
        return None
    plan = getattr(test_run, "plan", None)
    product = getattr(plan, "product", None)
    if product is not None:
        return product
    build = getattr(test_run, "build", None)
    version = getattr(build, "version", None)
    return getattr(version, "product", None)


def _has_run_permission(user, permission, test_run):
    if user.has_perm(permission) or user.has_perm(permission, test_run):
        return True
    # 平台把「同一产品的成员」当作可见范围（roles.visible_reports 用的是同一套判定）。
    # 少了这一条，报告会出现在测试质量趋势里，点开却是 403。
    product = _run_product(test_run)
    return product is not None and roles.is_product_member(user, product)


def _require_run_permission(user, permission, test_run):
    if not _has_run_permission(user, permission, test_run):
        raise PermissionDenied


RESOURCE_FOLDER_PERMISSIONS = {
    "case": "testcases.change_testcase",
    "plan": "testplans.change_testplan",
}


def _resource_browser_redirect(request, resource_type=None):
    next_url = request.POST.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(
        {
            "requirement": "ai_assistant:index",
            "case": "testcases-search",
            "plan": "plans-search",
        }.get(resource_type, "core-views-index")
    )


def _require_folder_management_permission(user, resource_type):
    if resource_type == "requirement":
        return
    permission = RESOURCE_FOLDER_PERMISSIONS.get(resource_type)
    if permission is None or not user.has_perm(permission):
        raise PermissionDenied


def _resource_for_assignment(user, resource_type, object_id):
    if resource_type == "requirement":
        # 与需求列表同一套可见范围：产品成员之间可以互相归档对方的需求。
        resource = get_object_or_404(
            roles.visible_requests(user).select_related("category__product"),
            pk=object_id,
        )
        product = resource.category.product if resource.category_id else None
        return resource, product

    if resource_type == "case":
        resource = get_object_or_404(
            TestCase.objects.select_related("category__product"), pk=object_id
        )
        if not (
            user.has_perm("testcases.change_testcase")
            or user.has_perm("testcases.change_testcase", resource)
        ):
            raise PermissionDenied
        return resource, resource.category.product

    if resource_type == "plan":
        resource = get_object_or_404(
            TestPlan.objects.select_related("product"), pk=object_id
        )
        if not (
            user.has_perm("testplans.change_testplan")
            or user.has_perm("testplans.change_testplan", resource)
        ):
            raise PermissionDenied
        return resource, resource.product

    raise PermissionDenied


@require_POST
@login_required
def create_resource_folder(request):
    resource_type = request.POST.get("resource_type", "")
    _require_folder_management_permission(request.user, resource_type)
    product = get_object_or_404(Product, pk=request.POST.get("product"))
    name = request.POST.get("name", "").strip()
    if not name or len(name) > 120:
        messages.error(request, "目录名称不能为空且不能超过 120 个字符。")
        return _resource_browser_redirect(request, resource_type)

    parent = None
    parent_id = request.POST.get("parent")
    if parent_id:
        parent = get_object_or_404(ProjectResourceFolder, pk=parent_id)
        if parent.product_id != product.pk or parent.resource_type != resource_type:
            raise PermissionDenied

    if ProjectResourceFolder.objects.filter(
        product=product,
        resource_type=resource_type,
        parent=parent,
        name__iexact=name,
    ).exists():
        messages.warning(request, "同一级目录下已存在同名目录。")
        return _resource_browser_redirect(request, resource_type)

    position = (
        ProjectResourceFolder.objects.filter(
            product=product, resource_type=resource_type, parent=parent
        ).aggregate(maximum=Max("position"))["maximum"]
        or 0
    ) + 10
    ProjectResourceFolder.objects.create(
        product=product,
        resource_type=resource_type,
        name=name,
        parent=parent,
        position=position,
        created_by=request.user,
        updated_by=request.user,
    )
    messages.success(request, "共享目录已创建，项目成员刷新后即可看到。")
    return _resource_browser_redirect(request, resource_type)


@require_POST
@login_required
def rename_resource_folder(request, pk):
    folder = get_object_or_404(ProjectResourceFolder, pk=pk)
    _require_folder_management_permission(request.user, folder.resource_type)
    name = request.POST.get("name", "").strip()
    if not name or len(name) > 120:
        messages.error(request, "目录名称不能为空且不能超过 120 个字符。")
        return _resource_browser_redirect(request, folder.resource_type)
    if (
        ProjectResourceFolder.objects.filter(
            product=folder.product,
            resource_type=folder.resource_type,
            parent=folder.parent,
            name__iexact=name,
        )
        .exclude(pk=folder.pk)
        .exists()
    ):
        messages.warning(request, "同一级目录下已存在同名目录。")
        return _resource_browser_redirect(request, folder.resource_type)
    folder.name = name
    folder.updated_by = request.user
    folder.save(update_fields=("name", "updated_by", "updated"))
    messages.success(request, "共享目录名称已更新。")
    return _resource_browser_redirect(request, folder.resource_type)


@require_POST
@login_required
def delete_resource_folder(request, pk):
    folder = get_object_or_404(ProjectResourceFolder, pk=pk)
    resource_type = folder.resource_type
    _require_folder_management_permission(request.user, resource_type)
    folder.delete()
    messages.success(request, "共享目录已删除，其中的资源已回到未归档状态。")
    return _resource_browser_redirect(request, resource_type)


@require_POST
@login_required
def assign_resource_folder(request):
    resource_type = request.POST.get("resource_type", "")
    object_id = request.POST.get("object_id", "")
    if not object_id.isdigit():
        raise PermissionDenied
    _resource, product = _resource_for_assignment(
        request.user, resource_type, int(object_id)
    )
    folder_id = request.POST.get("folder", "")
    if not folder_id:
        ProjectResourceAssignment.objects.filter(
            resource_type=resource_type, object_id=object_id
        ).delete()
        messages.success(request, "资源已移到未归档。")
        return _resource_browser_redirect(request, resource_type)

    folder = get_object_or_404(ProjectResourceFolder, pk=folder_id)
    if (
        product is None
        or folder.product_id != product.pk
        or folder.resource_type != resource_type
    ):
        raise PermissionDenied
    ProjectResourceAssignment.objects.update_or_create(
        resource_type=resource_type,
        object_id=object_id,
        defaults={"folder": folder, "assigned_by": request.user},
    )
    messages.success(request, f"资源已移动到“{folder.name}”。")
    return _resource_browser_redirect(request, resource_type)


def _queue_job(request, operation, payload, dedupe_key, model_config=None):
    job, created = enqueue_ai_job(
        request.user,
        operation,
        payload,
        model_config=model_config,
        dedupe_key=dedupe_key,
    )
    if created:
        messages.success(request, "AI 任务已提交到后台，可安全离开或刷新页面。")
    else:
        messages.info(request, "相同任务已经在排队或执行中，已打开现有任务。")
    return redirect("ai_assistant:job_detail", pk=job.pk)


@login_required
def job_list(request):
    jobs = AIJob.objects.filter(owner=request.user).select_related("model_config")
    page = Paginator(jobs, 50).get_page(request.GET.get("page"))
    return render(request, "ai_assistant/job_list.html", {"page": page})


@login_required
def job_detail(request, pk):
    job = get_object_or_404(
        AIJob.objects.select_related("model_config"), pk=pk, owner=request.user
    )
    return render(request, "ai_assistant/job_detail.html", {"job": job})


@login_required
def job_status(request, pk):
    job = get_object_or_404(AIJob, pk=pk, owner=request.user)
    response = JsonResponse(
        {
            "id": str(job.pk),
            "operation": job.get_operation_display(),
            "status": job.status,
            "status_label": job.get_status_display(),
            "progress": job.progress,
            "stage": job.stage,
            "error_message": job.error_message,
            "result": job.result,
            "result_url": job.result_url,
            "is_terminal": job.is_terminal,
            "started": job.started.isoformat() if job.started else None,
            "completed": job.completed.isoformat() if job.completed else None,
        }
    )
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@login_required
def cancel_job(request, pk):
    job = get_object_or_404(AIJob, pk=pk, owner=request.user)
    if job.status == "queued":
        job.status = "cancelled"
        job.progress = 100
        job.stage = "任务已取消"
        job.completed = timezone.now()
        job.save(update_fields=("status", "progress", "stage", "completed"))
        messages.success(request, "排队中的任务已取消。")
    elif job.status == "running":
        job.status = "cancel_requested"
        job.stage = "已请求取消，等待当前模型网络调用结束"
        job.save(update_fields=("status", "stage"))
        messages.info(
            request,
            "已请求取消，Worker 会在下一个阶段检查点停止；若已进入保存阶段，结果可能已经完成。",
        )
    else:
        messages.info(request, "该任务当前状态不能取消。")
    return redirect("ai_assistant:job_detail", pk=job.pk)


@require_POST
@login_required
def retry_job(request, pk):
    old_job = get_object_or_404(AIJob, pk=pk, owner=request.user)
    if old_job.status not in {"failed", "cancelled"}:
        messages.info(request, "只有失败或已取消的任务可以重试。")
        return redirect("ai_assistant:job_detail", pk=old_job.pk)
    config = old_job.model_config
    if config is not None and config.owner_id != request.user.pk:
        config = None
    try:
        new_job, _created = enqueue_ai_job(
            request.user,
            old_job.operation,
            old_job.payload,
            model_config=config,
            dedupe_key=old_job.dedupe_key,
            attempts=old_job.attempts + 1,
        )
    except Exception as exc:
        messages.error(request, f"任务重试提交失败：{exc}")
        return redirect("ai_assistant:job_detail", pk=old_job.pk)
    messages.success(request, "任务已重新提交。")
    return redirect("ai_assistant:job_detail", pk=new_job.pk)


@login_required
@require_POST
def set_project_context(request):
    product_id = request.POST.get("product", "")
    version_id = request.POST.get("version", "")
    product = Product.objects.filter(pk=product_id).first() if product_id.isdigit() else None
    version = (
        Version.objects.select_related("product").filter(pk=version_id).first()
        if version_id.isdigit()
        else None
    )
    if product_id and product is None:
        messages.error(request, "所选项目不存在。")
        return redirect("ai_assistant:dashboard")
    if version_id and version is None:
        messages.error(request, "所选版本不存在。")
        return redirect("ai_assistant:dashboard")
    if version and product and version.product_id != product.pk:
        messages.error(request, "所选版本不属于当前项目。")
        return redirect("ai_assistant:dashboard")
    if version and product is None:
        product = version.product
    if product:
        request.session["ai_product_id"] = product.pk
    else:
        request.session.pop("ai_product_id", None)
    if version:
        request.session["ai_version_id"] = version.pk
    else:
        request.session.pop("ai_version_id", None)
    messages.success(request, "项目视图已切换。")
    return redirect("ai_assistant:dashboard")


@login_required
def dashboard(request):
    def selected_id(name):
        value = request.GET.get(name, "")
        return int(value) if value.isdigit() else None

    if request.GET.get("scope") == "all":
        request.session.pop("ai_product_id", None)
        request.session.pop("ai_version_id", None)
        product_id = None
        version_id = None
    else:
        product_id = (
            selected_id("product")
            if "product" in request.GET
            else request.session.get("ai_product_id")
        )
        version_id = (
            selected_id("version")
            if "version" in request.GET
            else request.session.get("ai_version_id")
        )
        if "product" in request.GET:
            if product_id:
                request.session["ai_product_id"] = product_id
            else:
                request.session.pop("ai_product_id", None)
        if "version" in request.GET:
            if version_id:
                request.session["ai_version_id"] = version_id
            else:
                request.session.pop("ai_version_id", None)
    plan_id = selected_id("plan")
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()
    request_query = roles.visible_requests(request.user)
    if product_id:
        request_query = request_query.filter(category__product_id=product_id)
    ai_requests = list(
        request_query
        .select_related("category", "category__product")
        .prefetch_related("drafts")
        .order_by("-created")
    )
    request_rows = []
    draft_total = 0
    imported_total = 0
    for ai_request in ai_requests:
        drafts = list(ai_request.drafts.all())
        imported = sum(1 for draft in drafts if draft.imported_case_id)
        draft_total += len(drafts)
        imported_total += imported
        if not drafts:
            state, state_label = "waiting", "待生成用例"
        elif imported < len(drafts):
            state, state_label = "pending", "待导入"
        elif ai_request.has_coverage_gaps:
            state, state_label = "risk", "存在覆盖缺口"
        else:
            state, state_label = "complete", "已导入"
        request_rows.append(
            {
                "request": ai_request,
                "draft_count": len(drafts),
                "imported_count": imported,
                "state": state,
                "state_label": state_label,
                "coverage_score": (ai_request.coverage_analysis or {}).get(
                    "overall_score"
                ),
            }
        )

    latest_analyses = {}
    analysis_query = roles.visible_analyses(request.user)
    if product_id:
        analysis_query = analysis_query.filter(test_run__plan__product_id=product_id)
    if version_id:
        analysis_query = analysis_query.filter(test_run__build__version_id=version_id)
    if plan_id:
        analysis_query = analysis_query.filter(test_run__plan_id=plan_id)
    for analysis in (
        analysis_query
        .select_related("test_run")
        .order_by("-created")
    ):
        latest_analyses.setdefault(analysis.test_run_id, analysis)

    latest_reports = {}
    report_query = roles.visible_reports(request.user)
    if product_id:
        report_query = report_query.filter(test_run__plan__product_id=product_id)
    if version_id:
        report_query = report_query.filter(test_run__build__version_id=version_id)
    if plan_id:
        report_query = report_query.filter(test_run__plan_id=plan_id)
    reports = list(
        report_query
        .select_related("test_run")
        .order_by("-created")
    )
    for report in reports:
        latest_reports.setdefault(report.test_run_id, report)

    latest_verifications = {}
    verification_query = roles.visible_verifications(request.user)
    if product_id:
        verification_query = verification_query.filter(
            Q(source_report__test_run__plan__product_id=product_id)
            | Q(defect_draft__execution__run__plan__product_id=product_id)
        )
    verifications = list(
        verification_query
        .select_related("source_report", "regression_run")
        .order_by("-created")
    )
    for verification in verifications:
        if verification.source_report_id:
            latest_verifications.setdefault(verification.source_report_id, verification)

    defect_query = roles.visible_defects(request.user)
    if product_id:
        defect_query = defect_query.filter(execution__run__plan__product_id=product_id)
    if version_id:
        defect_query = defect_query.filter(execution__run__build__version_id=version_id)
    if plan_id:
        defect_query = defect_query.filter(execution__run__plan_id=plan_id)
    defect_drafts = list(
        defect_query
        .select_related("linked_reference")
        .order_by("-created")
    )
    latest_draft_by_execution = {}
    for draft in defect_drafts:
        latest_draft_by_execution.setdefault(draft.execution_id, draft)

    run_rows = []
    failure_rows = []
    total_failures = 0
    for analysis in latest_analyses.values():
        snapshot = analysis.execution_snapshot or {}
        metrics = snapshot.get("metrics") or {}
        failures = snapshot.get("failure_details") or []
        total_failures += len(failures)
        report = latest_reports.get(analysis.test_run_id)
        verification = latest_verifications.get(report.pk) if report else None
        run_rows.append(
            {
                "analysis": analysis,
                "test_run": analysis.test_run,
                "metrics": metrics,
                "report": report,
                "verification": verification,
            }
        )
        for failure in failures:
            execution_id = failure.get("execution_id")
            failure_rows.append(
                {
                    "test_run": analysis.test_run,
                    "failure": failure,
                    "draft": latest_draft_by_execution.get(execution_id),
                }
            )

    usage_summary = AIUsageLog.objects.filter(owner=request.user).aggregate(
        total_calls=Count("id"), total_tokens=Sum("total_tokens")
    )
    linked_draft_count = sum(
        1 for draft in defect_drafts if draft.linked_reference_id
    )
    regression_counts = {
        "passed": sum(1 for item in verifications if item.status == "passed"),
        "failed": sum(1 for item in verifications if item.status == "failed"),
        "incomplete": sum(1 for item in verifications if item.status == "incomplete"),
    }
    severity_stats = list(
        defect_query.values("severity").annotate(total=Count("id")).order_by("severity")
    )
    priority_stats = list(
        defect_query.values("priority").annotate(total=Count("id")).order_by("priority")
    )
    status_stats = list(
        defect_query.values("status").annotate(total=Count("id")).order_by("status")
    )
    severity_labels = dict(AIDefectDraft.SEVERITY_CHOICES)
    status_labels = dict(AIDefectDraft.STATUS_CHOICES)
    for item in severity_stats:
        item["label"] = severity_labels.get(item["severity"], item["severity"])
    for item in status_stats:
        item["label"] = status_labels.get(item["status"], item["status"])

    pending_actions = []
    if active_config is None:
        pending_actions.append(
            {
                "level": "warning",
                "title": "配置 AI 模型",
                "detail": "当前账号尚未设置默认模型，AI 功能暂不可用。",
                "url": reverse("ai_assistant:model_settings"),
            }
        )
    for row in request_rows:
        ai_request = row["request"]
        if row["draft_count"] == 0:
            pending_actions.append(
                {
                    "level": "info",
                    "title": f"为需求 #{ai_request.pk} 生成测试用例",
                    "detail": ai_request.title,
                    "url": f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
                }
            )
        elif row["imported_count"] < row["draft_count"]:
            pending_actions.append(
                {
                    "level": "warning",
                    "title": f"导入需求 #{ai_request.pk} 的测试用例",
                    "detail": f"还有 {row['draft_count'] - row['imported_count']} 条草稿未导入。",
                    "url": f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
                }
            )
        if ai_request.has_coverage_gaps:
            pending_actions.append(
                {
                    "level": "danger",
                    "title": f"补齐需求 #{ai_request.pk} 的覆盖缺口",
                    "detail": ai_request.title,
                    "url": f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
                }
            )
    for row in failure_rows:
        failure = row["failure"]
        draft = row["draft"]
        if failure.get("defect_count", 0) == 0 and not (
            draft and draft.linked_reference_id
        ):
            pending_actions.append(
                {
                    "level": "danger",
                    "title": f"处理 {failure.get('case_number', '失败用例')} 的缺陷",
                    "detail": failure.get("case_summary") or "失败执行尚未关联缺陷。",
                    "url": reverse(
                        "ai_assistant:execution_defect",
                        args=[failure.get("execution_id")],
                    ),
                }
            )
    for row in run_rows:
        if row["report"] is None:
            pending_actions.append(
                {
                    "level": "info",
                    "title": f"为 TR-{row['test_run'].pk} 生成测试报告",
                    "detail": row["test_run"].summary,
                    "url": reverse(
                        "ai_assistant:run_report", args=[row["test_run"].pk]
                    ),
                }
            )
        elif row["metrics"].get("failure", 0) and row["verification"] is None:
            pending_actions.append(
                {
                    "level": "warning",
                    "title": f"验证报告 #{row['report'].pk} 的修复回归",
                    "detail": "该报告包含失败用例，但还没有回归验证记录。",
                    "url": reverse(
                        "ai_assistant:edit_report", args=[row["report"].pk]
                    ),
                }
            )

    stage_flags = [
        bool(ai_requests),
        imported_total > 0,
        bool(run_rows),
        bool(run_rows) and (total_failures == 0 or linked_draft_count > 0),
        bool(reports),
        regression_counts["passed"] > 0
        or (bool(reports) and total_failures == 0),
    ]
    stage_names = ("需求", "用例", "执行", "缺陷", "报告", "回归")
    stages = [
        {"name": name, "complete": complete}
        for name, complete in zip(stage_names, stage_flags)
    ]
    closed_loop_score = round(sum(stage_flags) * 100 / len(stage_flags))

    return render(
        request,
        "ai_assistant/dashboard.html",
        {
            "active_config": active_config,
            "request_rows": request_rows,
            "run_rows": run_rows,
            "failure_rows": failure_rows,
            "pending_actions": pending_actions[:50],
            "stages": stages,
            "closed_loop_score": closed_loop_score,
            "metrics": {
                "requirements": len(ai_requests),
                "drafts": draft_total,
                "imported": imported_total,
                "runs": len(run_rows),
                "failures": total_failures,
                "defect_drafts": len(defect_drafts),
                "linked_defects": linked_draft_count,
                "reports": len(reports),
                "regressions": len(verifications),
                "calls": usage_summary["total_calls"] or 0,
                "tokens": usage_summary["total_tokens"] or 0,
            },
            "regression_counts": regression_counts,
            "severity_stats": severity_stats,
            "priority_stats": priority_stats,
            "status_stats": status_stats,
            "products": Product.objects.order_by("name"),
            "versions": Version.objects.filter(
                **({"product_id": product_id} if product_id else {})
            ).select_related("product").order_by("product__name", "value"),
            "plans": TestPlan.objects.filter(
                **({"product_id": product_id} if product_id else {}),
                **({"product_version_id": version_id} if version_id else {}),
            ).select_related("product", "product_version").order_by("name"),
            "selected_product": product_id,
            "selected_version": version_id,
            "selected_plan": plan_id,
        },
    )


_EMPTY_DEV_TASK_STATS = {"total": 0, "done": 0, "percent": 0}


def _dev_task_stats_map(user, request_ids=None):
    """一次查询算出每条需求的开发任务完成度，避免逐条需求再查库。

    统计范围是「用户看得见的需求下的全部任务单」，而不是只有自己建的：需求已经按
    产品共享，完成度也必须是团队口径，否则经理看到的进度会缺掉别人负责的那部分。
    """
    queryset = AIDevTask.objects.filter(request__in=roles.visible_requests(user))
    if request_ids is not None:
        queryset = queryset.filter(request_id__in=list(request_ids))
    rows = queryset.values("request_id").annotate(
        total=Count("id"), done=Count("id", filter=Q(status="done"))
    )
    stats = {}
    for row in rows:
        total, done = row["total"], row["done"]
        stats[row["request_id"]] = {
            "total": total,
            "done": done,
            "percent": round(done * 100 / total) if total else 0,
        }
    return stats


def _assignee_choices_by_product(user, products):
    """按产品列出可被指派的人，供任务单列表里的「指派」下拉使用。"""
    choices = {}
    for product in products:
        if product is None:
            continue
        choices[product.pk] = list(
            roles.assignable_users(product).order_by("username")
        )
    return choices


@login_required
def dev_task_list(request):
    """任务单页：按需求分组展示开发任务单，并从这里发起拆分与指派。"""
    status_filter = request.GET.get("status", "").strip()
    request_filter = request.GET.get("request", "").strip()
    scope = request.GET.get("scope", "all").strip()
    if scope not in {"all", "mine", "dispatched"}:
        scope = "all"

    tasks = (
        roles.visible_dev_tasks(request.user)
        .select_related(
            "request",
            "request__category",
            "request__category__product",
            "assignee",
            "assigned_by",
        )
        .order_by("request__created", "position", "id")
    )
    if status_filter in dict(AIDevTask.STATUS_CHOICES):
        tasks = tasks.filter(status=status_filter)
    if scope == "mine":
        tasks = tasks.filter(assignee=request.user)
    elif scope == "dispatched":
        tasks = tasks.filter(assigned_by=request.user)

    selected_request = None
    if request_filter.isdigit():
        selected_request = roles.visible_requests(request.user).filter(
            pk=int(request_filter)
        ).first()
        if selected_request is None:
            tasks = tasks.none()
        else:
            tasks = tasks.filter(request=selected_request)

    grouped, order = {}, []
    for task in tasks:
        if task.request_id not in grouped:
            grouped[task.request_id] = {"request": task.request, "tasks": []}
            order.append(task.request_id)
        grouped[task.request_id]["tasks"].append(task)

    stats_map = _dev_task_stats_map(request.user)
    groups = [grouped[key] for key in order]
    for group in groups:
        group["stats"] = stats_map.get(group["request"].pk, _EMPTY_DEV_TASK_STATS)
        group["can_assign"] = roles.can_assign_dev_tasks(
            request.user, group["request"]
        )
        group["can_edit"] = roles.can_edit_requirement(request.user, group["request"])
        group["can_split"] = roles.can_split_dev_tasks(request.user, group["request"])
    assignee_choices = _assignee_choices_by_product(
        request.user,
        [group["request"].category.product if group["request"].category_id else None
         for group in groups],
    )

    unsplit_requests = []
    if selected_request is None:
        candidates = (
            roles.visible_requests(request.user)
            .exclude(dev_tasks__isnull=False)
            .select_related("category", "category__product")
            .order_by("-created")[:10]
        )
        # 只能对自己有权拆分的需求展示按钮，否则点下去只会拿到 403。
        unsplit_requests = [
            item
            for item in candidates
            if roles.can_split_dev_tasks(request.user, item)
        ]

    total_tasks = sum(item["total"] for item in stats_map.values())
    done_tasks = sum(item["done"] for item in stats_map.values())
    overall_stats = {
        "total": total_tasks,
        "done": done_tasks,
        "percent": round(done_tasks * 100 / total_tasks) if total_tasks else 0,
    }

    return render(
        request,
        "ai_assistant/dev_tasks.html",
        {
            "groups": groups,
            "status_choices": AIDevTask.STATUS_CHOICES,
            "status_filter": status_filter,
            "scope": scope,
            "scope_choices": (
                ("all", "全部可见"),
                ("mine", "我负责的"),
                ("dispatched", "我派发的"),
            ),
            "request_filter": request_filter,
            "selected_request": selected_request,
            "request_options": roles.visible_requests(request.user).order_by(
                "-created"
            )[:50],
            "unsplit_requests": unsplit_requests,
            "overall_stats": overall_stats,
            "assignee_choices": assignee_choices,
            "active_config": AIModelConfig.objects.filter(
                owner=request.user, is_active=True
            ).first(),
            "can_split": roles.can_split_dev_tasks(request.user),
        },
    )


@login_required
@require_POST
def generate_dev_tasks(request, pk):
    """给一条需求提交「拆分开发任务」作业。"""
    ai_request = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    if not roles.can_split_dev_tasks(request.user, ai_request):
        raise PermissionDenied
    target = f"{reverse('ai_assistant:dev_task_list')}?request={ai_request.pk}"
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()
    if active_config is None:
        messages.warning(request, "请先配置一个 AI 模型并设为默认，再拆分开发任务。")
        return redirect("ai_assistant:model_settings")
    if ai_request.dev_tasks.exists():
        messages.warning(request, "该需求已经拆分过开发任务，请先删除现有任务单再重新拆分。")
        return redirect(target)
    try:
        job, created = enqueue_ai_job(
            request.user,
            "dev_task_breakdown",
            {"request_id": ai_request.pk},
            model_config=active_config,
            dedupe_key=f"dev-tasks:{ai_request.pk}:{ai_request.version}",
        )
    except (ValueError, RuntimeError) as exc:
        messages.error(request, str(exc))
        return redirect(target)
    if created:
        messages.success(request, "已提交拆分开发任务，稍后回到任务单页面查看结果。")
    else:
        messages.info(request, "这份需求正在拆分，已打开原任务。")
    return redirect("ai_assistant:job_detail", pk=job.pk)


@login_required
def dev_task_edit(request, pk=None):
    """新建或编辑一条开发任务单。

    负责人（被指派人）只能改状态，看不到标题、工时、负责人这些字段——模板会按
    ``can_manage`` 决定渲染哪一组字段，表单也会据此裁剪字段，所以 POST 时不会因为
    缺失字段报错。
    """
    task = None
    can_manage = True
    if pk is not None:
        task = get_object_or_404(roles.visible_dev_tasks(request.user), pk=pk)
        can_manage = roles.can_edit_dev_task(request.user, task)
        if not can_manage and not roles.can_update_dev_task_status(request.user, task):
            raise PermissionDenied

    if request.method == "POST":
        form = AIDevTaskForm(
            request.POST, instance=task, user=request.user, can_manage=can_manage
        )
        if form.is_valid():
            saved = form.save(commit=False)
            if task is None:
                saved.owner = request.user
                if not roles.can_split_dev_tasks(request.user, saved.request):
                    raise PermissionDenied
            if can_manage:
                _apply_task_assignee(request, saved)
            saved.save()
            messages.success(request, "任务单已保存。")
            return redirect(
                f"{reverse('ai_assistant:dev_task_list')}?request={saved.request_id}"
            )
    else:
        initial = {}
        request_id = request.GET.get("request", "").strip()
        if task is None and request_id.isdigit():
            initial["request"] = int(request_id)
        form = AIDevTaskForm(
            instance=task, initial=initial, user=request.user, can_manage=can_manage
        )

    return render(
        request,
        "ai_assistant/dev_task_form.html",
        {"form": form, "task": task, "can_manage": can_manage},
    )


def _apply_task_assignee(request, task):
    """记录负责人变更，并盖上「谁派的、什么时候派的」两个戳。"""
    old_assignee_id = None
    if task.pk:
        old_assignee_id = (
            AIDevTask.objects.filter(pk=task.pk)
            .values_list("assignee_id", flat=True)
            .first()
        )
    if task.assignee_id == old_assignee_id:
        return
    task.assigned_by = request.user if task.assignee_id else None
    task.assigned_at = timezone.now() if task.assignee_id else None


@login_required
@require_POST
def assign_dev_task(request, pk):
    """指派或改派一条任务单；传空的 assignee 表示收回指派。"""
    task = get_object_or_404(roles.visible_dev_tasks(request.user), pk=pk)
    if not roles.can_assign_dev_tasks(request.user, task.request):
        raise PermissionDenied
    target = f"{reverse('ai_assistant:dev_task_list')}?request={task.request_id}"

    assignee_id = request.POST.get("assignee", "").strip()
    if not assignee_id:
        task.assignee = None
        task.assigned_by = None
        task.assigned_at = None
        task.save(update_fields=["assignee", "assigned_by", "assigned_at", "updated"])
        messages.success(request, "已收回这条任务单的指派。")
        return redirect(target)

    product = task.request.category.product if task.request.category_id else None
    candidates = roles.assignable_users(product).filter(pk=assignee_id)
    assignee = candidates.first()
    if assignee is None:
        messages.error(request, "只能指派给该产品的成员。")
        return redirect(target)
    if task.assignee_id == assignee.pk:
        messages.info(request, "这条任务单的负责人没有变化。")
        return redirect(target)

    task.assignee = assignee
    task.assigned_by = request.user
    task.assigned_at = timezone.now()
    task.save(update_fields=["assignee", "assigned_by", "assigned_at", "updated"])
    messages.success(request, f"已指派给 {assignee.username}。")
    return redirect(target)


@login_required
@require_POST
def delete_dev_task(request, pk):
    """删除一条开发任务单。"""
    task = get_object_or_404(roles.visible_dev_tasks(request.user), pk=pk)
    if not roles.can_delete_dev_task(request.user, task):
        raise PermissionDenied
    request_id = task.request_id
    task.delete()
    messages.success(request, "任务单已删除。")
    return redirect(f"{reverse('ai_assistant:dev_task_list')}?request={request_id}")


@login_required
def index(request):
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()

    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置一个 AI 模型并设为默认，再使用 AI 助手。")
            return redirect("ai_assistant:model_settings")

        form = AIRequestForm(request.POST)
        if form.is_valid():
            action = request.POST.get("action", "generate")
            operation = (
                "requirement_analysis" if action == "analyze" else "test_case_generation"
            )
            if action not in {"analyze", "generate"}:
                form.add_error(None, "不支持的需求处理操作。")
            else:
                try:
                    job, created = submit_requirement(
                        request.user, form.cleaned_data, operation, active_config
                    )
                except (ValueError, RuntimeError) as exc:
                    form.add_error(None, str(exc))
                else:
                    if created:
                        messages.success(request, "AI 任务已提交到后台，可安全离开或刷新页面。")
                    else:
                        messages.info(request, "这份需求已提交，已打开原任务。")
                    return redirect("ai_assistant:job_detail", pk=job.pk)
    else:
        form = AIRequestForm()

    ai_requests = (
        roles.visible_requests(request.user)
        .select_related(
            "category",
            "category__product",
            "created_by",
            "analysis_model_config",
            "coverage_model_config",
        )
        .prefetch_related("drafts", "drafts__imported_case")
        .order_by("-created")
    )
    dev_task_stats = _dev_task_stats_map(request.user)
    for ai_request in ai_requests:
        ai_request.unimported_draft_count = sum(
            1 for draft in ai_request.drafts.all() if draft.imported_case_id is None
        )
        ai_request.dev_task_stats = dev_task_stats.get(
            ai_request.pk, _EMPTY_DEV_TASK_STATS
        )
        ai_request.can_edit = roles.can_edit_requirement(request.user, ai_request)
    return render(
        request,
        "ai_assistant/index.html",
        {
            "form": form,
            "ai_requests": ai_requests,
            "active_config": active_config,
            "can_submit": roles.can_submit_requirement(request.user),
        },
    )


@require_POST
@login_required
def generate_from_analysis(request, pk):
    ai_request = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    if not roles.can_generate_cases(request.user, ai_request):
        raise PermissionDenied
    if not ai_request.analysis:
        messages.warning(request, "该请求还没有可用的需求分析结果。")
        return redirect("ai_assistant:index")
    if ai_request.drafts.exists():
        messages.info(request, "该请求已经生成过测试用例草稿。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置一个 AI 模型并设为默认，再生成测试用例。")
        return redirect("ai_assistant:model_settings")

    return _queue_job(
        request,
        "test_case_generation",
        {"request_id": ai_request.pk},
        f"test_case_generation:request:{ai_request.pk}",
    )


@require_POST
@login_required
def analyze_coverage(request, pk):
    ai_request = get_object_or_404(
        roles.visible_requests(request.user).prefetch_related("drafts"),
        pk=pk,
    )
    if not roles.can_generate_cases(request.user, ai_request):
        raise PermissionDenied
    if not ai_request.drafts.all():
        messages.warning(request, "该请求还没有可分析的测试用例草稿。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置一个 AI 模型并设为默认，再分析用例覆盖率。")
        return redirect("ai_assistant:model_settings")

    return _queue_job(
        request,
        "coverage_analysis",
        {"request_id": ai_request.pk},
        f"coverage_analysis:request:{ai_request.pk}",
    )


@require_POST
@login_required
def supplement_from_coverage(request, pk):
    ai_request = get_object_or_404(
        roles.visible_requests(request.user).prefetch_related("drafts"),
        pk=pk,
    )
    if not roles.can_generate_cases(request.user, ai_request):
        raise PermissionDenied
    if not ai_request.coverage_analysis:
        messages.warning(request, "该请求还没有可用的覆盖分析结果。")
        return redirect("ai_assistant:index")
    if not ai_request.has_coverage_gaps:
        messages.info(request, "当前覆盖分析没有需要补充的测试缺口。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置一个 AI 模型并设为默认，再补充测试用例。")
        return redirect("ai_assistant:model_settings")

    return _queue_job(
        request,
        "coverage_supplement",
        {"request_id": ai_request.pk},
        f"coverage_supplement:request:{ai_request.pk}",
    )


@login_required
@permission_required("testcases.add_testcase", raise_exception=True)
def edit_draft(request, pk):
    draft = get_object_or_404(
        AITestCaseDraft.objects.select_related("request", "imported_case"),
        pk=pk,
        request__in=roles.visible_requests(request.user),
    )
    if draft.imported_case_id:
        messages.info(request, "该草稿已导入，请在 Kiwi 正式测试用例中继续编辑。")
        return redirect("testcases-edit", pk=draft.imported_case_id)

    form = AITestCaseDraftForm(request.POST or None, instance=draft)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            parent = AIRequest.objects.select_for_update().get(pk=draft.request_id)
            current = AITestCaseDraft.objects.select_for_update().get(pk=draft.pk)
            if current.imported_case_id or parent.version != draft.request.version:
                messages.error(request, "需求或草稿已变化，请刷新后重新复核。")
                return redirect("ai_assistant:edit_draft", pk=draft.pk)
            draft = form.save(commit=False)
            draft.requirement_version = parent.version
            draft.needs_update = False
            draft.save()
            AIRequest.objects.filter(pk=draft.request_id).update(
                coverage_analysis={},
                coverage_raw="",
                coverage_model_config=None,
                coverage_analyzed_at=None,
            )
            if not parent.drafts.filter(needs_update=True).exists():
                AIRequest.objects.filter(pk=parent.pk).update(needs_case_review=False)
        messages.success(request, f"{draft.case_number} 草稿已保存。")
        return redirect("ai_assistant:index")
    return render(
        request,
        "ai_assistant/edit_draft.html",
        {"form": form, "draft": draft},
    )


@login_required
def edit_requirement(request, pk):
    ai_request = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    if not roles.can_edit_requirement(request.user, ai_request):
        raise PermissionDenied
    if request.method == "POST":
        old_title = ai_request.title
        old_requirement = ai_request.requirement
        form = RequirementChangeForm(request.POST, instance=ai_request)
        if form.is_valid():
            changed = (
                form.cleaned_data["title"] != old_title
                or form.cleaned_data["requirement"] != old_requirement
            )
            if not changed:
                messages.info(request, "需求内容没有变化。")
            else:
                with transaction.atomic():
                    locked = AIRequest.objects.select_for_update().get(
                        pk=ai_request.pk
                    )
                    locked.title = form.cleaned_data["title"]
                    locked.requirement = form.cleaned_data["requirement"]
                    locked.version += 1
                    locked.needs_case_review = True
                    locked.changed_at = timezone.now()
                    locked.analysis = {}
                    locked.analysis_raw = ""
                    locked.analysis_model_config = None
                    locked.analyzed_at = None
                    locked.coverage_analysis = {}
                    locked.coverage_raw = ""
                    locked.coverage_model_config = None
                    locked.coverage_analyzed_at = None
                    locked.save()
                    locked.drafts.update(needs_update=True)
                    AIRequirementVersion.objects.create(
                        request=locked,
                        version=locked.version,
                        title=locked.title,
                        requirement=locked.requirement,
                        change_summary=form.cleaned_data["change_summary"],
                        changed_by=request.user,
                    )
                messages.success(request, f"需求已更新为 V{locked.version}，相关用例已标记待更新。")
                return redirect("ai_assistant:requirement_trace", pk=locked.pk)
    else:
        form = RequirementChangeForm(instance=ai_request)
    return render(
        request,
        "ai_assistant/edit_requirement.html",
        {"ai_request": ai_request, "form": form, "versions": ai_request.versions.select_related("changed_by")},
    )


@login_required
def requirement_trace(request, pk):
    ai_request = get_object_or_404(
        roles.visible_requests(request.user).select_related(
            "category__product"
        ).prefetch_related("drafts", "drafts__imported_case", "versions"),
        pk=pk,
    )
    rows = []
    for draft in ai_request.drafts.all():
        executions = []
        if draft.imported_case_id:
            for execution in TestExecution.objects.filter(
                case_id=draft.imported_case_id
            ).select_related("run", "status").order_by("-id")[:50]:
                if _has_run_permission(request.user, "testruns.view_testrun", execution.run):
                    executions.append(execution)
        defects = list(
            AIDefectDraft.objects.filter(
                owner=request.user,
                execution_id__in=[item.pk for item in executions],
            ).prefetch_related("regression_verifications")
        )
        rows.append({"draft": draft, "executions": executions, "defects": defects})
    dev_tasks = list(ai_request.dev_tasks.all())
    return render(
        request,
        "ai_assistant/requirement_trace.html",
        {
            "ai_request": ai_request,
            "rows": rows,
            "dev_tasks": dev_tasks,
            "can_edit": roles.can_edit_requirement(request.user, ai_request),
            "can_split": roles.can_split_dev_tasks(request.user, ai_request),
            "can_assign": roles.can_assign_dev_tasks(request.user, ai_request),
            "dev_task_stats": _dev_task_stats_map(
                request.user, request_ids=[ai_request.pk]
            ).get(ai_request.pk, _EMPTY_DEV_TASK_STATS),
            "uncovered": not ai_request.drafts.exists(),
            "unexecuted": sum(1 for row in rows if row["draft"].imported_case_id and not row["executions"]),
            "open_defects": sum(
                1 for row in rows for defect in row["defects"] if defect.status != "closed"
            ),
        },
    )


@require_POST
@permission_required("testcases.add_testcase", raise_exception=True)
def import_request(request, pk):
    ai_request = get_object_or_404(roles.visible_requests(request.user), pk=pk)
    raw_ids = request.POST.getlist("draft_ids")
    if not raw_ids:
        messages.warning(request, "请至少勾选一条待导入的测试用例。")
        return redirect("ai_assistant:index")
    try:
        selected_ids = [int(value) for value in raw_ids]
    except ValueError:
        messages.error(request, "勾选的测试用例参数无效。")
        return redirect("ai_assistant:index")

    try:
        created = import_test_case_drafts(
            ai_request, request.user, selected_draft_ids=selected_ids
        )
    except Exception as exc:
        messages.error(request, f"导入失败：{exc}")
    else:
        if created:
            messages.success(request, f"已导入 {len(created)} 条 Kiwi TestCase。")
        else:
            messages.info(request, "所选草稿均已导入或不属于当前请求。")
    return redirect("ai_assistant:index")


@login_required
def member_list(request):
    """产品成员与角色：哪个产品有谁、各是什么角色。

    成员用 guardian 的 ``management.view_product`` 对象权限表示，角色用 Django 的
    用户组表示——两者都在 Django admin 里看得见，不额外建表。
    """
    if not roles.can_manage_members(request.user):
        raise PermissionDenied

    products = list(roles.member_products(request.user).order_by("name"))
    product_filter = request.GET.get("product", "").strip()
    selected = None
    if product_filter.isdigit():
        selected = next(
            (item for item in products if item.pk == int(product_filter)), None
        )
    if selected is None and products:
        selected = products[0]

    members = []
    candidates = []
    if selected is not None:
        members = list(roles.members_of_product(selected))
        for member in members:
            member.ai_roles = sorted(roles.roles_of(member))
        member_ids = [member.pk for member in members]
        candidates = list(
            get_user_model()
            .objects.filter(is_active=True)
            .exclude(pk__in=member_ids)
            .order_by("username")[:50]
        )

    return render(
        request,
        "ai_assistant/members.html",
        {
            "products": products,
            "selected_product": selected,
            "product_filter": product_filter,
            "members": members,
            "candidates": candidates,
            "role_choices": roles.ROLE_CHOICES,
            "role_rows": [
                {"name": name, "description": roles.ROLE_DESCRIPTIONS[name]}
                for name in roles.ROLE_GROUPS
            ],
        },
    )


@login_required
@require_POST
def add_product_member(request):
    """把一个已有账号加进产品。"""
    if not roles.can_manage_members(request.user):
        raise PermissionDenied
    product = get_object_or_404(
        roles.member_products(request.user), pk=request.POST.get("product", "")
    )
    target = f"{reverse('ai_assistant:member_list')}?product={product.pk}"

    user_id = request.POST.get("user", "").strip()
    member = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if member is None:
        messages.error(request, "请选择一个有效的账号。")
        return redirect(target)
    if roles.is_product_member(member, product):
        messages.info(request, f"{member.username} 已经是该产品的成员。")
        return redirect(target)

    roles.add_product_member(member, product, granted_by=request.user)
    role_name = request.POST.get("role", "").strip()
    if role_name in roles.ROLE_GROUPS:
        roles.set_user_roles(
            member, set(roles.roles_of(member)) | {role_name}
        )
    messages.success(request, f"已把 {member.username} 加入「{product.name}」。")
    return redirect(target)


@login_required
@require_POST
def remove_product_member(request, pk):
    """把一个成员移出产品；顺带清掉他的角色，避免留下没有产品的空角色。"""
    if not roles.can_manage_members(request.user):
        raise PermissionDenied
    product = get_object_or_404(
        roles.member_products(request.user), pk=request.POST.get("product", "")
    )
    member = get_object_or_404(get_user_model(), pk=pk)
    target = f"{reverse('ai_assistant:member_list')}?product={product.pk}"

    if member == request.user:
        messages.warning(request, "不能把自己移出产品，请让其他管理员操作。")
        return redirect(target)
    roles.remove_product_member(member, product)
    if not roles.member_products(member).exists():
        roles.set_user_roles(member, set())
    messages.success(request, f"已把 {member.username} 移出「{product.name}」。")
    return redirect(target)


@login_required
@require_POST
def set_member_roles(request, pk):
    """整体设置某个成员的角色（多选）。"""
    if not roles.can_manage_members(request.user):
        raise PermissionDenied
    product = get_object_or_404(
        roles.member_products(request.user), pk=request.POST.get("product", "")
    )
    member = get_object_or_404(get_user_model(), pk=pk)
    target = f"{reverse('ai_assistant:member_list')}?product={product.pk}"

    selected = set(request.POST.getlist("roles")) & set(roles.ROLE_GROUPS)
    if not roles.is_product_member(member, product):
        messages.error(request, "该账号不是这个产品的成员，请先添加成员。")
        return redirect(target)
    roles.set_user_roles(member, selected)
    if selected:
        messages.success(
            request, f"{member.username} 的角色已更新为：{'、'.join(sorted(selected))}。"
        )
    else:
        messages.info(request, f"{member.username} 现在没有任何角色，只能查看自己被指派的内容。")
    return redirect(target)


@login_required
def model_settings(request):
    form = AIModelConfigForm(request.POST or None, owner=request.user)
    if request.method == "POST" and form.is_valid():
        first_config = not AIModelConfig.objects.filter(owner=request.user).exists()
        config = form.save(commit=False)
        if first_config:
            config.is_active = True
        config.save()
        messages.success(request, f"模型配置“{config.name}”已保存。")
        return redirect("ai_assistant:model_settings")

    configs = AIModelConfig.objects.filter(owner=request.user)
    return render(
        request,
        "ai_assistant/model_settings.html",
        {"form": form, "configs": configs},
    )


@login_required
def project_settings(request):
    return render(request, "ai_assistant/project_settings.html")


@login_required
def instruction_profiles(request):
    form = AIInstructionProfileForm(request.POST or None, owner=request.user)
    if request.method == "GET" and request.GET.get("product", "").isdigit():
        form.fields["product"].initial = int(request.GET["product"])
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        messages.success(
            request,
            f"规则包“{profile.name}”已保存为 V{profile.version}，新的需求任务会读取它。",
        )
        return redirect("ai_assistant:instruction_profiles")

    profiles = AIInstructionProfile.objects.filter(owner=request.user).select_related(
        "product"
    )
    return render(
        request,
        "ai_assistant/instruction_profiles.html",
        {"form": form, "profiles": profiles},
    )


def _default_classification():
    """产品分类不进平台界面：新产品统一落到已有分类，一个都没有时建「默认分类」。

    分类是上游 Kiwi 的模型，系统后台与 XML-RPC 的 Classification.* 仍在用它，
    平台侧只是不为它提供界面，所以这里保证新建产品总能拿到一个分类。
    """
    classification = Classification.objects.order_by("pk").first()
    if classification is None:
        classification = Classification.objects.create(name="默认分类")
    return classification


@login_required
@permission_required("management.add_product", raise_exception=True)
def create_product(request):
    next_url = request.POST.get("next") or request.GET.get("next", "")
    if not (next_url.startswith("/") and not next_url.startswith("//")):
        next_url = ""
    form = ProductForm(request.POST or None)
    # 「产品分类」不是平台概念：字段从表单里拿掉，保存时自动归类。
    form.fields.pop("classification", None)
    if request.method == "POST" and form.is_valid():
        product = form.save(commit=False)
        product.classification = _default_classification()
        product.save()
        messages.success(
            request,
            f"产品“{product.name}”已创建，并已自动生成默认用例分类、版本和构建。",
        )
        if next_url:
            return redirect(next_url)
        return redirect(
            f"{reverse('ai_assistant:instruction_profiles')}?product={product.pk}"
        )
    return render(
        request,
        "ai_assistant/create_product.html",
        {"form": form, "next_url": next_url},
    )


@login_required
def edit_instruction_profile(request, pk):
    profile = get_object_or_404(AIInstructionProfile, pk=pk, owner=request.user)
    form = AIInstructionProfileForm(
        request.POST or None, instance=profile, owner=request.user
    )
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        messages.success(
            request,
            f"规则包“{profile.name}”已更新为 V{profile.version}。",
        )
        return redirect("ai_assistant:instruction_profiles")
    return render(
        request,
        "ai_assistant/edit_instruction_profile.html",
        {"form": form, "profile": profile},
    )


@require_POST
@login_required
def toggle_instruction_profile(request, pk):
    profile = get_object_or_404(AIInstructionProfile, pk=pk, owner=request.user)
    profile.is_active = not profile.is_active
    profile.save(update_fields=("is_active", "updated"))
    state = "启用" if profile.is_active else "停用"
    messages.success(request, f"规则包“{profile.name}”已{state}。")
    return redirect("ai_assistant:instruction_profiles")


@login_required
def edit_model_config(request, pk):
    config = get_object_or_404(AIModelConfig, pk=pk, owner=request.user)
    form = AIModelConfigForm(
        request.POST or None, instance=config, owner=request.user
    )
    if request.method == "POST" and form.is_valid():
        config = form.save()
        messages.success(request, f"模型配置“{config.name}”已更新。")
        return redirect("ai_assistant:model_settings")
    return render(
        request,
        "ai_assistant/edit_model_config.html",
        {"form": form, "config": config},
    )


@require_POST
@login_required
def activate_model_config(request, pk):
    config = get_object_or_404(AIModelConfig, pk=pk, owner=request.user)
    if not config.api_key_encrypted:
        messages.error(request, "该模型没有 API Key，请先编辑后再设为默认。")
    else:
        config.is_active = True
        config.save()
        messages.success(request, f"已将模型“{config.name}”设为默认。")
    return redirect("ai_assistant:model_settings")


@require_POST
@login_required
def test_model_config(request, pk):
    config = get_object_or_404(AIModelConfig, pk=pk, owner=request.user)
    return _queue_job(
        request,
        "connection_test",
        {"model_config_id": config.pk},
        f"connection_test:model:{config.pk}",
        model_config=config,
    )


@login_required
def usage_logs(request):
    logs = AIUsageLog.objects.filter(owner=request.user).select_related(
        "model_config"
    )
    summary = logs.aggregate(
        total_calls=Count("id"),
        successful_calls=Count("id", filter=Q(status="success")),
        total_tokens=Sum("total_tokens"),
        average_duration_ms=Avg("duration_ms"),
    )
    total_calls = summary["total_calls"] or 0
    successful_calls = summary["successful_calls"] or 0
    summary["failed_calls"] = total_calls - successful_calls
    summary["success_rate"] = (
        round(successful_calls * 100 / total_calls, 1) if total_calls else 0
    )
    summary["total_tokens"] = summary["total_tokens"] or 0
    summary["average_duration_ms"] = round(summary["average_duration_ms"] or 0)

    page = Paginator(logs, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "ai_assistant/usage_logs.html",
        {"page": page, "summary": summary},
    )


@object_permission_required(
    "testruns.view_testrun", (TestRun, "pk", "pk"), accept_global_perms=True
)
@login_required
def run_analysis(request, pk):
    test_run = get_object_or_404(
        TestRun.objects.select_related(
            "plan",
            "build",
            "build__version",
            "build__version__product",
            "manager",
        ),
        pk=pk,
    )
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()

    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置一个 AI 模型并设为默认，再分析测试运行。")
            return redirect("ai_assistant:model_settings")
        return _queue_job(
            request,
            "test_run_analysis",
            {"test_run_id": test_run.pk},
            f"test_run_analysis:run:{test_run.pk}",
            model_config=active_config,
        )

    analyses = AITestRunAnalysis.objects.filter(
        owner=request.user, test_run=test_run
    ).select_related("model_config")
    return render(
        request,
        "ai_assistant/run_analysis.html",
        {
            "test_run": test_run,
            "analyses": analyses,
            "active_config": active_config,
            "current_snapshot": build_test_run_snapshot(test_run),
        },
    )


@login_required
def execution_defect(request, pk):
    execution = get_object_or_404(
        TestExecution.objects.select_related(
            "run",
            "run__plan",
            "case",
            "case__priority",
            "case__category",
            "status",
            "build",
            "build__version",
            "build__version__product",
            "assignee",
            "tested_by",
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", execution.run)
    if execution.status.weight >= 0:
        messages.warning(request, "当前执行不是失败状态，不能生成缺陷草稿。")
        return redirect("ai_assistant:run_analysis", pk=execution.run_id)

    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()
    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置一个 AI 模型并设为默认，再生成缺陷草稿。")
            return redirect("ai_assistant:model_settings")
        return _queue_job(
            request,
            "defect_draft_generation",
            {"execution_id": execution.pk},
            f"defect_draft_generation:execution:{execution.pk}",
            model_config=active_config,
        )

    drafts = AIDefectDraft.objects.filter(
        owner=request.user, execution=execution
    ).select_related("model_config", "linked_reference")
    return render(
        request,
        "ai_assistant/execution_defect.html",
        {
            "execution": execution,
            "drafts": drafts,
            "active_config": active_config,
            "existing_defects": execution.get_bugs().order_by("-created_on"),
        },
    )


@login_required
def edit_defect_draft(request, pk):
    draft = get_object_or_404(
        AIDefectDraft.objects.select_related(
            "execution", "execution__run", "execution__case", "linked_reference"
        ),
        pk=pk,
        owner=request.user,
    )
    if not draft.fingerprint:
        draft.fingerprint = defect_fingerprint(draft)
        draft.save(update_fields=("fingerprint", "updated"))
    _require_run_permission(
        request.user, "testruns.view_testrun", draft.execution.run
    )
    if request.method == "POST":
        old_status = draft.status
        form = AIDefectDraftForm(request.POST, instance=draft)
        if form.is_valid():
            draft = form.save()
            draft.fingerprint = defect_fingerprint(draft)
            draft.save(update_fields=("fingerprint", "updated"))
            if old_status != draft.status:
                AIDefectStatusHistory.objects.create(
                    defect=draft,
                    from_status=old_status,
                    to_status=draft.status,
                    source="manual",
                    reason=request.POST.get("status_reason", "人工更新"),
                    changed_by=request.user,
                )
            messages.success(request, "缺陷草稿已保存。")
            return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)
    else:
        form = AIDefectDraftForm(instance=draft)
    return render(
        request,
        "ai_assistant/edit_defect_draft.html",
        {
            "draft": draft,
            "form": form,
            "link_form": DefectLinkForm(
                initial={"name": draft.title[:64]}
            ),
            "can_link": _has_run_permission(
                request.user, "testruns.change_testrun", draft.execution.run
            ),
            "duplicate_candidates": duplicate_candidates(draft),
            "status_history": draft.status_history.select_related("changed_by"),
            "regression_form": RegressionVerificationForm(),
            "regressions": draft.regression_verifications.select_related("regression_run"),
        },
    )


@require_POST
@login_required
def link_defect_draft(request, pk):
    draft = get_object_or_404(
        AIDefectDraft.objects.select_related("execution", "execution__run"),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(
        request.user, "testruns.change_testrun", draft.execution.run
    )
    if draft.linked_reference_id:
        messages.info(request, "该草稿已经关联过缺陷，没有重复创建链接。")
        return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)
    form = DefectLinkForm(request.POST)
    if form.is_valid():
        with transaction.atomic():
            locked = AIDefectDraft.objects.select_for_update().get(
                pk=draft.pk, owner=request.user
            )
            if locked.linked_reference_id is None:
                reference = LinkReference.objects.create(
                    execution=locked.execution,
                    name=form.cleaned_data["name"],
                    url=form.cleaned_data["url"],
                    is_defect=True,
                )
                locked.linked_reference = reference
                locked.save(update_fields=("linked_reference", "updated"))
                transition_defect(
                    locked,
                    "in_progress",
                    user=request.user,
                    source="link",
                    reason="已关联外部缺陷地址",
                )
        messages.success(request, "缺陷地址已关联到这条 Kiwi 测试执行。")
    else:
        messages.error(request, "缺陷名称或地址格式不正确，请重新填写。")
    return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)


@require_POST
@login_required
def sync_defect_status(request, pk):
    draft = get_object_or_404(
        AIDefectDraft.objects.select_related(
            "execution", "execution__run", "linked_reference"
        ),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", draft.execution.run)
    try:
        details = sync_external_defect(draft, request)
    except Exception as exc:
        messages.error(request, f"外部缺陷状态同步失败：{exc}")
    else:
        messages.success(
            request,
            f"状态同步完成：{details.get('status') or '外部系统未返回明确状态'}",
        )
    return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)


@require_POST
@login_required
def create_defect_regression(request, pk):
    draft = get_object_or_404(
        AIDefectDraft.objects.select_related(
            "execution", "execution__case", "execution__run"
        ),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", draft.execution.run)
    form = RegressionVerificationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请填写有效的回归测试运行 ID。")
        return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)
    regression_run = get_object_or_404(TestRun, pk=form.cleaned_data["regression_run_id"])
    _require_run_permission(request.user, "testruns.view_testrun", regression_run)
    status, result = verify_defect_regression(draft, regression_run)
    AIRegressionVerification.objects.create(
        owner=request.user,
        defect_draft=draft,
        regression_run=regression_run,
        status=status,
        result=result,
        notes=form.cleaned_data["notes"],
    )
    if status == "passed":
        transition_defect(
            draft,
            "pending_verification",
            user=request.user,
            source="regression",
            reason=f"TR-{regression_run.pk} 回归通过，等待人工关闭",
        )
        messages.success(request, "回归通过，缺陷已转为“待验证”。")
    elif status == "failed":
        transition_defect(
            draft,
            "in_progress",
            user=request.user,
            source="regression",
            reason=f"TR-{regression_run.pk} 回归失败，自动重新打开",
        )
        messages.warning(request, "回归失败，缺陷已自动重新打开为“处理中”。")
    else:
        messages.info(request, "回归运行中的对应用例尚未完成，已保存验证记录。")
    return redirect("ai_assistant:edit_defect_draft", pk=draft.pk)


@login_required
def run_report(request, pk):
    test_run = get_object_or_404(
        TestRun.objects.select_related(
            "plan", "build", "build__version", "build__version__product", "manager"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", test_run)
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()
    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置一个 AI 模型并设为默认，再生成测试报告。")
            return redirect("ai_assistant:model_settings")
        return _queue_job(
            request,
            "test_report_generation",
            {"test_run_id": test_run.pk},
            f"test_report_generation:run:{test_run.pk}",
            model_config=active_config,
        )

    reports = (
        roles.visible_reports(request.user)
        .filter(test_run=test_run)
        .select_related("model_config", "source_analysis")
        .prefetch_related("regression_verifications", "regression_verifications__regression_run")
    )
    return render(
        request,
        "ai_assistant/run_report.html",
        {
            "test_run": test_run,
            "reports": reports,
            "active_config": active_config,
        },
    )


@login_required
def edit_report(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related(
            "test_run", "model_config"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    can_edit = report.owner_id == request.user.pk
    if request.method == "POST" and not can_edit:
        # 产品成员可以查看并审批别人的报告，但正文只能由作者本人改——改正文会清空
        # 已有的审批签名，不能让审批人顺手把自己刚签的字抹掉。
        raise PermissionDenied
    if request.method == "POST":
        form = AITestReportForm(request.POST, instance=report)
        if form.is_valid():
            with transaction.atomic():
                report = form.save()
                if report.approval_status != "pending":
                    report.approval_status = "pending"
                    report.approved_by = None
                    report.approved_at = None
                    report.signature = ""
                    report.save(
                        update_fields=(
                            "approval_status", "approved_by", "approved_at",
                            "signature", "updated",
                        )
                    )
                revision = report.revisions.count() + 1
                AITestReportRevision.objects.create(
                    report=report,
                    revision=revision,
                    content_snapshot=report_snapshot(report),
                    change_reason=form.cleaned_data["change_reason"],
                    edited_by=request.user,
                )
            messages.success(request, "测试报告已保存。")
            return redirect("ai_assistant:edit_report", pk=report.pk)
    else:
        form = AITestReportForm(instance=report)
    verifications = roles.visible_verifications(request.user).filter(
        source_report=report
    ).select_related("regression_run")
    return render(
        request,
        "ai_assistant/edit_report.html",
        {
            "report": report,
            "form": form,
            "regression_form": RegressionVerificationForm(
                initial={"regression_run_id": report.test_run_id}
            ),
            "verifications": verifications,
            "approval_form": ReportApprovalForm(),
            "can_edit": can_edit,
            "can_approve": roles.can_approve_report(request.user, report),
            "can_manage_gate": roles.can_manage_release_gate(request.user),
            "revisions": report.revisions.select_related("edited_by"),
            "version_history": AITestReport.objects.filter(
                series_uuid=report.series_uuid
            ).select_related("approved_by"),
        },
    )


@require_POST
@login_required
def create_regression_verification(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related("test_run"), pk=pk
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    form = RegressionVerificationForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请填写有效的回归测试运行 ID。")
        return redirect("ai_assistant:edit_report", pk=report.pk)
    regression_run = get_object_or_404(
        TestRun, pk=form.cleaned_data["regression_run_id"]
    )
    _require_run_permission(
        request.user, "testruns.view_testrun", regression_run
    )
    try:
        status, result = verify_regression(report, regression_run)
        AIRegressionVerification.objects.create(
            owner=request.user,
            source_report=report,
            regression_run=regression_run,
            status=status,
            result=result,
            notes=form.cleaned_data["notes"],
        )
    except Exception as exc:
        messages.error(request, f"回归验证失败：{exc}")
    else:
        messages.success(request, "修复后回归验证已完成并保存。")
    return redirect("ai_assistant:edit_report", pk=report.pk)


@require_POST
@login_required
def approve_report(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related(
            "test_run", "test_run__build__version__product"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    if not roles.can_approve_report(request.user, report):
        raise PermissionDenied
    form = ReportApprovalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请填写有效的审批结论。")
        return redirect("ai_assistant:edit_report", pk=report.pk)

    # 审批时现场重算门禁：不复用可能过期的 gate_result，否则「先评估、后改数据」
    # 就能绕过门禁。
    gate_result = evaluate_release_gate(
        report.test_run.build.version.product,
        report.metrics_snapshot,
        [report.test_run_id],
    )
    report.gate_result = gate_result
    decision = form.cleaned_data["decision"]
    waived = False
    if decision == "approved" and not gate_result["passed"]:
        if not roles.can_manage_release_gate(request.user):
            messages.error(
                request,
                "发布门禁未通过，不能直接批准。请修复阻断项，或由测试经理填写理由做风险放行。",
            )
            report.release_decision = derived_release_decision(gate_result, "pending")
            report.save(update_fields=("gate_result", "release_decision", "updated"))
            return redirect("ai_assistant:edit_report", pk=report.pk)
        reason = (form.cleaned_data.get("waive_reason") or "").strip()
        if not reason:
            messages.error(request, "发布门禁未通过：风险放行必须填写理由。")
            report.release_decision = derived_release_decision(gate_result, "pending")
            report.save(update_fields=("gate_result", "release_decision", "updated"))
            return redirect("ai_assistant:edit_report", pk=report.pk)
        report.gate_waived_by = request.user
        report.gate_waived_at = timezone.now()
        report.gate_waive_reason = reason
        waived = True
    if not waived:
        # 门禁通过（或被驳回）时清掉上一次的放行记录；每次放行的留痕在修订历史里。
        report.gate_waived_by = None
        report.gate_waived_at = None
        report.gate_waive_reason = ""

    report.approval_status = decision
    report.approved_by = request.user
    report.approved_at = timezone.now()
    report.approval_comment = form.cleaned_data["comment"]
    report.signature = (
        f"{request.user.get_full_name() or request.user.username} / "
        f"{report.approved_at:%Y-%m-%d %H:%M:%S} / {report.snapshot_hash[:12]}"
        if report.approval_status == "approved"
        else ""
    )
    report.release_decision = derived_release_decision(
        gate_result, report.approval_status, waived=waived
    )
    with transaction.atomic():
        report.save(
            update_fields=(
                "approval_status", "approved_by", "approved_at",
                "approval_comment", "signature", "release_decision",
                "gate_result", "gate_waived_by", "gate_waived_at",
                "gate_waive_reason", "updated",
            )
        )
        # 审批结论与风险放行都进版本历史：事后能查出「谁在什么理由下放行的」。
        AITestReportRevision.objects.create(
            report=report,
            revision=report.revisions.count() + 1,
            content_snapshot=report_snapshot(report),
            change_reason=(
                f"风险放行：{report.gate_waive_reason}" if waived
                else f"审批：{report.get_approval_status_display()}"
            )[:255],
            edited_by=request.user,
        )
    if waived:
        messages.warning(request, f"已由测试经理风险放行并{report.get_approval_status_display()}。")
    else:
        messages.success(request, f"报告已{report.get_approval_status_display()}。")
    return redirect("ai_assistant:edit_report", pk=report.pk)


@require_POST
@login_required
def evaluate_report_gate(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related(
            "test_run", "test_run__build__version__product"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    report.gate_result = evaluate_release_gate(
        report.test_run.build.version.product,
        report.metrics_snapshot,
        [report.test_run_id],
    )
    # 重新评估后发布结论跟着门禁走；放行记录不在这里清，它记的是上一次审批的依据。
    report.release_decision = derived_release_decision(
        report.gate_result,
        report.approval_status,
        waived=report.gate_waived_at is not None,
    )
    report.save(update_fields=("gate_result", "release_decision", "updated"))
    messages.success(request, "发布门禁已按当前产品规则重新评估。")
    return redirect("ai_assistant:edit_report", pk=report.pk)


@login_required
def export_report_html(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related(
            "test_run", "approved_by", "gate_waived_by"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    response = render(request, "ai_assistant/report_export.html", {"report": report})
    response["Content-Disposition"] = f'attachment; filename="test-report-{report.pk}-v{report.version}.html"'
    return response


@login_required
def export_report_pdf(request, pk):
    report = get_object_or_404(
        roles.visible_reports(request.user).select_related(
            "test_run", "approved_by", "gate_waived_by"
        ),
        pk=pk,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    response = HttpResponse(make_chinese_pdf(render_report_lines(report)), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="test-report-{report.pk}-v{report.version}.pdf"'
    return response


@login_required
def release_gate_settings(request):
    """产品级发布门禁规则：一个产品一条，只有测试经理能维护。"""
    if not roles.can_manage_release_gate(request.user):
        raise PermissionDenied
    products = Product.objects.order_by("name")
    if request.method == "POST":
        # 一个产品一条规则：把已存在的那条作为 instance 交给表单。否则 ModelForm 的唯一性
        # 校验会把「这个产品已经有规则」判成重复，第二次保存会被静默拦成表单错误。
        posted_product = request.POST.get("product")
        existing = None
        if posted_product and str(posted_product).isdigit():
            existing = AIReleaseGateRule.objects.filter(product_id=int(posted_product)).first()
        form = AIReleaseGateRuleForm(request.POST, instance=existing)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.updated_by = request.user
            rule.save()
            messages.success(
                request, f"“{rule.product.name}”的发布门禁规则已保存。"
            )
            return redirect("ai_assistant:release_gate_settings")
    else:
        initial = {}
        product_id = request.GET.get("product")
        if product_id and product_id.isdigit():
            existing = AIReleaseGateRule.objects.filter(product_id=int(product_id)).first()
            if existing is not None:
                initial = {
                    "product": existing.product_id,
                    "name": existing.name,
                    "block_priority": existing.block_priority,
                    "min_success_rate": existing.min_success_rate,
                    "require_all_executed": existing.require_all_executed,
                    "max_open_defects": existing.max_open_defects,
                    "is_active": existing.is_active,
                }
        form = AIReleaseGateRuleForm(initial=initial)
    rules = {
        rule.product_id: rule
        for rule in AIReleaseGateRule.objects.select_related("product", "updated_by")
    }
    return render(
        request,
        "ai_assistant/release_gate_settings.html",
        {
            "form": form,
            "products": [{"product": product, "rule": rules.get(product.pk)} for product in products],
        },
    )


@login_required
def iteration_reports(request):
    if request.method == "POST":
        form = IterationReportForm(request.POST)
        if form.is_valid():
            runs = list(
                TestRun.objects.filter(pk__in=form.cleaned_data["run_ids"]).select_related(
                    "plan", "build__version__product"
                )
            )
            if len(runs) != len(form.cleaned_data["run_ids"]):
                form.add_error("run_ids", "部分 TestRun 不存在")
            else:
                for run in runs:
                    _require_run_permission(request.user, "testruns.view_testrun", run)
                product = form.cleaned_data["product"]
                if any(run.plan.product_id != product.pk for run in runs):
                    form.add_error("run_ids", "所有 TestRun 必须属于所选产品")
                else:
                    metrics_snapshot, run_snapshots = build_iteration_snapshot(runs)
                    gate_result = evaluate_release_gate(
                        product, metrics_snapshot, [run.pk for run in runs]
                    )
                    iteration = AIIterationReport.objects.create(
                        owner=request.user,
                        title=form.cleaned_data["title"],
                        product=product,
                        version=form.cleaned_data["version"],
                        metrics_snapshot=metrics_snapshot,
                        run_snapshots=run_snapshots,
                        gate_result=gate_result,
                        conclusion=form.cleaned_data["conclusion"],
                        release_decision="go" if gate_result["passed"] else "no_go",
                        snapshot_hash=canonical_hash(
                            {"metrics": metrics_snapshot, "runs": run_snapshots}
                        ),
                    )
                    iteration.runs.set(runs)
                    messages.success(request, "迭代测试报告已生成，数据已冻结为快照。")
                    return redirect("ai_assistant:iteration_report_detail", pk=iteration.pk)
    else:
        form = IterationReportForm()
    items = roles.visible_iterations(request.user).select_related("product", "version")
    return render(request, "ai_assistant/iteration_reports.html", {"form": form, "items": items})


@login_required
def iteration_report_detail(request, pk):
    # 与列表同口径：同一产品的成员看得到彼此的迭代报告。
    report = get_object_or_404(
        roles.visible_iterations(request.user).select_related("product", "version").prefetch_related("runs"),
        pk=pk,
    )
    return render(request, "ai_assistant/iteration_report_detail.html", {"report": report})


@login_required
def report_trends(request):
    # 与看板同口径：同一产品的成员看得到彼此的报告。
    reports = roles.visible_reports(request.user).filter(is_current=True).select_related(
        "test_run", "test_run__plan__product", "test_run__build__version"
    )[:100]
    rows = []
    for report in reversed(list(reports)):
        metrics = report.metrics_snapshot.get("metrics", {})
        regressions = list(report.regression_verifications.all())
        rows.append(
            {
                "report": report,
                "success_rate": metrics.get("success_rate", 0),
                "defect_rate": metrics.get("failure_rate", 0),
                "regression_rate": round(
                    sum(1 for item in regressions if item.status == "passed") * 100 / len(regressions), 1
                ) if regressions else None,
            }
        )
    return render(request, "ai_assistant/report_trends.html", {"rows": rows})


@login_required
@permission_required("testcases.view_testcase", raise_exception=True)
def review_case(request, pk):
    test_case = get_object_or_404(
        TestCase.objects.select_related("category", "priority", "case_status"),
        pk=pk,
    )
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()

    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置一个 AI 模型并设为默认，再开始评审。")
            return redirect("ai_assistant:model_settings")
        return _queue_job(
            request,
            "test_case_review",
            {"test_case_id": test_case.pk},
            f"test_case_review:case:{test_case.pk}",
            model_config=active_config,
        )

    reviews = AITestCaseReview.objects.filter(
        owner=request.user, test_case=test_case
    ).select_related("model_config", "applied_by")
    return render(
        request,
        "ai_assistant/review_case.html",
        {
            "test_case": test_case,
            "reviews": reviews,
            "active_config": active_config,
        },
    )


@require_POST
@login_required
@permission_required("testcases.change_testcase", raise_exception=True)
def apply_review(request, pk):
    review = get_object_or_404(
        AITestCaseReview, pk=pk, owner=request.user
    )
    try:
        test_case, applied = apply_test_case_review(review, request.user)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect("ai_assistant:review_case", pk=review.test_case_id)
    if applied:
        messages.success(request, f"已将 AI 优化应用到 TC-{test_case.pk}。")
    else:
        messages.info(request, "该评审结果已经应用过，没有重复修改正式用例。")
    return redirect("ai_assistant:review_case", pk=test_case.pk)
