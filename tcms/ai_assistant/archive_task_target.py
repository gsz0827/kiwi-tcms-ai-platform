"""Explicit Web/API publication into a fresh, untouched native execution task."""

from collections import Counter, defaultdict
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404
from guardian.shortcuts import get_objects_for_user
from tcms.testruns.models import TestRun
from .models import APIRun, AutomationArchive


def prepare_target(user, source, data, identities):
    selected = data.get("target_run")
    if not selected:
        return None, {}
    if not user.has_perm("testruns.change_testexecution"):
        raise PermissionDenied("没有回写测试执行结果的权限。")
    target = get_object_or_404(
        get_objects_for_user(user, "testruns.change_testrun", klass=TestRun).select_for_update(),
        pk=selected.pk,
        plan_id=data["plan"].pk,
        build_id=data["build"].pk,
    )
    created = target.history.order_by("history_date", "history_id").first()
    if target.stop_date or not created or created.history_date > source.created:
        raise ValueError("请选择本次自动化提交前创建、尚未结束的执行任务，不能将旧结果回填到新任务。")
    if AutomationArchive.objects.filter(test_run=target).exists():
        raise ValueError("该任务已归档，请新建执行任务。")
    from tcms.web_testing.models import WebRun
    if WebRun.objects.filter(test_run=target, status__in=("queued", "running")).exclude(pk=source.pk if isinstance(source, WebRun) else None).exists():
        raise ValueError("该任务仍有 Web 自动化正在执行，请等待完成。")
    if APIRun.objects.filter(test_run=target, status__in=APIRun.ACTIVE_STATUSES).exists():
        raise ValueError("该任务仍有接口自动化正在执行，请等待完成。")
    executions = list(target.executions.select_for_update().select_related("status").order_by("pk"))
    expected = Counter((case.pk, version) for case, version in identities)
    actual = Counter((execution.case_id, execution.case_text_version) for execution in executions)
    if expected != actual:
        raise ValueError("执行任务的用例、实例数量或用例版本与本次自动化快照不一致，不能回写。")
    grouped = defaultdict(list)
    for execution in executions:
        if (
            execution.status.weight != 0
            or execution.start_date
            or execution.stop_date
            or execution.build_id != target.build_id
        ):
            raise ValueError("任务中的执行结果已修改，不能覆盖；请新建任务并重新执行。")
        grouped[(execution.case_id, execution.case_text_version)].append(execution)
    return target, grouped
