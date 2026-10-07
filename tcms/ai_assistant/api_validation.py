"""Declarative HTTP cases: no Python, shell, or user-provided expressions."""

import json
import re
from urllib.parse import urlsplit

from django.conf import settings

VARIABLE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key", re.I)
JSON_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")


def lookup_json(payload, path):
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False, None
    return True, current


def variable_names(value):
    if isinstance(value, str):
        return set(VARIABLE.findall(value))
    if isinstance(value, dict):
        return set().union(*(variable_names(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(variable_names(item) for item in value))
    return set()


def origin_for(url):
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment or parsed.query
                or any(char.isspace() for char in url) or "\\" in url):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        return parsed.scheme, parsed.hostname.lower(), port
    except (ValueError, TypeError) as exc:
        raise ValueError("请填写有效的 http/https 服务地址，不含账号、查询参数或片段。") from exc


def validate_destination(url):
    origin = origin_for(url)
    allowed = getattr(settings, "API_AUTOMATION_ALLOWED_ORIGINS", [])
    if origin not in [origin_for(item) for item in allowed]:
        raise ValueError("该服务地址尚未开放。请在部署配置 KIWI_API_ALLOWED_ORIGINS 中添加协议、主机和端口。")
    return origin


def validate_headers(value):
    if not isinstance(value, dict) or len(value) > 30:
        raise ValueError("请求头必须是最多 30 项的 JSON 对象。")
    blocked = {"host", "content-length", "transfer-encoding", "connection", "upgrade",
               "accept-encoding", "expect", "trailer", "te"}
    for key, item in value.items():
        if (not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key)
                or key.lower() in blocked or key.lower().startswith("proxy-")
                or not isinstance(item, str) or "\r" in item or "\n" in item):
            raise ValueError("请求头名称或值无效；Host、连接和传输控制头由执行器管理。")


def validate_case(data):
    if data["method"] not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise ValueError("不支持的请求方法。")
    path = data["path"]
    if (not path.startswith("/") or path.startswith("//") or "\\" in path
            or "?" in path or "#" in path or any(ord(c) < 32 for c in path)):
        raise ValueError("接口路径应以单个 / 开头，查询参数请填到查询参数字段。")
    validate_headers(data.get("headers", {}))
    if not isinstance(data.get("query", {}), dict):
        raise ValueError("查询参数必须是 JSON 对象。")
    if not 100 <= data["expected_status"] <= 599:
        raise ValueError("预期状态码应在 100 到 599 之间。")
    if not 0 <= data["max_elapsed_ms"] <= 30000:
        raise ValueError("响应时间上限应在 0 到 30000 毫秒之间，0 表示不检查。")
    assertions = data.get("assertions", [])
    if not isinstance(assertions, list) or len(assertions) > 30:
        raise ValueError("字段断言必须是最多 30 项的 JSON 数组。")
    for item in assertions:
        if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                or not JSON_PATH.fullmatch(item["path"])
                or item.get("operator") not in ("equals", "exists")
                or (item["operator"] == "equals" and "expected" not in item)):
            raise ValueError('断言示例：[{"path":"data.id","operator":"equals","expected":1}]；也支持 exists。')
    extracts = data.get("extracts", {})
    if not isinstance(extracts, dict) or len(extracts) > 20:
        raise ValueError("响应变量提取必须是最多 20 项的 JSON 对象。")
    for name, path in extracts.items():
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or not isinstance(path, str) or not JSON_PATH.fullmatch(path)):
            raise ValueError('响应提取示例：{"access_token":"data.token"}，变量名只能包含字母、数字和下划线。')
    try:
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, RecursionError) as exc:
        raise ValueError("用例配置需为有效 JSON，不支持 NaN、Infinity 或过深嵌套。") from exc
    if len(encoded) > 65536:
        raise ValueError("单条用例配置不能超过 64 KB。")


def expand(value, variables):
    def replace(match):
        if match[1] not in variables:
            raise ValueError(f"缺少环境参数：{match[1]}")
        return str(variables[match[1]])

    if isinstance(value, str):
        match = VARIABLE.fullmatch(value)
        if match:
            if match[1] not in variables:
                raise ValueError(f"缺少环境参数：{match[1]}")
            return variables[match[1]]
        return VARIABLE.sub(replace, value)
    if isinstance(value, dict):
        return {key: expand(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, variables) for item in value]
    return value


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: "[已隐藏]" if SENSITIVE.search(str(key)) else redact(item, secrets)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[已隐藏]")
    return value
