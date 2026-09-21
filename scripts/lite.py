#!/usr/bin/env python3
"""Lite v2: one OpenCode Worker run with deterministic routing and compact evidence."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import streaming
import submission
import verification

BASE = {
    "default": None,
    "writer_default": None,
    "projects": {},
    "variants": {},
    "aliases": {},
    "mode": "auto",
    "project_modes": {},
}
DEFAULT_CONFIG = (
    Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    / "opencode-worker"
    / "config.json"
)


class Failure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def config_path(value=None):
    return Path(value or os.environ.get("OPENCODE_WORKER_CONFIG") or DEFAULT_CONFIG).expanduser().resolve()


def load(path):
    if not path.exists():
        return copy.deepcopy(BASE)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise Failure("invalid_config", "Configuration must be an object.")
    merged = {**copy.deepcopy(BASE), **data}
    for key in ("projects", "variants", "aliases", "project_modes"):
        if not isinstance(merged.get(key), dict):
            raise Failure("invalid_config", f"{key} must be an object.")
    if merged.get("mode") not in ("auto", "manual", "off"):
        raise Failure("invalid_config", "mode must be auto, manual, or off.")
    if any(v not in ("auto", "manual", "off") for v in merged["project_modes"].values()):
        raise Failure("invalid_config", "project mode must be auto, manual, or off.")
    return merged


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def canonical_project(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise Failure("invalid_project", "Project directory does not exist.")
    probe = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0 and probe.stdout.strip():
        return str(Path(probe.stdout.strip()).resolve())
    return str(root)


def route_id(route, data):
    route = data.get("aliases", {}).get(route, route)
    if not isinstance(route, str) or not re.fullmatch(r"[^\s/]+/[^\s]+", route):
        raise Failure("invalid_route", "Expected provider/model or a saved alias.")
    return route


def resolve_route(data, root, model=None, variant=None):
    candidates = (
        ("one-shot", model),
        ("project", data.get("projects", {}).get(root)),
        ("global", data.get("writer_default") or data.get("default")),
    )
    source, selected = next(((s, r) for s, r in candidates if r), ("setup", None))
    if not selected:
        return source, None, None
    selected = route_id(selected, data)
    selected_variant = variant if variant is not None else data.get("variants", {}).get(selected)
    if selected_variant is not None and not isinstance(selected_variant, str):
        raise Failure("invalid_config", "variant must be a string.")
    return source, selected, selected_variant


def mode_for(data, root):
    return data.get("project_modes", {}).get(root, data.get("mode", "auto"))


def lock_path(root, cfg_path):
    digest = hashlib.sha256(os.fsencode(root)).hexdigest()
    return cfg_path.parent / "locks" / f"{digest}.lock"


@contextlib.contextmanager
def checkout_lock(root, cfg_path):
    path = lock_path(root, cfg_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Failure("busy", "Another Worker is active in this project directory.") from error
        yield


def worker_env():
    env = os.environ.copy()
    extra = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    if not isinstance(extra, dict) or (
        "agent" in extra and not isinstance(extra["agent"], dict)
    ):
        raise Failure("invalid_environment", "OPENCODE_CONFIG_CONTENT must be an object.")

    full_profile = json.loads(
        (Path(__file__).resolve().parent.parent / "assets" / "worker-agent.json").read_text()
    )
    profile = {
        "description": "Single scoped OpenCode execution Worker controlled by Codex Head",
        "mode": "primary",
        "prompt": (
            "Work only on the supplied repository task. Follow repository AGENTS.md when present. "
            "Explore, implement, test, debug, and fix within this one session. Do not expand scope, "
            "search for another project, access secrets, change global/system settings, push, deploy, "
            "publish, or launch another Codex/OpenCode worker. Use allowed project tools as needed. "
            "After final validation call codex_worker_submit_result once with the actual changes, "
            "checks, remaining risk, and exact final local validation commands when applicable."
        ),
        "permission": copy.deepcopy(full_profile["permission"]),
    }
    # Keep normal project MCP/custom tools available; explicit deny entries remain in the profile.
    profile["permission"]["*"] = "allow"
    extra.setdefault("agent", {})["codex-worker"] = profile
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(extra)
    try:
        submission.configure(env)
    except ValueError as error:
        raise Failure("submission_setup_failed", str(error)) from error
    return env


def read_brief(path):
    data = Path(path).expanduser().resolve().read_text()
    if not data.strip():
        raise Failure("empty_brief", "Brief must not be empty.")
    if len(data.encode("utf-8")) > 8192:
        raise Failure("brief_too_large", "Keep the brief at or below 8 KiB.")
    return data.strip()


def make_prompt(root, brief):
    return (
        f"PROJECT\n{root}\n\n{brief}\n\n"
        "WORKER RULES\n"
        "- Work only in the PROJECT above; do not search parent/home directories for another project.\n"
        "- Perform exploration, implementation, tests, debugging, and fixes in this same session.\n"
        "- Do not add reviewers, planners, routers, or nested workers.\n"
        "- Run relevant final validation after the final changes.\n"
        "- Finish by calling codex_worker_submit_result after the last development/validation tool call.\n"
        "- Include validation_commands with exact direct local shell validation commands; use [] for remote/MCP-only checks.\n"
        "- Use completed only when the requested work and validation are done; otherwise use needs_escalation.\n"
    )


def result_from_summary(rc, summary, model, variant, source, elapsed):
    public = summary.public()
    captured = public.get("result_submission", {})
    report = captured.get("report")
    submit_error = captured.get("error")
    evidence = (
        verification.assess(report, public)
        if isinstance(report, dict)
        else {
            "status": "unverified",
            "scope": "declared_local_command_exits_only",
            "passed": 0,
            "failed": 0,
            "unverified": 0,
        }
    )
    status = "needs_escalation"
    message = None
    if rc != 0:
        message = f"OpenCode exited with code {rc}."
    elif public.get("malformed_output"):
        message = "OpenCode JSONL output was malformed or inconsistent."
    elif submit_error or not isinstance(report, dict):
        message = f"Structured result unavailable: {submit_error or 'missing_submission'}."
    else:
        status = report["status"]
        if evidence.get("status") == "failed" and status == "completed":
            status = "needs_escalation"
            message = "A declared final validation command was observed failing."

    return {
        "status": status,
        "worker_used": bool(public.get("session_id")),
        "model": model,
        "variant": variant,
        "route_source": source,
        "elapsed_seconds": round(elapsed, 2),
        "result": report,
        "validation_evidence": evidence,
        "worker_tokens": public.get("worker_tokens"),
        "tools": public.get("tools", {}),
        **({"message": message} if message else {}),
    }


def run_worker(args, data, root, cfg_path):
    mode = mode_for(data, root)
    if mode == "off" or (mode == "manual" and not args.explicit):
        return {
            "status": "mode_blocked",
            "worker_used": False,
            "mode": mode,
            "message": "Enable Worker mode or explicitly invoke it in manual mode.",
        }

    source, model, variant = resolve_route(data, root, args.model, args.variant)
    if not model:
        return {
            "status": "setup_required",
            "worker_used": False,
            "route_source": source,
            "message": "No Worker model is configured. Set a default or project route.",
        }

    brief = read_brief(args.brief)
    prompt = make_prompt(root, brief)
    env = worker_env()
    command = ["run", "--format", "json", "--agent", "codex-worker", "--dir", root, "-m", model]
    if variant:
        command += ["--variant", variant]

    started = time.monotonic()
    try:
        with checkout_lock(root, cfg_path):
            rc, summary, _stderr, _log = streaming.run(
                command,
                root,
                args.timeout,
                prompt,
                env,
                lambda _events: False,
                progress_heartbeat=60,
            )
    except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
        elapsed = time.monotonic() - started
        summary = getattr(error, "worker_summary", None)
        result = {
            "status": "needs_escalation",
            "worker_used": bool(summary and summary.public().get("session_id")),
            "model": model,
            "variant": variant,
            "route_source": source,
            "elapsed_seconds": round(elapsed, 2),
            "message": f"Worker execution interrupted: {type(error).__name__}. No automatic retry.",
        }
        if summary:
            public = summary.public()
            result["worker_tokens"] = public.get("worker_tokens")
            result["tools"] = public.get("tools", {})
        return result

    return result_from_summary(
        rc, summary, model, variant, source, time.monotonic() - started
    )


def list_models(root):
    proc = subprocess.run(
        ["opencode", "models"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode:
        raise Failure("models_failed", f"opencode models exited with code {proc.returncode}.")
    models = sorted(
        set(
            line.strip()
            for line in proc.stdout.splitlines()
            if re.fullmatch(r"[^\s/]+/[^\s]+", line.strip())
        )
    )
    return {"models": models}


def parser():
    p = argparse.ArgumentParser(description="OpenCode Worker Lite v2")
    p.add_argument("--project", default=".")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("models")
    sub.add_parser("resolve")

    default = sub.add_parser("set-default")
    default.add_argument("model")
    default.add_argument("--variant")

    project = sub.add_parser("set-project")
    project.add_argument("model")
    project.add_argument("--variant")

    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=("auto", "manual", "off"))
    mode.add_argument("--project-only", action="store_true")

    run = sub.add_parser("run")
    run.add_argument("--brief", required=True)
    run.add_argument("--model")
    run.add_argument("--variant")
    run.add_argument("--explicit", action="store_true")
    run.add_argument("--timeout", type=float, default=1800)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    cfg_path = config_path(args.config)
    try:
        data = load(cfg_path)
        root = canonical_project(args.project)

        if args.action == "models":
            emit(list_models(root))
            return 0

        if args.action == "resolve":
            source, model, variant = resolve_route(data, root)
            emit(
                {
                    "project": root,
                    "mode": mode_for(data, root),
                    "route_source": source,
                    "model": model,
                    "variant": variant,
                }
            )
            return 0

        if args.action in ("set-default", "set-project"):
            selected = route_id(args.model, data)
            if args.action == "set-default":
                data["writer_default"] = selected
            else:
                data["projects"][root] = selected
            if args.variant is not None:
                data["variants"][selected] = args.variant
            save(cfg_path, data)
            emit(
                {
                    "ok": True,
                    "scope": "global" if args.action == "set-default" else "project",
                    "project": None if args.action == "set-default" else root,
                    "model": selected,
                    "variant": data["variants"].get(selected),
                }
            )
            return 0

        if args.action == "set-mode":
            if args.project_only:
                data["project_modes"][root] = args.mode
            else:
                data["mode"] = args.mode
            save(cfg_path, data)
            emit(
                {
                    "ok": True,
                    "scope": "project" if args.project_only else "global",
                    "mode": args.mode,
                    "project": root if args.project_only else None,
                }
            )
            return 0

        result = run_worker(args, data, root, cfg_path)
        emit(result)
        return 0 if result["status"] == "completed" else 2
    except (Failure, json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        emit(
            {
                "status": "needs_escalation",
                "worker_used": False,
                "error_code": getattr(error, "code", type(error).__name__),
                "message": str(error),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
