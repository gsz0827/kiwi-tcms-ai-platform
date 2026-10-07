import json
import time
import urllib.error
import urllib.request

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from tcms.core.contrib.linkreference.models import LinkReference
from tcms.management.models import Priority
from tcms.signals import NEW_TEST_CASE_SIGNAL
from tcms.testcases.models import (
    TestCase,
    TestCaseEmailSettings,
    TestCaseStatus,
)

from .crypto import decrypt_api_key
from .models import (
    AIInstructionProfile,
    AIModelConfig,
    AIRequest,
    AITestCaseReview,
    AIUsageLog,
)


ANALYSIS_SYSTEM_PROMPT = """
你是一名资深软件测试分析师。请在设计测试用例之前分析需求，识别功能范围、
业务规则、边界条件、异常路径、安全风险、质量风险和需要项目经理澄清的问题。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "summary": "需求概述",
  "risk_level": "high",
  "functional_points": ["功能点"],
  "business_rules": ["业务规则"],
  "boundary_conditions": ["边界条件"],
  "exception_scenarios": ["异常场景"],
  "security_risks": ["安全与权限风险"],
  "clarification_questions": ["待澄清问题"],
  "recommended_test_types": ["功能测试", "安全测试"]
}

risk_level 只能是 high、medium、low。内容必须具体、可验证，不能编造需求中不存在的事实。
"""


GENERATION_SYSTEM_PROMPT = """
你是一名资深软件测试工程师。
请根据需求设计高质量、可执行的测试用例，覆盖正常、异常、空值、边界值、
状态变化、权限安全、业务规则和误操作场景。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "test_cases": [
    {
      "case_number": "TC-001",
      "title": "用例标题",
      "priority": "P1",
      "test_type": "功能测试",
      "preconditions": ["前置条件"],
      "steps": [{"action": "操作步骤", "expected": "预期结果"}]
    }
  ]
}

priority 只能是 P1、P2、P3、P4、P5。至少生成 5 条、最多生成 30 条。
"""


DEV_TASK_SYSTEM_PROMPT = """
你是一名资深开发负责人。请把需求拆成可以直接排期开发的开发任务（开发文档），
按实际动手顺序排列：先把数据模型和接口定下来，再写业务逻辑，最后收尾联调。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "dev_tasks": [
    {
      "task_number": "DEV-001",
      "title": "任务标题",
      "module": "涉及模块",
      "description": "开发说明：改哪个文件或哪一层、关键逻辑是什么",
      "acceptance": "验收标准：怎样算做完，要可验证",
      "priority": "P2",
      "estimate_hours": 4
    }
  ]
}

priority 只能是 P1、P2、P3、P4、P5。estimate_hours 是预估工时（小时），填正整数。
至少拆 3 条、最多拆 15 条，每条只做一件事，不要出现「完成后端开发」这种包住整个需求的
大任务。module 用需求里出现的模块名，不要编造需求中不存在的功能和字段。
"""


COVERAGE_SYSTEM_PROMPT = """
你是一名资深测试评审专家。请把需求、需求分析和当前测试用例逐项对照，评估测试覆盖，
指出已覆盖、部分覆盖和未覆盖的检查项，并给出补充建议。评分必须依据实际用例内容，
不能因为用例数量多就给高分，也不能编造需求中不存在的规则。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "overall_score": 85,
  "summary": "覆盖情况概述",
  "coverage_items": [
    {
      "dimension": "功能点",
      "item": "发送验证码",
      "status": "covered",
      "matched_cases": ["TC-001"],
      "evidence": "用例验证了发送成功",
      "gap": ""
    }
  ],
  "missing_coverage": ["未覆盖短信服务超时"],
  "recommendations": [
    {"priority": "P1", "title": "补充短信服务超时用例", "reason": "高风险异常路径"}
  ]
}

overall_score 必须是 0 到 100 的整数。status 只能是 covered、partial、missing。
coverage_items 必须覆盖功能点、业务规则、边界、异常、安全与权限等适用维度。
matched_cases 只能填写输入中真实存在的用例编号。
"""


SUPPLEMENT_SYSTEM_PROMPT = """
你是一名资深软件测试工程师。请根据给出的需求、当前测试用例和覆盖分析，
只针对部分覆盖或未覆盖的内容补充新的测试用例，不要重复已有用例已经验证的场景。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "test_cases": [
    {
      "case_number": "TC-011",
      "title": "补充用例标题",
      "priority": "P1",
      "test_type": "异常测试",
      "preconditions": ["前置条件"],
      "steps": [{"action": "操作步骤", "expected": "预期结果"}]
    }
  ]
}

priority 只能是 P1、P2、P3、P4、P5。生成 1 到 20 条真正必要的新用例。
每条用例必须有可执行步骤和明确预期结果；不要修改、复述或重新编号已有用例。
"""


REVIEW_SYSTEM_PROMPT = """
你是一名资深测试评审专家。请评审给出的单条正式测试用例，检查标题、前置条件、
步骤、预期结果、边界、异常、安全、权限和可执行性，并给出可以直接应用的优化版本。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "score": 85,
  "strengths": ["优点"],
  "issues": [
    {"severity": "high", "title": "问题标题", "detail": "问题说明"}
  ],
  "missing_scenarios": ["缺失场景"],
  "optimized_summary": "优化后的标题",
  "optimized_preconditions": ["优化后的前置条件"],
  "optimized_steps": [
    {"action": "操作步骤", "expected": "预期结果"}
  ]
}

score 必须是 0 到 100 的整数。severity 只能是 high、medium、low。
优化后的步骤必须清晰、可执行，每一步都应尽量包含预期结果。
"""


RUN_ANALYSIS_SYSTEM_PROMPT = """
你是一名资深测试经理。请根据执行任务的结构化执行快照，分析完成度、质量风险、
失败聚类、阻塞问题和回归范围，并给出明确的发布建议。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "executive_summary": "执行结果摘要",
  "risk_level": "high",
  "completion_assessment": "完成度评价",
  "release_recommendation": "no_go",
  "status_insights": ["状态分布洞察"],
  "failure_clusters": [
    {
      "cluster": "失败类别",
      "affected_cases": ["TC-101"],
      "evidence": "快照中的直接证据",
      "likely_causes": ["待验证的可能原因"],
      "recommended_actions": ["下一步排查动作"]
    }
  ],
  "blocking_issues": ["发布阻塞项"],
  "regression_recommendations": [
    {"priority": "P1", "scope": "回归范围", "reason": "建议原因"}
  ],
  "next_actions": ["按优先级排列的行动"]
}

risk_level 只能是 high、medium、low。
release_recommendation 只能是 go、conditional_go、no_go。
只能把快照中的事实写成证据；无法确认的原因必须放在 likely_causes 中，不能冒充已确认根因。
如果运行尚未完成，必须在 completion_assessment 和发布建议中明确说明。
"""


