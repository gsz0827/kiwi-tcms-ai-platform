import json
import uuid

from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from tcms.testcases.models import TestCase
from tcms.testruns.models import TestExecution, TestRun

from .engineering import (
    canonical_hash,
    defect_fingerprint,
    derived_release_decision,
    evaluate_release_gate,
)
from .leases import WorkerHeartbeat
from .models import (
    AIDefectDraft,
    AIDevTask,
    AIJob,
    AIModelConfig,
    AIRequest,
    AIRequirementVersion,
    AITestCaseDraft,
    AITestCaseReview,
    AITestReport,
    AITestRunAnalysis,
)
from .services import (
    analyze_requirement,
    analyze_test_coverage,
    analyze_test_run,
    assign_unique_case_numbers,
    break_down_dev_tasks,
    capture_instruction_snapshot,
    generate_coverage_gap_test_cases,
    generate_defect_draft,
    generate_test_cases,
    generate_test_report,
    review_test_case,
    test_model_connection,
)
from .api_ai import execute_generation


class JobCancelled(RuntimeError):
    pass


def submit_requirement(owner, cleaned_data, operation, model_config):
    """Save a requirement, optionally enqueue AI, and replay identical submissions."""
    token = cleaned_data["submission_token"]
    fingerprint = canonical_hash({
        "title": cleaned_data["title"],
        "requirement": cleaned_data["requirement"],
        "category": cleaned_data["category"].pk,
        **({'document_sections': cleaned_data.get('document_sections')} if cleaned_data.get('document_sections') else {}),
        **({'target_version': cleaned_data['target_version'].pk} if cleaned_data.get('target_version') else {}),
        **({'assigned_to': cleaned_data['assigned_to'].pk} if cleaned_data.get('assigned_to') else {}),
        **({'priority': cleaned_data['priority']} if cleaned_data.get('priority', 'P3') != 'P3' else {}),
        **({'status': cleaned_data['status']} if cleaned_data.get('status', 'draft') != 'draft' else {}),
        "operation": operation,
    })
    dedupe_key = f"submission:{token}"
    with transaction.atomic():
        # Serialize submissions for this account, including requests from two tabs
        # or two web processes. The database constraint is an additional guard.
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        existing = AIRequest.objects.filter(
            created_by=owner, submission_token=token
        ).first()
        if existing:
            if existing.submission_fingerprint != fingerprint:
                raise ValueError("这份表单已提交过其他内容，请刷新页面后重新提交。")
            if operation == "save":
                return existing, False
            job = AIJob.objects.filter(
                owner=owner, dedupe_key=dedupe_key
            ).order_by("created").first()
            if job is None:
                raise ValueError("原任务记录已删除，请刷新页面后重新提交。")
            return job, False

        ai_request = AIRequest.objects.create(
            created_by=owner,
            submission_token=token,
            submission_fingerprint=fingerprint,
            title=cleaned_data["title"],
            requirement=cleaned_data["requirement"],
            category=cleaned_data["category"],
            document_sections=cleaned_data.get('document_sections', {}),
            target_version=cleaned_data.get('target_version'),
            assigned_to=cleaned_data.get('assigned_to'),
            priority=cleaned_data.get('priority', 'P3'),
            status=cleaned_data.get('status', 'draft'),
            skill_snapshot=capture_instruction_snapshot(
                owner, cleaned_data["category"]
            ),
        )
        AIRequirementVersion.objects.create(
            request=ai_request,
            version=ai_request.version,
            title=ai_request.title,
            requirement=ai_request.requirement,
            document_sections=ai_request.document_sections,
            attributes=ai_request.document_attributes,
            change_summary="创建需求",
            changed_by=owner,
        )
        if operation == "save":
            return ai_request, True
        return enqueue_ai_job(
            owner, operation, {"request_id": ai_request.pk},
            model_config=model_config, dedupe_key=dedupe_key,
        )


def enqueue_ai_job(
    owner, operation, payload, *, model_config=None, dedupe_key="", attempts=1
):
    config = model_config or AIModelConfig.objects.filter(
        owner=owner, is_active=True
    ).first()
    if config is None:
        raise RuntimeError("请先在“AI 模型配置”中设置默认模型")
    if config.owner_id != owner.pk:
        raise RuntimeError("模型配置不属于当前账号")

    with transaction.atomic():
        config = AIModelConfig.objects.select_for_update().get(
            pk=config.pk, owner=owner
        )
        if dedupe_key:
            existing = AIJob.objects.filter(
                owner=owner,
                operation=operation,
                dedupe_key=dedupe_key,
                status__in=AIJob.ACTIVE_STATUSES,
            ).first()
            if existing:
                if str(existing.payload.get("additional_instructions") or "").strip() != str(payload.get("additional_instructions") or "").strip():
                    raise ValueError("已有同一需求的任务正在处理，请等待完成后再修改补充要求。")
                return existing, False
        from .job_rules import prepare_payload
        payload = prepare_payload(owner, operation, payload)
        job = AIJob.objects.create(
            owner=owner,
            model_config=config,
            operation=operation,
            payload=payload,
            dedupe_key=dedupe_key,
            attempts=attempts,
        )
    return job, True


