#!/usr/bin/env python3
"""One coding Worker session. Routing and evidence are local; no retry/reviewer."""
import argparse
import contextlib
import copy
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

import platform_support
import grok_adapter
import streaming
import submission
import verification

BASE = dict(default=None, writer_default=None, projects={}, variants={}, aliases={},
            mode="auto", project_modes={}, grok={"default": None, "projects": {}})
DEFAULT_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "opencode-worker/config.json"


class Failure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def emit(value):
    # ASCII JSON remains valid UTF-8 on redirected legacy Windows code pages.
    print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))


def config_path(value=None):
    return Path(value or os.environ.get("OPENCODE_WORKER_CONFIG") or DEFAULT_CONFIG).expanduser().resolve()


def load(path):
    data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
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
    grok = data["grok"]
    if not isinstance(grok, dict):
        raise Failure("invalid_config", "grok must be an object.")
    grok = {"default": None, "projects": {}, **grok}
    if grok["default"] is not None and (not isinstance(grok["default"], str) or not grok["default"].strip()):
        raise Failure("invalid_config", "grok.default must be a nonempty string or null.")
    if not isinstance(grok["projects"], dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not v.strip()
            for k, v in grok["projects"].items()):
        raise Failure("invalid_config", "grok.projects must map strings to nonempty strings.")
    data["grok"] = grok
    if data["mode"] not in ("auto", "manual", "off") or any(
            v not in ("auto", "manual", "off") for v in data["project_modes"].values()):
        raise Failure("invalid_config", "Modes must be auto, manual, or off.")
    return data


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            platform_support.lock_file(handle)
        except BlockingIOError as error:
            raise Failure("busy", "Another Worker or configuration update owns this lock.") from error
        try:
            yield
        finally:
            platform_support.unlock_file(handle)


def canonical_project(value):
    root = Path(value).expanduser().resolve()
    if not root.is_dir() or root in (Path(root.anchor), Path.home().resolve()):
        raise Failure("invalid_project", "Use a specific existing project, not home or filesystem root.")
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, encoding="utf-8", timeout=10)
    if probe.returncode == 0 and probe.stdout.strip():
        root = Path(probe.stdout.strip()).resolve()
    if root in (Path(root.anchor), Path.home().resolve()):
        raise Failure("invalid_project", "Git root must be a specific project.")
    return os.path.normcase(str(root))


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


def resolve_grok_route(data, root, model=None):
    choices = (("one-shot", model), ("project", data["grok"]["projects"].get(root)),
               ("global", data["grok"]["default"]))
    source, selected = next(((source, value) for source, value in choices if value), ("grok-cli-default", None))
    if selected is not None and (not isinstance(selected, str) or not selected.strip()):
        raise Failure("invalid_route", "Grok model must be a nonempty model ID.")
    return source, selected


def mode_for(data, root):
    # A global off switch must not be undone by an older per-project auto setting.
    return "off" if data["mode"] == "off" else data["project_modes"].get(root, data["mode"])


def lock_path(root, cfg_path=None):
    # Use one namespace independent of --config. Do not unlink live lock files.
    digest = hashlib.sha256(os.fsencode(canonical_project(root))).hexdigest()
    return DEFAULT_CONFIG.parent / "locks" / (digest + ".lock")


def checkout_lock(root, cfg_path=None):
    return file_lock(lock_path(root))


def worker_env(read_only=False, skill_dirs=()):
    env = os.environ.copy()
    extra = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    if not isinstance(extra, dict) or ("agent" in extra and not isinstance(extra["agent"], dict)):
        raise Failure("invalid_environment", "OpenCode process configuration must be an object.")
    profile = json.loads((Path(__file__).resolve().parent.parent / "assets/worker-agent.json").read_text(encoding="utf-8"))
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
            # OpenCode uses forward-slash patterns even on Windows. Retain the
            # native spelling too for compatible older CLI versions.
            forms = {directory, directory.replace("\\", "/")} if os.name == "nt" else {directory}
            for pattern in sorted({p for form in forms for p in (form, form + "/*")}):
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
    with Path(path).expanduser().resolve().open("rb") as source:
        raw = source.read(8193)
    if not raw.strip() or len(raw) > 8192:
        raise Failure("invalid_brief", "Supply a nonempty UTF-8 brief at or below 8 KiB.")
    text = raw.decode("utf-8-sig").strip()
    if not text:
        raise Failure("invalid_brief", "Supply a nonempty UTF-8 brief at or below 8 KiB.")
    return text


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
    if (rc != 0 or summary.errors or summary.denied or public.get("malformed_output")
            or public.get("pending_tools") or not normal_stop):
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
            "session_id": public.get("session_id"), "engine": "opencode",
            "model": model, "variant": variant,
            "model_source": "selected_cli_arguments", "observed_model": None,
            "observed_variant": None, "route_source": source, "read_only": read_only,
            "elapsed_seconds": round(elapsed, 2), "result": report, "validation_evidence": evidence,
            "worker_tokens": public.get("worker_tokens"), "tools": public.get("tools", {}),
            **({"message": problem} if problem else {})}


