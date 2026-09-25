"""Bounded HTTP execution with immutable submissions and explicit TCMS writeback."""

import http.client
import http.cookiejar
from http.cookies import SimpleCookie, CookieError
import copy
import json
import math
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from tcms.testruns.models import TestExecution, TestExecutionStatus, TestRun

from .api_validation import SENSITIVE, expand, lookup_json, redact, validate_case, validate_destination, validate_headers, variable_names
from .crypto import decrypt_api_key, encrypt_api_key
from .models import APICase, APIEnvironment, APIResult, APIRun

CASE_FIELDS = ("name", "method", "path", "headers", "query", "body", "send_body",
               "expected_status", "assertions", "max_elapsed_ms", "test_case_id", "extracts", "sequence")
MAX_RESPONSE = 1024 * 1024


def can_write_run(owner, run):
    return (owner.is_active and owner.has_perm("testruns.change_testexecution")
            and (owner.has_perm("testruns.change_testrun")
                 or owner.has_perm("testruns.change_testrun", run)))


def prepare_case(case, environment):
    variables = environment["variables"]
    # Older queued snapshots do not contain extraction or ordering fields.
    prepared = dict(case, extracts=case.get("extracts", {}))
    for key in ("headers", "query", "assertions"):
        prepared[key] = expand(case[key], variables)
    prepared["body"] = expand(case["body"], variables) if case["send_body"] else {}
    # Path substitution must never turn a variable into another URL or query.
    prepared["path"] = expand(case["path"], {
        key: quote(str(value), safe="") for key, value in variables.items()
    })
    headers = {}
    for source in (environment["headers"], prepared["headers"], environment["secret_headers"]):
        headers.update({key.lower(): value for key, value in expand(source, variables).items()})
    prepared["headers"] = headers
    validate_case(prepared)
    return prepared


def required_variables(case, environment):
    return variable_names([
        case["path"], case["headers"], case["query"], case["assertions"],
        case["body"] if case["send_body"] else {},
        environment["headers"], environment["secret_headers"],
    ])