def _set_progress(job, progress, stage):
    job.refresh_from_db(fields=("status",))
    if job.status == "cancel_requested":
        raise JobCancelled("用户已请求取消任务")
    if job.status != "running":
        raise JobCancelled("任务已停止，不再继续执行")
    if not get_user_model().objects.filter(pk=job.owner_id, is_active=True).exists():
        raise JobCancelled("所属账号已停用，任务已停止")
    now = timezone.now()
    AIJob.objects.filter(pk=job.pk, status="running").update(
        progress=progress, stage=stage, heartbeat=now
    )
    job.progress = progress
    job.stage = stage
    job.heartbeat = now


def _result(url, **summary):
    return url, summary


def _requirement_fingerprint(ai_request, include_drafts=False):
    context = {key: getattr(ai_request, key) for key in (
        "version", "title", "requirement", "category_id", "skill_snapshot", "analysis", "document_sections", "target_version_id",
    )}
    if include_drafts:
        context["coverage_analysis"] = ai_request.coverage_analysis
        context["drafts"] = [
            {key: getattr(draft, key) for key in (
                "pk", "case_number", "summary", "priority", "test_type", "preconditions",
                "steps", "requirement_version", "needs_update",
            )}
            for draft in ai_request.drafts.all()
        ]
    return canonical_hash(context)


def _lock_unchanged_requirement(ai_request, fingerprint, include_drafts=False):
    locked = AIRequest.objects.select_for_update().get(
        pk=ai_request.pk, created_by=ai_request.created_by
    )
    if _requirement_fingerprint(locked, include_drafts) != fingerprint:
        raise RuntimeError("生成期间需求或用例已变更，本次结果未保存，请基于最新内容重新生成。")
    return locked


def _execute_requirement_analysis(job):
    from . import roles
    ai_request = roles.visible_requests(job.owner).get(pk=job.payload["request_id"])
    if not job.owner.is_active or not roles.can_generate_cases(job.owner, ai_request):
        raise RuntimeError("当前账号已无权分析该需求")
    if job.payload.get("source_version") and job.payload["source_version"] != ai_request.version:
        raise RuntimeError("需求已变更，请基于最新修订重新分析。")
    fingerprint = _requirement_fingerprint(ai_request)
    _set_progress(job, 25, "正在调用模型分析需求")
    analysis, config, raw_result = analyze_requirement(
        ai_request.title,
        ai_request.requirement_document,
        job.owner,
        model_config=job.model_config,
        skill_snapshot=job.payload.get("rules_snapshot", ai_request.skill_snapshot),
    )
    _set_progress(job, 82, "模型已返回，正在保存需求分析")
    with transaction.atomic():
        ai_request = _lock_unchanged_requirement(ai_request, fingerprint)
        ai_request.analysis = analysis
        ai_request.analysis_raw = raw_result
        ai_request.analysis_model_config = config
        ai_request.analyzed_at = timezone.now()
        ai_request.save(
            update_fields=("analysis", "analysis_raw", "analysis_model_config", "analyzed_at")
        )
    return _result(
        f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
        request_id=ai_request.pk,
        risk_level=analysis.get("risk_level"),
    )


