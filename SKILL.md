---
name: delegate-coding-cli
description: Delegate coding tasks from Hermes or Feishu webhook messages to local Codex CLI and Gemini CLI with direct, worktree, or copy workspace modes, model selection, safe execution, diff collection, and test reporting. Use for implementation, debugging, refactoring, code review, repository analysis, and multi-step coding tasks.
version: 1.7.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [coding, codex, gemini, automation, devops]
    category: devops
    requires_toolsets: [terminal]
---

# delegate-coding-cli

## When to Use

Use this skill when the user wants Hermes to implement, debug, refactor, review, or inspect code by delegating the task to a local human-facing coding CLI such as Codex CLI or Gemini CLI.

Hard rules:
- Hermes must not directly implement, edit, create, or rewrite user project files.
- Hermes may only prepare workspace inputs, invoke `delegate_coding_cli.py`, collect diffs/tests/logs, and report results.
- Every executor call must go through `delegate_coding_cli.py`.
- Default implementation executor: Gemini CLI with `gemini-3-flash-preview`.
- Codex CLI remains available as an explicit override or fallback with `gpt-5.4-mini` and medium reasoning effort.
- `quality_mode` controls review cost:
  - `auto`: the helper picks `fast` or `safe` once from the task/risk signal, using a wider risk heuristic than just a few keywords, so Hermes does not need to keep re-deciding.
  - `fast`: minimize review cost; usually skip step review and let the final review auto-escalate only when the helper sees enough risk.
  - `safe`: keep flash-lite as the first-pass reviewer for step review, then finish the final review with pro and escalate step review to pro when the findings, tests, or task risk justify it.
- When using Gemini as executor, route by stage:
  - **gemini-3-flash-preview**: default implementation and analysis
  - **gemini-3.1-flash-lite-preview**: routine step review and fast-mode first-pass final review
  - **gemini-3.1-pro-preview**: safe-mode final review and escalation review for risky, ambiguous, or high-stakes cases
- Final review failures must be reported in a structured format with `Target File`, `Line Number`, `Error Classification`, and `Required Fix Action`.
- Default review policy is mode-dependent: `safe` defaults to `always`; `fast` defaults to `auto`.
- Implementation runs may perform bounded internal fixup rounds (`max_fixup_rounds`) inside a single helper invocation; Hermes should not re-launch the skill after every step review unless the final handoff says `followup_required: true`.
- Default stdout is a tiny `orchestrator_brief` only. Full `summary.json` and `handoff.json` stay on disk; Hermes must fetch details with `--read-handoff ... --section ...` only when needed.

Do not use this skill for:
- Direct deployment to production.
- Pushing commits to remote.
- Deleting repositories or system files.
- Reading private keys, tokens, browser profiles, password stores, or unrelated home directories.
- Tasks outside a user-specified repository/workspace.

## Executor Choice

- Default routing:
- Hermes parses the Feishu/user message, extracts the repo, task, mode, model overrides, review preference, and quality mode, then invokes the helper script.
- If the user says "auto mode" or just "auto", interpret that as `quality_mode: auto` unless they explicitly mention the executor.
- Use Codex CLI only as an explicit override or fallback for code implementation, bug fixes, refactoring, tests, and repository-local changes.
- Default Codex implementation model: `gpt-5.4-mini` with medium reasoning effort.
- Use Gemini CLI for large-context analysis, architectural review, second opinion, documentation review, or when the user explicitly asks for Gemini.
- Default Gemini implementation model: `gemini-3-flash-preview`.
- Default Gemini step-review model: `gemini-3.1-flash-lite-preview`.
- Default Gemini final-review model: `gemini-3.1-pro-preview`.
- Review cadence is helper-driven: flash-lite runs first for step review, safe mode always finishes final review with pro, and fast mode lets final review escalate only when the helper decides the task or findings warrant it.
- Orchestrator context is stateless around helper runs: after launching the helper, do not keep executor/review logs in memory. When the helper exits, consume only the stdout brief unless a detail section is required.
- Use `--check-health` when you want a quick process-count and workspace-lock snapshot without delegating any coding work.
- Workspace modes:
  - `direct`: run the executor directly in the user repo, with pre/post git logs, no worktree, and no tmp copy.
  - `worktree`: create an isolated git worktree for the repo.
  - `copy`: copy a non-git project into an isolated temporary workspace.