def submit_run(owner, product, data, source_run=None, suite=None, trigger="manual"):
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner.pk)
        existing = APIRun.objects.filter(owner=owner, submission_token=data["submission_token"]).first()
        if existing:
            original = json.loads(decrypt_api_key(existing.snapshot_encrypted))
            selected = {
                "environment_id": data["environment"].pk,
                "case_ids": sorted(case.pk for case in data["cases"]),
                "test_run_id": data["test_run"].pk if data.get("test_run") else None,
                "passed_status": data["passed_status"].pk if data.get("passed_status") else None,
                "failed_status": data["failed_status"].pk if data.get("failed_status") else None,
                "stop_on_failure": bool(data.get("stop_on_failure")),
                "source_run_id": str(source_run.pk) if source_run else None,
                "share_cookies": bool(data.get("share_cookies")),
                "suite_id": suite.pk if suite else None,
            }
            original_selection = dict(original["selection"])
            original_selection.setdefault("stop_on_failure", False)
            original_selection.setdefault("source_run_id", None)
            original_selection.setdefault("share_cookies", False)
            original_selection.setdefault("suite_id", None)
            if original_selection != selected:
                raise ValueError("这份表单已提交过其他选择，请重新打开执行页面。")
            return existing
        environment = APIEnvironment.objects.select_for_update().get(
            pk=data["environment"].pk, owner=owner, product=product
        )
        validate_destination(environment.base_url)
        env = {
            "base_url": environment.base_url, "headers": environment.headers,
            "variables": environment.variables, "timeout": environment.timeout,
            "secret_headers": json.loads(decrypt_api_key(environment.secret_headers_encrypted) or "{}"),
        }
        if not 1 <= env["timeout"] <= 30:
            raise ValueError("环境超时配置无效。")
        validate_headers(env["headers"])
        validate_headers(env["secret_headers"])
        cases = list(APICase.objects.select_for_update().filter(
            pk__in=[case.pk for case in data["cases"]], owner=owner, product=product
        ).order_by("sequence", "pk"))
        if not cases or len(cases) != len(data["cases"]) or len(cases) > 20:
            raise ValueError("用例已发生变化，请重新选择，最多 20 条。")
        target = data.get("test_run")
        if target:
            target = TestRun.objects.select_for_update().get(pk=target.pk)
            if (target.plan.product_id != product.pk or target.stop_date is not None
                    or not can_write_run(owner, target)):
                raise ValueError("没有权限回写该测试运行，或运行已经结束。")
            if APIRun.objects.filter(test_run=target, status__in=APIRun.ACTIVE_STATUSES).exists():
                raise ValueError("该测试运行已有接口任务正在执行，请等它完成后再提交。")
            if not (data.get("passed_status") and data.get("failed_status")):
                raise ValueError("请选择回写状态。")
            if data["passed_status"].weight <= 0 or data["failed_status"].weight >= 0:
                raise ValueError("回写状态的成功/失败类型不匹配。")
        snapshots = []
        linked_ids = set()
        validation_env = copy.deepcopy(env)
        if source_run and (source_run.owner_id != owner.pk or source_run.product_id != product.pk
                           or not source_run.is_terminal):
            raise ValueError("只能重新执行自己已结束的同产品任务。")
        for case in cases:
            if case.test_case_id and case.test_case.category.product_id != product.pk:
                raise ValueError("用例的业务分类已移到其他产品，请重新配置自动化套件。")
            snapshot = {key: getattr(case, key) for key in CASE_FIELDS}
            if case.test_case_id:
                snapshot["name"] = case.test_case.summary
            snapshot["case_id"] = case.pk
            missing = required_variables(snapshot, validation_env) - validation_env["variables"].keys()
            if missing:
                raise ValueError(f"“{case.name}”缺少变量：{', '.join(sorted(missing))}。请在环境中配置，或选择排在前面的提取用例。")
            prepare_case(snapshot, validation_env)
            for name in case.extracts:
                validation_env["variables"][name] = "runtime-value"
            if target:
                executions = list(target.executions.filter(case_id=case.test_case_id))
                if (len(executions) != 1 or case.test_case.category.product_id != product.pk
                        or executions[0].pk in linked_ids):
                    raise ValueError(f"“{case.name}”需要关联该运行中唯一且未重复选择的一条测试用例。")
                execution = executions[0]
                linked_ids.add(execution.pk)
                snapshot["execution_id"] = execution.pk
                snapshot["history_id"] = execution.history.latest().history_id
            snapshots.append(snapshot)
        snapshot = {"environment": env, "cases": snapshots,
                    "stop_on_failure": bool(data.get("stop_on_failure")),
                    "share_cookies": bool(data.get("share_cookies")),
                    "selection": {
                        "environment_id": environment.pk,
                        "case_ids": sorted(case.pk for case in cases),
                        "test_run_id": target.pk if target else None,
                        "passed_status": data["passed_status"].pk if data.get("passed_status") else None,
                        "failed_status": data["failed_status"].pk if data.get("failed_status") else None,
                        "stop_on_failure": bool(data.get("stop_on_failure")),
                        "source_run_id": str(source_run.pk) if source_run else None,
                        "share_cookies": bool(data.get("share_cookies")),
                        "suite_id": suite.pk if suite else None,
                    },
                    "passed_status": data["passed_status"].pk if target else None,
                    "failed_status": data["failed_status"].pk if target else None}
        run = APIRun.objects.create(
            owner=owner, product=product, environment_name=environment.name,
            submission_token=data["submission_token"], test_run=target,
            source_run=source_run,
            suite=suite, trigger=trigger,
            snapshot_encrypted=encrypt_api_key(json.dumps(snapshot)),
        )
        APIResult.objects.bulk_create([
            APIResult(run=run, position=index, name=case["name"], test_case_id=case["test_case_id"])
            for index, case in enumerate(snapshots)
        ])
        return run


