"""Release client safety tests; these need only the Python standard library."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch
import uuid

from deployment.kiwi_release_gate import main


class ReleaseClientTests(unittest.TestCase):
    def invoke(self, reply=None, *, context=None, network_error=None, timeout=1):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "run.json"
            output = Path(directory) / "gate.json"
            run_id = str(uuid.uuid4())
            key = str(uuid.uuid4())
            source.write_text(
                json.dumps(
                    dict(
                        run_id=run_id,
                        terminal=True,
                        ci_context=context
                        or dict(type="api", suite=2, mode="formal", plan=3, build=4),
                    )
                ),
                encoding="utf-8",
            )
            document = json.loads(source.read_text(encoding="utf-8"))
            document["ci_context"]["submission_key"] = key
            source.write_text(json.dumps(document), encoding="utf-8")
            output.write_text('{"allowed":true,"status":"allowed"}', encoding="utf-8")
            payload = dict(
                allowed=True,
                status="allowed",
                reason="approved_release",
                run_id=run_id,
                suite_id=2,
                kind="api",
                plan_id=3,
                build_id=4,
            )
            payload.update(reply or {})
            response, opener = MagicMock(), MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            opener.open.return_value = response
            opener.open.side_effect = network_error
            with patch.dict(
                "os.environ",
                {
                    "KIWI_BASE_URL": "https://platform.example.test",
                    "KIWI_CI_TOKEN": "unit-test-token-only",
                },
                clear=True,
            ), patch("deployment.kiwi_release_gate.build_opener", return_value=opener), patch(
                "deployment.kiwi_release_gate.time.sleep"
            ), patch(
                "builtins.print"
            ) as printed:
                result = main(
                    [
                        "--type",
                        "api",
                        "--suite",
                        "2",
                        "--plan",
                        "3",
                        "--build",
                        "4",
                        "--run-report",
                        str(source),
                        "--output",
                        str(output),
                        "--timeout",
                        str(timeout),
                        "--key",
                        key,
                        "--simulate",
                    ]
                )
            self.assertNotIn("unit-test-token-only", str(printed.call_args_list))
            return result, json.loads(output.read_text(encoding="utf-8")), opener

    def test_allowed_only_records_simulation_not_deployment(self):
        code, result, opener = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(result["allowed"])
        self.assertTrue(result["simulation"])
        self.assertFalse(result["deployment_performed"])
        self.assertIn("/runs/", opener.open.call_args.args[0].full_url)
        self.assertEqual(opener.open.call_args.args[0].method, "POST")

    def test_blocked_stops(self):
        code, result, _ = self.invoke(
            dict(allowed=False, status="blocked", reason="release_gate_failed")
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["allowed"])

    def test_wrong_run_build_or_boolean_does_not_allow(self):
        for reply in (
            dict(run_id=str(uuid.uuid4())),
            dict(build_id=999),
            dict(allowed="true"),
            dict(status="waiting"),
            dict(kind="web"),
        ):
            with self.subTest(reply=reply):
                code, result, _ = self.invoke(reply)
                self.assertEqual(code, 2)
                self.assertFalse(result["allowed"])

    def test_network_error_overwrites_old_green_receipt(self):
        code, result, _ = self.invoke(network_error=OSError("network unavailable"))
        self.assertEqual(code, 2)
        self.assertFalse(result["allowed"])

    def test_debug_or_foreign_context_never_calls_server(self):
        for changed in (dict(mode="debug"), dict(build=999), dict(suite=99)):
            context = dict(type="api", suite=2, mode="formal", plan=3, build=4, **{})
            context.update(changed)
            code, result, opener = self.invoke(context=context)
            self.assertEqual(code, 2)
            self.assertFalse(result["allowed"])
            opener.open.assert_not_called()

    def test_waiting_times_out_without_green_receipt(self):
        code, result, _ = self.invoke(
            dict(allowed=False, status="waiting", reason="approval_required")
        )
        self.assertEqual(code, 1)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "approval_wait_timeout")


if __name__ == "__main__":
    unittest.main()