DEFECT_DRAFT_SYSTEM_PROMPT = """
你是一名资深缺陷分析工程师。请根据一条失败测试执行的结构化快照生成可编辑的缺陷草稿。
只把输入中可直接观察到的内容写入 evidence；无法确认的原因必须放入 likely_causes，
不得把推测写成已确认根因。actual_result 缺少证据时必须明确提示人工补充，不能编造。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "title": "简洁明确的缺陷标题",
  "severity": "high",
  "description": "问题描述",
  "preconditions": ["前置条件"],
  "reproduction_steps": ["复现步骤"],
  "expected_result": "预期结果",
  "actual_result": "实际结果或待人工补充提示",
  "environment": "测试环境",
  "evidence": ["快照中的直接证据"],
  "likely_causes": ["待验证的可能原因"]
}

severity 只能是 critical、high、medium、low。
"""


TEST_REPORT_SYSTEM_PROMPT = """
你是一名资深测试经理。请根据执行任务快照、AI 运行分析和已关联缺陷生成测试报告草稿。
报告中的数据必须与输入快照一致；不能把待验证原因写成已确认事实；运行未完成时不能建议直接发布。

只输出一个 JSON 对象，不要输出 Markdown、解释或推理过程。格式必须是：
{
  "title": "测试报告标题",
  "summary": "执行摘要",
  "scope": "测试范围",
  "conclusion": "测试结论",
  "release_decision": "conditional_go",
  "defect_summary": [
    {"execution_id": 1, "case_number": "TC-1", "title": "缺陷摘要", "url": "https://..."}
  ],
  "recommendations": ["后续建议"]
}

release_decision 只能是 go、conditional_go、no_go。
defect_summary 只能引用输入中真实存在的缺陷链接。
"""


class AIResponseError(RuntimeError):
    pass


def _empty_skill_snapshot():
    """Return one bucket per operation that can consume a rule package.

    新增 operation 时必须在这里补一个空列表，否则
    ``_build_skill_snapshot`` 里的 ``{profile.operation: []}`` 会直接 KeyError。
    """
    return {
        "requirement_analysis": [],
        "test_case_generation": [],
        "dev_task_breakdown": [],
    }


def capture_instruction_snapshot(user, category=None):
    """Capture published runtime rules for a new requirement workflow.

    The snapshot makes a queued job reproducible even if an administrator edits
    a rule package while the job is running.
    """
    product_id = category.product_id if category is not None else None
    if product_id is None:
        return _empty_skill_snapshot()
    profiles = AIInstructionProfile.objects.filter(
        owner=user,
        is_active=True,
        product_id=product_id,
    ).order_by(
        "operation", "product_id", "name"
    )
    snapshot = _empty_skill_snapshot()
    for profile in profiles:
        item = {
            "id": profile.pk,
            "name": profile.name,
            "description": profile.description,
            "version": profile.version,
            "operation": profile.operation,
            "product_id": profile.product_id,
            "instructions": profile.instructions,
        }
        operations = snapshot if profile.operation == "all" else {profile.operation: []}
        for operation in operations:
            snapshot[operation].append(item)
    return snapshot


def render_instruction_context(snapshot, operation):
    """Render a bounded, clearly-delimited context for a model system prompt."""
    if not isinstance(snapshot, dict):
        return ""
    profiles = snapshot.get(operation) or []
    if not profiles:
        return ""
    sections = []
    total_length = 0
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "未命名规则包").strip()
        version = profile.get("version", 1)
        instructions = str(profile.get("instructions") or "").strip()
        if not instructions:
            continue
        section = f"【{name} v{version}】\n{instructions}"
        if total_length + len(section) > 24000:
            break
        sections.append(section)
        total_length += len(section)
    if not sections:
        return ""
    return (
        "\n\n".join(sections)
        + "\n\n以上内容是测试领域参考规则，只能辅助分析原始需求；"
        "如果与原始需求冲突，以原始需求为准并提出待澄清问题。"
    )


def _active_model_config(user):
    config = (
        AIModelConfig.objects.filter(owner=user, is_active=True)
        .order_by("-updated")
        .first()
    )
    if config is None:
        raise RuntimeError("请先在“AI 模型配置”中设置默认模型")
    return config


def _get_config(user):
    config = _active_model_config(user)
    api_key = decrypt_api_key(config.api_key_encrypted)
    if not api_key:
        raise RuntimeError("当前模型没有 API Key，请先编辑模型配置")
    return config.api_base.rstrip("/"), api_key, config.model, config.timeout


