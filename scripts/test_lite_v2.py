import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import lite


class FakeSummary:
    def __init__(self, report, *, exit_code=0):
        command = report.get("validation_commands", [None])[0] if report.get("validation_commands") else None
        shell = []
        if command:
            shell.append(
                {
                    "command": command,
                    "command_truncated": False,
                    "tool_status": "completed" if exit_code == 0 else "error",
                    "exit": exit_code,
                    "sequence": 2,
                }
            )
        self._public = {
            "result_submission": {"report": report, "error": None},
            "session_id": "session-1",
            "tools": {"read": 2, "edit": 1, "bash": 1, "codex_worker_submit_result": 1},
            "worker_tokens": {"reported": {"input": 100, "output": 20}, "coverage": "observed_step_events"},
            "last_non_shell_activity": 1,
            "shell_commands": shell,
            "malformed_output": False,
        }

    def public(self):
        return self._public


class LiteV2Tests(unittest.TestCase):
    def base_config(self):
        return {
            **lite.BASE,
            "writer_default": "provider/default",
            "variants": {"provider/default": "max", "provider/project": "fast"},
        }

    def args(self, brief, **overrides):
        values = {
            "brief": str(brief),
            "model": None,
            "variant": None,
            "explicit": False,
            "timeout": 30,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_route_priority_and_variant(self):
        data = self.base_config()
        root = "/repo"
        data["projects"][root] = "provider/project"
        self.assertEqual(
            lite.resolve_route(data, root),
            ("project", "provider/project", "fast"),
        )
        self.assertEqual(
            lite.resolve_route(data, root, "provider/oneshot", "x"),
            ("one-shot", "provider/oneshot", "x"),
        )

    def test_alias_route(self):
        data = self.base_config()
        data["aliases"]["cheap"] = "provider/default"
        data["writer_default"] = "cheap"
        self.assertEqual(
            lite.resolve_route(data, "/repo"),
            ("global", "provider/default", "max"),
        )

    def test_manual_mode_blocks_without_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.txt"
            brief.write_text("GOAL\nDo work.")
            data = self.base_config()
            data["mode"] = "manual"
            with patch.object(lite, "worker_env") as env, patch.object(lite.streaming, "run") as run:
                result = lite.run_worker(self.args(brief), data, tmp, Path(tmp) / "config.json")
            self.assertEqual(result["status"], "mode_blocked")
            env.assert_not_called()
            run.assert_not_called()

    def test_single_worker_run_with_structured_result_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.txt"
            brief.write_text("GOAL\nImplement it.")
            report = {
                "status": "completed",
                "changed": ["a.py"],
                "validation": ["python3 -m unittest: passed"],
                "risk": "none",
                "validation_commands": ["python3 -m unittest"],
            }
            with patch.object(lite, "worker_env", return_value={}), patch.object(
                lite.streaming, "run", return_value=(0, FakeSummary(report), False, None)
            ) as run:
                result = lite.run_worker(
                    self.args(brief),
                    self.base_config(),
                    tmp,
                    Path(tmp) / "config.json",
                )
            self.assertEqual(run.call_count, 1)
            cli = run.call_args.args[0]
            self.assertIn("-m", cli)
            self.assertEqual(cli[cli.index("-m") + 1], "provider/default")
            self.assertIn("--variant", cli)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["model"], "provider/default")
            self.assertEqual(result["variant"], "max")
            self.assertEqual(result["validation_evidence"]["status"], "observed_pass")

    def test_observed_validation_failure_blocks_completed(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "brief.txt"
            brief.write_text("GOAL\nImplement it.")
            report = {
                "status": "completed",
                "changed": ["a.py"],
                "validation": ["python3 -m unittest: failed"],
                "risk": "none",
                "validation_commands": ["python3 -m unittest"],
            }
            with patch.object(lite, "worker_env", return_value={}), patch.object(
                lite.streaming, "run", return_value=(0, FakeSummary(report, exit_code=1), False, None)
            ):
                result = lite.run_worker(
                    self.args(brief),
                    self.base_config(),
                    tmp,
                    Path(tmp) / "config.json",
                )
            self.assertEqual(result["status"], "needs_escalation")
            self.assertEqual(result["validation_evidence"]["status"], "failed")

    def test_config_writes_preserve_unknown_full_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({**self.base_config(), "fallbacks": ["provider/backup"]}))
            data = lite.load(path)
            data["mode"] = "manual"
            lite.save(path, data)
            written = json.loads(path.read_text())
            self.assertEqual(written["fallbacks"], ["provider/backup"])
            self.assertEqual(written["mode"], "manual")

    def test_checkout_lock_rejects_second_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with lite.checkout_lock(tmp, cfg):
                with self.assertRaises(lite.Failure) as raised:
                    with lite.checkout_lock(tmp, cfg):
                        pass
            self.assertEqual(raised.exception.code, "busy")


if __name__ == "__main__":
    unittest.main()
