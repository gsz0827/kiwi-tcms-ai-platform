"""Read published result links using execution-time identity, never current config names."""

from django.db.models import Q
from .models import AutomationArchive


def published_execution_ids(rows, positions):
    return {
        row["execution_id"]
        for row in rows
        if row.get("position") in positions
        and type(row.get("execution_id")) is int
        and row["execution_id"] > 0
    }


def archived_trace(owner, web_positions, api_results):
    positions = {("web", str(pk)): values for pk, values in web_positions.items()}
    for result in api_results:
        positions.setdefault(("api", str(result.run_id)), set()).add(result.position)
    if not positions:
        return [], set()
    web_ids = [pk for kind, pk in positions if kind == "web"]
    api_ids = [pk for kind, pk in positions if kind == "api"]
    archives = list(
        AutomationArchive.objects.filter(owner=owner)
        .filter(Q(kind="web", source_id__in=web_ids) | Q(kind="api", source_id__in=api_ids))
        .select_related("report")
    )
    executions = set()
    for archive in archives:
        executions |= published_execution_ids(
            archive.results, positions[(archive.kind, str(archive.source_id))]
        )
    return archives, executions
