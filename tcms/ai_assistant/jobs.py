import json
import uuid

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from tcms.testcases.models import TestCase
from tcms.testruns.models import TestExecution, TestRun

from .engineering import (
    canonical_hash,
    defect_fingerprint,
    evaluate_release_gate,
)
from .models import (
    AIDefectDraft,
    AIJob,
    AIModelConfig,
    AIRequest,
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
    generate_coverage_gap_test_cases,
    generate_defect_draft,
    generate_test_cases,
    generate_test_report,
    review_test_case,
    test_model_connection,
)


class JobCancelled(RuntimeError):
    pass


def enqueue_ai_job(
    owner, operation, payload, *, model_config=None, dedupe_key="", attempts=1
):
    config = model_config or AIModelConfig.objects.filter(
        owner=owner, is_active=True
    ).first()
    if config is None:
        raise RuntimeError("请先在“AI 模型管理”中配置并启用一个模型")
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
                return existing, False
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
    now = timezone.now()
    AIJob.objects.filter(pk=job.pk, status="running").update(
        progress=progress, stage=stage, heartbeat=now
    )
    job.progress = progress
    job.stage = stage
    job.heartbeat = now


def _result(url, **summary):
    return url, summary


def _execute_requirement_analysis(job):
    ai_request = AIRequest.objects.get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    _set_progress(job, 25, "正在调用模型分析需求")
    analysis, config, raw_result = analyze_requirement(
        ai_request.title,
        ai_request.requirement,
        job.owner,
        model_config=job.model_config,
    )
    _set_progress(job, 82, "模型已返回，正在保存需求分析")
    ai_request.analysis = analysis
    ai_request.analysis_raw = raw_result
    ai_request.analysis_model_config = config
    ai_request.analyzed_at = timezone.now()
    ai_request.save(
        update_fields=(
            "analysis",
            "analysis_raw",
            "analysis_model_config",
            "analyzed_at",
        )
    )
    return _result(
        f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
        request_id=ai_request.pk,
        risk_level=analysis.get("risk_level"),
    )


def _execute_test_case_generation(job):
    ai_request = AIRequest.objects.get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    _set_progress(job, 25, "正在调用模型生成测试用例")
    test_cases = generate_test_cases(
        ai_request.title,
        ai_request.requirement,
        job.owner,
        analysis=ai_request.analysis or None,
        model_config=job.model_config,
    )
    _set_progress(job, 82, "模型已返回，正在保存用例草稿")
    with transaction.atomic():
        locked_request = AIRequest.objects.select_for_update().get(
            pk=ai_request.pk, created_by=job.owner
        )
        if locked_request.drafts.exists():
            raise RuntimeError("该请求已经生成过测试用例草稿")
        locked_request.result = json.dumps(test_cases, ensure_ascii=False, indent=2)
        locked_request.save(update_fields=("result",))
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
    return _result(
        f"{reverse('ai_assistant:index')}#request-{ai_request.pk}",
        request_id=ai_request.pk,
        generated_count=len(test_cases),
    )


def _execute_coverage_analysis(job):
    ai_request = AIRequest.objects.prefetch_related("drafts").get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    _set_progress(job, 25, "正在调用模型分析需求覆盖")
    result, config, raw_result = analyze_test_coverage(
        ai_request, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存覆盖矩阵")
    ai_request.coverage_analysis = result
    ai_request.coverage_raw = raw_result
    ai_request.coverage_model_config = config
    ai_request.coverage_analyzed_at = timezone.now()
    ai_request.save(
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
        coverage_score=result.get("overall_score"),
    )


def _execute_coverage_supplement(job):
    ai_request = AIRequest.objects.prefetch_related("drafts").get(
        pk=job.payload["request_id"], created_by=job.owner
    )
    _set_progress(job, 25, "正在调用模型补充覆盖缺口")
    test_cases = generate_coverage_gap_test_cases(
        ai_request, job.owner, model_config=job.model_config
    )
    _set_progress(job, 82, "模型已返回，正在保存补充用例")
    with transaction.atomic():
        locked_request = AIRequest.objects.select_for_update().get(
            pk=ai_request.pk, created_by=job.owner
        )
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
    _set_progress(job, 25, "正在调用模型分析测试运行")
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
            job.owner,
            test_run.build.version.product,
            snapshot,
            [test_run.pk],
        )
        if not report.gate_result["passed"]:
            report.release_decision = "no_go"
        report.save(update_fields=("gate_result", "release_decision", "updated"))
    return _result(
        reverse("ai_assistant:edit_report", args=[report.pk]), report_id=report.pk
    )


def _execute_connection_test(job):
    _set_progress(job, 25, "正在连接模型服务")
    result = test_model_connection(job.model_config)
    _set_progress(job, 82, "模型已响应，正在保存测试结果")
    return _result(reverse("ai_assistant:model_settings"), **result)


HANDLERS = {
    "requirement_analysis": _execute_requirement_analysis,
    "test_case_generation": _execute_test_case_generation,
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
        AIJob.objects.filter(pk=job.pk).update(
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


def execute_next_job():
    job = claim_next_job()
    if job is None:
        return False
    execute_job(job)
    return True
