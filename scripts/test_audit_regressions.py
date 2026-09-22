"""Focused audit regressions; no real OpenCode, provider, or credentials."""
import copy
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import lite
import streaming
import submission
from test_lite_v2 import events, finish, report, summary, tool


def outcome(items):
    return lite.result_from_summary(0, summary(items), "fixture/model", None, "global", 0.1)


def submit(value=None, call="submit"):
    return tool(submission.TOOL, call, output=json.dumps(
        {"accepted": True, "report": report() if value is None else value}))


def fake_cli(root, body):
    exe = root / "opencode"
    exe.write_text(f"#!{sys.executable} -S\n" + body)
    exe.chmod(0o700)
    return {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}


class AuditRegressions(unittest.TestCase):
    def test_old_stop_cannot_complete_new_step_or_later_submission(self):
        for items in (events() + [dict(type="step_start", sessionID="fixture-session")],
                      [finish()] + events(ending=False)):
            with self.subTest(items=items):
                self.assertEqual(outcome(items)["status"], "needs_escalation")

    def test_missing_or_invalid_session_cannot_supply_normal_stop(self):
        for sid in (None, "", 42, "s" * 201):
            end = finish()
            if sid is None:
                end.pop("sessionID")
            else:
                end["sessionID"] = sid
            with self.subTest(sid=sid):
                self.assertEqual(outcome(events(ending=False) + [end])["status"], "needs_escalation")

    def test_pending_tool_cannot_be_hidden_by_submission_and_stop(self):
        pending = tool("bash", "ongoing", input={"command": "python3 -m unittest"})
        pending["part"]["state"]["status"] = "running"
        self.assertEqual(outcome([pending] + events())["status"], "needs_escalation")

    def test_declared_validation_error_without_exit_blocks_completion(self):
        items = events()
        items[1]["part"]["state"].update(status="error", metadata={}, error="Tool failed")
        result = outcome(items)
        self.assertEqual(result["status"], "needs_escalation")
        self.assertEqual(result["validation_evidence"]["failed"], 1)

    def test_missing_exit_on_completed_check_is_not_a_false_failure(self):
        items = events()
        items[1]["part"]["state"]["metadata"] = {}
        result = outcome(items)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_evidence"]["status"], "unverified")

    def test_evicted_duplicate_check_is_not_a_fresh_validation(self):
        check = tool("bash", "old", input={"command": "python3 -m unittest"}, metadata={"exit": 0})
        items = [check, tool("edit", "after-check", input={"filePath": "main.py"})]
        items += [tool("bash", f"other-{i}", input={"command": f"echo {i}"}, metadata={"exit": 0})
                  for i in range(11)]
        items += [copy.deepcopy(check), submit(), finish()]
        self.assertNotEqual(outcome(items)["validation_evidence"]["status"], "observed_pass")

    def test_evicted_conflicting_check_cannot_rewrite_history(self):
        check = tool("bash", "old", input={"command": "python3 -m unittest"}, metadata={"exit": 1})
        items = [check] + [tool("bash", f"other-{i}", input={"command": f"echo {i}"}, metadata={"exit": 0})
                           for i in range(11)]
        changed = copy.deepcopy(check)
        changed["part"]["state"]["metadata"]["exit"] = 0
        self.assertEqual(outcome(items + [changed, submit(), finish()])["status"], "needs_escalation")

    def test_duplicate_check_within_tail_retains_original_order(self):
        check = tool("bash", "old", input={"command": "python3 -m unittest"}, metadata={"exit": 0})
        items = [check, tool("edit", "later", input={"filePath": "main.py"}), check, submit(), finish()]
        self.assertEqual(outcome(items)["validation_evidence"]["status"], "unverified")

    def test_only_recognized_well_formed_events_are_activity(self):
        for value in (42, {}, {"type": "noise"}, {"type": "text", "sessionID": "s", "part": []}):
            with self.subTest(value=value):
                out = streaming.Summary(lambda _: False)
                self.assertFalse(out.consume(value))
                self.assertTrue(out.invalid)
        out = streaming.Summary(lambda _: False)
        self.assertTrue(out.consume({"type": "reasoning", "sessionID": "s", "part": {"text": "thinking"}}))
        self.assertFalse(out.consume({"type": "text", "sessionID": "other", "part": {"text": "x"}}))
        # Providers may fail before a session exists.
        out = streaming.Summary(lambda _: False)
        self.assertTrue(out.consume({"type": "error", "error": {"name": "APIError", "data": {}}}))
        self.assertTrue(out.errors)

    def test_json_noise_does_not_keep_a_process_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = fake_cli(root, "import time\nfor _ in range(60):\n print('{}', flush=True)\n time.sleep(0.04)\n")
            with self.assertRaises(TimeoutError) as caught:
                streaming.run(["run"], directory, 5, "task", env, lambda _: False,
                              inactivity_timeout=0.3, progress_stream=False)
            self.assertEqual(caught.exception.timeout_kind, "inactivity")

    def test_tool_identity_history_is_bounded_and_overflow_fails_closed(self):
        out = streaming.Summary(lambda _: False)
        for i in range(10001):
            out.consume(tool("read", f"read-{i}"))
        self.assertTrue(out.invalid)
        self.assertLessEqual(len(out.tool_calls), 10000)
        self.assertLessEqual(len(out.tool_call_status), 10000)

    def test_unknown_or_missing_tool_identity_is_malformed(self):
        for value in (None, "", [], {}):
            event = tool("read", "valid")
            event["part"]["callID"] = value
            with self.subTest(value=value):
                self.assertEqual(outcome([event] + events())["status"], "needs_escalation")

    def test_skill_wildcard_paths_are_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.py").write_text("pass\n")
            brief = root / "brief.txt"
            brief.write_text("GOAL\nfixture")
            data = copy.deepcopy(lite.BASE)
            data["writer_default"] = "fixture/model"
            for name in ("skill*", "skill?"):
                skill = root / name
                skill.mkdir()
                (skill / "SKILL.md").write_text("fixture")
                args = SimpleNamespace(brief=str(brief), model=None, variant=None, explicit=True,
                                       inactivity_timeout=300, hard_timeout=None, read_only=False,
                                       skill_dir=[str(skill)])
                with self.subTest(name=name), patch.object(lite, "worker_env", side_effect=AssertionError("must not launch")) as env:
                    with self.assertRaises(lite.Failure) as caught:
                        lite.run_worker(args, data, str(root), root / "config.json")
                    self.assertEqual(caught.exception.code, "invalid_skill_dir")
                    env.assert_not_called()

    def test_brief_reads_at_most_limit_plus_one_bytes(self):
        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                if size != 8193:
                    raise AssertionError("unbounded brief read")
                return super().read(size)
        with patch.object(Path, "open", return_value=BoundedReader(b"x" * 9000)):
            with self.assertRaises(lite.Failure):
                lite.read_brief("fixture")

    def test_brief_exact_limit_and_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "brief"
            path.write_bytes(b"x" * 8192)
            self.assertEqual(len(lite.read_brief(path)), 8192)
            path.write_bytes(b"\xff")
            with self.assertRaises(ValueError):
                lite.read_brief(path)

    def test_sigterm_uses_cleanup_and_restores_existing_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child.pid"
            controller = root / "controller.py"
            env = fake_cli(root,
                "import os,signal,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(child)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n")
            controller.write_text(
                "import signal,sys\n"
                f"sys.path.insert(0, {str(Path(streaming.__file__).parent)!r})\n"
                "import streaming\n"
                "original=signal.getsignal(signal.SIGTERM)\n"
                "try:\n"
                f" streaming.run(['run'], {directory!r}, 10, 'task', dict(__import__('os').environ), lambda _: False, progress_stream=False)\n"
                "except KeyboardInterrupt as error:\n"
                " assert error.worker_launched\n"
                " assert signal.getsignal(signal.SIGTERM)==original\n"
                " print('cleaned', flush=True)\n")
            p = subprocess.Popen([sys.executable, "-S", str(controller)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            pid = None
            try:
                deadline = time.monotonic() + 5
                while not child.exists() and time.monotonic() < deadline and p.poll() is None:
                    time.sleep(0.02)
                self.assertTrue(child.exists(), "fake CLI did not start")
                pid = int(child.read_text())
                p.terminate()
                stdout, stderr = p.communicate(timeout=8)
                self.assertEqual(p.returncode, 0, stderr)
                self.assertIn("cleaned", stdout)
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            finally:
                if p.poll() is None:
                    p.kill()
                p.communicate(timeout=5)
                if pid:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_success_restores_callers_sigterm_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            env = fake_cli(Path(directory), "pass\n")
            previous = signal.getsignal(signal.SIGTERM)
            handler = lambda *_: None
            try:
                signal.signal(signal.SIGTERM, handler)
                streaming.run(["run"], directory, 5, "task", env, lambda _: False, progress_stream=False)
                self.assertIs(signal.getsignal(signal.SIGTERM), handler)
            finally:
                signal.signal(signal.SIGTERM, previous)

    def test_timeout_stops_same_group_descendant_after_leader_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "descendant.pid"
            body = "import os,signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            body += f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n"
            env = fake_cli(root,
                f"import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-S','-c',{body!r}])\ntime.sleep(30)\n")
            pid = None
            try:
                with self.assertRaises(TimeoutError):
                    streaming.run(["run"], directory, 1, "task", env, lambda _: False, progress_stream=False)
                self.assertTrue(pid_file.exists())
                pid = int(pid_file.read_text())
                # An orphaned zombie is dead but may await the host's reaper.
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       capture_output=True, text=True, timeout=3).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the JS submission contract test")
    def test_javascript_and_python_submission_contract_agree(self):
        good = report()
        values = [good, {**good, "changed": [], "risk": "\uac80\uc99d\uc644\ub8cc"},
                  {**good, "validation": []}, {**good, "risk": ""},
                  {**good, "risk": "x" * 1801}, {**good, "status": "other"},
                  {**good, "validation_commands": ["test"] * 2},
                  {**good, "validation_commands": ["x" * 257]}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sdk = root / "tool.mjs"
            sdk.write_text("export const tool = x => x;\n"
                           "tool.schema = {enum:()=>({}),string:()=>({}),array:()=>({optional(){return this;}})};\n")
            driver = root / "driver.mjs"
            plugin = (Path(submission.__file__).parent.parent / "assets/submit-result.mjs").as_uri()
            driver.write_text(f"import plugin from {json.dumps(plugin)};\n"
                "let input=''; for await (const c of process.stdin) input+=c;\n"
                "const {tool}=await plugin(); const results=[];\n"
                "for(const v of JSON.parse(input)){try {results.push(JSON.parse(await tool.codex_worker_submit_result.execute(v)).report);} catch {results.push(null);}}\n"
                "console.log(JSON.stringify(results));\n")
            proc = subprocess.run(["node", str(driver)], input=json.dumps(values), capture_output=True,
                                  text=True, timeout=10, check=True,
                                  env={**os.environ, "CODEX_WORKER_TOOL_SDK": sdk.as_uri()})
            expected = []
            for value in values:
                try:
                    expected.append(submission.validate(value))
                except ValueError:
                    expected.append(None)
            self.assertEqual(json.loads(proc.stdout), expected)


if __name__ == "__main__":
    unittest.main()