- For high-risk or ambiguous tasks, first request a plan-only run, then summarize the plan to the user before implementation.
- Never run two agents that edit the same workspace concurrently.

## Safety Rules

- Always operate through one of the helper-managed workspace modes: direct, worktree, or copy.
- Never run `--yolo` or approval bypass directly on the user's main working tree.
- Prefer Codex with `--sandbox workspace-write`.
- Prefer Gemini with `--skip-trust --sandbox --approval-mode=auto_edit`.
- Use Gemini `--approval-mode=yolo` only inside an isolated worktree or container, never on the main repo.
- Never auto-push, auto-merge, auto-deploy, or modify production configs.
- User-provided `workspace` paths are allowed only as new isolated targets under `/tmp`; never use them to edit the main worktree directly.
- If Codex CLI is unavailable, fall back to Gemini CLI.
- If Gemini CLI is unavailable, fail closed and report missing executor(s); do not let Hermes implement directly.
- Before calling an external CLI, write the prompt to a file and pass it via stdin or a safe subprocess array. Do not build shell strings with unescaped user input.
- After execution, always show:
  1. executor used
  2. workspace path
  3. branch/worktree name
  4. changed files
  5. test results
  6. concise summary
  7. next recommended action

## Pitfalls (learned the hard way)

1. **`review` parameter values are strictly `auto`, `always`, `never`** — NOT `"none"` or `"off"`. Using `"none"` causes `ValueError: review must be one of: auto, always, never`. When you want no review, use `"never"`.

2. **`direct` mode requires a git repo with at least one commit.** If the target directory is not a git repo, you must `git init && git add -A && git commit -m "init"` before invoking. The script checks `git_has_head()` and will fail with `"direct workspace mode requires a git repository with HEAD"`.

3. **Long tasks need `background=true`.** The terminal tool has a 600s foreground timeout. Tasks with `timeout: 1800` or any implementation task must run with `background=true, notify_on_complete=true`.

4. **Gemini CLI `--approval-mode` values**: for implementation use `auto_edit`, for plan/review use `plan`. The script handles this automatically, but if you're invoking `gemini` directly, remember these values.

5. **Review cadence**: Flash-lite is the first-pass reviewer for step review. Safe mode keeps step reviews on by default and finishes the final review with pro. Fast mode keeps step review rare and lets the final review auto-escalate only when needed.

6. **Two-tier structured handoff**: The helper writes `orchestrator_brief.json`, `summary.json`, and `handoff.json`. Downstream orchestrators should read the stdout brief first. Do not load full `handoff.json` unless the brief says details are required.

   Expected stdout shape:
   ```json
   {
     "success": true,
     "handoff_status": "passed",
     "followup_required": false,
     "next_recommended_action": "...",
     "paths": {
       "handoff": ".../handoff.json",
       "summary": ".../summary.json",
       "brief": ".../orchestrator_brief.json"
     },
     "available_sections": ["brief", "findings", "tests", "changed_files", "attempts", "logs", "full"]
   }
   ```

   Detail retrieval examples:
   `python3 -B scripts/delegate_coding_cli.py --read-handoff <handoff_path> --section findings`
   `python3 -B scripts/delegate_coding_cli.py --read-handoff <handoff_path> --section tests`

## Procedure

1. Parse the user request:
   - repo path
   - task
   - preferred executor: codex, gemini, auto
   - mode: plan, implement, review
   - quality mode: fast, safe, auto
   - workspace mode: direct, worktree, copy
   - optional Codex model, Gemini model, review policy, and isolated workspace path

