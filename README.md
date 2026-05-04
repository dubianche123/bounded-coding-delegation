<div align="right">
  <sub>
    <strong>English</strong> |
    <a href="README_CN.md">中文</a>
  </sub>
</div>

# Bounded Coding Delegation

Bounded Coding Delegation is a Hermes skill and local Python helper for delegating repository-local coding work to AI coding CLIs such as Gemini CLI and Codex CLI.

It is built around one principle:

**The orchestrator chooses the task boundary. The helper owns the execution loop.**

That means the expensive parent agent should not babysit every implementation, test, review, retry, and cleanup step. The helper runs a bounded local workflow, writes structured artifacts, and returns a tiny JSON brief that the parent orchestrator can parse cheaply.

## What It Solves

Local coding-agent workflows often fail in boring but painful places:

| Problem | What Goes Wrong | What This Helper Does |
|:--|:--|:--|
| Process leaks | Child CLIs survive interrupts, timeouts, or stuck pipes. | Tracks process groups and reaps them during timeout, shutdown, and cleanup. |
| Token bloat | The orchestrator reads full logs, review output, and large handoff JSON. | Prints a compact stdout brief and keeps detailed artifacts on disk. |
| No visibility | A long helper run looks frozen until the final JSON appears. | Emits progress heartbeats to stderr without polluting stdout JSON. |
| Weak model loops | Fast models can keep failing the same review and burn retries. | Stops at the fixup boundary and signals `needs_escalation` to the parent. |
| Ad hoc review policy | Every step asks the orchestrator what to do next. | Resolves `quality_mode` once and follows a deterministic review policy. |

## Core Behavior

The helper separates responsibilities:

- **Implementation**: defaults to Gemini CLI with `gemini-3-flash-preview`; Codex CLI can be selected explicitly.
- **Step review**: defaults to `gemini-3.1-flash-lite-preview` for routine checks and fixup decisions.
- **Final review**: safe mode uses `gemini-3.1-pro-preview`; fast mode starts lighter and escalates only when risk warrants it.
- **Parent escalation**: when fixup attempts are exhausted and review still fails, the helper does not auto-upgrade itself. It returns `needs_escalation` so Hermes can resubmit with a stronger model.

## Runtime Pipeline

1. Validate the target repository and requested workspace mode.
2. Create or select a controlled workspace.
3. Build a constrained delegation prompt.
4. Run implementation through Gemini CLI or Codex CLI.
5. Run detected tests when appropriate.
6. Run step review according to the selected policy.
7. Apply bounded fixup rounds when review finds actionable issues.
8. Stop with `needs_escalation` if max fixup attempts are exhausted and review still fails.
9. Otherwise run final review according to `quality_mode`.
10. Write `orchestrator_brief.json`, `summary.json`, `handoff.json`, and logs.
11. Print the final stdout payload as valid JSON.

## Progress Heartbeat

When `stdout_mode` is `brief`, stdout must stay machine-parseable. Progress messages are therefore written only to stderr:

```text
[Heartbeat] Starting Workspace: Preparing direct workspace for /path/to/repo.
[Heartbeat] Generating Implementation: Round 1: running gemini implementation.
[Heartbeat] Running Tests: Round 1: detecting and running configured tests.
[Heartbeat] Step Review [1]: Running first-pass review with gemini-3.1-flash-lite-preview.
[Heartbeat] Fixup Attempt [1]: Running targeted fixup with gemini.
[Heartbeat] Final Review: Running final review in safe mode.
[Heartbeat] Cleanup: Reaping active child processes after implementation run.
```

This gives humans and logs live observability while preserving stdout for downstream JSON parsing.

## Two-Tier Handoff

The default stdout payload is intentionally small:

