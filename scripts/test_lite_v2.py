"""Adversarial event and process fixtures. Never calls a provider or real OpenCode."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lite
import streaming
import submission


def report(**extra):
    return dict(status="completed", changed=["main.py"], validation=["fixture check: passed"],
                risk="none", validation_commands=["python3 -m unittest"], **extra)


def tool(name, call, **state):
    return dict(type="tool_use", sessionID="fixture-session", part=dict(
        tool=name, callID=call, state=dict(status="completed", **state)))


def finish(reason="stop"):
    return dict(type="step_finish", sessionID="fixture-session",
                part=dict(id="end", reason=reason, tokens=dict(input=10, output=2, reasoning=0,
                    total=12, cache=dict(read=0, write=0))))


def events(value=None, code=0, ending=True):
    value = report() if value is None else value
    result = [tool("edit", "edit", input={"filePath": "main.py"}),
              tool("bash", "check", input={"command": "python3 -m unittest"}, metadata={"exit": code}),
              tool(submission.TOOL, "submit", output=json.dumps({"accepted": True, "report": value}))]
    return result + ([finish()] if ending else [])


def summary(items):
    out = streaming.Summary(lambda _: False)
    for item in items:
        out.consume(item)
    return out


class LiteV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "main.py").write_text("pass\n")
        self.brief = self.root / "brief.txt"
        self.brief.write_text("GOAL\nImplement only the requested change.")
        self.cfg = self.root / "settings/config.json"
        self.default = self.root / "home/.config/opencode-worker/config.json"
        self.patcher = patch.object(lite, "DEFAULT_CONFIG", self.default)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.data = copy.deepcopy(lite.BASE)
        self.data.update(writer_default="provider/default", variants={"provider/default": "max"})

    def args(self, **extra):
        values = dict(brief=str(self.brief), model=None, variant=None, explicit=False,
                      timeout=10, read_only=False, skill_dir=[])
        values.update(extra)
        return SimpleNamespace(**values)

    def result(self, items=None, **extra):
        return lite.result_from_summary(0, summary(events() if items is None else items),
                                       "provider/default", "max", "global", 0.1, **extra)

    def test_priority_alias_variant(self):
        self.data["projects"][str(self.root)] = "cheap"
        self.data["aliases"]["cheap"] = "provider/project"
        self.data["variants"]["provider/project"] = "fast"
        self.assertEqual(lite.resolve_route(self.data, str(self.root)), ("project", "provider/project", "fast"))
        self.assertEqual(lite.resolve_route(self.data, str(self.root), "provider/once", "x"),
                         ("one-shot", "provider/once", "x"))
        self.assertEqual(lite.resolve_route(self.data, str(self.root), "provider/once"),
                         ("one-shot", "provider/once", None))

    def test_config_preserves_unknown_fields_without_shared_defaults(self):
        self.cfg.parent.mkdir()
        self.cfg.write_text(json.dumps(dict(self.data, fallbacks=["provider/backup"])))
        loaded = lite.load(self.cfg); loaded["mode"] = "manual"
        lite.save(self.cfg, loaded)
        self.assertEqual(lite.load(self.cfg)["fallbacks"], ["provider/backup"])
        a = lite.load(self.root / "missing1"); b = lite.load(self.root / "missing2")
        a["projects"]["x"] = "y"
        self.assertEqual(b["projects"], {})

    def test_invalid_config_values_fail_cleanly(self):
        self.cfg.parent.mkdir()
        for change in ({"writer_default": []}, {"aliases": {"cheap": []}}, {"variants": []},
                       {"project_modes": {"x": "wrong"}}, {"projects": {"x": 2}}):
            with self.subTest(change=change):
                self.cfg.write_text(json.dumps(dict(self.data, **change)))
                with self.assertRaises(lite.Failure): lite.load(self.cfg)

    def test_off_vetoes_project_auto_and_explicit(self):
        self.data["mode"] = "off"
        self.data["project_modes"][str(self.root)] = "auto"
        with patch.object(lite.streaming, "run") as run:
            result = lite.run_worker(self.args(explicit=True), self.data, str(self.root), self.cfg)
        self.assertEqual(result["status"], "mode_blocked"); run.assert_not_called()

    def test_manual_requires_explicit(self):
        self.data["mode"] = "manual"
        with patch.object(lite, "worker_env") as env:
            result = lite.run_worker(self.args(), self.data, str(self.root), self.cfg)
        self.assertEqual(result["status"], "mode_blocked"); env.assert_not_called()

    def test_normal_run_launches_once_without_inventory(self):
        self.data["fallbacks"] = ["provider/backup"]
        with patch.object(lite, "worker_env", return_value={}), patch.object(lite, "model_inventory") as inventory, \
             patch.object(lite.streaming, "run", return_value=(0, summary(events()), False, None)) as run:
            result = lite.run_worker(self.args(), self.data, str(self.root), self.cfg)
        self.assertEqual(run.call_count, 1); inventory.assert_not_called()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[cmd.index("-m") + 1], "provider/default")
        self.assertEqual(cmd[cmd.index("--variant") + 1], "max")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_evidence"]["status"], "observed_pass")

    def test_partial_result_survives_interruption_without_retry(self):
        error = TimeoutError("fixture")
        error.worker_summary = summary(events())
        error.worker_launched = True
        with patch.object(lite, "worker_env", return_value={}), patch.object(lite.streaming, "run", side_effect=error) as run:
            result = lite.run_worker(self.args(), self.data, str(self.root), self.cfg)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["status"], "needs_escalation")
        self.assertEqual(result["result"]["changed"], ["main.py"])
        self.assertTrue(result["worker_process_started"])
        self.assertIn("not guaranteed", result["message"])

    def test_error_after_accepted_report_is_not_completed(self):
        items = events() + [dict(type="error", error=dict(name="APIError", data=dict(statusCode=500)))]
        self.assertEqual(self.result(items)["status"], "needs_escalation")

    def test_missing_stop_is_not_completed(self):
        self.assertEqual(self.result(events(ending=False))["status"], "needs_escalation")

    def test_length_limited_stop_is_not_completed(self):
        self.assertEqual(self.result(events(ending=False) + [finish("length")])["status"], "needs_escalation")

    def test_denial_cannot_be_hidden_by_later_submission(self):
        denial = dict(type="tool_use", sessionID="fixture-session", part=dict(tool="bash", callID="denied",
                       state=dict(status="error", error="rejected permission")))
        self.assertEqual(self.result([denial] + events())["status"], "needs_escalation")

    def test_invalid_json_event_is_not_completed(self):
        self.assertEqual(self.result(events() + [42])["status"], "needs_escalation")

    def test_prose_is_not_submission(self):
        self.assertEqual(self.result([dict(type="text", part=dict(text="completed")), finish()])["status"], "needs_escalation")

    def test_activity_after_submission_invalidates_report(self):
        self.assertEqual(self.result(events() + [tool("read", "late")])["status"], "needs_escalation")

    def test_changed_session_invalidates_report(self):
        self.assertEqual(self.result(events() + [dict(type="step_start", sessionID="another")])["status"], "needs_escalation")

    def test_failed_declared_validation_blocks_completed(self):
        result = self.result(events(code=1))
        self.assertEqual(result["status"], "needs_escalation")
        self.assertEqual(result["validation_evidence"]["failed"], 1)

    def test_absent_validation_commands_are_unverified_not_false_failure(self):
        value = report(); value.pop("validation_commands")
        result = self.result(events(value))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_evidence"]["status"], "unverified")

    def test_model_selection_is_not_independent_observation(self):
        result = self.result()
        self.assertEqual(result["model_source"], "selected_cli_arguments")
        self.assertIsNone(result["observed_model"])
        self.assertIsNone(result["observed_variant"])
        self.assertEqual(result["session_id"], "fixture-session")

    def test_readonly_report_and_events_reject_mutation(self):
        self.assertEqual(self.result(read_only=True)["status"], "needs_escalation")
        value = report(); value["changed"] = []; value["validation_commands"] = []
        native = [tool("read", "read"), tool(submission.TOOL, "submit", output=json.dumps({"accepted": True, "report": value})), finish()]
        self.assertEqual(self.result(native, read_only=True)["status"], "completed")
        self.assertEqual(self.result(events(value), read_only=True)["status"], "needs_escalation")

    def test_lock_is_config_independent_and_full_compatible(self):
        expected = self.default.parent / "locks" / (lite.hashlib.sha256(os.fsencode(str(self.root.resolve()))).hexdigest() + ".lock")
        self.assertEqual(lite.lock_path(str(self.root), self.cfg), expected)
        self.assertEqual(lite.lock_path(str(self.root), self.root / "other/config.json"), expected)
        with lite.checkout_lock(str(self.root), self.cfg):
            with self.assertRaises(lite.Failure):
                with lite.checkout_lock(str(self.root), self.root / "other/config.json"): pass

    def test_symlink_and_git_subdirectory_share_checkout_lock(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        nested = self.root / "nested"; nested.mkdir()
        link = self.root / "alias"; link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(lite.lock_path(str(self.root)), lite.lock_path(str(nested)))
        self.assertEqual(lite.lock_path(str(self.root)), lite.lock_path(str(link)))

    def test_separate_checkouts_do_not_share_lock(self):
        other = self.root / "other"; other.mkdir()
        self.assertNotEqual(lite.lock_path(str(self.root)), lite.lock_path(str(other)))

    def test_rejects_empty_and_scratch_only_target(self):
        (self.root / "main.py").unlink()
        (self.root / "work").mkdir()
        with self.assertRaises(lite.Failure) as error:
            lite.run_worker(self.args(), self.data, str(self.root), self.cfg)
        self.assertEqual(error.exception.code, "invalid_project")

    def test_run_requires_explicit_project(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(lite, "run_worker") as run:
            self.assertEqual(lite.main(["run", "--brief", str(self.brief)]), 2)
        run.assert_not_called()

    def test_invalid_timeout_is_rejected_before_launch(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(timeout=value), self.assertRaises(lite.Failure):
                lite.run_worker(self.args(timeout=value), self.data, str(self.root), self.cfg)

    def test_permission_profile_readonly_and_shared_originals(self):
        with patch.object(submission, "configure", side_effect=lambda env: None):
            env = lite.worker_env(True)
            permissions = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["codex-worker"]["permission"]
            for name in ("*", "bash", "edit", "task"): self.assertEqual(permissions[name], "deny")
            original = str(self.root / "known-skill")
            env = lite.worker_env(False, [original])
            permissions = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agent"]["codex-worker"]["permission"]
            self.assertEqual(permissions["external_directory"][original], "allow")
            self.assertEqual(permissions["edit"][original + "/*"], "deny")
            self.assertEqual(list(permissions["read"])[-2:], ["*.env", "*.env.*"])
            self.assertEqual(permissions["bash"]["git push *"], "deny")

    def test_setup_checks_model_and_variant_without_overwriting_on_failure(self):
        setter = lite.parser().parse_args(["set-default", "provider/default", "--variant", "max"])
        with patch.object(lite, "model_inventory", return_value={"provider/default": ["fast"]}):
            with self.assertRaises(lite.Failure): lite.update_config(setter, self.cfg, str(self.root))
        self.assertFalse(self.cfg.exists())
        with patch.object(lite, "model_inventory", return_value={"provider/default": ["max"]}):
            lite.update_config(setter, self.cfg, str(self.root))
        self.assertEqual(lite.load(self.cfg)["variants"]["provider/default"], "max")

    def test_inventory_parses_only_enabled_variants(self):
        payload = 'provider/a\n' + json.dumps({'variants': {'max': {}, 'off': {'disabled': True}}}) + '\n'
        with patch.object(lite.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=payload)):
            self.assertEqual(lite.model_inventory(str(self.root), 'provider'), {'provider/a': ['max']})

    def test_inventory_malformed_metadata_is_rejected(self):
        for metadata in ([], {'variants': []}, {'variants': {'max': None}}):
            payload = 'provider/a\n' + json.dumps(metadata)
            with self.subTest(metadata=metadata), patch.object(lite.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=payload)):
                with self.assertRaises(lite.Failure): lite.model_inventory(str(self.root), 'provider')

    def test_config_updates_share_full_lock(self):
        args = lite.parser().parse_args(["set-mode", "manual"])
        with lite.file_lock(self.cfg.with_suffix(".lock")):
            with self.assertRaises(lite.Failure): lite.update_config(args, self.cfg, str(self.root))

    def test_clear_routes_modes_and_variant(self):
        lite.save(self.cfg, self.data)
        args = lite.parser().parse_args(["set-default", "provider/default", "--clear-variant"])
        with patch.object(lite, "model_inventory", return_value={"provider/default": []}):
            lite.update_config(args, self.cfg, str(self.root))
        self.assertNotIn("provider/default", lite.load(self.cfg)["variants"])
        args = lite.parser().parse_args(["set-default", "null"])
        with patch.object(lite, "model_inventory") as inventory:
            lite.update_config(args, self.cfg, str(self.root))
        inventory.assert_not_called()
        self.assertIsNone(lite.resolve_route(lite.load(self.cfg), str(self.root))[1])

    def test_full_subprocess_fixture_exercises_real_launcher_and_jsonl(self):
        exe = self.root / "opencode"
        captured = self.root / "fixture-capture.json"
        payload = events()
        exe.write_text("#!/usr/bin/env python3\nimport sys,json,os\n"
                       "assert sys.argv[1]=='run', 'unexpected extra CLI call'\n"
                       "assert not os.path.exists(" + repr(str(captured)) + "), 'unexpected replay'\n"
                       "json.dump({'args':sys.argv[1:],'prompt':sys.stdin.read()},open(" + repr(str(captured)) + ",'w'))\n"
                       "for event in " + repr(payload) + ": print(json.dumps(event),flush=True)\n")
        exe.chmod(0o700)
        sdk_root = self.root / "sdk"
        sdk = sdk_root / "node_modules/@opencode-ai/plugin/dist/tool.js"
        sdk.parent.mkdir(parents=True); sdk.write_text("// fake CLI only; no real SDK/provider\n")
        lite.save(self.cfg, self.data)
        env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
               "XDG_CONFIG_HOME": str(self.root / "isolated-config"), "OPENCODE_CONFIG_DIR": str(sdk_root)}
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        command = [sys.executable, str(Path(lite.__file__).resolve()), "--project", str(self.root),
                   "--config", str(self.cfg), "run", "--brief", str(self.brief), "--explicit"]
        proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["validation_evidence"]["status"], "observed_pass")
        self.assertIsNone(value["observed_model"])
        args = json.loads(captured.read_text())["args"]
        self.assertEqual(args[args.index("-m") + 1], "provider/default")
        self.assertEqual(args[args.index("--variant") + 1], "max")
        self.assertNotIn("private", proc.stdout)


if __name__ == "__main__":
    unittest.main()
