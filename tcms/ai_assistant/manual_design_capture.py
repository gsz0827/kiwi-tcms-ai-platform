"""Keep human-authored cases on the same requirement/development-document trace."""

from django.shortcuts import get_object_or_404
from . import roles
from .case_design_context import validate_context
from .models import AITestCaseDraft
from .scenario_design import parse_design


def lock_manual_source(user, source, snapshot):
    locked = get_object_or_404(roles.visible_requests(user).select_for_update(), pk=source.pk)
    tasks = validate_context(user, locked, snapshot, lock=True)
    return locked, tasks


def capture_manual_design(source, tasks, snapshot, case):
    design = parse_design(case.text)
    draft = AITestCaseDraft.objects.create(
        request=source,
        imported_case=case,
        requirement_version=source.version,
        case_number=f"TC-{case.pk}",
        summary=case.summary,
        priority=case.priority.value,
        test_type=design.test_type,
        preconditions=design.preconditions.splitlines(),
        steps=design.steps,
        source_context=dict(snapshot, origin="manual"),
    )
    draft.dev_tasks.set(tasks)