```json
{
  "success": true,
  "handoff_status": "passed",
  "followup_required": false,
  "escalation_required": false,
  "next_recommended_action": "Inspect the reported diff and logs, then ask before applying, committing, pushing, or deploying.",
  "paths": {
    "handoff": ".hermes/delegate-runs/<timestamp>/handoff.json",
    "summary": ".hermes/delegate-runs/<timestamp>/summary.json",
    "brief": ".hermes/delegate-runs/<timestamp>/orchestrator_brief.json"
  }
}
```

Full artifacts stay under:

```text
.hermes/delegate-runs/<timestamp>/
```

Read only the section the orchestrator actually needs:

```bash
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section findings
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section tests
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section changed_files
```

Available sections are `brief`, `findings`, `tests`, `changed_files`, `attempts`, `logs`, and `full`.

## Dynamic Escalation Signal

The helper is deliberately bounded. If step review still fails after the configured fixup budget, the helper stops and returns:

```json
{
  "success": false,
  "handoff_status": "needs_escalation",
  "followup_required": true,
  "escalation_required": true,
  "next_recommended_action": "Task exceeded max fixup attempts with current executor. Recommend escalating to a stronger model (e.g., gemini-3.1-pro-preview) and resubmitting the task."
}
```

The exact breaker reason is stored in `summary.json` as `error`, `escalation_reason`, and `escalation_error`. The helper does not perform the model upgrade itself; that decision belongs to the parent orchestrator.

## Quality Modes

| Mode | Behavior |
|:--|:--|
| `auto` | Resolves once to `fast` or `safe` from task risk signals. |
| `fast` | Keeps step review rare; final review starts with flash-lite and calls pro only when risk warrants it. |
| `safe` | Runs step review by default, permits high-risk step review confirmation, and always finishes with pro final review unless the circuit breaker stops first. |

## Installation

Clone the repository, then install the skill into Hermes:

```bash
mkdir -p ~/.hermes/skills/devops
cp -R . ~/.hermes/skills/devops/delegate-coding-cli
chmod +x ~/.hermes/skills/devops/delegate-coding-cli/scripts/delegate_coding_cli.py
```

Requirements:

- Python 3
- Git
- Gemini CLI and/or Codex CLI

At least one executor must be available.

## Usage

Create a request JSON file:

```json
{
  "repo": "/path/to/repo",
  "task": "Fix the login loading state after authentication failure.",
  "executor": "auto",
  "mode": "implement",
  "quality_mode": "auto",
  "review": "auto",
  "stdout_mode": "brief",
  "workspace_mode": "direct"
}
```

Run the helper:

```bash
python3 -B scripts/delegate_coding_cli.py --request-json /tmp/delegate-request.json
```

`stdout_mode` controls only the final stdout payload:

| Value | Output |
|:--|:--|
| `brief` | Minimal orchestrator JSON. Default and recommended. |
| `summary` | Compact run summary without full handoff or executor payloads. |
| `full` | Verbose legacy summary for debugging. |

Useful request fields:

| Field | Values |
|:--|:--|
| `mode` | `plan`, `implement`, `review` |
| `executor` | `auto`, `gemini`, `codex` |
| `quality_mode` | `auto`, `fast`, `safe` |
| `workspace_mode` | `direct`, `worktree`, `copy` |
| `review` | `auto`, `always`, `never` |
| `max_fixup_rounds` | Non-negative integer |

## Safety Boundaries

The helper is conservative by design:

- It refuses broad or sensitive repository paths.
- It does not push, merge, deploy, or publish.
- It sanitizes sensitive environment variables before child CLI calls.
- It writes prompts to files and invokes subprocesses without shell interpolation.
- It uses workspace locks to avoid concurrent helper runs in the same workspace.
- It tracks child process groups and cleans them up on timeout, interrupt, and shutdown.

## Repository Layout

```text
.
├── SKILL.md
├── README.md
├── README_CN.md
├── LICENSE
└── scripts
    └── delegate_coding_cli.py
```

## Project Status

This is an MVP extracted from an active Hermes workflow. The goal is not to replace high-level orchestration; it is to make the local execution layer bounded, observable, and cheap to hand off.
