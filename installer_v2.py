"""Transactional installation of independent skill entries over one immutable runtime."""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid


NAMES = ("opencode-worker", "grok-worker")
ITEMS = ("scripts", "assets", "references", "LICENSE", "VERSION")


def version_ok(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value))


def content_hash(root):
    digest = hashlib.sha256()
    for item in ITEMS:
        path = root / item
        if path.is_dir():
            files = (p for p in sorted(path.rglob("*")) if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix != ".pyc")
        elif path.is_file():
            files = (path,)
        else:
            raise ValueError("공유 번들 파일이 없습니다: " + str(path))
        for file in files:
            digest.update(file.relative_to(root).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def check_layout(home):
    for path in (home / "skills", home / "worker-bundles", home / "worker-backups"):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("설치 경로가 디렉터리가 아니거나 심볼릭 링크입니다: " + str(path))
    for name in NAMES:
        path = home / "skills" / name
        if path.is_symlink():
            raise ValueError("스킬 경로가 심볼릭 링크입니다: " + str(path))


@contextlib.contextmanager
def install_lock(home):
    """Fail closed when another installer owns this Codex home."""
    path = home / ".worker-install.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("설치 잠금 경로가 일반 파일이 아닙니다: " + str(path))
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("다른 Worker 설치가 진행 중입니다: " + str(home)) from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def managed(path, bundles):
    if not path.is_dir() or not (path / "BUNDLE_VERSION").is_file():
        return False
    version = (path / "BUNDLE_VERSION").read_text(encoding="utf-8").strip()
    if not version_ok(version):
        raise ValueError("기존 스킬의 번들 버전이 올바르지 않습니다: " + str(path))
    bundle = bundles / version
    if (not bundle.is_dir() or bundle.is_symlink() or not (bundle / "VERSION").is_file()
            or (bundle / "VERSION").read_text(encoding="utf-8").strip() != version):
        raise ValueError("기존 공유 번들이 없거나 버전이 다릅니다: " + str(bundle))
    return True


def stage_skill(staged, source, name, version, skill_version, shim):
    origin = source if name == "opencode-worker" else source / "grok-worker"
    target = staged / name
    (target / "agents").mkdir(parents=True)
    (target / "scripts").mkdir()
    body = (origin / "SKILL.md").read_text(encoding="utf-8")
    prefix = "references/" if name == "opencode-worker" else "../references/"
    body = body.replace("](" + prefix, "](../../worker-bundles/" + version + "/references/")
    (target / "SKILL.md").write_text(body, encoding="utf-8")
    shutil.copy2(origin / "agents" / "openai.yaml", target / "agents" / "openai.yaml")
    (target / "BUNDLE_VERSION").write_text(version + "\n", encoding="utf-8")
    (target / "SKILL_VERSION").write_text(skill_version + "\n", encoding="utf-8")
    # The engine is an installed literal, not an overridable environment default.
    engine = "grok" if name == "grok-worker" else "opencode"
    bound_shim = shim.replace("__WORKER_ENGINE__", repr(engine))
    (target / "scripts" / "lite.py").write_text(bound_shim, encoding="utf-8")
    return target


def _install(home, source, version, shim, replace=False, skill="all", dry_run=False,
             migrate_unmanaged=False):
    if not version_ok(version):
        raise ValueError("번들 버전이 올바르지 않습니다.")
    if skill not in (*NAMES, "all"):
        raise ValueError("알 수 없는 스킬: " + skill)
    selected = NAMES if skill == "all" else (skill,)
    versions = json.loads((source / "skill-versions.json").read_text(encoding="utf-8"))
    if (not isinstance(versions, dict) or set(versions) != set(NAMES)
            or not all(version_ok(value) for value in versions.values())):
        raise ValueError("스킬 버전 파일이 올바르지 않습니다.")
    check_layout(home)
    skills, bundles = home / "skills", home / "worker-bundles"
    bundle = bundles / version
    destinations = {name: skills / name for name in selected}
    present = {name: path.exists() for name, path in destinations.items()}
    unmanaged = {name for name, path in destinations.items()
                 if present[name] and not managed(path, bundles)}
    if unmanaged and not migrate_unmanaged:
        raise ValueError("관리 대상이 아닌 스킬입니다. --migrate-unmanaged로 백업 후 이전하세요: " + ", ".join(sorted(unmanaged)))
    if any(present[name] and name not in unmanaged for name in selected) and not replace:
        raise ValueError("관리 중인 스킬을 갱신하려면 --replace를 사용하세요.")
    for name, path in destinations.items():
        if present[name] and name not in unmanaged:
            old_bundle = (path / "BUNDLE_VERSION").read_text(encoding="utf-8").strip()
            old_skill = ((path / "SKILL_VERSION").read_text(encoding="utf-8").strip()
                         if (path / "SKILL_VERSION").is_file() else None)
            if old_bundle == version and old_skill == versions[name]:
                raise ValueError("같은 스킬·번들 버전은 덮어쓰지 않습니다: " + str(path))
    expected = content_hash(source)
    if bundle.exists():
        if (not bundle.is_dir() or bundle.is_symlink() or not (bundle / "VERSION").is_file()
                or (bundle / "VERSION").read_text(encoding="utf-8").strip() != version
                or content_hash(bundle) != expected):
            raise ValueError("같은 버전의 공유 번들 내용이 다릅니다. VERSION을 올리세요: " + str(bundle))
        create_bundle = False
    else:
        create_bundle = True
    plan = "설치 계획 (파일 변경 없음):\n" + "\n".join(
        f"{name}: {'백업 후 이전' if name in unmanaged else '교체' if present[name] else '신규 설치'}"
        for name in selected) + f"\n공유 번들: {bundle} ({'신규' if create_bundle else '재사용'})"
    if dry_run:
        return plan
    home.mkdir(parents=True, exist_ok=True)
    skills.mkdir(parents=True, exist_ok=True)
    bundles.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="worker-install-", dir=home) as staging:
        staged = Path(staging)
        if create_bundle:
            staged_bundle = staged / "bundle"
            staged_bundle.mkdir()
            for item in ITEMS:
                origin, target = source / item, staged_bundle / item
                if origin.is_dir():
                    shutil.copytree(origin, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(origin, target)
            (staged_bundle / "BUNDLE_HASH").write_text(expected + "\n", encoding="utf-8")
        staged_skills = {name: stage_skill(staged, source, name, version, versions[name], shim)
                         for name in selected}
        backups, promoted = {}, []
        bundle_promotion = None
        try:
            if create_bundle:
                bundle_promotion = (staged_bundle, bundle)
                os.replace(staged_bundle, bundle)
            for name, target in destinations.items():
                if present[name]:
                    # Originals must outlive TemporaryDirectory cleanup, even
                    # when an interrupt or filesystem error also breaks rollback.
                    backup_root = home / "worker-backups"
                    backup_root.mkdir(exist_ok=True)
                    backup = backup_root / (name + "-" + uuid.uuid4().hex)
                    # Journal before each rename: a signal can arrive after the
                    # syscall has completed but before Python regains control.
                    backups[target] = backup
                    os.replace(target, backup)
                source_skill = staged_skills[name]
                promoted.append((source_skill, target))
                os.replace(source_skill, target)
        except BaseException as error:
            # Includes KeyboardInterrupt/SystemExit; successful rollback must
            # re-raise the original exception, not report a successful install.
            failures = []
            for source_skill, target in reversed(promoted):
                try:
                    # os.replace removes the staged source only when promotion
                    # succeeded. If it still exists, the destination belongs to
                    # another process and must never be removed by this rollback.
                    if not source_skill.exists() and target.exists():
                        shutil.rmtree(target)
                except OSError as recovery_error:
                    failures.append(str(target) + ": " + str(recovery_error))
            for target, backup in reversed(tuple(backups.items())):
                try:
                    if backup.exists():
                        os.replace(backup, target)
                except OSError as recovery_error:
                    failures.append(str(backup) + ": " + str(recovery_error))
            # A surviving new entry may still reference this bundle. Never
            # remove it after incomplete recovery, nor remove any old bundle.
            if not failures and bundle_promotion is not None:
                try:
                    source_bundle, target_bundle = bundle_promotion
                    if not source_bundle.exists() and target_bundle.exists():
                        shutil.rmtree(target_bundle)
                except OSError as recovery_error:
                    failures.append(str(bundle) + ": " + str(recovery_error))
            if failures:
                saved = [str(path) for path in backups.values() if path.exists()]
                raise OSError("Installation rollback incomplete. Preserve recovery backups: "
                              + (", ".join(saved) or "none remaining")
                              + "; bundle: " + str(bundle)
                              + "; errors: " + "; ".join(failures)) from error
            raise
        # The transaction is committed. Managed backups are now expendable;
        # failed cleanup leaves them available, never triggers a late rollback.
        for target, backup in backups.items():
            if target.name not in unmanaged:
                try:
                    shutil.rmtree(backup)
                except OSError:
                    pass
    saved = [path for path in backups.values() if path.exists()]
    return ("설치 완료:\n" + "\n".join(str(path) for path in destinations.values())
            + "\n공유 번들:\n" + str(bundle)
            + ("\n이전 스킬 백업: " + ", ".join(map(str, saved)) if saved else ""))


def install(home, source, version, shim, replace=False, skill="all", dry_run=False,
            migrate_unmanaged=False):
    home = home.expanduser().resolve()
    if dry_run:
        return _install(home, source, version, shim, replace, skill, True, migrate_unmanaged)
    home.mkdir(parents=True, exist_ok=True)
    with install_lock(home):
        return _install(home, source, version, shim, replace, skill, False, migrate_unmanaged)