def send_http(environment, case):
    """Pinned connection, no redirects/proxies/retries; optional per-run cookie jar."""
    scheme, host, port = validate_destination(environment["base_url"])
    address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
    timeout = environment["timeout"]
    started = time.monotonic()
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    sock = socket.create_connection((address, port), timeout=timeout)
    remaining = max(0.001, timeout - (time.monotonic() - started))
    sock.settimeout(remaining)
    active_socket = [sock]

    def expire():
        try:
            active_socket[0].shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass

    conn.sock = sock
    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        if scheme == "https":
            conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            active_socket[0] = conn.sock
        prefix = urlsplit(environment["base_url"]).path.rstrip("/")
        path = quote(prefix + case["path"], safe="/%:@-._~!$&'()*+,;=")
        if case["query"]:
            path += "?" + urlencode(case["query"], doseq=True)
        body = json.dumps(case["body"]).encode() if case["send_body"] else None
        headers = dict(case["headers"])
        jar = environment.get("_cookies")
        url_host = f"[{host}]" if ":" in host else host
        cookie_request = Request(f"{scheme}://{url_host}:{port}{path}", headers=headers,
                                 method=case["method"])
        if jar is not None:
            jar.add_cookie_header(cookie_request)
            cookie = cookie_request.get_header("Cookie")
            if cookie and "cookie" not in headers:
                headers["cookie"] = cookie
                case["headers"]["cookie"] = cookie
        if body is not None:
            headers.setdefault("content-type", "application/json")
        conn.request(case["method"], path, body=body, headers=headers)
        response = conn.getresponse()
        for header in response.headers.get_all("Set-Cookie", []):
            try:
                incoming = SimpleCookie()
                incoming.load(header)
                for morsel in incoming.values():
                    environment.setdefault("_cookie_secrets", []).extend((morsel.value, quote(morsel.value, safe="")))
            except CookieError:
                pass
        if jar is not None:
            jar.extract_cookies(response, cookie_request)
            jar.clear_expired_cookies()
            # Bound session memory even for a misbehaving server.
            if len(jar) > 100:
                raise ValueError("Cookie 数量超过限制")
            for cookie in jar:
                environment["_cookie_secrets"].extend((cookie.value, quote(cookie.value, safe="")))
        content = response.read(MAX_RESPONSE + 1)
        if len(content) > MAX_RESPONSE:
            raise ValueError("响应超过 1 MB 限制，已停止读取。")
        elapsed = round((time.monotonic() - started) * 1000)
        if elapsed > timeout * 1000:
            raise TimeoutError
        return response.status, content, elapsed
    finally:
        timer.cancel()
        conn.close()
        sock.close()


def check_response(case, status, content, elapsed):
    checks = [{"label": "HTTP 状态码", "passed": status == case["expected_status"],
               "expected": case["expected_status"], "actual": status}]
    if case["max_elapsed_ms"]:
        checks.append({"label": "响应耗时（毫秒）", "passed": elapsed <= case["max_elapsed_ms"],
                       "expected": case["max_elapsed_ms"], "actual": elapsed})
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError):
        payload = None
    for assertion in case["assertions"]:
        found, current = lookup_json(payload, assertion["path"])
        equals = type(current) is type(assertion.get("expected")) and current == assertion.get("expected")
        checks.append({"label": assertion["path"],
                       "passed": found and (assertion["operator"] == "exists" or equals),
                       "expected": assertion.get("expected") if assertion["operator"] == "equals" else "字段存在",
                       "actual": current if found else "字段不存在或响应不是 JSON"})
    return checks, payload


