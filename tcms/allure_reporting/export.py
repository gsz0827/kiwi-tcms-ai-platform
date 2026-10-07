"""Export our custom runners using Allure's documented result format, not pytest."""

import hashlib
import json
import uuid
from zoneinfo import ZoneInfo
from django.conf import settings
from django.utils import timezone
from tcms.ai_assistant.crypto import decrypt_api_key

MAX_RESULTS = {"web": 20, "api": 200}
MAX_ATTACHMENTS = 6 * 1024 * 1024


def milliseconds(value):
    if not value:
        return None
    if timezone.is_naive(value):
        # Our database uses UTC-naive dates; the account's display zone is separate.
        value = timezone.make_aware(value, ZoneInfo(settings.TIME_ZONE))
    return int(value.timestamp() * 1000)


def make_results(kind, run):
    terminal = run.terminal if kind == "web" else run.is_terminal
    if not terminal:
        raise ValueError("任务尚未结束。")
    snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
    cases = snapshot.get("cases", [])
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_RESULTS[kind]:
        raise ValueError("执行快照缺失或范围超过报告限制。")
    saved = {result.position: result for result in run.results.all()}
    start = milliseconds(run.started or run.created)
    stop = max(start, milliseconds((run.finished if kind == "web" else run.completed) or run.created))
    counts = {state: 0 for state in ("passed", "failed", "broken", "skipped")}
    documents, attachments, attachment_size = [], {}, 0
    for index, case in enumerate(cases, 1 if kind == "web" else 0):
        result = saved.get(index)
        state = getattr(result, "status", "pending")
        status = {"passed": "passed", "failed": "failed", "error": "broken"}.get(state, "skipped")
        counts[status] += 1
        name = str(case.get("name", "未命名用例"))[:255]
        identity = (
            case.get("business_case_id" if kind == "web" else "test_case_id")
            or case.get("id" if kind == "web" else "case_id")
            or index
        )
        full_name = f"{kind}.product.{run.product_id}.case.{identity}"
        dataset = case.get("dataset", 0)
        elapsed = getattr(result, "elapsed_ms", None) or 0
        begin = (milliseconds(getattr(result, "started", None)) if kind == "api" else None) or start
        end = (milliseconds(getattr(result, "completed", None)) if kind == "api" else None) or min(
            stop, begin + elapsed
        )
        item = {
            "uuid": str(uuid.uuid4()),
            "name": name,
            "fullName": full_name,
            "historyId": hashlib.sha256(f"{full_name}.dataset.{dataset}".encode()).hexdigest(),
            "testCaseId": hashlib.sha256(full_name.encode()).hexdigest(),
            "status": status,
            "stage": "finished",
            "start": begin,
            "stop": max(begin, end),
            "labels": [
                {"name": "parentSuite", "value": "Web 自动化" if kind == "web" else "接口自动化"},
                {"name": "suite", "value": run.product.name},
                {"name": "subSuite", "value": f"数据组 {int(dataset)+1}"},
                {"name": "framework", "value": "Kiwi 自定义执行器"},
            ],
            "parameters": [
                {"name": "执行编号", "value": str(run.pk), "excluded": True},
                {"name": "数据组", "value": str(int(dataset) + 1)},
            ],
            "steps": [],
            "attachments": [],
        }
        if status == "skipped":
            item["statusDetails"] = {
                "message": "未取得执行结果：任务取消、中断或前序失败；不计为通过。"
            }
        elif status == "failed":
            item["statusDetails"] = {
                "message": "测试断言或前置步骤失败，请结合步骤与原执行记录核对。"
            }
        elif status == "broken":
            item["statusDetails"] = {"message": "请求或执行环境异常，不等同于业务断言失败。"}
        if result and kind == "web":
            # No locator/input/expected values are copied from the encrypted snapshot.
            item["steps"] = [
                {
                    "name": str(step.get("action", "测试步骤"))[:200],
                    "status": "passed" if step.get("status") == "passed" else "failed",
                    "stage": "finished",
                }
                for step in result.steps[:30]
            ]
            if result.screenshot:
                content = bytes(result.screenshot)
                if (
                    len(content) <= 2 * 1024 * 1024
                    and attachment_size + len(content) <= MAX_ATTACHMENTS
                    and content.startswith(b"\x89PNG\r\n\x1a\n")
                ):
                    filename = str(uuid.uuid4()) + "-attachment.png"
                    attachments[filename] = content
                    attachment_size += len(content)
                    item["attachments"].append(
                        {
                            "name": "失败截图（仅任务所属账号可见）",
                            "source": filename,
                            "type": "image/png",
                        }
                    )
                else:
                    item["statusDetails"] = {
                        "message": "截图超出报告附件限制或格式不受支持，请查看原执行记录。"
                    }
        elif result:
            # Only assertion labels/outcomes, not expected/actual or HTTP payloads.
            item["steps"] = [
                {
                    "name": str(check.get("label", "接口断言"))[:200],
                    "status": "passed" if check.get("passed") is True else "failed",
                    "stage": "finished",
                }
                for check in result.checks[:50]
            ]
            if result.status_code:
                item["parameters"].append({"name": "HTTP 状态码", "value": str(result.status_code)})
        documents.append(item)
    return (
        documents,
        attachments,
        dict(counts, total=len(documents), run_id=str(run.pk), execution_status=run.status),
    )
