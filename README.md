<div align="right">
  <sub>
    <strong>English</strong> |
    <a href="README_CN.md">中文</a>
  </sub>
</div>

# Bounded Coding Delegation

## A helper-managed code delegation engine for local AI coding CLIs

**Version**: 1.0 MVP  
**Author**: Leo  
**Status**: Hermes skill / local CLI helper

Bounded Coding Delegation is a Hermes skill and Python helper that routes repository-local coding work to human-facing coding CLIs such as Gemini CLI and Codex CLI. It is designed for the part of agentic coding that often becomes messy in practice: process cleanup, review cadence, retry boundaries, and structured handoff back to the orchestrator.

The core rule is simple:

**The orchestrator should decide the task boundary. The helper should own the execution loop.**

Instead of asking a high-cost orchestrator to supervise every implementation and review step, this project lets Hermes launch one bounded helper run. The helper prepares a workspace, delegates implementation, runs tests where available, performs step review and fixup rounds, runs a final review, then writes machine-readable `summary.json` and `handoff.json` artifacts.

## Why This Exists

Many local agent workflows accidentally turn the orchestrator into a process supervisor. That is expensive and fragile. The top-level agent keeps deciding whether to review, whether to retry, whether to call a stronger model, and whether the child process is still alive. Over long sessions, that can inflate token usage and make process leaks harder to diagnose.

Bounded Coding Delegation moves repeatable execution policy into a small deterministic helper:

| Problem | Ad Hoc Delegation | Bounded Coding Delegation |
|:--|:--|:--|
| Review cadence | The orchestrator decides step by step. | `quality_mode` resolves once per run into `fast` or `safe`. |
| Model routing | Expensive reviewers can be called too often. | Flash-lite handles routine step review; pro is reserved for safe final review and high-risk escalation. |
| Retry control | The orchestrator relaunches tasks manually. | The helper runs bounded internal fixup rounds. |
| Process cleanup | Child CLIs can linger after timeout or interrupt. | The helper tracks active process groups and reaps them on timeout, interrupt, and shutdown. |
| Handoff | The next agent scrapes prose. | The helper writes structured JSON with findings, review status, tests, and next action. |

## Execution Model

The helper keeps three roles separate:

- **Implementation**: Gemini CLI defaults to `gemini-3-flash-preview`; Codex CLI remains available as an explicit override or fallback.
- **Step review**: Gemini flash-lite defaults to `gemini-3.1-flash-lite-preview` for routine checks and bounded fixup decisions.
- **Final review**: Gemini pro defaults to `gemini-3.1-pro-preview` in safe mode; fast mode starts lighter and escalates only when the helper sees enough risk.

The orchestrator still matters, but it should mostly choose the repo, task, mode, and quality policy. After that, the helper carries the execution loop until it can return a structured handoff.

## Quality Modes

`quality_mode` controls the review budget:

| Mode | Behavior |
|:--|:--|
| `auto` | The helper picks `fast` or `safe` once from task risk signals. |
| `fast` | Keeps step review rare; final review starts with flash-lite and escalates only when risk warrants it. |
| `safe` | Runs step review by default, lets risky step reviews call pro, and always finishes final review with pro. |

This keeps the common path quick without pretending every task deserves the same review budget.

## Runtime Pipeline

1. Validate the repository path and workspace mode.
2. Create or select a helper-managed workspace.
3. Build a constrained delegation prompt.
4. Run implementation through Gemini CLI or Codex CLI.
5. Run detected tests when reasonable.
6. Run step review when the selected policy calls for it.
7. Apply bounded fixup rounds when review finds actionable issues.
8. Run final review according to the selected quality mode.
9. Write logs, `summary.json`, and `handoff.json`.

## Structured Handoff

Each run writes a `handoff.json` artifact under:

```text
.hermes/delegate-runs/<timestamp>/handoff.json
```

The handoff includes:

- task and workspace metadata
- selected executor and model routing
- changed files
- test results
- step review feedback and findings
- final review feedback and findings
- cleanup log
- `followup_required`
- `next_recommended_action`

Downstream orchestrators should read this object instead of scraping terminal prose.

## Installation

Clone the repository, then install the skill into your Hermes skill directory:

```bash
mkdir -p ~/.hermes/skills/devops
cp -R . ~/.hermes/skills/devops/delegate-coding-cli
chmod +x ~/.hermes/skills/devops/delegate-coding-cli/scripts/delegate_coding_cli.py
```

The helper expects:

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
  "workspace_mode": "direct"
}
```

Run the helper:

```bash
python3 -B scripts/delegate_coding_cli.py --request-json /tmp/delegate-request.json
```

Useful modes:

| Field | Values |
|:--|:--|
| `mode` | `plan`, `implement`, `review` |
| `executor` | `auto`, `gemini`, `codex` |
| `quality_mode` | `auto`, `fast`, `safe` |
| `workspace_mode` | `direct`, `worktree`, `copy` |
| `review` | `auto`, `always`, `never` |

## Safety Boundaries

The helper is intentionally conservative:

- It refuses broad or sensitive repository paths.
- It does not push, merge, deploy, or publish.
- It sanitizes sensitive environment variables before child CLI calls.
- It writes prompts to files and invokes subprocesses without shell interpolation.
- It uses workspace locks to avoid two helper runs editing the same workspace.
- It tracks child process groups and cleans them up on timeout or shutdown.

## Repository Layout

```text
.
├── SKILL.md
├── README.md
├── LICENSE
└── scripts
    └── delegate_coding_cli.py
```

## Project Status

This is an MVP extracted from an active Hermes workflow. The goal is not to replace high-level orchestration, but to make the execution layer boring, bounded, and inspectable.