def extract_response(case, payload):
    values, checks = {}, []
    display_payload = copy.deepcopy(payload)
    for name, path in sorted(case.get("extracts", {}).items(), key=lambda item: item[1].count("."), reverse=True):
        found, value = lookup_json(payload, path)
        valid = found and isinstance(value, (str, int, float, bool))
        valid = valid and len(str(value)) <= 4096 and not (isinstance(value, float) and not math.isfinite(value))
        checks.append({"label": f"提取变量 {name} ← {path}", "passed": bool(valid),
                       "expected": "存在且为可引用的简单值", "actual": "已提取（值隐藏）" if valid else "字段缺失、为空或类型/长度不支持"})
        if valid:
            values[name] = value
        if found:
            parts = path.split(".")
            parent = display_payload
            for part in parts[:-1]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            parent[int(parts[-1]) if isinstance(parent, list) else parts[-1]] = "[已隐藏]"
    return values, checks, display_payload


def secrets_for(case, environment):
    values = [str(value) for key, value in environment["variables"].items() if SENSITIVE.search(key)]
    values.extend(str(value) for value in expand(
        environment["secret_headers"], environment["variables"]
    ).values())

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if SENSITIVE.search(key) and isinstance(item, (str, int, float)):
                    values.append(str(item))
                    if isinstance(item, str) and item.lower().startswith("bearer "):
                        values.append(item[7:])
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)
    collect({key: case.get(key) for key in ("headers", "query", "body")})
    for key, value in case.get("headers", {}).items():
        if key.lower() == "cookie":
            try:
                cookies = SimpleCookie()
                cookies.load(value)
                values.extend(morsel.value for morsel in cookies.values())
            except CookieError:
                pass
    values.extend(value[7:] for value in list(values) if value.lower().startswith("bearer "))
    return values + [quote(value, safe="") for value in values]


def writeback_result(run, result, snapshot, case):
    if not case.get("execution_id"):
        return "未关联测试运行"
    if result.status not in ("passed", "failed"):
        return "请求异常，未改动测试执行状态，请排查后重新执行"
    with transaction.atomic():
        target = TestRun.objects.select_for_update().filter(pk=run.test_run_id).first()
        owner = get_user_model().objects.get(pk=run.owner_id)
        if not target or target.stop_date or not can_write_run(owner, target):
            return "未回写：运行已结束、删除或账号权限已变更"
        execution = TestExecution.objects.select_for_update().filter(
            pk=case["execution_id"], run=target, case_id=case["test_case_id"]
        ).first()
        if not execution or execution.history.latest().history_id != case["history_id"]:
            return "未回写：提交后测试执行已被修改，请人工核对报告"
        status = TestExecutionStatus.objects.filter(
            pk=snapshot["passed_status" if result.status == "passed" else "failed_status"]
        ).first()
        if not status or (status.weight > 0) != (result.status == "passed") or status.weight == 0:
            return "未回写：所选执行状态已被删除或改变类型"
        execution.status = status
        execution.tested_by = owner
        execution.start_date = result.started
        execution.stop_date = result.completed
        execution._history_user = owner
        execution._change_reason = f"接口自动化报告 {run.pk}，结果 #{result.pk}"
        execution.save()
        return f"已回写测试执行 #{execution.pk}：{status.name}"