2. Validate repo path:
   - Must exist.
   - Must be a directory.
   - Prefer git repository.
   - If not a git repo and `direct` mode requested: `cd REPO && git init && git add -A && git commit -m "init"` first.
   - Refuse paths such as `/`, `$HOME`, `~/.ssh`, `~/.config`, `/etc`, `/usr`, `/var`, `/private`.

3. Create safe workspace:
   - `direct`: use the user repo path as the executor workspace, and save pre/post git logs under `.hermes/delegate-runs/<timestamp>/`
   - `worktree`: create a new git worktree branch internally
   - `copy`: copy the project into an isolated temporary workspace

4. Build delegate prompt:
   Include:
   - Original user task.
   - Repo/workspace path.
   - Explicit constraints.
   - "Make minimal, focused changes."
   - "Run relevant tests if available."
   - "Do not push/deploy."
   - "At the end, summarize changed files and tests."

5. Run executor:
   - Codex implementation:
     `codex exec --cd WORKDIR --sandbox workspace-write --model CODEX_MODEL --ephemeral --output-last-message OUTFILE -`
   - Gemini analysis:
     `gemini --skip-trust --model GEMINI_MODEL --prompt "Follow the task from stdin." --output-format json`
   - Gemini implementation inside sandbox:
     `gemini --skip-trust --model GEMINI_MODEL --prompt "Follow the task from stdin." --sandbox --approval-mode=auto_edit --output-format json`
   - Gemini step review:
     `gemini --skip-trust --model GEMINI_STEP_REVIEW_MODEL --prompt "Follow the task from stdin." --approval-mode=plan --output-format json`
   - Gemini final review:
     `gemini --skip-trust --model GEMINI_FINAL_REVIEW_MODEL --prompt "Follow the task from stdin." --approval-mode=plan --policy FINAL_REVIEW_POLICY.md --output-format json`

6. Collect results:
   - `git status --short`
   - `git diff --stat`
   - `git diff -- . ':!*.lock'`
   - Run reasonable tests if available:
     - Python: `pytest -q`
     - Node: `npm test` or `pnpm test`
     - CMake/C++: detect existing build instructions; do not invent destructive build steps.
   - The helper may re-run implementation internally for bounded fixup rounds before producing the final handoff.
   - Final review is direct pro in safe mode; fast mode uses flash-lite first and escalates to pro only when the helper decides the review is high-risk or ambiguous.
   - Downstream orchestration should read stdout `followup_required` and `next_recommended_action` first. Only use `--read-handoff <handoff_path> --section findings` when it needs actionable details.
   - Treat helper execution as stateless: do not preserve implementation/review logs in orchestrator context while the helper runs.

7. Report to user:
   - Do not hide failures.
   - Include exact commands that failed.
   - Do not claim tests passed unless they actually passed.
   - Ask before commit/push/deploy.

8. Post-task cleanup:
   - The helper now cleans active child process groups on timeout, interrupt, and shutdown.
   - Read `cleanup_log` from `summary.json` / `handoff.json` when you need to verify what was reaped.
   - If the whole shell is already wedged and the helper cannot start, fall back to manual cleanup only then.

## Pitfalls

- **`direct` mode on non-git directory**: The script will fail immediately. Always verify `.git` exists first; if not, `git init` + empty commit.
- **Codex hangs after file generation**: For large tasks (1000+ lines), Codex may generate the file successfully but the delegate process stalls at the output-collection or review step. Symptoms: file exists on disk, `ps aux | grep codex` shows the process still running, but `delegate_coding_cli.py` produces no output. Remedy: let the helper reach its timeout or shutdown path so it can reap the child process group; if the helper itself is already wedged, inspect the file and use manual kill only as a last resort. The file is usable even if the process didn't exit cleanly.
- **Monitor via file existence, not process output**: The delegate script may produce zero stdout until completion. For long tasks, poll the expected output file path (`ls -la <path>`) rather than relying on `process log`.

6. **Background execution required**: Tasks over 10 minutes are common for large code generation. Always use `background=true` with `notify_on_complete=true`.