def _token_value(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _record_ai_usage(config, operation, status, started_at, usage=None, error=None):
    usage = usage if isinstance(usage, dict) else {}
    duration_ms = max(0, round((time.monotonic() - started_at) * 1000))
    try:
        AIUsageLog.objects.create(
            owner_id=config.owner_id,
            model_config=config,
            config_name=config.name,
            model_name=config.model,
            operation=operation,
            status=status,
            duration_ms=duration_ms,
            prompt_tokens=_token_value(usage.get("prompt_tokens")),
            completion_tokens=_token_value(usage.get("completion_tokens")),
            total_tokens=_token_value(usage.get("total_tokens")),
            error_message=str(error)[:500] if error else "",
        )
    except Exception:
        # 可观测性记录失败不能覆盖原始 AI 调用结果。
        pass


MAX_AI_RESPONSE_BYTES = 4 * 1024 * 1024


class _NoModelRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward model credentials or requirement text to a redirect.
        return None


def _open_ai_request(request, timeout):
    return urllib.request.build_opener(_NoModelRedirect()).open(request, timeout=timeout)


def _request_config_content(
    config, system_prompt, user_prompt, max_tokens=None, operation="other"
):
    started_at = time.monotonic()
    try:
        api_key = decrypt_api_key(config.api_key_encrypted)
        if not api_key:
            raise RuntimeError("当前模型没有 API Key，请先编辑模型配置")

        payload = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        request = urllib.request.Request(
            url=f"{config.api_base.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Kiwi-TCMS-AI-Assistant/1.0",
            },
            method="POST",
        )
        try:
            with _open_ai_request(request, timeout=config.timeout) as response:
                raw_body = response.read(MAX_AI_RESPONSE_BYTES + 1)
                if len(raw_body) > MAX_AI_RESPONSE_BYTES:
                    raise RuntimeError("AI 响应超过 4 MB，请缩小本次生成范围。")
                body = raw_body.decode("utf-8")
        except urllib.error.HTTPError as exc:
            # Provider error bodies may echo the API key or submitted documents.
            code = exc.code
            exc.close()
            if 300 <= code < 400:
                raise RuntimeError("AI 服务返回重定向，请填写最终服务的 Base URL。") from None
            raise RuntimeError(f"AI 服务返回 HTTP {code}，请检查模型配置或服务状态。") from None
        except (urllib.error.URLError, OSError):
            raise RuntimeError("无法连接 AI 服务，请检查地址、网络、证书或超时设置。") from None
        except UnicodeError:
            raise RuntimeError("AI 服务响应编码异常。") from None

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError, ValueError, RecursionError):
            raise RuntimeError("AI 服务响应格式异常，请确认支持 Chat Completions 接口。") from None
        if not content:
            raise RuntimeError("AI返回内容为空")
    except Exception as exc:
        safe_error = (
            "接口用例生成调用失败，请检查模型配置或稍后重试。" if operation == "api_case_generation" else
            "Web 用例生成调用失败，请检查模型配置或稍后重试。" if operation == "web_case_generation" else exc
        )
        _record_ai_usage(config, operation, "error", started_at, error=safe_error)
        if operation in {"api_case_generation", "web_case_generation"}:
            raise RuntimeError(safe_error) from None
        raise

    _record_ai_usage(
        config,
        operation,
        "success",
        started_at,
        usage=data.get("usage"),
    )
    return content


def _request_ai_content(
    user, system_prompt, user_prompt, operation="other", model_config=None
):
    config = model_config or _active_model_config(user)
    if config.owner_id != user.pk:
        raise RuntimeError("模型配置不属于当前账号")
    content = _request_config_content(
        config, system_prompt, user_prompt, operation=operation
    )
    return content, config


def test_model_connection(config):
    started_at = time.monotonic()
    content = _request_config_content(
        config,
        "你正在响应一次 API 连接测试。不要解释，只回复 OK。",
        "请回复 OK。",
        max_tokens=16,
        operation="connection_test",
    )
    elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
    return {"reply": content[:100], "elapsed_ms": elapsed_ms}