def execute_run(run):
    try:
        snapshot = json.loads(decrypt_api_key(run.snapshot_encrypted))
        env = snapshot["environment"]
        env["_cookie_secrets"] = []
        if snapshot.get("share_cookies"):
            env["_cookies"] = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(
                strict_ns_domain=http.cookiejar.DefaultCookiePolicy.DomainStrict))
        runtime_secrets = []
        for index, case in enumerate(snapshot["cases"]):
            run.refresh_from_db(fields=("status",))
            if run.status != "running":
                break
            if not get_user_model().objects.filter(pk=run.owner_id, is_active=True).exists():
                raise ValueError("账号已停用")
            result = run.results.get(position=index)
            if result.status != "pending":
                raise ValueError("执行记录已存在，拒绝重复发送请求")
            result.started = timezone.now()
            extracts = case.get("extracts", {})
            missing = required_variables(case, env) - env["variables"].keys()
            if missing:
                result.status = "skipped"
                result.error = "前置步骤未提供变量：" + ", ".join(sorted(missing))[:400]
                result.completed = timezone.now()
                result.save()
                for name in extracts:
                    env["variables"].pop(name, None)
                if snapshot.get("stop_on_failure"):
                    run.results.filter(status="pending").update(status="skipped", error="因前序步骤未完成，已停止后续用例")
                    break
                continue
            try:
                prepared = prepare_case(case, env)
                secrets = secrets_for(prepared, env) + runtime_secrets
                result.request_summary = redact({
                    "method": prepared["method"], "path": prepared["path"],
                    "query": prepared["query"], "headers": prepared["headers"],
                    "body": prepared["body"] if prepared["send_body"] else None,
                }, secrets)
                result.status_code, content, result.elapsed_ms = send_http(env, prepared)
                runtime_secrets.extend(env.get("_cookie_secrets", []))
                if "cookie" in prepared["headers"]:
                    result.request_summary["headers"]["cookie"] = "[已隐藏]"
                checks, payload = check_response(prepared, result.status_code, content, result.elapsed_ms)
                extracted, extraction_checks, display_payload = extract_response(case, payload)
                # Extracted values stay only in this run's memory, even on failure.
                for value in extracted.values():
                    runtime_secrets.extend((str(value), quote(str(value), safe="")))
                secrets = secrets + runtime_secrets
                result.request_summary = redact(result.request_summary, secrets)
                checks.extend(extraction_checks)
                result.status = "passed" if all(item["passed"] for item in checks) else "failed"
                for name in extracts:
                    env["variables"].pop(name, None)
                if result.status == "passed":
                    env["variables"].update(extracted)
                # Redact assertion values too, including responses with credentials.
                for check in checks:
                    if SENSITIVE.search(check["label"]) and not check["label"].startswith("提取变量 "):
                        check["expected"] = check["actual"] = "[已隐藏]"
                result.checks = redact(checks, secrets)
                result.response_summary = (
                    json.dumps(redact(display_payload, secrets), ensure_ascii=False, indent=2)[:12000]
                    if payload is not None else "响应不是 JSON；为避免记录敏感文本，仅保存状态码和断言结果。"
                )
            except (TimeoutError, socket.timeout):
                result.status, result.error = "error", "请求超时；未自动重试，请确认服务是否已处理该请求。"
            except Exception as exc:
                # Exception messages can embed request URLs or credentials.
                result.status = "error"
                result.error = f"请求无法完成（{type(exc).__name__}）。检查服务地址、网络、证书、变量或响应大小；未自动重试。"
            if result.status != "passed":
                for name in extracts:
                    env["variables"].pop(name, None)
            result.completed = timezone.now()
            try:
                with transaction.atomic():
                    result.writeback = writeback_result(run, result, snapshot, case)
                    result.save()
            except Exception:
                result.writeback = "回写失败，接口结果已保存，请人工核对测试执行"
                result.save()
            if snapshot.get("stop_on_failure") and result.status in ("failed", "error"):
                run.results.filter(status="pending").update(status="skipped", error="因前序用例失败，已停止后续执行")
                break
        with transaction.atomic():
            current = APIRun.objects.select_for_update().get(pk=run.pk)
            if current.status in ("running", "cancel_requested"):
                current.status = "cancelled" if current.status == "cancel_requested" else "completed"
                current.completed = timezone.now()
                current.save(update_fields=("status", "completed"))
            current.results.filter(status="pending").update(status="skipped")
    except Exception:
        with transaction.atomic():
            APIRun.objects.filter(pk=run.pk).update(
                status="interrupted", completed=timezone.now(),
                error="执行中断。已发送的请求不会自动重放，请核对已有结果后再创建新任务。",
            )
            run.results.filter(status="pending").update(
                status="skipped", completed=timezone.now(),
                error="任务中断，未取得执行结果；请求可能已发送，请核对目标服务。",
            )


def execute_next_api_run():
    with transaction.atomic():
        run = APIRun.objects.select_for_update().filter(status="queued").order_by("created").first()
        if run is None:
            return False
        run.status = "running"
        run.started = timezone.now()
        run.save(update_fields=("status", "started"))
    execute_run(run)
    return True
