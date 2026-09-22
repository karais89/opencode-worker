#!/usr/bin/env python3
"""One OpenCode session. Routing and evidence are local; no model retry/reviewer."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import math
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

BASE = dict(default=None, writer_default=None, projects={}, variants={}, aliases={},
            mode="auto", project_modes={})
DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "opencode-worker/config.json"


class Failure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def config_path(value=None):
    return Path(value or os.environ.get("OPENCODE_WORKER_CONFIG") or DEFAULT_CONFIG).expanduser().resolve()


def load(path):
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise Failure("invalid_config", "Configuration must be an object.")
    data = {**copy.deepcopy(BASE), **data}
    for field in ("default", "writer_default"):
        if data[field] is not None and (not isinstance(data[field], str) or not data[field].strip()):
            raise Failure("invalid_config", field + " must be a nonempty string or null.")
    for field in ("projects", "variants", "aliases", "project_modes"):
        if not isinstance(data[field], dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                                  or not v.strip() for k, v in data[field].items()):
            raise Failure("invalid_config", field + " must map strings to nonempty strings.")
    if data["mode"] not in ("auto", "manual", "off") or any(
            v not in ("auto", "manual", "off") for v in data["project_modes"].values()):
        raise Failure("invalid_config", "Modes must be auto, manual, or off.")
    return data


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Failure("busy", "Another Worker or configuration update owns this lock.") from error
        yield


def canonical_project(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir() or root in (Path(root.anchor), Path.home().resolve()):
        raise Failure("invalid_project", "Use a specific existing project, not home or filesystem root.")
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
    if probe.returncode == 0 and probe.stdout.strip():
        root = Path(probe.stdout.strip()).resolve()
    if root in (Path(root.anchor), Path.home().resolve()):
        raise Failure("invalid_project", "Git root must be a specific project.")
    return str(root)


def route_id(route, data):
    if not isinstance(route, str):
        raise Failure("invalid_route", "Expected provider/model or a saved alias.")
    route = data.get("aliases", {}).get(route, route)
    if not isinstance(route, str) or not re.fullmatch(r"[^\s/]+/[^\s]+", route):
        raise Failure("invalid_route", "Expected provider/model or a saved alias.")
    return route


def resolve_route(data, root, model=None, variant=None):
    choices = (("one-shot", model), ("project", data["projects"].get(root)),
               ("global", data.get("writer_default") or data.get("default")))
    source, selected = next(((s, r) for s, r in choices if r), ("setup", None))
    if selected is None:
        return source, None, None
    selected = route_id(selected, data)
    chosen_variant = variant if variant is not None else data["variants"].get(selected)
    if chosen_variant is not None and (not isinstance(chosen_variant, str) or not chosen_variant.strip()):
        raise Failure("invalid_variant", "Variant must be a nonempty string.")
    return source, selected, chosen_variant


def mode_for(data, root):
    # A global off switch must not be undone by an older per-project auto setting.
    return "off" if data["mode"] == "off" else data["project_modes"].get(root, data["mode"])


def lock_path(root, cfg_path=None):
    # Same namespace as Full, independent of --config. Do not unlink live lock files.
    digest = hashlib.sha256(os.fsencode(canonical_project(root))).hexdigest()
    return DEFAULT_CONFIG.parent / "locks" / (digest + ".lock")


def checkout_lock(root, cfg_path=None):
    return file_lock(lock_path(root))


def worker_env(read_only=False, skill_dirs=()):
    env = os.environ.copy()
    extra = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    if not isinstance(extra, dict) or ("agent" in extra and not isinstance(extra["agent"], dict)):
        raise Failure("invalid_environment", "OpenCode process configuration must be an object.")
    profile = json.loads((Path(__file__).resolve().parent.parent / "assets/worker-agent.json").read_text())
    profile["prompt"] = (
        "Perform only the supplied task in its repository; follow relevant AGENTS.md and Skills. "
        "Use authorized tools and finish relevant validation in this session. Preserve unrelated edits. "
        "Do not search for another project, access secrets, modify global settings, push/deploy/publish, "
        "commit without task intent, launch subagents, or bypass denials via other tools. "
        "Submit actual changes, checks and risk using codex_worker_submit_result after final validation. "
        "If submission is rejected, correct only the report in this same session."
    )
    permission = profile["permission"]
    permission["*"] = "allow"
    if read_only:
        permission = {"*": "deny", "read": permission["read"], "glob": "allow", "grep": "allow",
                      "list": "allow", "external_directory": "deny", "edit": "deny",
                      "bash": "deny", "task": "deny", "doom_loop": "deny"}
        profile["permission"] = permission
    if skill_dirs:
        permission["external_directory"] = {"*": "deny"}
        if not read_only:
            permission["edit"] = dict(permission["edit"])
        for directory in skill_dirs:
            for pattern in (directory, directory + "/*"):
                permission["external_directory"][pattern] = "allow"
                permission["read"][pattern] = "allow"
                if not read_only:
                    permission["edit"][pattern] = "deny"
        # Last matching rule wins. External originals must not expose secrets.
        for pattern in ("*.env", "*.env.*"):
            permission["read"].pop(pattern, None)
            permission["read"][pattern] = "deny"
    extra.setdefault("agent", {})["codex-worker"] = profile
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(extra)
    try:
        submission.configure(env)
    except ValueError as error:
        raise Failure("submission_setup_failed", str(error)) from error
    return env


def read_brief(path):
    raw = Path(path).expanduser().resolve().read_bytes()
    if not raw.strip() or len(raw) > 8192:
        raise Failure("invalid_brief", "Supply a nonempty UTF-8 brief at or below 8 KiB.")
    return raw.decode("utf-8").strip()


def make_prompt(root, brief, read_only=False, skill_dirs=()):
    mode = ("Read-only: native read/glob/grep/list only; no shell, tests, edits or MCP. Submit changed=[]."
            if read_only else "Explore, implement, debug and validate the requested work in this session.")
    context = "\nKnown read-only Skill originals: " + ", ".join(skill_dirs) if skill_dirs else ""
    return (f"PROJECT\n{root}\n\n{brief}\n\n{mode}{context}\n"
            "Do not expand scope or add workers. Submit the final result after the last tool call. "
            "Declare exact final local validation_commands when available, [] for remote checks. "
            "Do not rerun checks solely to satisfy evidence formatting; report uncertainty honestly.")


def result_from_summary(rc, summary, model, variant, source, elapsed, read_only=False):
    public = summary.public()
    capture = public.get("result_submission", {})
    report = capture.get("report")
    evidence = verification.assess(report or {}, public)
    normal_stop = (summary.last_finish or {}).get("part", {}).get("reason") == "stop"
    problem = None
    if rc != 0 or summary.errors or summary.denied or public.get("malformed_output") or not normal_stop:
        problem = "Execution failed, was denied, or has no verified normal stop. Do not replay automatically."
    elif not public.get("session_id") or capture.get("error") or not report:
        problem = "A valid session-bound structured result is unavailable. Do not infer completion from prose."
    elif read_only and (report["changed"] or any(t not in ("read", "glob", "grep", "list", submission.TOOL)
                                              for t in public.get("tools", {}))):
        problem = "Read-only report or observed tools violate the requested scope."
    elif evidence["status"] == "failed":
        problem = "A declared final validation command was observed failing."
    return {"status": "needs_escalation" if problem else report["status"],
            "worker_used": bool(public.get("session_id")), "worker_process_started": True,
            "session_id": public.get("session_id"), "model": model, "variant": variant,
            "model_source": "selected_cli_arguments", "observed_model": None,
            "observed_variant": None, "route_source": source, "read_only": read_only,
            "elapsed_seconds": round(elapsed, 2), "result": report, "validation_evidence": evidence,
            "worker_tokens": public.get("worker_tokens"), "tools": public.get("tools", {}),
            **({"message": problem} if problem else {})}


def run_worker(args, data, root, cfg_path):
    mode = mode_for(data, root)
    if mode == "off" or (mode == "manual" and not args.explicit):
        return dict(status="mode_blocked", worker_used=False, mode=mode)
    source, model, variant = resolve_route(data, root, args.model, args.variant)
    if model is None:
        return dict(status="setup_required", worker_used=False, message="Select a Worker route first.")
    if not math.isfinite(args.inactivity_timeout) or args.inactivity_timeout <= 0:
        raise Failure("invalid_timeout", "Inactivity timeout must be finite and positive.")
    if args.hard_timeout is not None and (not math.isfinite(args.hard_timeout) or args.hard_timeout <= 0):
        raise Failure("invalid_timeout", "Hard timeout must be finite and positive when set.")
    brief = read_brief(args.brief)
    ignored = {".git", ".DS_Store", "brief.txt", "task.txt", "work", "outputs", Path(args.brief).name}
    if not any(p.name not in ignored for p in Path(root).iterdir()):
        raise Failure("invalid_project", "Empty/scratch-only target; Head must identify the actual project.")
    skill_dirs = []
    for raw in getattr(args, "skill_dir", []):
        p = Path(raw).expanduser().resolve()
        if not p.is_dir() or not (p / "SKILL.md").is_file() or p in (Path.home().resolve(), Path(p.anchor)):
            raise Failure("invalid_skill_dir", "Supply an explicitly authorized, known Skill directory.")
        skill_dirs.append(str(p))
    readonly = getattr(args, "read_only", False)
    with checkout_lock(root, cfg_path):
        env = worker_env(readonly, skill_dirs)
        cmd = ["run", "--format", "json", "--agent", "codex-worker", "--dir", root, "-m", model]
        if variant:
            cmd += ["--variant", variant]
        started = time.monotonic()
        try:
            rc, summary, _, _ = streaming.run(cmd, root, args.hard_timeout,
                make_prompt(root, brief, readonly, skill_dirs), env, lambda _: False, progress_heartbeat=60,
                inactivity_timeout=args.inactivity_timeout)
        except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
            summary = getattr(error, "worker_summary", None)
            kind = getattr(error, "timeout_kind", None)
            result = result_from_summary(-1, summary, model, variant, source,
                                         time.monotonic() - started, readonly) if summary else {}
            if kind == "inactivity":
                reason = (f"Worker interrupted after {args.inactivity_timeout:g}s with no OpenCode JSON/event activity "
                          "(inactivity timeout).")
            elif kind == "hard":
                reason = f"Worker interrupted by the optional hard overall limit of {args.hard_timeout:g}s."
            else:
                reason = "Worker interrupted before normal completion."
            result.update(status="needs_escalation",
                          worker_process_started=getattr(error, "worker_launched", False),
                          timeout_kind=kind,
                          message=reason + " Preserve partial changes; child cleanup is not guaranteed by this host. "
                                           "This is not a provider error. No automatic retry.")
            result.setdefault("worker_used", False)
            return result
        return result_from_summary(rc, summary, model, variant, source, time.monotonic() - started, readonly)


def model_inventory(root, provider=None):
    cmd = ["opencode", "models"] + ([provider, "--verbose"] if provider else [])
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise Failure("models_failed", "OpenCode inventory unavailable; existing configuration was not changed.")
    lines = proc.stdout.splitlines(keepends=True)
    found = {}
    for i, line in enumerate(lines):
        route = line.strip()
        if re.fullmatch(r"[^\s/]+/[^\s]+", route):
            found[route] = []
            if provider:
                info, _ = json.JSONDecoder().raw_decode("".join(lines[i + 1:]).lstrip())
                variants = info.get("variants", {}) if isinstance(info, dict) else None
                if not isinstance(variants, dict) or any(not isinstance(v, dict) for v in variants.values()):
                    raise Failure("invalid_inventory", "OpenCode variant metadata is malformed.")
                found[route] = [v for v, options in variants.items() if not options.get("disabled", False)]
    return found


def update_config(args, path, root):
    # Share Full's config lock and atomic write convention; preserve unknown fields.
    with file_lock(path.with_suffix(".lock")):
        data = load(path)
        if args.action == "set-mode":
            if args.project_only:
                if args.mode == "inherit":
                    data["project_modes"].pop(root, None)
                else:
                    data["project_modes"][root] = args.mode
            elif args.mode == "inherit":
                raise Failure("invalid_mode", "inherit requires --project-only.")
            else:
                data["mode"] = args.mode
        else:
            clearing = args.model == "null"
            if clearing and (args.variant is not None or args.clear_variant):
                raise Failure("invalid_variant", "Do not set a variant while clearing a route.")
            selected = None if clearing else route_id(args.model, data)
            if selected:
                inventory = model_inventory(root, selected.split("/")[0] if args.variant else None)
                if selected not in inventory:
                    raise Failure("unknown_model", "Model is not in the current OpenCode inventory.")
                if args.variant is not None and args.variant not in inventory[selected]:
                    raise Failure("unknown_variant", "Variant is not supported by the selected model.")
                if args.clear_variant:
                    data["variants"].pop(selected, None)
                elif args.variant is not None:
                    data["variants"][selected] = args.variant
            if args.action == "set-project":
                if clearing:
                    data["projects"].pop(root, None)
                else:
                    data["projects"][root] = selected
            else:
                data["writer_default"] = selected
                if clearing:
                    data["default"] = None
        save(path, data)
    return dict(ok=True, config_path=str(path), project=root,
                route_source=resolve_route(data, root)[0], model=resolve_route(data, root)[1], mode=mode_for(data, root))


def parser():
    p = argparse.ArgumentParser(description="OpenCode Worker Lite v2")
    p.add_argument("--project")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("models").add_argument("provider", nargs="?")
    sub.add_parser("resolve")
    for name in ("set-default", "set-project"):
        setter = sub.add_parser(name); setter.add_argument("model")
        choice = setter.add_mutually_exclusive_group()
        choice.add_argument("--variant"); choice.add_argument("--clear-variant", action="store_true")
    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=("auto", "manual", "off", "inherit"))
    mode.add_argument("--project-only", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--brief", required=True)
    run.add_argument("--model"); run.add_argument("--variant")
    run.add_argument("--explicit", action="store_true")
    run.add_argument("--read-only", action="store_true")
    run.add_argument("--skill-dir", action="append", default=[])
    # Production timeout policy: interrupt only after sustained event silence.
    # The old total-time limit is preserved as an explicit optional hard limit,
    # but disabled by default so long active sessions are not killed.
    run.add_argument("--inactivity-timeout", type=float, default=300,
                     help="Interrupt after this many seconds with no OpenCode JSON/event activity (default 300).")
    run.add_argument("--hard-timeout", "--timeout", dest="hard_timeout", type=float, default=None,
                     help="Optional overall wall-clock hard limit in seconds; disabled by default. "
                          "The legacy --timeout total limit is this same explicit hard limit.")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if not args.project and (args.action in ("run", "resolve", "set-project") or getattr(args, "project_only", False)):
            raise Failure("project_required", "Head must supply --project explicitly.")
        path = config_path(args.config)
        root = canonical_project(args.project or ".")
        if args.action == "models":
            emit({"models": model_inventory(root, args.provider)}); return 0
        if args.action.startswith("set-"):
            emit(update_config(args, path, root)); return 0
        data = load(path)
        if args.action == "resolve":
            source, model, variant = resolve_route(data, root)
            emit(dict(project=root, mode=mode_for(data, root), route_source=source, model=model, variant=variant)); return 0
        result = run_worker(args, data, root, path)
        emit(result)
        return 0 if result["status"] == "completed" else 2
    except (Failure, ValueError, OSError, subprocess.SubprocessError) as error:
        emit(dict(status="needs_escalation", worker_used=False,
                  error_code=getattr(error, "code", type(error).__name__), message=str(error)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
