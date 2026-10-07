"""A regression compares new execution evidence, not the original failed run."""

import hashlib
import json

from tcms.testruns.models import TestExecution
from .execution_identity import canonical_case_id


def execution_context(execution):
    properties = sorted(execution.properties().values_list("name", "value"))
    return hashlib.sha256(json.dumps(properties, ensure_ascii=False).encode()).hexdigest()




def validate_regression_run(source_run, target):
    if target.pk == source_run.pk:
        raise ValueError("不能使用原失败任务作为复测任务，请新建修复后复测任务。")
    product_id = source_run.plan.product_id
    if (
        target.plan.product_id != product_id
        or target.build.version.product_id != product_id
        or target.build.version_id != target.plan.product_version_id
    ):
        raise ValueError("复测任务的项目或计划、构建版本不匹配。")


def evaluate_execution(source, target, *, as_of=None, required=1):
    validate_regression_run(source.run, target)
    identity = canonical_case_id(source)
    source_history = source.history.all()
    if as_of:
        source_history = source_history.filter(history_date__lte=as_of)
    evidence = source_history.order_by("-history_date", "-history_id").first()
    boundary = evidence.stop_date or evidence.history_date if evidence else source.stop_date
    created = target.history.order_by("history_date", "history_id").first()
    if boundary and (not created or created.history_date <= boundary):
        raise ValueError("复测任务早于原失败结果，请新建任务，不能复用旧执行记录。")
    matches = [
        item
        for item in target.executions.select_related("status", "case", "run__plan").order_by("pk")
        if canonical_case_id(item) == identity
        and execution_context(item) == execution_context(source)
    ]
    if len(matches) < required:
        return "missing", matches, identity
    weights = []
    for item in matches:
        latest = item.history.latest()
        finished = item.stop_date or latest.history_date
        valid = (
            item.build_id == target.build_id
            and item.case.category.product_id == target.plan.product_id
            and (not boundary or finished > boundary)
        )
        weights.append(item.status.weight if valid else 0)
    if any(weight < 0 for weight in weights):
        return "failed", matches, identity
    if all(weight > 0 for weight in weights):
        return "passed", matches, identity
    return "pending", matches, identity


def failed_report_executions(report):
    failures = report.metrics_snapshot.get("failure_details") or []
    if not failures:
        raise ValueError("来源报告没有失败执行，不需要创建修复后复测任务。")
    if report.metrics_snapshot.get("failure_details_truncated"):
        raise ValueError("报告失败明细已截断，请按缺陷分别创建复测任务，避免遗漏。")
    executions = []
    for row in failures:
        execution = (
            TestExecution.objects.select_related("case__category", "run__plan")
            .filter(pk=row.get("execution_id"), run_id=report.test_run_id, case_id=row.get("case_id"))
            .first()
        )
        if not execution:
            raise ValueError("来源报告的失败执行已删除或发生变化，请核对历史记录。")
        if row.get("execution_context") and row["execution_context"] != execution_context(execution):
            raise ValueError("来源失败执行的参数环境已修改，请核对历史报告，不能混用其他环境结果。")
        executions.append(execution)
    return executions