7. **System resource exhaustion from zombie processes**: After long multi-step sessions (especially with Codex CLI), orphaned child processes can still accumulate if an executor crashes below the helper layer. The helper now handles the common timeout/interrupt/shutdown paths, but if the shell is already refusing to fork (`/bin/bash: fork: Resource temporarily unavailable`), all tools can fail — `terminal`, `execute_code`, even `git` and `ls`. **Diagnosis**: use `execute_code` with Python's `subprocess` module (which may still work briefly) to run `ps aux | wc -l` and check process count. Typical healthy count is <200; >4000 means zombie saturation. **Recovery sequence**:
     a. Try `execute_code` with `subprocess.run(['ps', 'aux'])` to identify stuck Codex/Antigravity/node processes.
     b. Kill helper/renderer processes via `os.kill(pid, signal.SIGKILL)` inside `execute_code` — this bypasses the broken shell.
     c. If even `execute_code` fails, instruct the user to run manually in macOS Terminal:
        `killall -9 Codex 2>/dev/null; pkill -f "codex|antigravity|npx" 2>/dev/null`
     d. As a last resort, use Activity Monitor to force-quit Codex/Antigravity helper processes, or restart the terminal session.
     e. Once terminal recovers, verify with `echo "terminal works"` before attempting any delegate run.
     **Prevention**: rely on the helper's automatic cleanup first. Only reach for manual process killing if the helper itself cannot start or the shell is already too broken to complete a delegate run.

8. **"Remove X" tasks cause over-deletion**: When the task description says "remove X check/logic from function Y", the implementation often deletes the protective check entirely instead of relocating or conditionally bypassing it. Real-world example: task said "the pending check blocks closing the modal after battle starts, remove it" — the implementation deleted the check from `closeModal()`, but the correct fix was to keep the check and reset `pending=false` in `initDefense()`. **Mitigation**: phrase the task as "ensure X can happen by resetting state S in the right places, while keeping the protective check intact" rather than "remove X from Y". If the final review catches this pattern (`needs_followup` with a regression finding), and the fix is a 1-3 line targeted edit, apply it manually via `patch` rather than re-launching a full delegate run — it's faster and avoids another round of potential over-deletion.

9. **Manual patch beats re-delegate for small post-review fixes**: When the final review exits with `needs_followup` and the finding is a single targeted fix (restore one line, change one value, add one guard), use `read_file` + `patch` directly instead of launching another delegate round. The delegate overhead (gemini startup, prompt construction, review cycle) is 3-10 minutes for what may be a 30-second edit. Only re-delegate when the fix requires understanding broader context or touching multiple interrelated functions.

10. **Exit code 1 ≠ failure**: The delegate script may exit with code 1 when the final review has `handoff_status: "needs_followup"` — this means the review found issues, not that the process crashed. Always check `handoff.json` for `handoff_status` before assuming failure. A `"passed"` status with exit code 0 is clean; `"needs_followup"` with exit code 1 means targeted fixes are needed.

11. **Reading handoff review feedback quickly**: The `handoff.json` file can be large (200+ lines). To extract review findings without reading the whole file, use: `grep "review_feedback\|review_findings\|error_classification" <handoff_path> | head -10`. This gives the actionable items in seconds. For structured extraction, `python3 -c "import json; ..."` works but may be blocked by security — grep is more reliable.

12. **Batch multi-feature enhancements into one run**: When the user lists 3-6 improvements at once (e.g., "fix bug A, add feature B, improve C"), write a single request JSON with all items as numbered sections. The delegate handles multi-point tasks well — splitting into separate runs wastes startup/review overhead per run. Group related changes; only split when changes are truly independent and each is 500+ lines.

13. **Overly-broad guard clauses in shared functions**: When fixing "X blocks Y" bugs, the implementation often adds a blanket guard clause to the shared function instead of scoping it to the specific caller. Real-world example: `closeModal()` got `if (pending) return` to block closing the enemy-attack modal, but this blocked ALL modals (build, upgrade, events). The correct fix was `if (pending && modalContent.includes('敌军来袭')) return` — scoped to the specific modal type. **Mitigation**: in the task description, explicitly say "only block when [specific condition], other callers must be unaffected." When reviewing guard-clause fixes, check whether the condition is specific enough to not collateral-damage other code paths.