def _decode_json(content):
    text = content.strip()
    fence = chr(96) * 3
    if text.startswith(fence):
        lines = text.splitlines()
        text = "\n".join(lines[1:])
        if text.rstrip().endswith(fence):
            text = text.rstrip()[: -len(fence)].rstrip()

    candidates = [text]
    object_start, object_end = text.find("{"), text.rfind("}")
    if object_start >= 0 and object_end > object_start:
        candidates.append(text[object_start : object_end + 1])
    array_start, array_end = text.find("["), text.rfind("]")
    if array_start >= 0 and array_end > array_start:
        candidates.append(text[array_start : array_end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise AIResponseError("AI 未返回合法 JSON，无法解析结构化结果")


def _string_list(value):
    if isinstance(value, str):
        return [line.strip("- ").strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _steps(value):
    if not isinstance(value, list):
        return []
    normalized = []
    for item in value:
        if isinstance(item, str):
            action, expected = item.strip(), ""
        elif isinstance(item, dict):
            action = str(
                item.get("action") or item.get("step") or item.get("operation") or ""
            ).strip()
            expected = str(
                item.get("expected")
                or item.get("expected_result")
                or item.get("result")
                or ""
            ).strip()
        else:
            continue
        if action or expected:
            normalized.append({"action": action, "expected": expected})
    return normalized


def parse_requirement_analysis(content):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 需求分析结果必须是 JSON 对象")

    summary = str(data.get("summary") or data.get("overview") or "").strip()
    if not summary:
        raise AIResponseError("AI 需求分析结果缺少 summary")
    risk_level = str(data.get("risk_level") or "medium").strip().lower()
    if risk_level not in {"high", "medium", "low"}:
        risk_level = "medium"

    return {
        "summary": summary,
        "risk_level": risk_level,
        "functional_points": _string_list(data.get("functional_points")),
        "business_rules": _string_list(data.get("business_rules")),
        "boundary_conditions": _string_list(data.get("boundary_conditions")),
        "exception_scenarios": _string_list(data.get("exception_scenarios")),
        "security_risks": _string_list(data.get("security_risks")),
        "clarification_questions": _string_list(
            data.get("clarification_questions")
        ),
        "recommended_test_types": _string_list(
            data.get("recommended_test_types")
        ),
    }


def parse_coverage_analysis(content):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 覆盖分析结果必须是 JSON 对象")

    try:
        overall_score = int(data.get("overall_score", 0))
    except (TypeError, ValueError) as exc:
        raise AIResponseError("AI 覆盖评分不是有效整数") from exc
    overall_score = max(0, min(100, overall_score))

    summary = str(data.get("summary") or "").strip()
    if not summary:
        raise AIResponseError("AI 覆盖分析结果缺少 summary")

    raw_items = data.get("coverage_items")
    if not isinstance(raw_items, list) or not raw_items:
        raise AIResponseError("AI 覆盖分析结果缺少 coverage_items")
    if len(raw_items) > 200:
        raise AIResponseError("AI 覆盖分析检查项超过 200 条安全上限")

    coverage_items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = str(raw_item.get("item") or raw_item.get("name") or "").strip()
        if not item:
            continue
        status = str(raw_item.get("status") or "partial").strip().lower()
        if status not in {"covered", "partial", "missing"}:
            status = "partial"
        coverage_items.append(
            {
                "dimension": str(raw_item.get("dimension") or "其他").strip(),
                "item": item,
                "status": status,
                "matched_cases": _string_list(raw_item.get("matched_cases")),
                "evidence": str(raw_item.get("evidence") or "").strip(),
                "gap": str(raw_item.get("gap") or "").strip(),
            }
        )
    if not coverage_items:
        raise AIResponseError("AI 覆盖分析结果没有有效检查项")

    recommendations = []
    for raw_item in data.get("recommendations") or []:
        if isinstance(raw_item, str):
            title, priority, reason = raw_item.strip(), "P2", ""
        elif isinstance(raw_item, dict):
            title = str(raw_item.get("title") or raw_item.get("item") or "").strip()
            priority = str(raw_item.get("priority") or "P2").strip().upper()
            reason = str(raw_item.get("reason") or raw_item.get("detail") or "").strip()
        else:
            continue
        if priority not in {"P1", "P2", "P3", "P4", "P5"}:
            priority = "P2"
        if title:
            recommendations.append(
                {"priority": priority, "title": title, "reason": reason}
            )

    return {
        "overall_score": overall_score,
        "summary": summary,
        "coverage_items": coverage_items,
        "missing_coverage": _string_list(data.get("missing_coverage")),
        "recommendations": recommendations,
    }


def parse_test_cases(content):
    data = _decode_json(content)
    if isinstance(data, dict):
        cases = data.get("test_cases") or data.get("cases")
    else:
        cases = data
    if not isinstance(cases, list) or not cases:
        raise AIResponseError("AI 返回结果中没有 test_cases 数组")
    if len(cases) > 50:
        raise AIResponseError("AI 返回用例数量超过 50 条安全上限")

    normalized, used_numbers = [], set()
    for index, item in enumerate(cases, start=1):
        if not isinstance(item, dict):
            raise AIResponseError(f"第 {index} 条用例不是 JSON 对象")
        summary = str(item.get("title") or item.get("summary") or "").strip()
        if not summary:
            raise AIResponseError(f"第 {index} 条用例缺少标题")
        case_number = str(item.get("case_number") or f"TC-{index:03d}").strip()[:50]
        if case_number in used_numbers:
            case_number = f"TC-{index:03d}"
        used_numbers.add(case_number)
        priority = str(item.get("priority") or "P3").strip().upper()
        if priority not in {"P1", "P2", "P3", "P4", "P5"}:
            priority = "P3"
        steps = _steps(item.get("steps"))
        if not steps:
            raise AIResponseError(f"第 {index} 条用例缺少可执行步骤")
        normalized.append(
            {
                "case_number": case_number,
                "summary": summary[:255],
                "priority": priority,
                "test_type": str(item.get("test_type") or "").strip()[:64],
                "preconditions": _string_list(item.get("preconditions")),
                "steps": steps,
            }
        )
    return normalized


def parse_dev_tasks(content):
    """把模型返回的开发任务归一化成 AIDevTask 的字段字典列表。"""
    data = _decode_json(content)
    if isinstance(data, dict):
        tasks = data.get("dev_tasks") or data.get("tasks")
    else:
        tasks = data
    if not isinstance(tasks, list) or not tasks:
        raise AIResponseError("AI 返回结果中没有 dev_tasks 数组")
    if len(tasks) > 30:
        raise AIResponseError("AI 返回开发任务数量超过 30 条安全上限")

    normalized, used_numbers = [], set()
    for index, item in enumerate(tasks, start=1):
        if not isinstance(item, dict):
            raise AIResponseError(f"第 {index} 条开发任务不是 JSON 对象")
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            raise AIResponseError(f"第 {index} 条开发任务缺少标题")
        task_number = str(item.get("task_number") or f"DEV-{index:03d}").strip()[:50]
        if task_number in used_numbers:
            task_number = f"DEV-{index:03d}"
        used_numbers.add(task_number)
        priority = str(item.get("priority") or "P3").strip().upper()
        if priority not in {"P1", "P2", "P3", "P4", "P5"}:
            priority = "P3"
        try:
            estimate = int(item.get("estimate_hours"))
        except (TypeError, ValueError):
            estimate = None
        if estimate is not None:
            estimate = min(estimate, 999) if estimate > 0 else None
        normalized.append(
            {
                "task_number": task_number,
                "title": title[:200],
                "module": str(item.get("module") or "").strip()[:200],
                "description": str(item.get("description") or "").strip(),
                "acceptance": str(item.get("acceptance") or "").strip(),
                "priority": priority,
                "estimate_hours": estimate,
                "position": index,
            }
        )
    return normalized


def assign_unique_case_numbers(test_cases, existing_numbers):
    used_numbers = {
        str(number).strip().upper()
        for number in existing_numbers
        if str(number).strip()
    }
    next_number = 1
    normalized = []
    for test_case in test_cases:
        test_case = dict(test_case)
        case_number = str(test_case.get("case_number") or "").strip()[:50]
        if not case_number or case_number.upper() in used_numbers:
            while f"TC-{next_number:03d}" in used_numbers:
                next_number += 1
            case_number = f"TC-{next_number:03d}"
            next_number += 1
        used_numbers.add(case_number.upper())
        test_case["case_number"] = case_number
        normalized.append(test_case)
    return normalized


def parse_test_case_review(content):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 评审结果必须是 JSON 对象")

    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError) as exc:
        raise AIResponseError("AI 评审分数不是有效整数") from exc
    score = max(0, min(100, score))

    issues = []
    for item in data.get("issues") or []:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "medium").lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        title = str(item.get("title") or item.get("issue") or "").strip()
        detail = str(item.get("detail") or item.get("description") or "").strip()
        if title or detail:
            issues.append({"severity": severity, "title": title, "detail": detail})

    optimized_summary = str(data.get("optimized_summary") or "").strip()
    optimized_steps = _steps(data.get("optimized_steps"))
    if not optimized_summary:
        raise AIResponseError("AI 评审结果缺少 optimized_summary")
    if not optimized_steps:
        raise AIResponseError("AI 评审结果缺少 optimized_steps")

    return {
        "score": score,
        "strengths": _string_list(data.get("strengths")),
        "issues": issues,
        "missing_scenarios": _string_list(data.get("missing_scenarios")),
        "optimized_summary": optimized_summary[:255],
        "optimized_preconditions": _string_list(data.get("optimized_preconditions")),
        "optimized_steps": optimized_steps,
    }


def parse_test_run_analysis(content):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 执行任务分析结果必须是 JSON 对象")

    executive_summary = str(data.get("executive_summary") or "").strip()
    if not executive_summary:
        raise AIResponseError("AI 执行任务分析缺少 executive_summary")

    risk_level = str(data.get("risk_level") or "medium").strip().lower()
    if risk_level not in {"high", "medium", "low"}:
        risk_level = "medium"
    release_recommendation = str(
        data.get("release_recommendation") or "conditional_go"
    ).strip().lower()
    if release_recommendation not in {"go", "conditional_go", "no_go"}:
        release_recommendation = "conditional_go"

    failure_clusters = []
    for item in (data.get("failure_clusters") or [])[:30]:
        if not isinstance(item, dict):
            continue
        cluster = str(item.get("cluster") or item.get("name") or "").strip()
        if not cluster:
            continue
        failure_clusters.append(
            {
                "cluster": cluster,
                "affected_cases": _string_list(item.get("affected_cases"))[:100],
                "evidence": str(item.get("evidence") or "").strip(),
                "likely_causes": _string_list(item.get("likely_causes"))[:20],
                "recommended_actions": _string_list(
                    item.get("recommended_actions")
                )[:20],
            }
        )

    regression_recommendations = []
    for item in (data.get("regression_recommendations") or [])[:30]:
        if isinstance(item, str):
            priority, scope, reason = "P2", item.strip(), ""
        elif isinstance(item, dict):
            priority = str(item.get("priority") or "P2").strip().upper()
            scope = str(item.get("scope") or item.get("title") or "").strip()
            reason = str(item.get("reason") or "").strip()
        else:
            continue
        if priority not in {"P1", "P2", "P3", "P4", "P5"}:
            priority = "P2"
        if scope:
            regression_recommendations.append(
                {"priority": priority, "scope": scope, "reason": reason}
            )

    return {
        "executive_summary": executive_summary,
        "risk_level": risk_level,
        "completion_assessment": str(
            data.get("completion_assessment") or ""
        ).strip(),
        "release_recommendation": release_recommendation,
        "status_insights": _string_list(data.get("status_insights"))[:50],
        "failure_clusters": failure_clusters,
        "blocking_issues": _string_list(data.get("blocking_issues"))[:50],
        "regression_recommendations": regression_recommendations,
        "next_actions": _string_list(data.get("next_actions"))[:50],
    }


def parse_defect_draft(content):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 缺陷草稿必须是 JSON 对象")

    title = str(data.get("title") or "").strip()
    if not title:
        raise AIResponseError("AI 缺陷草稿缺少 title")
    severity = str(data.get("severity") or "medium").strip().lower()
    if severity not in {"critical", "high", "medium", "low"}:
        severity = "medium"

    return {
        "title": title[:255],
        "severity": severity,
        "description": str(data.get("description") or "").strip(),
        "preconditions": _string_list(data.get("preconditions"))[:50],
        "reproduction_steps": _string_list(data.get("reproduction_steps"))[:100],
        "expected_result": str(data.get("expected_result") or "").strip(),
        "actual_result": str(data.get("actual_result") or "").strip(),
        "environment": str(data.get("environment") or "").strip(),
        "evidence": _string_list(data.get("evidence"))[:50],
        "likely_causes": _string_list(data.get("likely_causes"))[:30],
    }


def parse_test_report(content, valid_defect_urls=None):
    data = _decode_json(content)
    if not isinstance(data, dict):
        raise AIResponseError("AI 测试报告必须是 JSON 对象")

    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    if not title or not summary:
        raise AIResponseError("AI 测试报告缺少 title 或 summary")
    release_decision = str(
        data.get("release_decision") or "conditional_go"
    ).strip().lower()
    if release_decision not in {"go", "conditional_go", "no_go"}:
        release_decision = "conditional_go"

    allowed_urls = set(valid_defect_urls or [])
    defect_summary = []
    for item in (data.get("defect_summary") or [])[:100]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and allowed_urls and url not in allowed_urls:
            continue
        defect_summary.append(
            {
                "execution_id": item.get("execution_id"),
                "case_number": str(item.get("case_number") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "url": url,
            }
        )

    return {
        "title": title[:255],
        "summary": summary,
        "scope": str(data.get("scope") or "").strip(),
        "conclusion": str(data.get("conclusion") or "").strip(),
        "release_decision": release_decision,
        "defect_summary": defect_summary,
        "recommendations": _string_list(data.get("recommendations"))[:50],
    }


def build_test_run_snapshot(test_run):
    from .regression_checks import execution_context
    executions = test_run.executions.all()
    status_rows = list(
        executions.values("status_id", "status__name", "status__weight")
        .annotate(count=Count("id"))
        .order_by("-status__weight", "status__name")
    )
    total_count = sum(row["count"] for row in status_rows)
    success_count = sum(
        row["count"] for row in status_rows if row["status__weight"] > 0
    )
    failure_count = sum(
        row["count"] for row in status_rows if row["status__weight"] < 0
    )
    pending_count = sum(
        row["count"] for row in status_rows if row["status__weight"] == 0
    )
    completed_count = success_count + failure_count

    failed_executions = list(
        executions.filter(status__weight__lt=0)
        .select_related(
            "status", "case", "case__priority", "tested_by", "assignee"
        )
        .order_by("id")[:100]
    )
    failed_ids = [execution.pk for execution in failed_executions]
    defect_counts = {
        row["execution_id"]: row["count"]
        for row in LinkReference.objects.filter(
            execution_id__in=failed_ids, is_defect=True
        )
        .values("execution_id")
        .annotate(count=Count("id"))
    }

    failure_details = []
    for execution in failed_executions:
        duration = execution.actual_duration
        failure_details.append(
            {
                "execution_id": execution.pk,
                "execution_context": execution_context(execution),
                "case_id": execution.case_id,
                "case_number": f"TC-{execution.case_id}",
                "case_summary": execution.case.summary,
                "priority": str(execution.case.priority),
                "status": execution.status.name,
                "status_weight": execution.status.weight,
                "assignee": execution.assignee.username
                if execution.assignee_id
                else "",
                "tested_by": execution.tested_by.username
                if execution.tested_by_id
                else "",
                "duration_seconds": round(duration.total_seconds(), 2)
                if duration
                else None,
                "defect_count": defect_counts.get(execution.pk, 0),
            }
        )

    pending_samples = [
        {
            "execution_id": execution.pk,
            "case_number": f"TC-{execution.case_id}",
            "case_summary": execution.case.summary,
            "status": execution.status.name,
        }
        for execution in executions.filter(status__weight=0)
        .select_related("status", "case")
        .order_by("id")[:30]
    ]

    defect_link_count = LinkReference.objects.filter(
        execution__run=test_run, is_defect=True
    ).count()
    completion_rate = round(completed_count * 100 / total_count, 1) if total_count else 0
    success_rate = round(success_count * 100 / total_count, 1) if total_count else 0
    failure_rate = round(failure_count * 100 / total_count, 1) if total_count else 0

    return {
        "run": {
            "id": test_run.pk,
            "summary": test_run.summary,
            "notes": (test_run.notes or "")[:2000],
            "plan": str(test_run.plan),
            "product": str(test_run.build.version.product),
            "version": str(test_run.build.version),
            "build": str(test_run.build),
            "manager": test_run.manager.username,
            "start_date": test_run.start_date.isoformat()
            if test_run.start_date
            else None,
            "stop_date": test_run.stop_date.isoformat()
            if test_run.stop_date
            else None,
        },
        "metrics": {
            "total": total_count,
            "completed": completed_count,
            "success": success_count,
            "failure": failure_count,
            "pending": pending_count,
            "completion_rate": completion_rate,
            "success_rate": success_rate,
            "failure_rate": failure_rate,
            "defect_links": defect_link_count,
        },
        "status_breakdown": [
            {
                "status": row["status__name"],
                "weight": row["status__weight"],
                "count": row["count"],
            }
            for row in status_rows
        ],
        "failure_details": failure_details,
        "failure_details_truncated": failure_count > len(failure_details),
        "pending_samples": pending_samples,
        "pending_samples_truncated": pending_count > len(pending_samples),
    }


def build_test_execution_snapshot(execution):
    defect_links = list(
        LinkReference.objects.filter(execution=execution, is_defect=True)
        .order_by("created_on")
        .values("name", "url")
    )
    properties = [
        {"name": prop.name, "value": prop.value}
        for prop in execution.properties().order_by("name", "value")
    ]
    duration = execution.actual_duration
    return {
        "execution": {
            "id": execution.pk,
            "status": execution.status.name,
            "status_weight": execution.status.weight,
            "assignee": execution.assignee.username if execution.assignee_id else "",
            "tested_by": execution.tested_by.username if execution.tested_by_id else "",
            "start_date": execution.start_date.isoformat()
            if execution.start_date
            else None,
            "stop_date": execution.stop_date.isoformat()
            if execution.stop_date
            else None,
            "duration_seconds": round(duration.total_seconds(), 2)
            if duration
            else None,
        },
        "case": {
            "id": execution.case_id,
            "case_number": f"TC-{execution.case_id}",
            "summary": execution.case.summary,
            "requirement": (execution.case.requirement or "")[:3000],
            "text": (execution.case.text or "")[:8000],
            "priority": str(execution.case.priority),
            "category": str(execution.case.category),
        },
        "run": {
            "id": execution.run_id,
            "summary": execution.run.summary,
            "plan": str(execution.run.plan),
            "product": str(execution.build.version.product),
            "version": str(execution.build.version),
            "build": str(execution.build),
        },
        "parameters": properties,
        "existing_defects": defect_links,
        "evidence_limitations": [
            "Kiwi 当前执行记录没有独立的实际结果字段",
            "草稿中的实际结果必须由测试人员根据真实观察补充或确认",
        ],
    }


def analyze_requirement(
    title, requirement, user, model_config=None, skill_snapshot=None
):
    user_prompt = f"""
需求标题：
{title}

需求描述：
{requirement}

请先完成结构化测试需求分析与风险识别。
"""
    skill_context = render_instruction_context(skill_snapshot, "requirement_analysis")
    system_prompt = ANALYSIS_SYSTEM_PROMPT
    if skill_context:
        system_prompt += f"\n\n运行时 AI 规则包：\n{skill_context}"
    content, config = _request_ai_content(
        user,
        system_prompt,
        user_prompt,
        operation="requirement_analysis",
        model_config=model_config,
    )
    return parse_requirement_analysis(content), config, content


def generate_test_cases(
    title, requirement, user, analysis=None, model_config=None, skill_snapshot=None,
    dev_task_context=None,
):
    analysis_context = ""
    if analysis:
        analysis_context = f"""

已确认的需求分析结果：
{json.dumps(analysis, ensure_ascii=False, indent=2)}

生成用例时必须覆盖上述功能点、规则、边界、异常、安全风险和建议测试类型。
"""
    user_prompt = f"""
需求标题：
{title}

需求描述：
{requirement}
{analysis_context}

请生成结构化软件测试用例。
"""
    if dev_task_context:
        user_prompt += (
            "\n参考开发任务（开发文档，不是测试执行任务）：\n"
            + json.dumps(dev_task_context, ensure_ascii=False, indent=2)
            + "\n以需求和验收标准为主要依据，开发文档仅补充实现与接口细节；"
            "不要遗漏端到端流程、边界、异常与安全场景，不要把实现描述当作预期结果。"
            "文档冲突或缺少信息时注明待确认，不要编造业务规则。\n"
        )
    skill_context = render_instruction_context(
        skill_snapshot, "test_case_generation"
    )
    system_prompt = GENERATION_SYSTEM_PROMPT
    if skill_context:
        system_prompt += f"\n\n运行时 AI 规则包：\n{skill_context}"
    content, _config = _request_ai_content(
        user,
        system_prompt,
        user_prompt,
        operation="test_case_generation",
        model_config=model_config,
    )
    return parse_test_cases(content)


def break_down_dev_tasks(
    title, requirement, user, analysis=None, model_config=None, skill_snapshot=None
):
    """把需求拆成可以直接排期的开发任务（开发文档）。"""
    analysis_context = ""
    if analysis:
        analysis_context = f"""

已确认的需求分析结果：
{json.dumps(analysis, ensure_ascii=False, indent=2)}

拆分任务时必须覆盖上述功能点、业务规则、边界条件、异常场景与安全风险。
"""
    user_prompt = f"""
需求标题：
{title}

需求描述：
{requirement}
{analysis_context}

请把这份需求拆成开发任务。
"""
    skill_context = render_instruction_context(skill_snapshot, "dev_task_breakdown")
    system_prompt = DEV_TASK_SYSTEM_PROMPT
    if skill_context:
        system_prompt += f"\n\n运行时 AI 规则包：\n{skill_context}"
    content, _config = _request_ai_content(
        user,
        system_prompt,
        user_prompt,
        operation="dev_task_breakdown",
        model_config=model_config,
    )
    return parse_dev_tasks(content)


def analyze_test_coverage(ai_request, user, model_config=None):
    drafts = list(ai_request.drafts.all())
    if not drafts:
        raise RuntimeError("当前请求还没有测试用例草稿")

    draft_payload = [
        {
            "case_number": draft.case_number,
            "title": draft.summary,
            "priority": draft.priority,
            "test_type": draft.test_type,
            "preconditions": draft.preconditions,
            "steps": draft.steps,
        }
        for draft in drafts
    ]
    user_prompt = f"""
需求标题：
{ai_request.title}

需求描述：
{ai_request.requirement_document}

需求分析（可能为空）：
{json.dumps(ai_request.analysis or {}, ensure_ascii=False, indent=2)}

当前测试用例：
{json.dumps(draft_payload, ensure_ascii=False, indent=2)}

请逐项分析这些用例对需求的覆盖情况，并输出覆盖矩阵。
"""
    content, config = _request_ai_content(
        user,
        COVERAGE_SYSTEM_PROMPT,
        user_prompt,
        operation="coverage_analysis",
        model_config=model_config,
    )
    return parse_coverage_analysis(content), config, content


def generate_coverage_gap_test_cases(ai_request, user, model_config=None):
    coverage = ai_request.coverage_analysis or {}
    if not coverage:
        raise RuntimeError("当前请求还没有可用的覆盖分析结果")

    drafts = list(ai_request.drafts.all())
    if not drafts:
        raise RuntimeError("当前请求还没有测试用例草稿")

    gap_items = [
        item
        for item in coverage.get("coverage_items") or []
        if isinstance(item, dict) and item.get("status") in {"partial", "missing"}
    ]
    if not (
        gap_items
        or coverage.get("missing_coverage")
        or coverage.get("recommendations")
    ):
        raise RuntimeError("当前覆盖分析没有需要补充的测试缺口")

    draft_payload = [
        {
            "case_number": draft.case_number,
            "title": draft.summary,
            "priority": draft.priority,
            "test_type": draft.test_type,
            "preconditions": draft.preconditions,
            "steps": draft.steps,
        }
        for draft in drafts
    ]
    gap_payload = {
        "coverage_items": gap_items,
        "missing_coverage": coverage.get("missing_coverage") or [],
        "recommendations": coverage.get("recommendations") or [],
    }
    user_prompt = f"""
需求标题：
{ai_request.title}

需求描述：
{ai_request.requirement_document}

需求分析（可能为空）：
{json.dumps(ai_request.analysis or {}, ensure_ascii=False, indent=2)}

当前已有测试用例：
{json.dumps(draft_payload, ensure_ascii=False, indent=2)}

需要补充的覆盖缺口：
{json.dumps(gap_payload, ensure_ascii=False, indent=2)}

请只生成能够补齐上述缺口的新测试用例。
"""
    content, _config = _request_ai_content(
        user,
        SUPPLEMENT_SYSTEM_PROMPT,
        user_prompt,
        operation="coverage_supplement",
        model_config=model_config,
    )
    return parse_test_cases(content)


def review_test_case(test_case, user, model_config=None):
    user_prompt = f"""
正式测试用例 ID：TC-{test_case.pk}
标题：{test_case.summary}
需求：{test_case.requirement or "未填写"}
优先级：{test_case.priority}
分类：{test_case.category}

当前用例正文：
{test_case.text or "未填写"}

请评审并提供结构化优化版本。
"""
    content, config = _request_ai_content(
        user,
        REVIEW_SYSTEM_PROMPT,
        user_prompt,
        operation="test_case_review",
        model_config=model_config,
    )
    return parse_test_case_review(content), config, content


def analyze_test_run(test_run, user, model_config=None):
    snapshot = build_test_run_snapshot(test_run)
    if snapshot["metrics"]["total"] == 0:
        raise RuntimeError("该执行任务还没有可分析的测试执行")

    user_prompt = f"""
执行任务执行快照：
{json.dumps(snapshot, ensure_ascii=False, indent=2)}

请基于快照生成结构化执行任务分析。区分直接证据与待验证的可能原因。
"""
    content, config = _request_ai_content(
        user,
        RUN_ANALYSIS_SYSTEM_PROMPT,
        user_prompt,
        operation="test_run_analysis",
        model_config=model_config,
    )
    return parse_test_run_analysis(content), config, content, snapshot


def generate_defect_draft(execution, user, model_config=None):
    if execution.status.weight >= 0:
        raise RuntimeError("只有失败状态的测试执行可以生成缺陷草稿")
    snapshot = build_test_execution_snapshot(execution)
    user_prompt = f"""
失败测试执行快照：
{json.dumps(snapshot, ensure_ascii=False, indent=2)}

请生成结构化缺陷草稿。实际结果缺少直接记录时必须提示人工补充。
"""
    content, config = _request_ai_content(
        user,
        DEFECT_DRAFT_SYSTEM_PROMPT,
        user_prompt,
        operation="defect_draft_generation",
        model_config=model_config,
    )
    return parse_defect_draft(content), config, content, snapshot


def generate_test_report(
    test_run, user, source_analysis=None, model_config=None
):
    snapshot = build_test_run_snapshot(test_run)
    if snapshot["metrics"]["total"] == 0:
        raise RuntimeError("该执行任务还没有可生成报告的测试执行")

    defects = []
    for link in (
        LinkReference.objects.filter(execution__run=test_run, is_defect=True)
        .select_related("execution", "execution__case")
        .order_by("execution_id", "created_on")[:200]
    ):
        defects.append(
            {
                "execution_id": link.execution_id,
                "case_number": f"TC-{link.execution.case_id}",
                "case_summary": link.execution.case.summary,
                "name": link.name,
                "url": link.url,
            }
        )

    payload = {
        "execution_snapshot": snapshot,
        "run_analysis": source_analysis.result if source_analysis else {},
        "linked_defects": defects,
    }
    user_prompt = f"""
测试报告输入：
{json.dumps(payload, ensure_ascii=False, indent=2)}

请生成一份结构化、可供人工编辑确认的测试报告草稿。
"""
    content, config = _request_ai_content(
        user,
        TEST_REPORT_SYSTEM_PROMPT,
        user_prompt,
        operation="test_report_generation",
        model_config=model_config,
    )
    result = parse_test_report(
        content, valid_defect_urls=[item["url"] for item in defects]
    )
    return result, config, content, snapshot


def verify_regression(source_report, regression_run):
    from collections import Counter
    from .regression_checks import failed_report_executions, evaluate_execution, execution_context
    from .execution_identity import canonical_case_id

    sources = failed_report_executions(source_report)
    required = Counter((canonical_case_id(execution),execution_context(execution)) for execution in sources)
    items = []
    counts = {"passed": 0, "failed": 0, "pending": 0, "missing": 0}
    for source, execution in zip(source_report.metrics_snapshot["failure_details"], sources):
        outcome, matches, identity = evaluate_execution(
            execution, regression_run, as_of=source_report.created,
            required=required[(canonical_case_id(execution),execution_context(execution))],
        )
        counts[outcome] += 1
        items.append({
            "case_id": source["case_id"], "business_case_id": identity,
            "case_number": source.get("case_number") or f"TC-{identity}",
            "case_summary": source.get("case_summary") or "",
            "source_execution_id": execution.pk, "source_status": source.get("status") or "",
            "outcome": outcome,
            "regression_executions": [{"execution_id": item.pk, "status": item.status.name,
                                        "status_weight": item.status.weight} for item in matches],
        })
    status = "failed" if counts["failed"] else "passed" if counts["passed"] == len(items) else "incomplete"
    return status, {
        "source_run_id": source_report.test_run_id, "regression_run_id": regression_run.pk,
        "total_source_failures": len(items), "counts": counts, "items": items,
    }


def format_test_case_text(draft):
    lines = [
        f"**AI 用例编号：** {draft.case_number}",
        f"**测试类型：** {draft.test_type or '未指定'}",
        "",
        "### 前置条件",
    ]
    lines.extend(f"- {item}" for item in draft.preconditions or ["无"])
    lines.extend(["", "### 测试步骤"])
    for index, step in enumerate(draft.steps, start=1):
        lines.append(f"{index}. {step.get('action') or '执行操作'}")
        if step.get("expected"):
            lines.append(f"   - 预期：{step['expected']}")
    lines.extend(
        ["", f"> 由 AI 测试助手请求 #{draft.request_id} 生成，导入前应人工复核。"]
    )
    return "\n".join(lines)


def format_optimized_test_case_text(review):
    lines = ["### 前置条件"]
    lines.extend(f"- {item}" for item in review.optimized_preconditions or ["无"])
    lines.extend(["", "### 测试步骤"])
    for index, step in enumerate(review.optimized_steps, start=1):
        lines.append(f"{index}. {step.get('action') or '执行操作'}")
        if step.get("expected"):
            lines.append(f"   - 预期：{step['expected']}")
    lines.extend(["", f"> 由 AI 评审记录 #{review.pk} 优化，应用前已人工确认。"])
    return "\n".join(lines)


def import_test_case_drafts(ai_request, author, selected_draft_ids=None):
    if not ai_request.category_id:
        raise RuntimeError("该 AI 请求没有选择目标分类，无法导入")
    with transaction.atomic():
        locked_request = (
            AIRequest.objects.select_for_update()
            .select_related("category")
            .get(pk=ai_request.pk)
        )
        drafts_query = locked_request.drafts.select_for_update().filter(
            imported_case__isnull=True
        )
        if selected_draft_ids is not None:
            drafts_query = drafts_query.filter(pk__in=selected_draft_ids)
        drafts = list(drafts_query.order_by("id"))
        if not drafts:
            return []
        case_status = (
            TestCaseStatus.objects.filter(is_confirmed=False).order_by("pk").first()
            or TestCaseStatus.objects.order_by("pk").first()
        )
        if case_status is None:
            raise RuntimeError("Kiwi 中没有可用的 TestCaseStatus")

        priority_cache, created = {}, []
        for draft in drafts:
            priority = priority_cache.get(draft.priority)
            if priority is None:
                priority = (
                    Priority.objects.filter(
                        value__iexact=draft.priority, is_active=True
                    ).first()
                    or Priority.objects.filter(value__iexact="P3").first()
                    or Priority.objects.filter(is_active=True).order_by("pk").first()
                )
                if priority is None:
                    raise RuntimeError("Kiwi 中没有可用的 Priority")
                priority_cache[draft.priority] = priority
            test_case = TestCase.objects.create(
                summary=draft.summary,
                requirement=locked_request.title[:255],
                text=format_test_case_text(draft),
                case_status=case_status,
                category=locked_request.category,
                priority=priority,
                author=author,
            )
            TestCaseEmailSettings.objects.create(
                case=test_case,
                notify_on_case_update=False,
                notify_on_case_delete=False,
                auto_to_case_author=False,
                auto_to_case_tester=False,
                auto_to_run_manager=False,
                auto_to_run_tester=False,
                auto_to_execution_assignee=False,
            )
            draft.imported_case = test_case
            draft.save(update_fields=("imported_case",))
            NEW_TEST_CASE_SIGNAL.send(sender=TestCase, instance=test_case)
            created.append(test_case)
    return created


def apply_test_case_review(review, user):
    with transaction.atomic():
        locked_review = (
            AITestCaseReview.objects.select_for_update()
            .select_related("test_case")
            .get(pk=review.pk, owner=user)
        )
        if locked_review.applied_at:
            return locked_review.test_case, False

        test_case = TestCase.objects.select_for_update().get(
            pk=locked_review.test_case_id
        )
        if (test_case.summary != locked_review.original_summary
                or (test_case.text or "") != (locked_review.original_text or "")):
            raise ValueError("测试用例已在评审后修改，请重新评审，避免覆盖最新内容。")
        test_case.summary = locked_review.optimized_summary
        test_case.text = format_optimized_test_case_text(locked_review)
        test_case.reviewer = user
        test_case.save(update_fields=("summary", "text", "reviewer"))

        locked_review.applied_at = timezone.now()
        locked_review.applied_by = user
        locked_review.save(update_fields=("applied_at", "applied_by"))
        return test_case, True
