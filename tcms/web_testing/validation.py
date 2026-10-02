import os
from urllib.parse import urlsplit, urljoin

from django.conf import settings


ACTIONS = {
    "goto": "打开页面", "click": "点击", "fill": "输入文字", "select": "选择选项",
    "check": "勾选", "assert_visible": "断言元素可见", "assert_text": "断言包含文本",
    "assert_url": "断言网址包含", "assert_title": "断言标题包含",
}


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
        raise ValueError("每条用例需要 1–30 个步骤。")
    if not any(isinstance(s, dict) and s.get("action", "").startswith("assert_") for s in steps):
        raise ValueError("用例至少需要一个断言，不能仅凭操作完成判为通过。")
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in ACTIONS or set(step) - {"action", "selector", "value"}:
            raise ValueError("步骤包含不支持的操作或字段。")
        for field in ("selector", "value"):
            if not isinstance(step.get(field, ""), str) or len(step.get(field, "")) > 2000:
                raise ValueError("定位器和输入值必须是最多 2000 字符的文本。")
        action = step["action"]
        if action in {"click", "fill", "select", "check", "assert_visible", "assert_text"} and not step.get("selector"):
            raise ValueError("点击、输入及元素断言需要定位器。")
        if action in {"goto", "assert_text", "assert_url", "assert_title"} and not step.get("value"):
            raise ValueError("打开页面和文本断言需要填写目标值。")
        if action == "goto":
            value = step["value"]
            if not (value.startswith("/") and not value.startswith("//")):
                validate_url(value)
    return steps


def target_url(base, value):
    return validate_url(urljoin(base.rstrip("/") + "/", value))
