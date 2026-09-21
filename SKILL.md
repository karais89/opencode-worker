---
name: opencode-worker
description: Delegate substantial repository implementation, bug fixes, multi-file changes, testing, debugging, and repository investigation from Codex/ChatGPT Head to one OpenCode Worker execution. Use when the user explicitly asks for OpenCode/worker delegation or when a coding task benefits from a thin Head -> Worker -> Head handoff. Keep simple questions, design-only discussion, tiny edits, and explicit worker opt-outs with Head.
---

# OpenCode Worker Lite v2

Use Codex/ChatGPT as **Head** and OpenCode only as the **execution Worker**.

Keep one normal path:

1. Head decides the goal, constraints, completion criteria, and exact repository.
2. Head writes one short self-contained brief.
3. Run one configured OpenCode Worker.
4. Worker explores, implements, tests, debugs, and fixes inside that same session.
5. Worker submits one compact structured result.
6. Head checks the result and only investigates concrete gaps.

Do not add a Planner, Reviewer, Router, automatic retry chain, corrective chain, or a second Worker.

## Delegate

Confirm the actual repository root before running. Do not ask OpenCode to search parent directories or the home directory for the intended project.

Write a short brief containing only:

- GOAL
- PLAN
- CONSTRAINTS
- DONE WHEN

Do not copy the whole conversation or pre-explore implementation details that the Worker can discover itself.

Run:

~~~sh
python3 <this-skill>/scripts/lite.py --project /absolute/repo run --brief /absolute/brief.txt
~~~

For an explicit $opencode-worker request, add --explicit. That explicit request authorizes the configured OpenCode route for this task, but never bypasses native host permission UI.

Normal execution performs exactly one OpenCode model run. It does not discover models, try fallbacks, run a Reviewer, or replay provider failures.

## Worker route

Use the saved route in this order:

1. one-shot --model / --variant
2. project route
3. global writer_default/default

The launcher reuses the existing ~/.config/opencode-worker/config.json format so Full and Lite can share model/variant settings. It never performs model discovery during a normal run.

Use references/lite-v2.md only for setup or route changes.

Respect mode:

- auto: implicit or explicit delegation allowed.
- manual: require --explicit.
- off: never launch the Worker.

## Boundaries

The launcher injects the existing repository-local Writer permission profile with a shorter Lite prompt. Keep repository-local read/edit/shell/test/build/debug capabilities, while continuing to deny secrets, global/system configuration, push/publish/deploy, destructive Git, broad deletion, and nested Codex/OpenCode workers.

The same canonical checkout uses one non-blocking lock. Never delete or bypass the lock to force concurrent Writers.

## Result

The Worker must finish by calling codex_worker_submit_result after its final validation. Do not parse final prose as the protocol.

Read:

- status
- changed
- validation
- risk
- validation_commands
- actual model and variant
- elapsed time
- provider-reported token telemetry when available
- validation_evidence

validation_evidence only corroborates declared local command exit codes. It does not independently prove test counts, requirement coverage, remote/MCP completion, or code quality.

If a declared validation command is observed failing, do not report completed. Missing or partial evidence is not automatically failure; report the unresolved risk accurately.

Do not routinely reread the full repository, diff, or rerun every test after Worker completion. Inspect only when the result conflicts with repository state, validation failed or was not run, a requirement is concretely missing, or the change is high risk.

## Report

Tell the user:

- what changed,
- what validation actually completed,
- what remains risky or blocked,
- the observed Worker model/variant and elapsed time when available.

Do not invent token savings, model identity, timing, or pass counts.

Keep this workflow thin: **Head -> one OpenCode Worker -> compact result -> Head**.
