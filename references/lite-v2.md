# Lite v2 routing and control

Read this only for setup, route changes, or a concrete execution problem.

## Config compatibility

Default config:

~~~text
~/.config/opencode-worker/config.json
~~~

Lite v2 intentionally accepts the existing P008 fields:

- writer_default or default
- projects
- variants
- aliases
- mode
- project_modes

Unknown extra fields are preserved when Lite v2 writes the config, so switching between the Full baseline and Lite v2 does not erase Full-only settings.

## Route priority

For run:

1. --model, with optional --variant
2. projects[canonical_repo]
3. writer_default, then default

If no explicit variant is supplied, variants[resolved_model] is used when present.

Normal run never calls opencode models. The explicit models subcommand is for setup only.

## Modes

Global mode is auto by default.

- auto: run normally.
- manual: require --explicit.
- off: block every run.

project_modes[canonical_repo] overrides the global mode.

## Why these controls remain

These controls are deterministic local work. They do not create extra model turns:

- route resolution,
- checkout lock,
- permission profile injection,
- JSONL observation,
- structured result validation,
- local command-exit corroboration.

Lite v2 deliberately omits fallback/replay classification, Reviewer/Planner agents, automatic corrective Writers, and model discovery on the normal path.

## Structured result

The Worker calls codex_worker_submit_result with:

- status: completed | needs_escalation
- changed: changed relative paths
- validation: actual checks/results or blockers
- risk: remaining uncertainty
- validation_commands: exact final local validation commands when applicable

The launcher compares declared validation_commands only with observed direct local bash command exits. This is narrow corroboration, not a general test parser or independent code review.
