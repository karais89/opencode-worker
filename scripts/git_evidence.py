"""Repository-relative paths and before/after Git working-tree evidence."""
import hashlib
import os
from pathlib import Path
import subprocess


def git_snapshot(root):
    """Hash dirty paths against HEAD, preserving preexisting edits separately."""
    commands = (["git", "-C", root, "diff", "HEAD", "--name-only", "-z"],
                ["git", "-C", root, "ls-files", "--others", "--exclude-standard", "-z"])
    names = set()
    for command in commands:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise ValueError("Git change evidence requires a checkout with a HEAD commit.")
        names.update(os.fsdecode(value) for value in result.stdout.split(b'\0') if value)
    snapshot = {}
    for name in names:
        path = Path(root) / name
        if path.is_symlink():
            snapshot[name.replace('\\', '/')] = 'symlink:' + os.readlink(path)
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    digest.update(chunk)
            snapshot[name.replace('\\', '/')] = digest.hexdigest()
        elif path.is_dir():
            snapshot[name.replace('\\', '/')] = 'directory'
        else:
            snapshot[name.replace('\\', '/')] = 'deleted'
    return snapshot


def changed_since(before, after):
    return sorted(name for name in before.keys() | after.keys()
                  if before.get(name) != after.get(name))
