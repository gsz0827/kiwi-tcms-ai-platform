"""Trigger a Kiwi automation suite, await completion and emit CI artifacts (stdlib only)."""
import argparse
import json
import os
import ssl
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, HTTPSHandler, ProxyHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def write_reports(payload, output):
    Path(output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    results = payload.get("results", [])
    suite = ET.Element("testsuite", name="Kiwi API automation", tests=str(len(results)),
        failures=str(sum(r["status"] == "failed" for r in results)),
        errors=str(sum(r["status"] == "error" for r in results)),
        skipped=str(sum(r["status"] in ("pending", "skipped") for r in results)))
    for result in results:
        case = ET.SubElement(suite, "testcase", name=result["name"],
            time=str((result.get("elapsed_ms") or 0) / 1000))
        status = result["status"]
        if status != "passed":
            node = ET.SubElement(case, {"failed": "failure", "error": "error"}.get(status, "skipped"),
                                 message=result.get("error") or status)
            node.text = json.dumps(result.get("checks", []), ensure_ascii=False)
    ET.ElementTree(suite).write(str(Path(output).with_suffix(".xml")), encoding="utf-8", xml_declaration=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=int, required=True)
    parser.add_argument("--output", default="api-report.json")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--key", default=os.environ.get("KIWI_CI_KEY"), help="同一次构建重试使用相同 UUID")
    args = parser.parse_args(argv)
    base = os.environ.get("KIWI_BASE_URL", "").rstrip("/")
    token = os.environ.get("KIWI_CI_TOKEN", "")
    try:
        url = urlsplit(base)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("KIWI_BASE_URL 必须是 HTTPS 站点地址")
        if not token or args.timeout < 1 or args.suite < 1:
            raise ValueError("请设置 CI 令牌、有效套件编号和超时")
        key = str(uuid.UUID(args.key)) if args.key else str(uuid.uuid4())
        print(f"本次提交标识：{key}（网络中断后可用 --key 重试）", flush=True)
        context = ssl.create_default_context(cafile=os.environ.get("KIWI_CA_BUNDLE") or None)
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        endpoint = f"{base}/ai/api-testing/ci/suites/{args.suite}/runs/"
        deadline = time.monotonic() + args.timeout

        def request(address, method):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            req = Request(address, method=method, headers={"Authorization": f"Bearer {token}",
                "Idempotency-Key": key, "Accept": "application/json"})
            with opener.open(req, timeout=min(30, remaining)) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("报告过大")
                return json.loads(data)

        payload = request(endpoint, "POST")
        run_id = str(uuid.UUID(payload["run_id"]))
        while not payload["terminal"]:
            if time.monotonic() >= deadline:
                raise TimeoutError
            time.sleep(min(3, max(0, deadline - time.monotonic())))
            payload = request(endpoint + run_id + "/", "GET")
        write_reports(payload, args.output)
        print(f"执行状态：{payload['status']}；报告已保存至 {args.output}")
        return 0 if payload["passed"] else 1
    except HTTPError as exc:
        print(f"CI 请求被拒绝（HTTP {exc.code}），请检查令牌、套件及平台状态。", file=sys.stderr)
    except (ValueError, KeyError, OSError, URLError, TimeoutError):
        print("CI 请求或报告处理失败，请检查地址、证书、网络与超时。已提交的任务不会自动取消。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
