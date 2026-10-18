"""Fail-closed release check for one existing formal CI run; never deploys."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import ssl
import sys
import time
import uuid
from urllib.parse import urlsplit
from urllib.request import Request, HTTPSHandler, ProxyHandler, build_opener

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployment.kiwi_ci import MappedHTTPSHandler, NoRedirect


def write_result(path, result, simulate):
    value = dict(
        result,
        checked_at=datetime.now(timezone.utc).isoformat(),
        simulation=simulate,
        deployment_performed=False,
    )
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type", choices=("api", "web"), required=True)
    parser.add_argument("--suite", type=int, required=True)
    parser.add_argument("--key", required=True, help="本次 Jenkins 构建的固定提交 UUID")
    parser.add_argument("--plan", type=int, required=True)
    parser.add_argument("--build", type=int, required=True)
    parser.add_argument("--run-report", default="kiwi-report.json")
    parser.add_argument("--output", default="kiwi-release-gate.json")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--simulate", action="store_true", help="仅记录模拟发布获准，不执行部署")
    args = parser.parse_args(argv)
    denied = {"allowed": False, "status": "blocked", "reason": "release_check_error"}
    try:
        write_result(args.output, denied, args.simulate)
        if min(args.suite, args.plan, args.build, args.timeout) < 1:
            raise ValueError
        base = os.environ.get("KIWI_BASE_URL", "").rstrip("/")
        url = urlsplit(base)
        token = os.environ.get("KIWI_CI_TOKEN", "")
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or not token
        ):
            raise ValueError
        connect_host = os.environ.get("KIWI_CONNECT_HOST", "")
        import re

        if connect_host and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", connect_host):
            raise ValueError
        # Reports are bounded and have to come from this build's formal client invocation.
        report_path = Path(args.run_report)
        if report_path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError
        report = json.loads(report_path.read_text(encoding="utf-8"))
        context = report["ci_context"]
        key = str(uuid.UUID(args.key))
        if context.get("submission_key") != key:
            raise ValueError
        expected = {
            "type": args.type,
            "suite": args.suite,
            "mode": "formal",
            "plan": args.plan,
            "build": args.build,
        }
        if any(context.get(key) != value for key, value in expected.items()):
            raise ValueError
        run_id = str(uuid.UUID(report["run_id"]))
        if report.get("terminal") is not True:
            raise ValueError
        tls = ssl.create_default_context(cafile=os.environ.get("KIWI_CA_BUNDLE") or None)
        handler = (
            MappedHTTPSHandler(context=tls, connect_host=connect_host)
            if connect_host
            else HTTPSHandler(context=tls)
        )
        opener = build_opener(ProxyHandler({}), handler, NoRedirect())
        route = "/web-testing" if args.type == "web" else "/ai/api-testing"
        endpoint = f"{base}{route}/ci/suites/{args.suite}/runs/{run_id}/release-gate/"
        body = json.dumps(
            {"plan_id": args.plan, "build_id": args.build, "submission_key": key}
        ).encode()
        deadline = time.monotonic() + args.timeout
        last_wait_reason = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                write_result(args.output, dict(denied, reason="approval_wait_timeout"), args.simulate)
                return 1
            req = Request(
                endpoint,
                method="POST",
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with opener.open(req, timeout=min(30, remaining)) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError
                payload = json.loads(data)
            if (
                not isinstance(payload, dict)
                or payload.get("run_id") != run_id
                or payload.get("suite_id") != args.suite
                or payload.get("kind") != args.type
                or payload.get("plan_id") != args.plan
                or payload.get("build_id") != args.build
                or type(payload.get("allowed")) is not bool
                or payload.get("status") not in {"allowed", "blocked", "waiting"}
                or (payload["allowed"] != (payload["status"] == "allowed"))
            ):
                raise ValueError
            write_result(args.output, payload, args.simulate)
            if payload["status"] != "waiting":
                print(
                    "发布检查："
                    + (
                        "通过（仅模拟，不部署）"
                        if args.simulate and payload["allowed"]
                        else "通过" if payload["allowed"] else "已阻断"
                    )
                )
                return 0 if payload["allowed"] else 1
            if payload.get("reason") != last_wait_reason:
                print(f"发布检查等待：{payload.get('reason')}；执行 {run_id}", flush=True)
                last_wait_reason = payload.get("reason")
            time.sleep(min(5, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        # Never print credentials, request headers, response bodies or secret URLs.
        try:
            write_result(args.output, denied, args.simulate)
        except OSError:
            pass
        print("发布检查失败，已阻断。请核对本次正式执行、令牌、证书及平台状态。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
