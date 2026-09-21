# opencode-worker — Lite v2 experiment

This branch is the Lite v2 experiment for P008.

Baseline Full implementation:

- branch: main
- baseline commit: 326ecff40a5a1864d0124e4e652bb90f76a6f54a

Lite v2 keeps the original goal — Codex/ChatGPT is the Head and OpenCode is the execution Worker — but removes the large worker.py orchestration path from normal delegation.

## Normal path

Head -> one OpenCode run -> structured result -> Head

Normal Lite v2 execution intentionally has:

- one OpenCode model run,
- no Reviewer or Planner,
- no automatic fallback/replay,
- no automatic correction chain,
- no model inventory lookup during run.

It retains only the control features that materially help reproducibility and safety:

- saved model/variant route,
- project-specific route override,
- auto/manual/off mode,
- actual model/variant and elapsed-time reporting,
- Writer permission profile,
- structured codex_worker_submit_result,
- observed local validation-command exit evidence,
- canonical checkout lock.

## Setup

Lite v2 reuses the existing P008 config by default:

~~~text
~/.config/opencode-worker/config.json
~~~

Existing writer_default/default, projects, variants, aliases, mode, and project_modes fields are accepted.

List models only when setting up or changing a route:

~~~sh
python3 scripts/lite.py models
~~~

Set the default Worker:

~~~sh
python3 scripts/lite.py set-default provider/model --variant max
~~~

Set a project-specific Worker:

~~~sh
python3 scripts/lite.py --project /repo set-project provider/model --variant max
~~~

Resolve without launching OpenCode:

~~~sh
python3 scripts/lite.py --project /repo resolve
~~~

Modes:

~~~sh
python3 scripts/lite.py set-mode auto
python3 scripts/lite.py --project /repo set-mode manual --project-only
python3 scripts/lite.py --project /repo set-mode off --project-only
~~~

## Run

Write a short brief and run:

~~~sh
python3 scripts/lite.py --project /repo run --brief /tmp/task.txt
~~~

For an explicit Skill invocation:

~~~sh
python3 scripts/lite.py --project /repo run --brief /tmp/task.txt --explicit
~~~

A one-shot route override is available for experiments:

~~~sh
python3 scripts/lite.py --project /repo run --brief /tmp/task.txt --model provider/model --variant max
~~~

Normal run does not call opencode models and does not try another model if the selected route fails.

## Output

stdout is one compact JSON object. The main fields are:

- status
- model
- variant
- route_source
- elapsed_seconds
- result
- validation_evidence
- worker_tokens
- tools

Raw OpenCode JSONL, source, diffs, and shell transcripts are not returned inline.

## Validation

Run the focused Lite v2 tests:

~~~sh
python3 -m unittest discover -s scripts -p 'test_lite_v2.py'
~~~

Then run the existing P008 regression suite to ensure shared helpers remain compatible:

~~~sh
python3 -m unittest discover -s scripts -p 'test_*.py'
~~~

This branch is intentionally an A/B candidate, not a replacement for main until real-task cost, latency, quality, and Head follow-up work are compared.
