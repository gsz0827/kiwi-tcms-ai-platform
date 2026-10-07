"""Business-case identity captured at submission, never guessed from live bindings."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from tcms.testcases.models import TestCase
from .models import AutomationArchive


def capture_version(case, product_id):
    if not case:
        return None
    if case.category.product_id != product_id:
        raise ValueError("关联用例已移到其他项目，请重新配置。")
    history = case.history.order_by("-history_date", "-history_id").first()
    return history.history_id if history else None


def resolve_archive_case(user, source, row):
    case_id = row.get("business_case_id")
    if not case_id:
        raise ValueError(
            "本次执行未保存业务用例关联。请先关联用例库中的用例，再重新执行；不会自动创建重复用例。"
        )
    case = (
        TestCase.objects.select_for_update()
        .filter(pk=case_id, category__product_id=source.product_id)
        .first()
    )
    if not case:
        raise ValueError("执行时关联的业务用例已删除或移到其他项目，不能归档。")
    if not (
        user.has_perm("testcases.view_testcase") or user.has_perm("testcases.view_testcase", case)
    ):
        raise PermissionDenied("无权查看执行时关联的业务用例。")
    version = row.get("business_case_version")
    history = case.history.filter(history_id=version).first() if version else None
    if not version:
        # Older snapshots with a captured ID can recover only the version that
        # actually existed at submission time, not today's latest content.
        history = (
            case.history.filter(history_date__lte=source.created)
            .order_by("-history_date", "-history_id")
            .first()
        )
    if not history:
        raise ValueError("无法确认执行时的用例版本，请关联业务用例后重新执行。")
    return case, history.history_id


def canonical_case_id(execution):
    """Read-only compatibility for old archives which created duplicate cases."""
    archive = AutomationArchive.objects.filter(test_run_id=execution.run_id).first()
    if not archive:
        return execution.case_id
    row = next((r for r in archive.results if r.get("execution_id") == execution.pk), None)
    if not row:
        return execution.case_id
    if row.get("business_case_id"):
        candidate = row["business_case_id"]
    else:
        from .automation_archive import source_run, source_rows

        try:
            source = source_run(archive.owner, archive.kind, archive.source_id)
            captured = next(
                r for r in source_rows(source, archive.kind) if r["position"] == row["position"]
            )
            candidate = captured.get("business_case_id")
        except (ValueError, KeyError, StopIteration, Http404):
            return execution.case_id
    if (
        candidate
        and TestCase.objects.filter(
            pk=candidate, category__product_id=execution.run.plan.product_id
        ).exists()
    ):
        return candidate
    return execution.case_id