14. **Counter-increment before derived calculation**: When a counter (wave number, turn, level) is incremented before computing a derived value (interval, reward, difficulty), the derived value is off by one. Real-world example: `wave++` then `interval = 5 + wave` gave interval 7 instead of 6 for wave 1. **Mitigation**: in task descriptions, specify whether the counter should be incremented before or after the derived calculation, or say "use the pre-increment value for the formula." When reviewing, look for `++` / `+= 1` immediately before a formula that references the same variable.

## Verification

A successful run should produce:
- A helper-managed workspace in direct, worktree, or copy mode.
- A final CLI output file.
- A diff summary.
- A test report.
- No changes to the original main worktree unless direct mode was explicitly requested or the user explicitly asks to apply them.

## Feishu / Hermes Invocation

When a Feishu webhook message asks for coding work, Hermes should normalize the message into a request JSON file and call:

```bash
python3 -B ~/.hermes/skills/devops/delegate-coding-cli/scripts/delegate_coding_cli.py \
  --request-json /tmp/hermes-feishu-request.json
```

By default, this command prints only the minimal orchestrator brief. Full artifacts are written under `.hermes/delegate-runs/<timestamp>/`.

Supported request JSON fields:

   ```json
   {
     "repo": "/path/to/repo",
     "task": "Implement the requested coding change.",
     "executor": "auto",
     "mode": "implement",
     "timeout": 1800,
     "quality_mode": "auto",
     "max_fixup_rounds": 2,
     "workspace_mode": "direct",
     "codex_model": "gpt-5.4-mini",
     "codex_reasoning_effort": "medium",
     "gemini_model": "gemini-3-flash-preview",
     "review_model": "gemini-3.1-flash-lite-preview",
     "final_review_model": "gemini-3.1-pro-preview",
     "review": "always",
     "stdout_mode": "brief",
     "check_health": false,
     "workspace": "/tmp/hermes-coding-worktrees/custom-task-worktree"
   }
   ```

Field defaults:
- `executor`: `auto`
- `executor: auto` means the helper chooses the implementation backend; it does not mean the review strategy is automatic.
- `mode`: `implement`
- `quality_mode`: `auto` (the helper resolves this to `fast` or `safe` once per run using a broader risk heuristic)
- `workspace_mode`: `direct`
- `codex_model`: `gpt-5.4-mini`
- `codex_reasoning_effort`: `medium`
- `gemini_model`: `gemini-3-flash-preview` default implementation model
- `review_model`: `gemini-3.1-flash-lite-preview` default step-review model
- `final_review_model`: `gemini-3.1-pro-preview` default final-review model
- `review`: defaults to `always` in safe mode and `auto` in fast mode; explicit values still override the helper default.
- `stdout_mode`: `brief` by default. `summary` prints a compact run summary without full handoff/executor payloads. `full` preserves the legacy verbose stdout and should be used only for debugging; normal Hermes orchestration should stay on `brief`.
- `max_fixup_rounds`: defaults to `2` in safe mode and `0` in fast mode; the helper can retry implementation internally up to this many times when step review finds fixable issues.
- `check_health`: default `false`; when `true`, the helper prints a process and workspace-lock snapshot and exits without delegating work.
- `workspace_mode`: `direct` uses the repo path itself; `worktree` and `copy` use an isolated workspace path. If `workspace` is omitted, the helper chooses its default isolated path when needed.

Feishu message examples Hermes should understand:

```text
使用 delegate-coding-cli
repo: /Users/me/project
task: 修复登录失败后没有清理 loading 状态的问题
quality_mode: auto
review: auto
```

```text
delegate coding
repo=/Users/me/project
mode=plan
task=先分析支付回调幂等性风险，不要改代码
quality_mode=safe
```
