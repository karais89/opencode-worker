#!/usr/bin/env python3
"""Install two Codex skills backed by one immutable, versioned runner bundle."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile


SOURCE = Path(__file__).resolve().parent
VERSION = (SOURCE / "VERSION").read_text(encoding="utf-8").strip()
RUNTIME_ITEMS = ("scripts", "assets", "references", "LICENSE", "VERSION")
SHIM = '''#!/usr/bin/env python3
"""Launch the shared Worker bundle installed with this skill."""
from pathlib import Path
import runpy
import sys

skill = Path(__file__).resolve().parent.parent
version = (skill / "BUNDLE_VERSION").read_text(encoding="utf-8").strip()
runner = skill.parent.parent / "worker-bundles" / version / "scripts" / "lite.py"
if not runner.is_file():
    raise SystemExit("Worker bundle missing: " + str(runner))
sys.path.insert(0, str(runner.parent))
runpy.run_path(str(runner), run_name="__main__")
'''


def install(home: Path, replace: bool = False) -> None:
    home = home.expanduser().resolve()
    skills = home / "skills"
    bundles = home / "worker-bundles"
    bundle = bundles / VERSION
    destinations = [skills / name for name in ("opencode-worker", "grok-worker")]
    existing = [str(path) for path in [bundle, *destinations] if path.exists()]
    if existing and not replace:
        raise ValueError("설치 대상이 이미 있습니다. 교체하려면 --replace를 사용하세요: " + ", ".join(existing))
    if replace:
        present = [path.exists() for path in destinations]
        if any(present) and not all(present):
            raise ValueError("두 스킬 중 하나만 있습니다. 수동으로 설치 상태를 확인하세요.")
        for path in destinations:
            if path.exists() and (not path.is_dir() or not (path / "BUNDLE_VERSION").is_file()):
                raise ValueError("관리 대상 스킬이 아니므로 교체하지 않습니다: " + str(path))
        if bundle.exists() and (not bundle.is_dir() or not (bundle / "VERSION").is_file()):
            raise ValueError("관리 대상 번들이 아니므로 교체하지 않습니다: " + str(bundle))
        if all(present):
            versions = [(path / "BUNDLE_VERSION").read_text(encoding="utf-8").strip()
                        for path in destinations]
            if not versions[0] or versions[0] != versions[1]:
                raise ValueError("두 스킬의 번들 버전이 다릅니다. 수동으로 설치 상태를 확인하세요.")
            prior = bundles / versions[0]
            if (not prior.is_dir() or not (prior / "VERSION").is_file()
                    or (prior / "VERSION").read_text(encoding="utf-8").strip() != versions[0]):
                raise ValueError("기존 공유 번들이 없거나 버전이 다릅니다. 수동으로 설치 상태를 확인하세요.")
            if bundle.exists():
                raise ValueError("대상 번들 버전이 이미 있습니다. 같은 버전은 덮어쓰지 않습니다: " + str(bundle))
        elif bundle.exists():
            raise ValueError("스킬 없이 번들만 있습니다. 수동으로 설치 상태를 확인하세요: " + str(bundle))
    bundles.mkdir(parents=True, exist_ok=True)
    skills.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-install-", dir=home) as staging:
        staged = Path(staging)
        staged_bundle = staged / "bundle"
        staged_bundle.mkdir()
        for name in RUNTIME_ITEMS:
            source = SOURCE / name
            target = staged_bundle / name
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            else:
                shutil.copy2(source, target)
        staged_skills = []
        for name in ("opencode-worker", "grok-worker"):
            source = SOURCE if name == "opencode-worker" else SOURCE / "grok-worker"
            target = staged / name
            (target / "agents").mkdir(parents=True)
            (target / "scripts").mkdir()
            body = (source / "SKILL.md").read_text(encoding="utf-8")
            # Source links stay useful in the checkout; installed links point to one shared references area.
            prefix = "references/" if name == "opencode-worker" else "../references/"
            body = body.replace("](" + prefix, "](../../worker-bundles/" + VERSION + "/references/")
            (target / "SKILL.md").write_text(body, encoding="utf-8")
            shutil.copy2(source / "agents" / "openai.yaml", target / "agents" / "openai.yaml")
            (target / "BUNDLE_VERSION").write_text(VERSION + "\n", encoding="utf-8")
            (target / "scripts" / "lite.py").write_text(SHIM, encoding="utf-8")
            staged_skills.append(target)
        targets = [bundle, *destinations]
        sources = [staged_bundle, *staged_skills]
        backups = {}
        installed = []
        try:
            if replace:
                for index, target in enumerate(targets):
                    if target.exists():
                        backup = staged / ("backup-" + str(index))
                        os.replace(target, backup)
                        backups[target] = backup
            for source, target in zip(sources, targets):
                os.replace(source, target)
                installed.append(target)
        except OSError:
            for target in reversed(installed):
                shutil.rmtree(target)
            for target, backup in backups.items():
                os.replace(backup, target)
            raise
    print("설치 완료:", *(str(path) for path in destinations), "공유 번들:", bundle, sep="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode/Grok Worker 스킬과 공유 번들 설치")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                        help="Codex 홈 디렉터리 (기본값: CODEX_HOME 또는 ~/.codex)")
    parser.add_argument("--replace", action="store_true", help="이전 관리 버전의 두 스킬을 새 번들로 교체")
    args = parser.parse_args()
    try:
        install(args.home, args.replace)
    except (OSError, ValueError) as error:
        print("설치 실패:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
