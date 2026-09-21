---
name: opencode-worker
description: Delegate feature implementation, bug fixes, multi-file changes, test/debug/fix loops, and substantial repository investigations to an autonomous OpenCode worker. Keep simple questions, explanations, tiny edits, design-only discussion, and explicit worker opt-outs with Head.
---

# OpenCode Worker

Let Head decide the goal, important requirements, constraints and completion criteria. Write one short self-contained brief, referencing REQUEST.md if present. Let the Worker discover AGENTS and original Skills/tools, explore and perform the authorized work in its own session. Do not duplicate implementation exploration first. Split large requests into cohesive, independently validatable units; do not fragment small tasks.

## Delegate

Run `python3 <this-skill>/scripts/worker.py --project /absolute/repo run --brief /absolute/brief.txt --evidence-dir /outside/repo/evidence`. For an explicit $opencode-worker request that asks the Worker to execute repository work, add `--explicit`. Treat that explicit request as task-scoped authorization to launch the configured OpenCode Worker and use its saved provider/model route. Do not ask for a second conversational confirmation merely because the saved route uses an external model provider. This authorization never overrides off mode, expands Worker permissions, or bypasses native host authorization/rejection. Use the saved model route. Add `--use-fallbacks` only when alternative routes are authorized; saved fallback routes alone do not enable them. `--no-fallback` takes precedence.

Use one normal Writer for implementation, including its internal explore/implement/test/debug/fix iterations. Do not manage individual tool calls, add a Reviewer, or create an automatic corrective chain. Keep the canonical checkout lock: the same checkout cannot run overlapping workers, while independent checkouts may run concurrently.

For substantial code investigation, add `--read-only`. Request concise findings with file/line evidence in validation, changed=[] and explicit unknowns in risk. This mode permits native read/glob/grep/list, not shell, tests, edits or arbitrary MCP tools. Do not use it for tasks requiring execution or silently upgrade its permissions. Keep simple questions and final design judgments with Head.

In a restricted Codex host, request normal host permission escalation directly on the FIRST OpenCode model lookup or Worker launch (for example exec_command with sandbox_permissions="require_escalated"). For an explicit Worker execution request, do not insert a separate conversational "external provider" confirmation before requesting the host permission. OpenCode needs its user log/cache/config directories, checkout lock and provider network access; repository write access alone is insufficient. Use the host's native permission interface with the authorized task/provider scope. If the host itself requires user approval, surface that native approval normally. If it rejects the request or required authorization is unavailable, stop and report it; never bypass rejection or replay possible side effects. Do not add per-tool approval relays.

## Accept the result

Use the compact result to check request/result agreement, validation, obvious scope violations and unresolved risk. Do not routinely reread source/diffs, private evidence or rerun tests. Investigate only a concrete suspicion. Treat Worker completion as a report, not Head acceptance.

The Worker submits through codex_worker_submit_result. Invalid fields are corrected in the same live session, without replaying development. Final prose is not protocol. Missing/rejected/conflicting submission or execution failure is needs_escalation, not permission for a fresh run. Keep the process-only submission plugin; do not write project/global configuration for it.

Read validation_evidence as corroboration of declared local command exits only. observed_pass does not prove test counts, requirement coverage or remote completion. partial/unverified is not automatically failure: distinguish Worker-reported validation from independently observed exits and assess the specific unresolved risk. Never present missing evidence as verified success. A declared observed failing command prevents completed; do not add a blanket review or retry loop. Read references/control.md only for setup, routing or a specific execution/evidence problem.

## Correct one confirmed defect

Only after Head reproduces a concrete implementation defect with actual input, or identifies an unambiguous requirement violation, allow ONE corrective Writer. Vague suspicion, desire for more review and report/transport failure do not qualify. Write only the observed problem, input/evidence and expected behavior in the correction brief.

Run the same project/config command with `--fix-from <returned-evidence-dir>/writer.json --brief /absolute/correction.txt`. The controller restores the original brief and pins the last executed model/variant and approved command options; it uses a fresh session, disables fallback and does not expand authorization. Do not supply conflicting route/variant options. Legacy evidence without the original brief is not enough to reconstruct the task. The checkout lock and one-attempt marker include failed/interrupted correction attempts and reject correction-of-correction.

Let the Worker own the targeted fix and necessary validation. Perform a focused result check afterward. If unresolved or another correction is needed, stop with needs_escalation and the concrete remaining issue; do not take over implementation or evade the limit using a new ordinary run.

## Preserve boundaries and report

Keep normal authorized repository-local development capabilities available to Writers: files, shell, dependencies, CLI/MCP, Editor, tests/build/debug/fix. Keep shared original Skills read-only. Keep restrictions on secrets, global/system configuration, unauthorized external changes, destructive git, push/publish/deploy, broad deletion and nested workers. These policies are not an OS sandbox.

Give brief conversational updates at delegation, execution confirmation, meaningful Worker stage changes, result receipt, and final Head outcome. While the Worker command is running, surface the bounded `[opencode-worker]` progress telemetry emitted by the controller on stderr: elapsed time, controller-observed phase, tool-call count, changed-file count, and a 30-second heartbeat when the phase does not change. Do not expose raw model text, source, paths, shell commands, diffs or private evidence for progress, and do not invent percentage complete. A heartbeat only proves the process is still running; if it reports no recent event, state the last observed activity age rather than claiming normal progress. For an actual correction, name the concrete issue and correction 1/1. Do not poll/read private logs solely for display, narrate individual calls or repeat unchanged status.

Finish with outcome, validation and remaining risk. Add one short line with the observed model, total executed Writer calls and summed elapsed seconds: `Worker: <model> · 1회 · <seconds>초` or `Worker: <model> · 2회(수정 1회) · <seconds>초`. For read-only runs, identify the investigation in the outcome. If no Worker launched, say `Worker: 미사용 · <reason>`. State unavailable fields honestly; never invent timing or token savings. Keep this a thin Head → OpenCode → Head handoff, not a new orchestration system.
