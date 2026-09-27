"""Repository-relative paths and before/after Git working-tree evidence."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess


def _git(root, *args):
    result = subprocess.run(["git", "-C", root, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise ValueError("Git change evidence requires a readable checkout with a HEAD commit.")
    return result.stdout


def _dirty_entries(root):
    # Raw modes respect core.filemode and distinguish gitlinks from ordinary
    # directories. Disabling renames gives exactly one NUL-delimited path per
    # record, and --ignore-submodules=none overrides repository/user hiding rules.
    fields = _git(root, "diff", "HEAD", "--raw", "-z", "--no-abbrev", "--no-renames",
                  "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", "--").split(b'\0')
    if fields[-1] == b'':
        fields.pop()
    if len(fields) % 2:
        raise ValueError("Malformed Git change evidence.")
    entries = {}
    for header, name in zip(fields[::2], fields[1::2]):
        parts = header.split()
        if len(parts) != 5 or not parts[0].startswith(b':') or not name:
            raise ValueError("Malformed Git change evidence.")
        entries[os.fsdecode(name)] = (parts[1].decode('ascii'), parts[3].decode('ascii'))
    return entries


def _filemode_enabled(root):
    result = subprocess.run(["git", "-C", root, "config", "--bool", "--get", "core.filemode"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode not in (0, 1):
        raise ValueError("Cannot read Git filemode configuration.")
    return result.returncode == 1 or result.stdout.strip() == b'true'


def git_snapshot(root, *, _depth=0):
    """Fingerprint dirty paths against HEAD, including Git modes and submodules.

    Only the Git executable bit is relevant, not arbitrary filesystem permission
    bits. Submodules include their checked-out commit and recursively fingerprinted
    dirty files; a stable 'dirty' flag alone would lose subsequent modifications.
    """
    if _depth > 32:
        raise ValueError("Submodule nesting exceeds the Git evidence limit.")
    entries = _dirty_entries(root)
    untracked = {os.fsdecode(value) for value in
                 _git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b'\0') if value}
    filemode = _filemode_enabled(root) if untracked else False
    snapshot = {}
    for name in entries.keys() | untracked:
        path = Path(root) / name
        mode, oid = entries.get(name, (None, None))
        if path.is_symlink():
            value = 'symlink:' + os.readlink(path)
        elif path.is_file():
            if mode is None:
                mode = '100755' if filemode and path.stat().st_mode & stat.S_IXUSR else '100644'
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            value = 'file:' + mode + ':' + digest.hexdigest()
        elif path.is_dir() and mode == '160000':
            if not (path / '.git').exists():
                # A deinitialized submodule is not the parent checkout: git -C
                # would otherwise silently walk up and fingerprint the parent.
                value = 'gitlink:uninitialized:' + oid
            else:
                commit = _git(str(path), "rev-parse", "--verify", "HEAD").strip().decode('ascii')
                dirty = git_snapshot(str(path), _depth=_depth + 1)
                digest = hashlib.sha256(json.dumps(dirty, sort_keys=True).encode('utf-8')).hexdigest()
                value = 'gitlink:' + commit + ':' + digest
        elif path.is_dir():
            value = 'directory'
        else:
            value = 'deleted'
        snapshot[name.replace('\\', '/')] = value
    return snapshot


def changed_since(before, after):
    return sorted(name for name in before.keys() | after.keys()
                  if before.get(name) != after.get(name))
