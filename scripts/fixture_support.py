"""Portable fake CLI; no installed OpenCode, shell wrapper or provider needed."""
import os
from pathlib import Path


def fake_cli(root, body):
    script = Path(root) / 'opencode-fixture.py'
    script.write_text(body, encoding='utf-8')
    return {**os.environ, 'OPENCODE_WORKER_BIN': str(script)}
