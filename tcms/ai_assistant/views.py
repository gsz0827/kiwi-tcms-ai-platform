from django.contrib import messages
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

from .forms import (
    AIDefectDraftForm,
    AIReleaseGateRuleForm,
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
from tcms.management.models import Product, Version
from tcms.core.contrib.linkreference.models import LinkReference
from tcms.testcases.models import TestCase
from tcms.testplans.models import TestPlan
from tcms.testruns.models import TestExecution, TestRun

from .models import (
    AIDefectDraft,
    AIDefectStatusHistory,
    AIIterationReport,
    AIJob,
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
from .engineering import (
    build_iteration_snapshot,
    canonical_hash,
    defect_fingerprint,
    duplicate_candidates,
    evaluate_release_gate,
    make_chinese_pdf,
    render_report_lines,
    report_snapshot,
    sync_external_defect,
    transition_defect,
    verify_defect_regression,
)
from .jobs import enqueue_ai_job
from .services import (
    apply_test_case_review,
    build_test_run_snapshot,
    import_test_case_drafts,
    verify_regression,
)


def _has_run_permission(user, permission, test_run):
    return user.has_perm(permission) or user.has_perm(permission, test_run)


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
        resource = get_object_or_404(
            AIRequest.objects.select_related("category__product"),
            pk=object_id,
            created_by=user,
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
    request_query = AIRequest.objects.filter(created_by=request.user)
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
    analysis_query = AITestRunAnalysis.objects.filter(owner=request.user)
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
    report_query = AITestReport.objects.filter(owner=request.user)
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
    verification_query = AIRegressionVerification.objects.filter(owner=request.user)
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

    defect_query = AIDefectDraft.objects.filter(owner=request.user)
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
                "detail": "当前账号尚未启用模型，AI 功能暂不可用。",
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


@login_required
def index(request):
    active_config = AIModelConfig.objects.filter(
        owner=request.user, is_active=True
    ).first()

    if request.method == "POST":
        if active_config is None:
            messages.warning(request, "请先配置并启用一个 AI 模型，再使用 AI 助手。")
            return redirect("ai_assistant:model_settings")

        form = AIRequestForm(request.POST)
        if form.is_valid():
            ai_request = form.save(commit=False)
            ai_request.created_by = request.user
            ai_request.save()
            AIRequirementVersion.objects.create(
                request=ai_request,
                version=ai_request.version,
                title=ai_request.title,
                requirement=ai_request.requirement,
                change_summary="创建需求",
                changed_by=request.user,
            )
            action = request.POST.get("action", "generate")
            operation = (
                "requirement_analysis" if action == "analyze" else "test_case_generation"
            )
            return _queue_job(
                request,
                operation,
                {"request_id": ai_request.pk},
                f"{operation}:request:{ai_request.pk}",
                model_config=active_config,
            )
    else:
        form = AIRequestForm()

    ai_requests = (
        AIRequest.objects.filter(created_by=request.user)
        .select_related(
            "category",
            "category__product",
            "analysis_model_config",
            "coverage_model_config",
        )
        .prefetch_related("drafts", "drafts__imported_case")
        .order_by("-created")
    )
    for ai_request in ai_requests:
        ai_request.unimported_draft_count = sum(
            1 for draft in ai_request.drafts.all() if draft.imported_case_id is None
        )
    return render(
        request,
        "ai_assistant/index.html",
        {
            "form": form,
            "ai_requests": ai_requests,
            "active_config": active_config,
        },
    )


@require_POST
@login_required
def generate_from_analysis(request, pk):
    ai_request = get_object_or_404(
        AIRequest, pk=pk, created_by=request.user
    )
    if not ai_request.analysis:
        messages.warning(request, "该请求还没有可用的需求分析结果。")
        return redirect("ai_assistant:index")
    if ai_request.drafts.exists():
        messages.info(request, "该请求已经生成过测试用例草稿。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置并启用一个 AI 模型，再生成测试用例。")
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
        AIRequest.objects.prefetch_related("drafts"),
        pk=pk,
        created_by=request.user,
    )
    if not ai_request.drafts.all():
        messages.warning(request, "该请求还没有可分析的测试用例草稿。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置并启用一个 AI 模型，再分析用例覆盖率。")
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
        AIRequest.objects.prefetch_related("drafts"),
        pk=pk,
        created_by=request.user,
    )
    if not ai_request.coverage_analysis:
        messages.warning(request, "该请求还没有可用的覆盖分析结果。")
        return redirect("ai_assistant:index")
    if not ai_request.has_coverage_gaps:
        messages.info(request, "当前覆盖分析没有需要补充的测试缺口。")
        return redirect("ai_assistant:index")
    if not AIModelConfig.objects.filter(owner=request.user, is_active=True).exists():
        messages.warning(request, "请先配置并启用一个 AI 模型，再补充测试用例。")
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
        request__created_by=request.user,
    )
    if draft.imported_case_id:
        messages.info(request, "该草稿已导入，请在 Kiwi 正式测试用例中继续编辑。")
        return redirect("testcases-edit", pk=draft.imported_case_id)

    form = AITestCaseDraftForm(request.POST or None, instance=draft)
    if request.method == "POST" and form.is_valid():
        draft = form.save(commit=False)
        draft.requirement_version = draft.request.version
        draft.needs_update = False
        draft.save()
        AIRequest.objects.filter(pk=draft.request_id).update(
            coverage_analysis={},
            coverage_raw="",
            coverage_model_config=None,
            coverage_analyzed_at=None,
        )
        if not draft.request.drafts.filter(needs_update=True).exists():
            AIRequest.objects.filter(pk=draft.request_id).update(needs_case_review=False)
        messages.success(request, f"{draft.case_number} 草稿已保存。")
        return redirect("ai_assistant:index")
    return render(
        request,
        "ai_assistant/edit_draft.html",
        {"form": form, "draft": draft},
    )