def _execute_test_case_generation(job):
    from . import roles
    from .case_design_context import validate_context

    ai_request = roles.visible_requests(job.owner).get(pk=job.payload["request_id"])
    if not roles.can_generate_cases(job.owner, ai_request):
        raise RuntimeError("当前账号已无权为该需求生成用例")
    context = job.payload.get("design_context")
    options = {}
    if context:
        validate_context(job.owner, ai_request, context)
        options["dev_task_context"] = context["dev_tasks"]
    fingerprint = _requirement_fingerprint(ai_request)
    _set_progress(job, 25, "正在调用模型生成测试用例")
    test_cases = generate_test_cases(
        ai_request.title, ai_request.requirement_document, job.owner,
        analysis=ai_request.analysis or None, model_config=job.model_config,
        skill_snapshot=job.payload.get("rules_snapshot", ai_request.skill_snapshot), **options,
    )
    _set_progress(job, 82, "模型已返回，正在保存用例草稿")
    with transaction.atomic():
        locked_request = _lock_unchanged_requirement(ai_request, fingerprint)
        if not roles.can_generate_cases(job.owner, locked_request) or not roles.visible_requests(
            job.owner
        ).filter(pk=locked_request.pk).exists():
            raise RuntimeError("当前账号已无权为该需求生成用例")
        tasks = validate_context(job.owner, locked_request, context, lock=True) if context else []
        if context:
            if locked_request.drafts.filter(
                source_context__fingerprint=context["fingerprint"], source_context__origin="ai"
            ).exists():
                raise RuntimeError("相同设计依据已生成过草稿，请查看已有用例")
            test_cases = assign_unique_case_numbers(
                test_cases, list(locked_request.drafts.values_list("case_number", flat=True))
            )
        elif locked_request.drafts.exists():
            raise RuntimeError("该请求已经生成过测试用例草稿")
        if not locked_request.drafts.exists():
            locked_request.result = json.dumps(test_cases, ensure_ascii=False, indent=2)
            locked_request.save(update_fields=("result",))
        drafts = AITestCaseDraft.objects.bulk_create([
            AITestCaseDraft(
                request=locked_request, requirement_version=locked_request.version,
                source_context=(dict(context, origin="ai") if context else {}), **test_case,
            ) for test_case in test_cases
        ])
        for draft in drafts:
            if tasks:
                draft.dev_tasks.set(tasks)
    result_url = (
        reverse("ai_assistant:case_design", args=[ai_request.pk]) + "#design-drafts"
        if context else f"{reverse('ai_assistant:index')}#request-{ai_request.pk}"
    )
    return _result(result_url, request_id=ai_request.pk, generated_count=len(test_cases))



def _execute_dev_task_breakdown(job):
    from . import roles
    ai_request = roles.visible_requests(job.owner).get(pk=job.payload["request_id"])
    if not job.owner.is_active or not roles.can_generate_cases(job.owner, ai_request):
        raise RuntimeError("当前账号已无权分析该需求")
    if job.payload.get("source_version") and job.payload["source_version"] != ai_request.version:
        raise RuntimeError("需求已变更，请基于最新修订重新分析。")
    fingerprint = _requirement_fingerprint(ai_request)
    _set_progress(job, 25, "正在调用模型拆分开发任务")
    dev_tasks = break_down_dev_tasks(
        ai_request.title,
        ai_request.requirement_document,
        job.owner,
        analysis=ai_request.analysis or None,
        model_config=job.model_config,
        skill_snapshot=job.payload.get("rules_snapshot", ai_request.skill_snapshot),
    )
    _set_progress(job, 82, "模型已返回，正在保存开发任务")
    with transaction.atomic():
        locked_request = _lock_unchanged_requirement(ai_request, fingerprint)
        if locked_request.dev_tasks.exists():
            raise RuntimeError("该需求已经拆分过开发任务，请先删除现有开发任务再重新拆分")
        AIDevTask.objects.bulk_create(
            [
                AIDevTask(
                    request=locked_request,
                    owner=job.owner,
                    requirement_version=locked_request.version,
                    target_version=locked_request.target_version,
                    **dev_task,
                )
                for dev_task in dev_tasks
            ]
        )
    return _result(
        f"{reverse('ai_assistant:dev_task_list')}?request={ai_request.pk}",
        request_id=ai_request.pk,
        generated_count=len(dev_tasks),
    )


