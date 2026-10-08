"""Freeze rule versions at task submission, not at worker execution."""
import copy
from .models import AIRequest
from .services import capture_instruction_snapshot

OPERATIONS = ("requirement_analysis", "test_case_generation", "dev_task_breakdown")


def prepare_payload(owner, operation, payload):
    data = copy.deepcopy(payload)
    if operation not in OPERATIONS or not data.get("request_id"):
        return data
    if "rules_snapshot" in data:
        # A retry must replay the originally submitted guidance.
        return data
    source = AIRequest.objects.select_related("category").get(pk=data["request_id"])
    snapshot = capture_instruction_snapshot(owner, source.category)
    extra = str(data.get("additional_instructions") or "").strip()
    if len(extra) > 4000:
        raise ValueError("本次补充要求不能超过 4000 个字符。")
    if extra:
        data["additional_instructions"] = extra
        snapshot[operation].append({"name": "本次补充要求", "version": 1,
            "instructions": extra, "scope": "task", "operation": operation})
    data["rules_snapshot"] = snapshot
    return data