@login_required
def edit_requirement(request, pk):
    ai_request = get_object_or_404(AIRequest, pk=pk, created_by=request.user)
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
                        pk=ai_request.pk, created_by=request.user
                    )
                    locked.title = form.cleaned_data["title"]
                    locked.requirement = form.cleaned_data["requirement"]
                    locked.version += 1
                    locked.needs_case_review = True
                    locked.changed_at = timezone.now()
                    locked.analysis = {}
                    locked.coverage_analysis = {}
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
        AIRequest.objects.select_related("category__product").prefetch_related(
            "drafts", "drafts__imported_case", "versions"
        ),
        pk=pk,
        created_by=request.user,
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
    return render(
        request,
        "ai_assistant/requirement_trace.html",
        {
            "ai_request": ai_request,
            "rows": rows,
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
    ai_request = get_object_or_404(
        AIRequest, pk=pk, created_by=request.user
    )
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
        messages.error(request, "该模型没有 API 密钥，请先编辑后再启用。")
    else:
        config.is_active = True
        config.save()
        messages.success(request, f"已启用模型“{config.name}”。")
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
            messages.warning(request, "请先配置并启用一个 AI 模型，再分析测试运行。")
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
            messages.warning(request, "请先配置并启用一个 AI 模型，再生成缺陷草稿。")
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
            messages.warning(request, "请先配置并启用一个 AI 模型，再生成测试报告。")
            return redirect("ai_assistant:model_settings")
        return _queue_job(
            request,
            "test_report_generation",
            {"test_run_id": test_run.pk},
            f"test_report_generation:run:{test_run.pk}",
            model_config=active_config,
        )

    reports = (
        AITestReport.objects.filter(owner=request.user, test_run=test_run)
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
        AITestReport.objects.select_related("test_run", "model_config"),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
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
    verifications = AIRegressionVerification.objects.filter(
        owner=request.user, source_report=report
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
            "revisions": report.revisions.select_related("edited_by"),
            "version_history": AITestReport.objects.filter(
                owner=request.user, series_uuid=report.series_uuid
            ).select_related("approved_by"),
        },
    )


@require_POST
@login_required
def create_regression_verification(request, pk):
    report = get_object_or_404(
        AITestReport.objects.select_related("test_run"), pk=pk, owner=request.user
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
        AITestReport.objects.select_related("test_run"), pk=pk, owner=request.user
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    form = ReportApprovalForm(request.POST)
    if not form.is_valid():
        messages.error(request, "请填写有效的审批结论。")
        return redirect("ai_assistant:edit_report", pk=report.pk)
    report.approval_status = form.cleaned_data["decision"]
    report.approved_by = request.user
    report.approved_at = timezone.now()
    report.approval_comment = form.cleaned_data["comment"]
    report.signature = (
        f"{request.user.get_full_name() or request.user.username} / "
        f"{report.approved_at:%Y-%m-%d %H:%M:%S} / {report.snapshot_hash[:12]}"
        if report.approval_status == "approved"
        else ""
    )
    report.save(
        update_fields=(
            "approval_status", "approved_by", "approved_at",
            "approval_comment", "signature", "updated",
        )
    )
    messages.success(request, f"报告已{report.get_approval_status_display()}。")
    return redirect("ai_assistant:edit_report", pk=report.pk)


@require_POST
@login_required
def evaluate_report_gate(request, pk):
    report = get_object_or_404(
        AITestReport.objects.select_related(
            "test_run", "test_run__build__version__product"
        ),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    report.gate_result = evaluate_release_gate(
        request.user,
        report.test_run.build.version.product,
        report.metrics_snapshot,
        [report.test_run_id],
    )
    if not report.gate_result["passed"]:
        report.release_decision = "no_go"
    report.save(update_fields=("gate_result", "release_decision", "updated"))
    messages.success(request, "发布门禁已按当前规则重新评估。")
    return redirect("ai_assistant:edit_report", pk=report.pk)


@login_required
def export_report_html(request, pk):
    report = get_object_or_404(
        AITestReport.objects.select_related("test_run", "approved_by"),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    response = render(request, "ai_assistant/report_export.html", {"report": report})
    response["Content-Disposition"] = f'attachment; filename="test-report-{report.pk}-v{report.version}.html"'
    return response


@login_required
def export_report_pdf(request, pk):
    report = get_object_or_404(
        AITestReport.objects.select_related("test_run", "approved_by"),
        pk=pk,
        owner=request.user,
    )
    _require_run_permission(request.user, "testruns.view_testrun", report.test_run)
    response = HttpResponse(make_chinese_pdf(render_report_lines(report)), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="test-report-{report.pk}-v{report.version}.pdf"'
    return response


@login_required
def release_gate_settings(request):
    if request.method == "POST":
        form = AIReleaseGateRuleForm(request.POST, owner=request.user)
        if form.is_valid():
            rule = form.save(commit=False)
            rule.owner = request.user
            rule.save()
            messages.success(request, "发布门禁规则已保存。")
            return redirect("ai_assistant:release_gate_settings")
    else:
        form = AIReleaseGateRuleForm(owner=request.user)
    rules = AIReleaseGateRule.objects.filter(owner=request.user).select_related("product")
    return render(request, "ai_assistant/release_gate_settings.html", {"form": form, "rules": rules})


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
                        request.user, product, metrics_snapshot, [run.pk for run in runs]
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
    items = AIIterationReport.objects.filter(owner=request.user).select_related("product", "version")
    return render(request, "ai_assistant/iteration_reports.html", {"form": form, "items": items})


@login_required
def iteration_report_detail(request, pk):
    report = get_object_or_404(
        AIIterationReport.objects.select_related("product", "version").prefetch_related("runs"),
        pk=pk,
        owner=request.user,
    )
    return render(request, "ai_assistant/iteration_report_detail.html", {"report": report})


@login_required
def report_trends(request):
    reports = AITestReport.objects.filter(owner=request.user, is_current=True).select_related(
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
            messages.warning(request, "请先配置并启用一个 AI 模型，再开始评审。")
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
    test_case, applied = apply_test_case_review(review, request.user)
    if applied:
        messages.success(request, f"已将 AI 优化应用到 TC-{test_case.pk}。")
    else:
        messages.info(request, "该评审结果已经应用过，没有重复修改正式用例。")
    return redirect("ai_assistant:review_case", pk=test_case.pk)