def _execute_coverage_analysis(job):
    ai_request = AIRequest.objects.prefetch_related("drafts").get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    fingerprint = _requirement_fingerprint(ai_request, include_drafts=True)
    _set_progress(job, 25, "正在调用模型分析需求覆盖")
    result, config, raw_result = analyze_test_coverage(
        ai_request, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存覆盖矩阵")
    with transaction.atomic():
        ai_request = _lock_unchanged_requirement(ai_request, fingerprint, include_drafts=True)
        ai_request.coverage_analysis = result
        ai_request.coverage_raw = raw_result
        ai_request.coverage_model_config = config
        ai_request.coverage_analyzed_at = timezone.now()
        ai_request.save(
            update_fields=(
                "coverage_analysis", "coverage_raw",
                "coverage_model_config", "coverage_analyzed_at",
            )
        )
    return _result(
        f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
        request_id=ai_request.pk,
        coverage_score=result.get("overall_score"),
    )


def _execute_coverage_supplement(job):
    ai_request = AIRequest.objects.prefetch_related("drafts").get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    fingerprint = _requirement_fingerprint(ai_request, include_drafts=True)
    _set_progress(job, 25, "正在调用模型补充覆盖缺口")
    test_cases = generate_coverage_gap_test_cases(
        ai_request, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存补充用例")
    with transaction.atomic():
        locked_request = _lock_unchanged_requirement(ai_request, fingerprint, include_drafts=True)
        existing_numbers = list(
            locked_request.drafts.values_list("case_number", flat=True)
        )
        test_cases = assign_unique_case_numbers(test_cases, existing_numbers)
        AITestCaseDraft.objects.bulk_create(
            [
                AITestCaseDraft(
                    request=locked_request,
                    requirement_version=locked_request.version,
                    **test_case,
                )
                for test_case in test_cases
            ]
        )
        locked_request.coverage_analysis = {}
        locked_request.coverage_raw = ""
        locked_request.coverage_model_config = None
        locked_request.coverage_analyzed_at = None
        locked_request.save(
            update_fields=(
                "coverage_analysis",
                "coverage_raw",
                "coverage_model_config",
                "coverage_analyzed_at",
            )
        )
    return _result(
        f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
        request_id=ai_request.pk,
        generated_count=len(test_cases),
    )


def _execute_test_case_review(job):
    test_case = TestCase.objects.get(pk=job.payload["test_case_id"])
    _set_progress(job, 25, "正在调用模型评审测试用例")
    result, config, raw_result = review_test_case(
        test_case, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存评审建议")
    review = AITestCaseReview.objects.create(
        owner=job.owner,
        test_case=test_case,
        model_config=config,
        raw_result=raw_result,
        original_summary=test_case.summary,
        original_text=test_case.text or "",
        **result,
    )
    return _result(
        reverse("ai_assistant:review_case", args=[test_case.pk]), review_id=review.pk
    )


def _execute_test_run_analysis(job):
    test_run = TestRun.objects.get(pk=job.payload["test_run_id"])
    _set_progress(job, 25, "正在调用模型分析执行任务")
    result, config, raw_result, snapshot = analyze_test_run(
        test_run, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存运行分析")
    analysis = AITestRunAnalysis.objects.create(
        owner=job.owner,
        test_run=test_run,
        model_config=config,
        execution_snapshot=snapshot,
        result=result,
        raw_result=raw_result,
    )
    return _result(
        reverse("ai_assistant:run_analysis", args=[test_run.pk]),
        analysis_id=analysis.pk,
    )


def _execute_defect_draft(job):
    execution = TestExecution.objects.select_related(
        "run", "run__plan", "case", "case__priority", "case__category",
        "status", "build", "build__version", "build__version__product",
        "assignee", "tested_by",
    ).get(pk=job.payload["execution_id"])
    _set_progress(job, 25, "正在调用模型生成缺陷草稿")
    result, config, raw_result, _snapshot = generate_defect_draft(
        execution, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存缺陷草稿")
    draft = AIDefectDraft.objects.create(
        owner=job.owner,
        execution=execution,
        model_config=config,
        raw_result=raw_result,
        **result,
    )
    draft.fingerprint = defect_fingerprint(draft)
    draft.save(update_fields=("fingerprint", "updated"))
    return _result(
        reverse("ai_assistant:edit_defect_draft", args=[draft.pk]), draft_id=draft.pk
    )


def _execute_test_report(job):
    test_run = TestRun.objects.get(pk=job.payload["test_run_id"])
    source_analysis = AITestRunAnalysis.objects.filter(
        owner=job.owner, test_run=test_run
    ).first()
    _set_progress(job, 25, "正在调用模型生成测试报告")
    result, config, raw_result, snapshot = generate_test_report(
        test_run,
        job.owner,
        source_analysis=source_analysis,
        model_config=job.model_config,
    )
    _set_progress(job, 82, "模型已返回，正在保存测试报告")
    with transaction.atomic():
        previous = (
            AITestReport.objects.select_for_update()
            .filter(owner=job.owner, test_run=test_run, is_current=True)
            .order_by("-version", "-created")
            .first()
        )
        if previous:
            previous.is_current = False
            previous.current_marker = None
            previous.save(update_fields=("is_current", "current_marker", "updated"))
        report = AITestReport.objects.create(
            owner=job.owner,
            test_run=test_run,
            source_analysis=source_analysis,
            model_config=config,
            series_uuid=previous.series_uuid if previous else uuid.uuid4(),
            version=(previous.version + 1) if previous else 1,
            previous_version=previous,
            current_marker=True,
            metrics_snapshot=snapshot,
            snapshot_hash=canonical_hash(snapshot),
            raw_result=raw_result,
            **result,
        )
        report.gate_result = evaluate_release_gate(
            test_run.build.version.product,
            snapshot,
            [test_run.pk],
        )
        # 发布结论完全由门禁推导：LLM 的措辞只进 conclusion/summary，不再决定能不能发布。
        report.release_decision = derived_release_decision(
            report.gate_result, report.approval_status
        )
        report.save(update_fields=("gate_result", "release_decision", "updated"))
    return _result(
        reverse("ai_assistant:edit_report", args=[report.pk]), report_id=report.pk
    )


def _execute_connection_test(job):
    _set_progress(job, 25, "正在连接模型服务")
    result = test_model_connection(job.model_config)
    _set_progress(job, 82, "模型已响应，正在保存测试结果")
    return _result(reverse("ai_assistant:model_settings"), **result)


def _execute_web_case_generation(job):
    from tcms.web_testing.ai_generation import execute_generation as execute_web_generation
    return execute_web_generation(job)


HANDLERS = {
    "web_case_generation": _execute_web_case_generation,
    "api_case_generation": execute_generation,
    "requirement_analysis": _execute_requirement_analysis,
    "test_case_generation": _execute_test_case_generation,
    "dev_task_breakdown": _execute_dev_task_breakdown,
    "coverage_analysis": _execute_coverage_analysis,
    "coverage_supplement": _execute_coverage_supplement,
    "test_case_review": _execute_test_case_review,
    "test_run_analysis": _execute_test_run_analysis,
    "defect_draft_generation": _execute_defect_draft,
    "test_report_generation": _execute_test_report,
    "connection_test": _execute_connection_test,
}


def claim_next_job():
    with transaction.atomic():
        job = (
            AIJob.objects.select_for_update()
            .select_related("owner", "model_config")
            .filter(status="queued")
            .order_by("created")
            .first()
        )
        if job is None:
            return None
        now = timezone.now()
        job.status = "running"
        job.progress = 12
        job.stage = "后台任务已领取，正在准备数据"
        job.started = now
        job.heartbeat = now
        job.error_message = ""
        job.save(
            update_fields=(
                "status", "progress", "stage", "started", "heartbeat", "error_message"
            )
        )
        return job


def execute_job(job):
    try:
        if job.model_config is None:
            raise RuntimeError("任务使用的模型配置已被删除")
        if job.model_config.owner_id != job.owner_id:
            raise RuntimeError("任务模型配置与账号不匹配")
        handler = HANDLERS.get(job.operation)
        if handler is None:
            raise RuntimeError(f"不支持的 AI 任务类型：{job.operation}")
        _set_progress(job, 18, "正在准备任务上下文")
        result_url, result = handler(job)
        _set_progress(job, 94, "业务结果已保存，正在完成任务")
    except JobCancelled as exc:
        AIJob.objects.filter(pk=job.pk, status__in=("running", "cancel_requested")).update(
            status="cancelled",
            progress=100,
            stage="任务已取消",
            error_message=str(exc),
            completed=timezone.now(),
        )
    except Exception as exc:  # worker boundary: persist a safe task failure
        AIJob.objects.filter(pk=job.pk).update(
            status="failed",
            stage="任务执行失败",
            error_message=str(exc)[:2000],
            completed=timezone.now(),
            heartbeat=timezone.now(),
        )
    else:
        completed_count = AIJob.objects.filter(pk=job.pk, status="running").update(
            status="completed",
            progress=100,
            stage="任务已完成",
            result=result,
            result_url=result_url,
            completed=timezone.now(),
            heartbeat=timezone.now(),
        )
        if not completed_count:
            AIJob.objects.filter(pk=job.pk, status="cancel_requested").update(
                status="cancelled",
                progress=100,
                stage="任务已取消",
                error_message="用户已请求取消任务",
                completed=timezone.now(),
                heartbeat=timezone.now(),
            )


def execute_next_job(heartbeat_interval=None):
    """领取并执行一个排队中的任务；执行期间持续刷新心跳。

    心跳让「worker 被强制结束」和「任务还在跑」可以区分：心跳停了，
    ``leases.reclaim_stale_jobs()`` 会把这条任务标成中断，而不是永远停在执行中。
    """
    job = claim_next_job()
    if job is None:
        return False
    options = {} if heartbeat_interval is None else {"interval": heartbeat_interval}
    with WorkerHeartbeat(job=job, **options):
        execute_job(job)
    return True
