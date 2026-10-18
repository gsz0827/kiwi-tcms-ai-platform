"""Jenkins parameter adapters; arguments never use user-controlled shell interpolation."""

import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from deployment.kiwi_ci import main, write_reports
from deployment.kiwi_release_gate import main as gate_main, write_result


def run():
    gate = len(sys.argv) > 1 and sys.argv[1] in ("gate", "simulate")
    try:
        kind = os.environ.get("CI_TEST_TYPE", "api")
        if kind not in ("api", "web"):
            raise ValueError
        suite = int(os.environ["CI_SUITE_ID"])
        if suite < 1:
            raise ValueError
        mode = os.environ.get("CI_WEB_MODE", "debug")
        if mode not in ("debug", "formal"):
            raise ValueError
        key = uuid.uuid5(
            uuid.NAMESPACE_URL,
            ":".join([os.environ["JOB_NAME"], os.environ["BUILD_NUMBER"], kind, str(suite)]),
        )
        if gate:
            if mode != "formal":
                raise ValueError
            args = [
                "--type",
                kind,
                "--suite",
                str(suite),
                "--key",
                str(key),
                "--plan",
                str(int(os.environ["CI_PLAN_ID"])),
                "--build",
                str(int(os.environ["CI_BUILD_ID"])),
                "--timeout",
                "30" if sys.argv[1] == "simulate" else "600",
            ]
            if sys.argv[1] == "simulate":
                args += ["--simulate", "--output", "kiwi-release-simulation.json"]
            return gate_main(args)
        args = [
            "--type",
            kind,
            "--suite",
            str(suite),
            "--key",
            str(key),
            "--mode",
            mode,
            "--output",
            "kiwi-report.json",
            "--timeout",
            "900",
        ]
        for field in ("plan", "build", "environment"):
            value = os.environ.get("CI_" + field.upper() + "_ID", "")
            if value:
                number = int(value)
                if number < 1:
                    raise ValueError
                args += ["--" + field, str(number)]
        return main(args)
    except (ValueError, KeyError):
        if gate:
            destination = (
                "kiwi-release-simulation.json"
                if sys.argv[1] == "simulate"
                else "kiwi-release-gate.json"
            )
            write_result(
                destination,
                {"allowed": False, "status": "blocked", "reason": "invalid_jenkins_parameters"},
                sys.argv[1] == "simulate",
            )
        else:
            write_reports(
                dict(status="error", error="Jenkins 执行参数无效。", results=[]), "kiwi-report.json"
            )
        return 2


if __name__ == "__main__":
    sys.exit(run())