def make_grok_prompt(root, brief):
    return (f"PROJECT\n{root}\n\n{brief}\n\n"
            "Work only in this repository. Follow its AGENTS.md and relevant project instructions. "
            "Preserve preexisting edits. Explore, implement, and validate the requested task in this one session. "
            "Do not access secrets, change global settings, push, deploy, publish, commit, launch other agents, "
            "or bypass a denied tool. Finish validation before the final answer. "
            "The final answer must be one JSON object only, with status (completed or needs_escalation), "
            "changed (array of exact repository-relative file paths changed by this task), "
            "validation (nonempty array of actual result or blocker strings), risk (nonempty string), "
            "and optional validation_commands (array of exact final local commands, maximum 10). "
            "No Markdown or extra prose. Report uncertainty honestly; do not rerun a check "
            "solely to improve reporting format.")


def grok_result_from_summary(rc, summary, model, source, elapsed, before, after):
    public = summary.public()
    changed = grok_adapter.changed_since(before, after)
    report = None
    problem = None
    try:
        report = submission.validate(json.loads(summary.report()))
    except (ValueError, TypeError, json.JSONDecodeError):
        problem = "Grok did not produce a valid structured result. Do not infer completion from prose."
    evidence = verification.assess(report or {}, public)
    if (rc != 0 or public["malformed_output"] or public["errors"] or public["tool_errors"]
            or public["pending_tools"] or public["stop_reason"] != "end_turn" or not public["session_id"]):
        problem = "Grok failed, was denied, or has no verified normal end event. Do not replay automatically."
    elif report is not None and sorted(report["changed"]) != changed:
        problem = "Reported changed paths differ from the observed Git working-tree delta."
    elif evidence["status"] == "failed":
        problem = "A declared final validation command was observed failing."
    models = public.get("model_usage")
    observed_model = next(iter(models)) if isinstance(models, dict) and len(models) == 1 else None
    return {"status": "needs_escalation" if problem else report["status"],
            "worker_used": bool(public["session_id"]), "worker_process_started": True,
            "session_id": public["session_id"], "engine": "grok", "model": model,
            "variant": None,
            "model_source": "selected_cli_arguments" if model else "grok_cli_default",
            "observed_model": observed_model, "observed_variant": None,
            "route_source": source, "read_only": False, "elapsed_seconds": round(elapsed, 2),
            "result": report, "observed_changed": changed,
            "preexisting_changes": sorted(before),
            "validation_evidence": evidence,
            "worker_tokens": public["worker_tokens"], "worker_cost_usd": public["worker_cost_usd"],
            "tools": {"total": public["tool_calls"], "errors": public["tool_errors"]},
            **({"message": problem} if problem else {})}


