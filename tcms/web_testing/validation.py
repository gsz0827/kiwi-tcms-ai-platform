import os
import re
from urllib.parse import urlsplit, urljoin

from django.conf import settings


ACTIONS = {
    "goto": "打开页面", "click": "点击", "fill": "输入文字", "select": "选择选项",
    "check": "勾选", "assert_visible": "断言元素可见", "assert_hidden": "断言元素隐藏",
    "assert_enabled": "断言元素可用", "assert_text": "断言包含文本",
    "assert_text_exact": "断言文本全等", "assert_value": "断言输入值",
    "assert_count": "断言元素数量", "assert_url": "断言网址包含", "assert_title": "断言标题包含",
}
LOCATOR_ACTIONS = set(ACTIONS) - {"goto", "assert_url", "assert_title"}
VALUE_ACTIONS = {"goto", "fill", "select", "assert_text", "assert_text_exact", "assert_value", "assert_count", "assert_url", "assert_title"}
REQUIRED_VALUES = VALUE_ACTIONS - {"fill", "select", "assert_value", "assert_text_exact"}
VALUE_LABELS = {
    "goto": "页面路径，如 /login/", "fill": "输入文字，可留空", "select": "选项 value，可留空",
    "assert_text": "应包含的文本", "assert_text_exact": "完整预期文本，可留空",
    "assert_value": "预期输入值，可留空", "assert_count": "预期数量，如 0 或 {{count}}",
    "assert_url": "网址应包含的内容", "assert_title": "标题应包含的内容",
}


def editor_schema():
    return [dict(action=key, label=label, locator=key in LOCATOR_ACTIONS,
                 value=key in VALUE_ACTIONS, required=key in REQUIRED_VALUES,
                 placeholder=VALUE_LABELS.get(key, ""), assertion=key.startswith("assert_"))
            for key, label in ACTIONS.items()]


def origin(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return parts.scheme, parts.hostname.lower(), port
    except (ValueError, TypeError):
        raise ValueError("测试地址必须是无账号密码的 HTTP 或 HTTPS 地址。") from None


def allowed_origins():
    values = getattr(settings, "WEB_TEST_ALLOWED_ORIGINS", None)
    if values is None:
        values = os.environ.get("WEB_TEST_ALLOWED_ORIGINS", "https://kiwi-web:8443,https://web:8443").split(",")
    return {origin(value.strip()) for value in values if value.strip()}


def validate_url(url):
    if origin(url) not in allowed_origins():
        raise ValueError("该测试站点未获准访问，请在服务配置 WEB_TEST_ALLOWED_ORIGINS 中加入完整来源地址。")
    return url


def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 30:
        raise ValueError("每条脚本需要 1–30 个步骤。")
    if not any(isinstance(s, dict) and isinstance(s.get("action"), str) and s["action"].startswith("assert_") for s in steps):
        raise ValueError("配置至少需要一个断言，不能仅凭操作完成判为通过。")
    for number, step in enumerate(steps, 1):
        prefix = f"第 {number} 步："
        if not isinstance(step, dict) or not isinstance(step.get("action"), str) or step["action"] not in ACTIONS or set(step) - {"action", "selector", "value"}:
            raise ValueError(prefix + "包含不支持的操作或字段。")
        for field in ("selector", "value"):
            if not isinstance(step.get(field, ""), str) or len(step.get(field, "")) > 2000:
                raise ValueError(prefix + "定位器和输入值必须是最多 2000 字符的文本。")
        action = step["action"]
        if action in LOCATOR_ACTIONS and not step.get("selector", "").strip():
            raise ValueError(prefix + "此操作需要定位器。")
        if action in REQUIRED_VALUES and not step.get("value"):
            raise ValueError(prefix + "请填写目标值或预期结果。")
        if action == "assert_count":
            value = step["value"]
            variable = re.fullmatch(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", value)
            if not variable and not (re.fullmatch(r"[0-9]{1,5}", value) and int(value) <= 10000):
                raise ValueError(prefix + "预期数量必须为 0–10000 的整数或变量占位符。")
        if action == "goto":
            value = step["value"]
            if not (value.startswith("/") and not value.startswith("//")):
                try:
                    validate_url(value)
                except ValueError as exc:
                    raise ValueError(prefix + str(exc)) from None
    return steps


def target_url(base, value):
    return validate_url(urljoin(base.rstrip("/") + "/", value))
