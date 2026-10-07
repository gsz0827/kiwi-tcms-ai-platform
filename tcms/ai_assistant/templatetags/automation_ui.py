from django import template
from tcms.ai_assistant.roles import is_read_only

register = template.Library()


@register.simple_tag(takes_context=True)
def automation_access(context):
    user = context["request"].user
    return {"can_write": user.is_authenticated and user.is_active and not is_read_only(user)}


@register.simple_tag(takes_context=True)
def automation_scope(context):
    request = context["request"]
    return {
        "return_to": request.get_full_path(),
        "hidden": [
            (key, request.GET[key])
            for key in ("folder", "business_case", "association", "case_q")
            if request.GET.get(key)
        ],
    }


@register.simple_tag
def automation_sections(form):
    groups = [
        (
            "基本信息",
            ("product", "name", "title", "test_case", "target_case", "category", "description"),
        ),
        ("执行环境", ("environment", "base_url", "timeout", "ignore_https_errors", "setup_case")),
        ("请求配置", ("method", "path", "query", "headers", "send_body", "body")),
        ("操作与断言", ("steps", "configuration", "expected_status", "assertions", "max_elapsed_ms")),
        ("执行范围", ("cases",)),
        (
            "执行设置",
            (
                "execution_mode",
                "plan",
                "build",
                "sequence",
                "datasets",
                "stop_on_failure",
                "share_cookies",
            ),
        ),
        (
            "AI 生成资料",
            ("model_config", "documentation", "requirements", "environment_variables", "count"),
        ),
        ("复核确认", ("evidence", "review_notes", "confirmed")),
        ("环境参数与认证", ("variables", "secret_headers", "clear_secrets")),
        ("变量提取", ("extracts",)),
        ("结果回写（可选）", ("test_run", "passed_status", "failed_status")),
        ("定时执行", ("schedule_enabled", "interval_minutes", "next_run_at")),
    ]
    output = []
    seen = set()
    visible = {field.name: field for field in form.visible_fields()}
    for title, names in groups:
        fields = [visible[name] for name in names if name in visible]
        if fields:
            seen.update(field.name for field in fields)
            if title == '操作与断言' and 'steps' not in visible and 'expected_status' in visible:
                title = '响应断言'
            output.append({"title": title, "fields": fields})
    rest = [field for name, field in visible.items() if name not in seen]
    if rest:
        output.append({"title": "其他设置", "fields": rest})
    return output


@register.inclusion_tag("ai_assistant/automation/draft_preview.html")
def automation_draft_preview(draft, kind):
    """Read-only rendering; never normalize or modify a saved AI configuration."""
    import json
    from tcms.web_testing.validation import ACTIONS

    def display(value):
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    context = {"kind": kind, "steps": [], "request_rows": [], "assertions": []}
    if kind == "web":
        values = draft.steps
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    action = item.get("action", "")
                    context["steps"].append(
                        {
                            "action": (
                                ACTIONS.get(action, action)
                                if isinstance(action, str)
                                else display(action)
                            ),
                            "selector": display(item.get("selector", "")),
                            "value": display(item.get("value", "")),
                        }
                    )
        context["raw"] = json.dumps(values, ensure_ascii=False, indent=2)
    else:
        values = draft.configuration
        if isinstance(values, dict):
            labels = [
                ("method", "请求方法"),
                ("path", "请求路径"),
                ("expected_status", "预期状态码"),
                ("sequence", "执行顺序"),
                ("query", "查询参数"),
                ("headers", "请求头"),
                ("extracts", "变量提取"),
                ("max_elapsed_ms", "最大耗时（毫秒）"),
            ]
            if values.get("send_body"):
                labels.append(("body", "请求体"))
            for key, label in labels:
                if key in values:
                    context["request_rows"].append({"label": label, "value": display(values[key])})
            assertions = values.get("assertions", [])
            if isinstance(assertions, list):
                for item in assertions:
                    if isinstance(item, dict):
                        operator = item.get("operator", "")
                        context["assertions"].append(
                            {
                                "path": display(item.get("path", "")),
                                "operator": (
                                    {"equals": "等于", "exists": "存在"}.get(operator, operator)
                                    if isinstance(operator, str)
                                    else display(operator)
                                ),
                                "expected": (
                                    "—" if operator == "exists" else display(item.get("expected"))
                                ),
                            }
                        )
        context["raw"] = json.dumps(values, ensure_ascii=False, indent=2)
    return context