def run_grok_worker(args, data, root, cfg_path, brief):
    if args.variant is not None:
        raise Failure("unsupported_option", "Grok uses model IDs; --variant is OpenCode-only.")
    if getattr(args, "read_only", False) or getattr(args, "skill_dir", []):
        raise Failure("unsupported_option", "Grok read-only and --skill-dir are not yet supported safely.")
    source, model = resolve_grok_route(data, root, args.model)
    with checkout_lock(root, cfg_path):
        before = grok_adapter.git_snapshot(root)
        started = time.monotonic()
        try:
            rc, summary = grok_adapter.run(root, make_grok_prompt(root, brief), model,
                hard_timeout=args.hard_timeout, inactivity_timeout=args.inactivity_timeout)
        except (TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
            summary = getattr(error, "worker_summary", grok_adapter.Summary())
            after = grok_adapter.git_snapshot(root)
            partial = grok_result_from_summary(-1, summary, model, source,
                time.monotonic() - started, before, after)
            partial.update(status="needs_escalation", timeout_kind=getattr(error, "timeout_kind", None),
                worker_process_started=getattr(error, "worker_launched", False),
                message="Grok Worker interrupted. Preserve partial changes and check child cleanup before any new run.")
            return partial
        after = grok_adapter.git_snapshot(root)
        return grok_result_from_summary(rc, summary, model, source,
                                        time.monotonic() - started, before, after)


def run_worker(args, data, root, cfg_path):
    mode = mode_for(data, root)
    if mode == "off" or (mode == "manual" and not args.explicit):
        return dict(status="mode_blocked", worker_used=False, mode=mode)
    engine = getattr(args, "engine", "opencode")
    if engine == "opencode":
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
        if (not p.is_dir() or not (p / "SKILL.md").is_file()
                or p in (Path.home().resolve(), Path(p.anchor))
                or any(char in str(p) for char in "*?")):
            raise Failure("invalid_skill_dir", "Supply an authorized, known Skill directory without wildcard characters (* or ?).")
        skill_dirs.append(str(p))
    readonly = getattr(args, "read_only", False)
    if engine == "grok":
        return run_grok_worker(args, data, root, cfg_path, brief)
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
    cmd = platform_support.opencode_command(["models"] + ([provider, "--verbose"] if provider else []))
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
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


def grok_model_inventory(root):
    command = platform_support.grok_command(["models"])
    proc = subprocess.run(command, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=60)
    if proc.returncode:
        raise Failure("models_failed", "Grok inventory unavailable; existing configuration was not changed.")
    found = {}
    in_models = False
    for line in proc.stdout.splitlines():
        if line.strip() == "Available models:":
            in_models = True
            continue
        if not in_models:
            continue
        match = re.fullmatch(r"\s*[-*]\s+(\S+)(?:\s+\(default\))?\s*", line)
        if match:
            found[match.group(1)] = []
    if not found:
        raise Failure("invalid_inventory", "Grok model list was empty or unrecognized.")
    return found


def update_config(args, path, root):
    # Share the config lock and atomic write convention; preserve unknown fields.
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
        elif getattr(args, "engine", "opencode") == "grok":
            if args.variant is not None or args.clear_variant:
                raise Failure("unsupported_option", "Grok --variant is not supported.")
            clearing = args.model == "null"
            selected = None if clearing else args.model
            if selected is not None:
                if not isinstance(selected, str) or not selected.strip():
                    raise Failure("invalid_route", "Grok model must be a nonempty model ID.")
                if selected not in grok_model_inventory(root):
                    raise Failure("unknown_model", "Model is not in the current Grok inventory.")
            if args.action == "set-project":
                if clearing:
                    data["grok"]["projects"].pop(root, None)
                else:
                    data["grok"]["projects"][root] = selected
            else:
                data["grok"]["default"] = selected
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
    engine = getattr(args, "engine", "opencode")
    source, model = (resolve_grok_route(data, root) if engine == "grok"
                     else resolve_route(data, root)[:2])
    return dict(ok=True, config_path=str(path), project=root, engine=engine,
                route_source=source, model=model, mode=mode_for(data, root))


def parser():
    p = argparse.ArgumentParser(description="Coding Worker (OpenCode or Grok Build)")
    p.add_argument("--project")
    p.add_argument("--config")
    sub = p.add_subparsers(dest="action", required=True)
    models = sub.add_parser("models")
    models.add_argument("provider", nargs="?")
    models.add_argument("--engine", choices=("opencode", "grok"), default="opencode")
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--engine", choices=("opencode", "grok"), default="opencode")
    for name in ("set-default", "set-project"):
        setter = sub.add_parser(name); setter.add_argument("model")
        setter.add_argument("--engine", choices=("opencode", "grok"), default="opencode")
        choice = setter.add_mutually_exclusive_group()
        choice.add_argument("--variant"); choice.add_argument("--clear-variant", action="store_true")
    mode = sub.add_parser("set-mode")
    mode.add_argument("mode", choices=("auto", "manual", "off", "inherit"))
    mode.add_argument("--project-only", action="store_true")
    run = sub.add_parser("run")
    run.add_argument("--brief", required=True)
    run.add_argument("--engine", choices=("opencode", "grok"), default="opencode")
    run.add_argument("--model"); run.add_argument("--variant")
    run.add_argument("--explicit", action="store_true")
    run.add_argument("--read-only", action="store_true")
    run.add_argument("--skill-dir", action="append", default=[])
    # Production timeout policy: interrupt only after sustained event silence.
    # The old total-time limit is preserved as an explicit optional hard limit,
    # but disabled by default so long active sessions are not killed.
    run.add_argument("--inactivity-timeout", type=float, default=300,
                     help="Interrupt after this many seconds with no recognized Worker JSON event (default 300).")
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
            if args.engine == "grok" and args.provider:
                raise Failure("unsupported_option", "Grok models does not take a provider filter.")
            emit({"models": grok_model_inventory(root) if args.engine == "grok"
                  else model_inventory(root, args.provider)}); return 0
        if args.action.startswith("set-"):
            emit(update_config(args, path, root)); return 0
        data = load(path)
        if args.action == "resolve":
            if args.engine == "grok":
                source, model = resolve_grok_route(data, root)
                variant = None
            else:
                source, model, variant = resolve_route(data, root)
            emit(dict(project=root, engine=args.engine, mode=mode_for(data, root),
                      route_source=source, model=model, variant=variant)); return 0
        result = run_worker(args, data, root, path)
        emit(result)
        return 0 if result["status"] == "completed" else 2
    except (Failure, ValueError, OSError, subprocess.SubprocessError) as error:
        emit(dict(status="needs_escalation", worker_used=False,
                  error_code=getattr(error, "code", type(error).__name__), message=str(error)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
