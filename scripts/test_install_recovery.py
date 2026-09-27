"""Interrupt and rollback regression tests using isolated installation homes."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install


class InstallRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"

    def next_source(self):
        source = self.root / "next-source"
        shutil.copytree(install.SOURCE, source,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
        (source / "VERSION").write_text("2.3.0\n", encoding="utf-8")
        return source

    def tree_contents(self, path):
        return {file.relative_to(path).as_posix(): file.read_bytes()
                for file in path.rglob("*") if file.is_file()}

    def install_quietly(self, home, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            install.install(home, **kwargs)

    def test_interruptions_before_and_after_every_rename_restore_originals(self):
        source = self.next_source()
        original_replace = os.replace
        for exception in (KeyboardInterrupt, SystemExit, OSError):
            for position in range(1, 6):
                for after_move in (False, True):
                    with self.subTest(exception=exception, position=position, after_move=after_move):
                        home = (self.root / (exception.__name__ + str(position) + str(after_move))).resolve()
                        self.install_quietly(home)
                        originals = self.tree_contents(home / "skills")
                        moves = 0

                        def interrupt(src, dst):
                            nonlocal moves
                            moves += 1
                            if moves == position:
                                if after_move:
                                    original_replace(src, dst)
                                raise exception("fixture interruption")
                            return original_replace(src, dst)

                        with patch.object(install, "SOURCE", source), patch.object(install, "VERSION", "2.3.0"):
                            with patch.object(install.installer_v2.os, "replace", side_effect=interrupt):
                                with self.assertRaises(exception):
                                    self.install_quietly(home, replace=True)
                        self.assertGreaterEqual(moves, position)
                        self.assertEqual(self.tree_contents(home / "skills"), originals)
                        self.assertFalse((home / "worker-bundles/2.3.0").exists())
                        self.assertTrue((home / "worker-bundles" / install.VERSION).is_dir())
                        self.assertFalse(list((home / "worker-backups").glob("*")))
                        self.assertFalse(list(home.glob("worker-install-*")))

    def test_failed_rollback_preserves_originals_and_attempts_other_restores(self):
        source = self.next_source()
        original_replace, original_rmtree = os.replace, shutil.rmtree
        for blocked in ("restore", "remove"):
            with self.subTest(blocked=blocked):
                home = (self.root / ("rollback-" + blocked)).resolve()
                self.install_quietly(home)
                opencode, grok = (home / "skills" / name for name in install.installer_v2.NAMES)
                originals = {p: self.tree_contents(p) for p in (opencode, grok)}
                interrupted = False

                def replace(src, dst):
                    nonlocal interrupted
                    src, dst = Path(src), Path(dst)
                    if not interrupted and dst == grok and src.name == "grok-worker":
                        interrupted = True
                        raise KeyboardInterrupt("fixture interruption")
                    if blocked == "restore" and dst == opencode and src.parent.name == "worker-backups":
                        raise OSError("fixture restore failure")
                    return original_replace(src, dst)

                def remove(path, *args, **kwargs):
                    if blocked == "remove" and interrupted and Path(path) == opencode:
                        raise OSError("fixture cleanup failure")
                    return original_rmtree(path, *args, **kwargs)

                with patch.object(install, "SOURCE", source), patch.object(install, "VERSION", "2.3.0"):
                    with patch.object(install.installer_v2.os, "replace", side_effect=replace):
                        with patch.object(install.installer_v2.shutil, "rmtree", side_effect=remove):
                            with self.assertRaisesRegex(OSError, "rollback incomplete") as error:
                                self.install_quietly(home, replace=True)
                self.assertIsInstance(error.exception.__cause__, KeyboardInterrupt)
                saved = list((home / "worker-backups").iterdir())
                self.assertEqual(len(saved), 1)
                self.assertEqual(self.tree_contents(saved[0]), originals[opencode])
                self.assertIn(str(saved[0]), str(error.exception))
                self.assertEqual(self.tree_contents(grok), originals[grok])
                self.assertTrue((home / "worker-bundles/2.3.0/scripts/lite.py").is_file())
                self.assertFalse(list(home.glob("worker-install-*")))
                if blocked == "remove":
                    self.assertEqual((opencode / "BUNDLE_VERSION").read_text().strip(), "2.3.0")
                else:
                    self.assertFalse(opencode.exists())

    def test_interrupted_unmanaged_migration_restores_original_folder(self):
        target = self.home.resolve() / "skills/grok-worker"
        target.mkdir(parents=True)
        (target / "personal-note.txt").write_text("keep this", encoding="utf-8")
        originals = self.tree_contents(target)
        original_replace = os.replace
        interrupted = False

        def interrupt(src, dst):
            nonlocal interrupted
            if not interrupted and Path(dst) == target and Path(src).name == "grok-worker":
                interrupted = True
                raise KeyboardInterrupt("fixture interruption")
            return original_replace(src, dst)

        with patch.object(install.installer_v2.os, "replace", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.install_quietly(self.home, skill="grok-worker", migrate_unmanaged=True)
        self.assertTrue(interrupted)
        self.assertEqual(self.tree_contents(target), originals)
        self.assertFalse((self.home / "worker-bundles" / install.VERSION).exists())
        self.assertFalse(list((self.home / "worker-backups").glob("*")))

    def test_failed_bundle_promotion_does_not_remove_concurrent_destination(self):
        home = self.home.resolve()
        bundle = home / "worker-bundles" / install.VERSION
        original_replace = os.replace

        def collide(src, dst):
            src, dst = Path(src), Path(dst)
            if src.name == "bundle" and dst == bundle:
                shutil.copytree(src, dst)
                (dst / "CONCURRENT_INSTALL").write_text("keep", encoding="utf-8")
                raise OSError("simulated concurrent installer won")
            return original_replace(src, dst)

        with patch.object(install.installer_v2.os, "replace", side_effect=collide):
            with self.assertRaisesRegex(OSError, "concurrent installer won"):
                self.install_quietly(home, skill="opencode-worker")
        self.assertEqual((bundle / "CONCURRENT_INSTALL").read_text(encoding="utf-8"), "keep")
        self.assertFalse((home / "skills/opencode-worker").exists())

    def test_concurrent_install_is_rejected_before_writing_skills_or_bundle(self):
        home = self.home.resolve()
        ready, release = self.root / "lock-ready", self.root / "lock-release"
        script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "import installer_v2\n"
            "home,ready,release=map(Path,sys.argv[1:])\n"
            "home.mkdir(parents=True,exist_ok=True)\n"
            "with installer_v2.install_lock(home):\n"
            " ready.write_text('ready',encoding='utf-8')\n"
            " while not release.exists(): time.sleep(0.01)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(home), str(ready), str(release)],
            cwd=install.SOURCE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop_process():
            release.touch(exist_ok=True)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        self.addCleanup(stop_process)
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), "lock holder exited or timed out: " + str(process.poll()))
        with self.assertRaisesRegex(ValueError, "진행 중"):
            self.install_quietly(home, skill="opencode-worker")
        self.assertFalse((home / "skills").exists())
        self.assertFalse((home / "worker-bundles").exists())
        release.touch()
        self.assertEqual(process.wait(timeout=5), 0)


if __name__ == "__main__":
    unittest.main()
