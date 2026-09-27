#!/usr/bin/env python3
"""Install independently versioned Codex skills over a shared runtime bundle."""
import argparse
import installer_v2
import os
from pathlib import Path
import sys


SOURCE = Path(__file__).resolve().parent
VERSION = (SOURCE / "VERSION").read_text(encoding="utf-8").strip()
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


def write_notice(message: str, stream=None) -> None:
    stream = sys.stdout if stream is None else stream
    encoding = stream.encoding or "utf-8"
    stream.write(message.encode(encoding, errors="replace").decode(encoding) + "\n")


def install(home: Path, replace: bool = False, skill: str = "all", dry_run: bool = False,
            migrate_unmanaged: bool = False) -> None:
    write_notice(installer_v2.install(home, SOURCE, VERSION, SHIM, replace, skill, dry_run,
                                      migrate_unmanaged))


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode/Grok Worker 스킬과 공유 번들 설치")
    parser.add_argument("--home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
                        help="Codex 홈 디렉터리 (기본값: CODEX_HOME 또는 ~/.codex)")
    parser.add_argument("--skill", choices=("all", *installer_v2.NAMES), default="all",
                        help="설치할 스킬 (기본값: 둘 다)")
    parser.add_argument("--replace", action="store_true", help="관리 중인 스킬 갱신")
    parser.add_argument("--migrate-unmanaged", action="store_true",
                        help="기존 비관리 스킬을 영구 백업하고 이전")
    parser.add_argument("--dry-run", action="store_true", help="파일 변경 없이 설치 계획 확인")
    args = parser.parse_args()
    try:
        install(args.home, args.replace, args.skill, args.dry_run, args.migrate_unmanaged)
    except (OSError, ValueError) as error:
        write_notice("설치 실패: " + str(error), sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
