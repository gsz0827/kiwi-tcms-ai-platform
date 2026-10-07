"""Read-only presentation of immutable automation results, without reexecution."""

import json
from .crypto import decrypt_api_key

LABELS = {
    "passed": "通过",
    "failed": "失败",
    "error": "执行异常",
    "pending": "待执行",
    "skipped": "未执行",
}


def result_context(kind, run, results):
    records = {item.position: item for item in results}
    terminal = run.terminal if kind == "web" else run.is_terminal
    first = 1 if kind == "web" else 0
    rows = []
    if kind == "web":
        try:
            cases = json.loads(decrypt_api_key(run.snapshot_encrypted)).get("cases", [])
            if not isinstance(cases, list) or len(cases) > 20:
                cases = []
        except (ValueError, TypeError, AttributeError, RuntimeError):
            cases = []
        for position, case in enumerate(cases, first):
            if isinstance(case, dict):
                rows.append((position, case.get("name", "未命名用例"), records.pop(position, None)))
    rows.extend((position, item.name, item) for position, item in sorted(records.items()))
    counts = {key: 0 for key in (*LABELS, "unknown")}
    display = []
    for position, name, item in rows:
        state = item.status if item else ("skipped" if terminal else "pending")
        counts[state if state in counts else "unknown"] += 1
        label = LABELS.get(state, "状态未知")
        if kind == "api":
            label = {"failed": "断言失败", "error": "请求异常"}.get(state, label)
        display.append(
            {
                "number": position + (1 if kind == "api" else 0),
                "name": name,
                "state": state,
                "label": label,
                "result": item,
                "diagnostic_id": f"diagnostic-{kind}-{run.pk}-{position}",
            }
        )
    total = max(run.total, len(display)) if kind == "web" else len(display)
    missing = total - len(display)
    counts["skipped" if terminal else "pending"] += missing
    return {
        "execution_kind": kind,
        "execution_rows": display,
        "execution_counts": counts,
        "execution_total": total,
        "execution_unexecuted": counts["pending"] + counts["skipped"],
        "execution_unknown": counts["unknown"],
    }
