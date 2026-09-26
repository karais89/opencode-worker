"""Installed entrypoints use one bundle and preserve the existing CLI contract."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import install


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "codex"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "seed.txt"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "-qm", "seed"], check=True)

    def next_source(self):
        source = self.root / "next-source"
        shutil.copytree(install.SOURCE, source,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        (source / "VERSION").write_text("2.2.0\n", encoding="utf-8")
        return source

    def test_two_skills_share_one_bundle_and_cli_resolves_both_engines(self):
        install.install(self.home)
        bundle = self.home / "worker-bundles" / install.VERSION
        self.assertTrue((bundle / "scripts" / "grok_adapter.py").is_file())
        self.assertTrue((bundle / "references" / "grok.md").is_file())
        self.assertTrue((bundle / "references" / "worker-contract.md").is_file())
        config = self.root / "config.json"
        config.write_text(json.dumps({"writer_default": "provider/open", "grok": {
            "default": "grok-4.7", "projects": {}}}), encoding="utf-8")
        for name, engine, model in (("opencode-worker", "opencode", "provider/open"),
                                    ("grok-worker", "grok", "grok-4.7")):
            skill = self.home / "skills" / name
            self.assertFalse((skill / "references").exists())
            self.assertFalse((skill / "scripts" / "grok_adapter.py").exists())
            instructions = (skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("../../worker-bundles/" + install.VERSION + "/references/worker-contract.md",
                          instructions)
            command = [sys.executable, str(skill / "scripts" / "lite.py"),
                       "--project", str(self.repo), "--config", str(config), "resolve"]
            if engine == "grok":
                command.extend(["--engine", "grok"])
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            report = json.loads(result.stdout)
            self.assertEqual((report["engine"], report["model"]), (engine, model))
        self.assertEqual(len(list((self.home / "worker-bundles").iterdir())), 1)

    def test_existing_unmanaged_skill_is_preserved(self):
        target = self.home / "skills" / "opencode-worker"
        target.mkdir(parents=True)
        (target / "note.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            install.install(self.home, replace=True)
        self.assertEqual((target / "note.txt").read_text(encoding="utf-8"), "keep")

    def test_agents_home_layout_is_supported(self):
        agents_home = self.root / ".agents"
        install.install(agents_home)
        self.assertTrue((agents_home / "skills" / "opencode-worker" / "SKILL.md").is_file())
        self.assertTrue((agents_home / "skills" / "grok-worker" / "SKILL.md").is_file())
        self.assertTrue((agents_home / "worker-bundles" / install.VERSION / "scripts" / "lite.py").is_file())

    def test_installed_grok_entrypoint_runs_fake_cli(self):
        install.install(self.home)
        report = {"status": "completed", "changed": ["answer.txt"],
                  "validation": ["fixture passed"], "risk": "none", "validation_commands": []}
        fake = self.root / "grok-fake.py"
        fake.write_text(
            "import json, os, pathlib, sys\n"
            "assert os.environ.get('GROK_MEMORY') == '0'\n"
            "root = pathlib.Path(sys.argv[sys.argv.index('--cwd')+1])\n"
            "(root/'answer.txt').write_text('done\\n')\n"
            "print(json.dumps({'type':'text','data':" + repr(json.dumps(report)) + "}), flush=True)\n"
            "print(json.dumps({'type':'end','stopReason':'end_turn','sessionId':'fake'}), flush=True)\n",
            encoding="utf-8")
        brief = self.root / "brief.txt"
        brief.write_text("GOAL: write answer.txt\nDONE WHEN: its content is done", encoding="utf-8")
        env = dict(os.environ, GROK_WORKER_BIN=str(fake), OPENCODE_WORKER_CONFIG=str(self.root / "config.json"))
        command = [sys.executable, str(self.home / "skills" / "grok-worker" / "scripts" / "lite.py"),
                   "--project", str(self.repo), "run", "--engine", "grok", "--brief", str(brief),
                   "--explicit", "--inactivity-timeout", "5"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")
        self.assertEqual((self.repo / "answer.txt").read_text(encoding="utf-8"), "done\n")

    def test_installed_opencode_entrypoint_runs_fake_cli(self):
        install.install(self.home)
        report = {"status": "completed", "changed": ["answer.txt"],
                  "validation": ["fixture passed"], "risk": "none", "validation_commands": []}
        events = [
            {"type": "tool_use", "sessionID": "fake-session", "part": {
                "tool": "edit", "callID": "edit", "state": {"status": "completed",
                "input": {"filePath": "answer.txt"}}}},
            {"type": "tool_use", "sessionID": "fake-session", "part": {
                "tool": "codex_worker_submit_result", "callID": "submit", "state": {
                "status": "completed", "output": json.dumps({"accepted": True, "report": report})}}},
            {"type": "step_finish", "sessionID": "fake-session", "part": {
                "id": "end", "reason": "stop", "tokens": {"input": 1, "output": 1,
                "reasoning": 0, "total": 2, "cache": {"read": 0, "write": 0}}}},
        ]
        fake = self.root / "opencode-fake.py"
        fake.write_text("import json,sys\nassert sys.argv[1]=='run'\n"
                        "assert '-m' in sys.argv and sys.argv[sys.argv.index('-m')+1]=='provider/open'\n"
                        "sys.stdin.read()\nfor event in " + repr(events) + ": print(json.dumps(event),flush=True)\n",
                        encoding="utf-8")
        sdk = self.root / "sdk" / "node_modules/@opencode-ai/plugin/dist/tool.js"
        sdk.parent.mkdir(parents=True)
        sdk.write_text("// fake SDK path\n", encoding="utf-8")
        config = self.root / "config.json"
        config.write_text(json.dumps({"writer_default": "provider/open"}), encoding="utf-8")
        brief = self.root / "brief.txt"
        brief.write_text("GOAL: write answer.txt\nDONE WHEN: its content is done", encoding="utf-8")
        env = dict(os.environ, OPENCODE_WORKER_BIN=str(fake), OPENCODE_CONFIG_DIR=str(self.root / "sdk"))
        env.pop("OPENCODE_CONFIG_CONTENT", None)
        command = [sys.executable, str(self.home / "skills" / "opencode-worker" / "scripts" / "lite.py"),
                   "--project", str(self.repo), "--config", str(config), "run", "--brief", str(brief),
                   "--explicit", "--inactivity-timeout", "5"]
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["status"], "completed")

    def test_upgrade_moves_both_skills_to_new_bundle_and_keeps_old_bundle(self):
        install.install(self.home)
        source = self.next_source()
        with patch.object(install, "SOURCE", source), patch.object(install, "VERSION", "2.2.0"):
            install.install(self.home, replace=True)
        for name in ("opencode-worker", "grok-worker"):
            skill = self.home / "skills" / name
            self.assertEqual((skill / "BUNDLE_VERSION").read_text(encoding="utf-8").strip(), "2.2.0")
            self.assertIn("worker-bundles/2.2.0/references/worker-contract.md",
                          (skill / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((self.home / "worker-bundles" / install.VERSION / "scripts" / "lite.py").is_file())
        self.assertTrue((self.home / "worker-bundles" / "2.2.0" / "scripts" / "lite.py").is_file())

    def test_mixed_versions_and_missing_bundle_block_upgrade(self):
        install.install(self.home)
        old_version = install.VERSION
        grok = self.home / "skills" / "grok-worker"
        (grok / "BUNDLE_VERSION").write_text("other\n", encoding="utf-8")
        source = self.next_source()
        with patch.object(install, "SOURCE", source), patch.object(install, "VERSION", "2.2.0"):
            with self.assertRaisesRegex(ValueError, "버전이 다릅니다"):
                install.install(self.home, replace=True)
            (grok / "BUNDLE_VERSION").write_text(old_version + "\n", encoding="utf-8")
            (self.home / "worker-bundles" / old_version / "VERSION").unlink()
            with self.assertRaisesRegex(ValueError, "공유 번들"):
                install.install(self.home, replace=True)
        self.assertFalse((self.home / "worker-bundles" / "2.2.0").exists())

    def test_same_version_and_partial_install_block_replace(self):
        install.install(self.home)
        opencode = self.home / "skills" / "opencode-worker"
        original = (opencode / "SKILL.md").read_bytes()
        with self.assertRaisesRegex(ValueError, "같은 버전은 덮어쓰지 않습니다"):
            install.install(self.home, replace=True)
        self.assertEqual((opencode / "SKILL.md").read_bytes(), original)
        shutil.rmtree(self.home / "skills" / "grok-worker")
        with self.assertRaisesRegex(ValueError, "하나만 있습니다"):
            install.install(self.home, replace=True)
        self.assertEqual((opencode / "SKILL.md").read_bytes(), original)

    def test_replace_restores_previous_install_if_promotion_fails(self):
        install.install(self.home)
        opencode = self.home / "skills" / "opencode-worker"
        old = (opencode / "SKILL.md").read_bytes()
        original = os.replace
        failed = False

        def fail_once(source, target):
            nonlocal failed
            if not failed and Path(target) == self.home / "skills" / "grok-worker" and Path(source).name == "grok-worker":
                failed = True
                raise OSError("fixture promotion failure")
            return original(source, target)

        source = self.next_source()
        with patch.object(install, "SOURCE", source), patch.object(install, "VERSION", "2.2.0"):
            with patch.object(install.os, "replace", side_effect=fail_once):
                with self.assertRaises(OSError):
                    install.install(self.home, replace=True)
        self.assertTrue(failed)
        self.assertEqual((opencode / "SKILL.md").read_bytes(), old)
        self.assertTrue((self.home / "skills" / "grok-worker" / "SKILL.md").is_file())
        self.assertTrue((self.home / "worker-bundles" / install.VERSION / "scripts" / "lite.py").is_file())


if __name__ == "__main__":
    unittest.main()
