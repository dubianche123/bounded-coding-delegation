#!/usr/bin/env python3
"""Delegate repository-local coding work to Codex CLI or Gemini CLI safely."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fcntl
import json
import os
import re
import signal
import shlex
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Any, Iterable


INSTALL_COMMANDS = {
    "codex": "npm i -g @openai/codex",
    "gemini": "npm install -g @google/gemini-cli",
    "git": "install git with your OS package manager or Xcode Command Line Tools on macOS",
    "python3": "install Python 3 from https://www.python.org/ or your OS package manager",
}

SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
    ".netrc",
    ".ssh",
    ".config",
    ".gnupg",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}

SENSITIVE_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}

LOG_EXCLUDE_PATHSPEC = ":(exclude).hermes/delegate-runs/**"
LOCK_EXCLUDE_PATHSPEC = ":!*.lock"
GENERATED_STATUS_PREFIXES = (
    "__pycache__/",
    ".pytest_cache/",
    "node_modules/",
    ".mypy_cache/",
    ".ruff_cache/",
)
CACHE_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
DEFAULT_CODEX_MODEL = "gpt-5.4-mini"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
DEFAULT_GEMINI_MODEL = "gemini-3-flash-preview"
DEFAULT_GEMINI_REVIEW_MODEL = "gemini-3.1-flash-lite-preview"
DEFAULT_GEMINI_FINAL_REVIEW_MODEL = "gemini-3.1-pro-preview"
DEFAULT_WORKSPACE_MODE = "direct"
DEFAULT_QUALITY_MODE = "auto"
DEFAULT_STDOUT_MODE = "brief"
HANDOFF_SECTIONS = ("brief", "findings", "tests", "changed_files", "attempts", "logs", "full")

ACTIVE_PROCESSES: dict[int, subprocess.Popen[Any]] = {}
ACTIVE_PROCESSES_LOCK = threading.RLock()
CLEANUP_EVENTS: list[dict[str, Any]] = []


@dataclasses.dataclass
class DelegateRequest:
    repo: str
    task: str
    executor: str = "auto"
    mode: str = "implement"
    timeout: int = 1800
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT
    gemini_model: str = DEFAULT_GEMINI_MODEL
    review_model: str = DEFAULT_GEMINI_REVIEW_MODEL
    final_review_model: str = DEFAULT_GEMINI_FINAL_REVIEW_MODEL
    quality_mode: str = DEFAULT_QUALITY_MODE
    review: str = "always"
    max_fixup_rounds: int = 2
    check_health: bool = False
    stdout_mode: str = DEFAULT_STDOUT_MODE
    workspace: str | None = None
    workspace_mode: str = DEFAULT_WORKSPACE_MODE


@dataclasses.dataclass
class CommandResult:
    cmd: list[str]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_json(self) -> dict:
        return {
            "cmd": self.cmd,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "duration_sec": round(self.duration_sec, 2),
            "timed_out": self.timed_out,
            "stdout_tail": tail(self.stdout),
            "stderr_tail": tail(self.stderr),
        }


def tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _decode_stream(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode(errors="replace")


def _register_active_process(process: subprocess.Popen[Any]) -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES[process.pid] = process


def _unregister_active_process(pid: int) -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.pop(pid, None)


def _record_cleanup_event(event: dict[str, Any]) -> None:
    with ACTIVE_PROCESSES_LOCK:
        CLEANUP_EVENTS.append(event)


def terminate_process_group(process: subprocess.Popen[Any], reason: str, grace_seconds: float = 5.0) -> dict[str, Any]:
    event: dict[str, Any] = {
        "pid": process.pid,
        "reason": reason,
        "signals": [],
    }
    if process.poll() is not None:
        event["already_exited"] = True
        event["returncode"] = process.returncode
        return event

    for signame, sig in (("SIGTERM", signal.SIGTERM),):
        try:
            os.killpg(process.pid, sig)
            event["signals"].append(signame)
        except ProcessLookupError:
            event["group_missing"] = True
            break
        except PermissionError as exc:
            event["signal_error"] = str(exc)

    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            event["signals"].append("SIGKILL")
        except ProcessLookupError:
            event["group_missing"] = True
        except PermissionError as exc:
            event["signal_error"] = str(exc)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            event["wait_error"] = "process did not exit after SIGKILL"

    event["returncode"] = process.returncode
    return event


def cleanup_active_processes(reason: str) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    with ACTIVE_PROCESSES_LOCK:
        processes = list(ACTIVE_PROCESSES.values())
    for process in processes:
        try:
            event = terminate_process_group(process, reason)
            try:
                process.communicate()
            except Exception as drain_exc:
                event["drain_error"] = str(drain_exc)
        except Exception as exc:
            event = {"pid": process.pid, "reason": reason, "signals": [], "error": str(exc)}
        cleaned.append(event)
        _record_cleanup_event(event)
        _unregister_active_process(process.pid)
    return cleaned


def reset_runtime_state() -> None:
    with ACTIVE_PROCESSES_LOCK:
        ACTIVE_PROCESSES.clear()
        CLEANUP_EVENTS.clear()


def acquire_workspace_lock(workspace: Path, timestamp: str) -> tuple[Any, dict[str, Any]]:
    lock_path = workspace / ".hermes" / "delegate.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"Workspace is already locked: {lock_path}") from exc
    lock_info = {
        "lock_path": str(lock_path),
        "pid": os.getpid(),
        "timestamp": timestamp,
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(lock_info, indent=2, ensure_ascii=False))
    handle.flush()
    os.fsync(handle.fileno())
    return handle, lock_info


def release_workspace_lock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def inspect_lockfile(lock_path: Path) -> dict[str, Any] | None:
    if not lock_path.exists():
        return None

    handle = lock_path.open("a+", encoding="utf-8")
    locked = False
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked = True
        handle.seek(0)
        raw = handle.read().strip()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw or None
        if not locked:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return {
            "lock_path": str(lock_path),
            "locked": locked,
            "content": payload,
        }
    finally:
        handle.close()


def collect_runtime_health(repo: Path) -> dict[str, Any]:
    ps_result: CommandResult | None = None
    ps_error: str | None = None
    snapshot: dict[str, Any] = {
        "available": False,
        "total_processes": None,
        "codex_like_processes": None,
        "gemini_like_processes": None,
        "antigravity_like_processes": None,
        "node_like_processes": None,
        "zombie_processes": None,
        "healthy": None,
    }
    try:
        ps_result = run_command(["ps", "-axo", "pid,ppid,stat,command"], repo, 20)
        lines = [line.strip() for line in ps_result.stdout.splitlines() if line.strip()]
        process_rows = lines[1:] if lines and lines[0].lower().startswith("pid") else lines
        snapshot = {
            "available": True,
            "total_processes": len(process_rows),
            "codex_like_processes": 0,
            "gemini_like_processes": 0,
            "antigravity_like_processes": 0,
            "node_like_processes": 0,
            "zombie_processes": 0,
        }
        for row in process_rows:
            parts = row.split(None, 3)
            if len(parts) < 4:
                continue
            stat = parts[2]
            command = parts[3].lower()
            if "codex" in command:
                snapshot["codex_like_processes"] += 1
            if "gemini" in command:
                snapshot["gemini_like_processes"] += 1
            if "antigravity" in command:
                snapshot["antigravity_like_processes"] += 1
            if "node" in command:
                snapshot["node_like_processes"] += 1
            if stat.startswith("Z"):
                snapshot["zombie_processes"] += 1
        snapshot["healthy"] = snapshot["total_processes"] < 2000 and snapshot["zombie_processes"] < 10
    except Exception as exc:
        ps_error = str(exc)
    return {
        "timestamp": now_timestamp(),
        "repo": str(repo),
        "process_snapshot": snapshot,
        "active_helper_processes": len(ACTIVE_PROCESSES),
        "cleanup_events_seen": len(CLEANUP_EVENTS),
        "workspace_lock": inspect_lockfile(repo / ".hermes" / "delegate.lock"),
        "ps_command": ps_result.to_json() if ps_result else None,
        "ps_error": ps_error,
    }


def now_timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def path_is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def is_sensitive_name(path: Path) -> bool:
    parts = set(path.parts)
    if parts.intersection(SENSITIVE_NAMES):
        return True
    name = path.name.lower()
    if name in SENSITIVE_NAMES:
        return True
    if any(name.endswith(suffix) for suffix in SENSITIVE_SUFFIXES):
        return True
    if "token" in name and not name.endswith(".md"):
        return True
    if "secret" in name and not name.endswith(".md"):
        return True
    return False


def is_generated_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts.intersection(CACHE_DIR_NAMES):
        return True
    text = str(path)
    return text.startswith(GENERATED_STATUS_PREFIXES)


QUALITY_RISK_WORDS = (
    "security",
    "auth",
    "payment",
    "migration",
    "database",
    "permission",
    "crypto",
    "concurrency",
    "race",
    "idempot",
    "transaction",
    "rollback",
    "cache",
    "state",
    "session",
    "token",
    "oauth",
    "jwt",
    "acl",
    "rbac",
    "filesystem",
    "storage",
    "destructive",
    "breaking",
    "refactor",
    "rewrite",
    "replace",
    "optimi",
    "data loss",
    "安全",
    "权限",
    "支付",
    "迁移",
    "并发",
    "幂等",
    "事务",
    "回滚",
    "缓存",
    "状态",
    "会话",
    "令牌",
)

QUALITY_CRITICAL_RISK_WORDS = (
    "security",
    "auth",
    "payment",
    "migration",
    "database",
    "permission",
    "crypto",
    "concurrency",
    "race",
    "idempot",
    "transaction",
    "rollback",
    "crash",
    "deadlock",
    "memory leak",
    "overflow",
    "fork",
    "zombie",
    "data loss",
    "安全",
    "权限",
    "支付",
    "迁移",
    "并发",
    "幂等",
    "事务",
    "回滚",
)

PRO_ESCALATION_WORDS = (
    "security",
    "auth",
    "payment",
    "migration",
    "database",
    "permission",
    "crypto",
    "concurrency",
    "race",
    "idempot",
    "transaction",
    "rollback",
    "cache",
    "state",
    "session",
    "token",
    "oauth",
    "jwt",
    "acl",
    "rbac",
    "filesystem",
    "storage",
    "data loss",
    "regression",
    "architecture",
    "breaking",
    "high risk",
    "critical",
    "refactor",
    "rewrite",
    "replace",
    "optimi",
    "安全",
    "权限",
    "支付",
    "迁移",
    "并发",
    "幂等",
    "事务",
    "回滚",
    "缓存",
    "状态",
    "会话",
    "令牌",
)


def task_has_risk_words(task: str) -> bool:
    lowered = task.lower()
    critical_hits = sum(1 for word in QUALITY_CRITICAL_RISK_WORDS if word in lowered)
    if critical_hits >= 1:
        return True
    matches = sum(1 for word in QUALITY_RISK_WORDS if word in lowered)
    if matches >= 2:
        return True
    if any(word in lowered for word in ("critical", "major", "high risk", "breaking change", "regression")) and matches >= 1:
        return True
    if len(lowered.split()) >= 18 and matches >= 1:
        return True
    return False


def resolve_quality_mode(requested_mode: str, task: str) -> str:
    if requested_mode == "auto":
        return "safe" if task_has_risk_words(task) else "fast"
    if requested_mode not in {"fast", "safe"}:
        raise ValueError("quality_mode must be one of: auto, fast, safe")
    return requested_mode


def default_review_policy_for_quality_mode(quality_mode: str) -> str:
    return "always" if quality_mode == "safe" else "auto"


def default_fixup_rounds_for_quality_mode(quality_mode: str) -> int:
    return 2 if quality_mode == "safe" else 0


def validate_repo_path(repo_arg: str) -> Path:
    raw = Path(repo_arg).expanduser()
    if not raw.exists():
        raise ValueError(f"Repo path does not exist: {raw}")
    if not raw.is_dir():
        raise ValueError(f"Repo path is not a directory: {raw}")

    resolved = raw.resolve()
    home = Path.home().resolve()
    forbidden_roots = [
        Path("/").resolve(),
        home,
        (home / ".ssh").resolve(),
        (home / ".config").resolve(),
        Path("/etc").resolve(),
        Path("/usr").resolve(),
        Path("/var").resolve(),
        Path("/private").resolve(),
    ]

    if resolved == Path("/").resolve() or resolved == home:
        raise ValueError(f"Refusing broad unsafe path: {resolved}")
    for root in forbidden_roots[2:]:
        if resolved == root:
            raise ValueError(f"Refusing unsafe path: {resolved}")
        if root != Path("/private").resolve() and path_is_relative_to(resolved, root):
            raise ValueError(f"Refusing path inside unsafe root {root}: {resolved}")
    private = Path("/private").resolve()
    private_tmp = Path("/private/tmp").resolve()
    if path_is_relative_to(resolved, private) and not path_is_relative_to(resolved, private_tmp):
        raise ValueError(f"Refusing path inside unsafe root {private}: {resolved}")
    if is_sensitive_name(resolved):
        raise ValueError(f"Refusing sensitive path: {resolved}")
    return resolved


def validate_workspace_path(workspace_arg: str) -> Path:
    raw = Path(workspace_arg).expanduser()
    resolved = raw.resolve(strict=False)
    tmp_roots = {Path("/tmp").resolve(), Path("/private/tmp").resolve()}
    if not any(path_is_relative_to(resolved, root) and resolved != root for root in tmp_roots):
        raise ValueError(f"Workspace must be an isolated path under /tmp: {resolved}")
    if is_sensitive_name(resolved):
        raise ValueError(f"Refusing sensitive workspace path: {resolved}")
    if resolved.exists():
        raise ValueError(f"Workspace path already exists; provide a new isolated path: {resolved}")
    return resolved


def run_command(
    cmd: list[str],
    cwd: Path,
    timeout: int,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.time()
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        env=env,
        start_new_session=True,
    )
    _register_active_process(process)
    stdout = ""
    stderr = ""
    returncode: int | None = None
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _decode_stream(exc.stdout)
        stderr = _decode_stream(exc.stderr)
        cleanup_event: dict[str, Any]
        try:
            cleanup_event = terminate_process_group(process, "timeout")
        except Exception as cleanup_exc:
            cleanup_event = {"pid": process.pid, "reason": "timeout", "signals": [], "error": str(cleanup_exc)}
        _record_cleanup_event(cleanup_event)
        try:
            drained_stdout, drained_stderr = process.communicate()
        except Exception as drain_exc:
            drained_stdout, drained_stderr = "", ""
            cleanup_event["drain_error"] = str(drain_exc)
        stdout += drained_stdout
        stderr += drained_stderr
        returncode = process.returncode
    except BaseException as exc:
        if process.poll() is None:
            cleanup_event = {"pid": process.pid, "reason": type(exc).__name__, "signals": []}
            try:
                cleanup_event = terminate_process_group(process, type(exc).__name__)
            except Exception as cleanup_exc:
                cleanup_event["error"] = str(cleanup_exc)
            _record_cleanup_event(cleanup_event)
            try:
                process.communicate()
            except Exception:
                pass
        raise
    finally:
        _unregister_active_process(process.pid)
    return CommandResult(
        cmd=cmd,
        cwd=str(cwd),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_sec=time.time() - started,
        timed_out=timed_out,
    )


def require_tools() -> list[dict]:
    needed = ["git", "python3"]
    missing = []
    for tool in dict.fromkeys(needed):
        if shutil.which(tool) is None:
            missing.append({"tool": tool, "install": INSTALL_COMMANDS.get(tool, "")})
    if shutil.which("codex") is None and shutil.which("gemini") is None:
        missing.extend(
            [
                {"tool": "codex", "install": INSTALL_COMMANDS["codex"]},
                {"tool": "gemini", "install": INSTALL_COMMANDS["gemini"]},
            ]
        )
    return missing


def git_toplevel(path: Path) -> Path | None:
    result = run_command(["git", "-C", str(path), "rev-parse", "--show-toplevel"], path, 20)
    if result.ok and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return None


def git_has_head(repo: Path) -> bool:
    result = run_command(["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], repo, 20)
    return result.ok


def git_status_short(repo: Path) -> str:
    result = run_command(["git", "-C", str(repo), "status", "--short"], repo, 20)
    return result.stdout


def git_current_branch(repo: Path) -> str | None:
    result = run_command(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], repo, 20)
    branch = result.stdout.strip()
    if result.ok and branch and branch != "HEAD":
        return branch
    return None


def ensure_no_tracked_sensitive_files(repo: Path) -> list[str]:
    result = run_command(["git", "-C", str(repo), "ls-files", "-z"], repo, 30)
    if not result.ok:
        return []
    paths = [Path(part) for part in result.stdout.split("\0") if part]
    return [str(path) for path in paths if is_sensitive_name(path)]


def copy_ignore(_dir: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        path = Path(name)
        if name in {".git", ".hermes"} or is_sensitive_name(path):
            ignored.add(name)
    return ignored


def init_baseline_git(workspace: Path, timeout: int) -> list[CommandResult]:
    commands = [
        ["git", "init"],
        ["git", "add", "."],
        ["git", "-c", "user.name=Hermes Delegate", "-c", "user.email=hermes@example.invalid", "commit", "-m", "baseline"],
    ]
    return [run_command(cmd, workspace, timeout) for cmd in commands]


def capture_git_state(workspace: Path, log_dir: Path, prefix: str, include_stat: bool) -> dict:
    status = run_git_capture(
        ["git", "-C", str(workspace), "status", "--short", "--", ".", LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
        log_dir / f"{prefix}_git_status_short.txt",
    )
    diff = run_git_capture(
        ["git", "-C", str(workspace), "diff", "--", ".", LOCK_EXCLUDE_PATHSPEC, LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
        log_dir / f"{prefix}_git_diff.patch",
    )
    result = {
        "status": status.to_json(),
        "diff": diff.to_json(),
    }
    if include_stat:
        stat = run_git_capture(
            ["git", "-C", str(workspace), "diff", "--stat", "--", ".", LOCK_EXCLUDE_PATHSPEC, LOG_EXCLUDE_PATHSPEC],
            workspace,
            30,
            log_dir / f"{prefix}_git_diff_stat.txt",
        )
        result["diff_stat"] = stat.to_json()
    return result


def create_workspace(
    repo: Path,
    timestamp: str,
    timeout: int,
    workspace_mode: str,
    requested_workspace: Path | None = None,
) -> tuple[Path, str | None, str, list[str], list[CommandResult]]:
    warnings: list[str] = []
    setup_results: list[CommandResult] = []
    toplevel = git_toplevel(repo)
    repo_name = repo.name or "workspace"

    if workspace_mode == "direct":
        if not toplevel or not git_has_head(toplevel):
            raise ValueError("direct workspace mode requires a git repository with HEAD; use copy mode for non-git projects.")
        if requested_workspace is not None:
            warnings.append("workspace was provided but ignored in direct mode.")
        return repo.resolve(), git_current_branch(toplevel), "direct", warnings, setup_results

    if workspace_mode == "worktree":
        if not toplevel or not git_has_head(toplevel):
            raise ValueError("worktree mode requires a git repository with HEAD.")
        sensitive = ensure_no_tracked_sensitive_files(toplevel)
        if sensitive:
            joined = ", ".join(sensitive[:20])
            raise ValueError(f"Refusing to delegate because tracked sensitive-looking files exist: {joined}")

        branch = f"hermes/delegate-{timestamp}"
        workspace = requested_workspace or Path("/tmp/hermes-coding-worktrees") / f"{toplevel.name}-{timestamp}"
        workspace.parent.mkdir(parents=True, exist_ok=True)
        result = run_command(
            ["git", "-C", str(toplevel), "worktree", "add", "-b", branch, str(workspace), "HEAD"],
            toplevel,
            timeout,
        )
        setup_results.append(result)
        if not result.ok:
            raise RuntimeError(f"Failed to create git worktree: {tail(result.stderr or result.stdout, 1000)}")
        if git_status_short(toplevel).strip():
            warnings.append("Source repository has uncommitted changes that are not included in the new worktree.")
        return workspace.resolve(), branch, "git-worktree", warnings, setup_results

    if workspace_mode != "copy":
        raise ValueError(f"Unsupported workspace mode: {workspace_mode}")

    workspace = requested_workspace or Path("/tmp/hermes-coding-workspaces") / f"{repo_name}-{timestamp}"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo, workspace, ignore=copy_ignore)
    setup_results.extend(init_baseline_git(workspace, timeout))
    if toplevel:
        warnings.append("Git repository has no commits; used a temporary copy with a baseline commit instead of a worktree.")
    else:
        warnings.append("Path is not a git repository; used a temporary copy with a baseline commit.")
    return workspace.resolve(), None, "temporary-copy", warnings, setup_results


def sanitize_env() -> dict[str, str]:
    env = os.environ.copy()
    sensitive_markers = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "PRIVATE_KEY", "CREDENTIAL")
    for key in list(env.keys()):
        upper = key.upper()
        if any(marker in upper for marker in sensitive_markers):
            env.pop(key, None)
    return env


def build_delegate_prompt(
    task: str,
    workspace: Path,
    executor: str,
    mode: str,
    quality_mode: str,
    codex_model: str,
    gemini_model: str,
    review_model: str,
    final_review_model: str,
    codex_reasoning_effort: str,
    workspace_mode: str,
) -> str:
    mode_instruction = {
        "plan": "Plan only. Do not edit files. Return a concise implementation plan, risks, and tests to run.",
        "review": (
            "Review only. Do not edit files. Identify bugs, risks, missing tests, and concrete recommendations. "
            "If the review fails, output the feedback strictly in a structured format containing: 1. Target File, "
            "2. Line Number, 3. Error Classification, 4. Required Fix Action. If there are no findings, output exactly PASS."
        ),
        "implement": "Implement the requested change with minimal, focused edits. The helper may run bounded step-review fixup rounds and a final review without needing another Hermes handoff.",
    }[mode]

    return f"""You are a local coding CLI delegated by Hermes.

Mode: {mode}
Quality mode: {quality_mode}
Executor: {executor}
Codex model: {codex_model}
Codex reasoning effort: {codex_reasoning_effort}
Gemini primary model: {gemini_model}
Gemini step review model: {review_model}
Gemini final review model: {final_review_model}
Workspace mode: {workspace_mode}
Workspace: {workspace}

Original user task:
{task.strip()}

Hard constraints:
- Work only inside the workspace path above.
- Do not read private keys, tokens, browser profiles, password stores, ~/.ssh, ~/.config, .env files, or unrelated home directories.
- Do not push, deploy, merge, publish, delete repositories, or modify system files.
- Make minimal, focused changes.
- Run relevant tests if available.
- Do not invent destructive build steps.
- For quality mode `fast`, keep step review rare and let final review escalate automatically only when the helper sees enough risk.
- For quality mode `safe`, use flash-lite as the first-pass reviewer for step review, then finish with pro for the final review and any risky step-review escalation.
- At the end, summarize changed files and tests.

Mode-specific instruction:
{mode_instruction}
"""


def codex_supports_approval_flag() -> bool:
    result = run_command(["codex", "exec", "--help"], Path.cwd(), 20)
    return "--ask-for-approval" in result.stdout


def run_executor(
    executor: str,
    mode: str,
    workspace: Path,
    prompt: str,
    log_dir: Path,
    timeout: int,
    codex_model: str,
    gemini_model: str | None,
    gemini_policy_paths: list[Path] | None = None,
    label: str = "executor",
) -> tuple[CommandResult, Path | None, list[str]]:
    env = sanitize_env()
    warnings: list[str] = []
    last_message_path: Path | None = None

    if executor == "codex":
        sandbox = "read-only" if mode in {"plan", "review"} else "workspace-write"
        last_message_path = log_dir / f"{label}_codex_last_message.md"
        cmd = ["codex", "exec", "--cd", str(workspace), "--sandbox", sandbox]
        if codex_model:
            cmd.extend(["--model", codex_model])
        if codex_supports_approval_flag():
            cmd.extend(["--ask-for-approval", "never"])
        else:
            warnings.append("Installed Codex CLI does not expose --ask-for-approval; omitted that flag.")
        cmd.extend(["--ephemeral", "--output-last-message", str(last_message_path), "-"])
        return run_command(cmd, workspace, timeout, input_text=prompt, env=env), last_message_path, warnings

    if executor == "gemini":
        if gemini_model:
            cmd = ["gemini", "--skip-trust", "--model", gemini_model, "--prompt", "Follow the task from stdin.", "--output-format", "json"]
        else:
            cmd = ["gemini", "--skip-trust", "--prompt", "Follow the task from stdin.", "--output-format", "json"]
        for policy_path in gemini_policy_paths or []:
            cmd.extend(["--policy", str(policy_path)])
        if mode in {"plan", "review"}:
            cmd.extend(["--approval-mode", "plan"])
        else:
            cmd.extend(["--sandbox", "--approval-mode", "auto_edit"])
        return run_command(cmd, workspace, timeout, input_text=prompt, env=env), None, warnings

    raise ValueError(f"Unsupported executor: {executor}")


def normalize_optional_model(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "auto":
        return None
    return text


def normalize_workspace_mode(value: Any) -> str:
    text = str(value or DEFAULT_WORKSPACE_MODE).strip().lower()
    if text not in {"direct", "worktree", "copy"}:
        raise ValueError("workspace_mode must be one of: direct, worktree, copy")
    return text


def choose_executor(requested: str) -> str:
    codex_available = shutil.which("codex") is not None
    gemini_available = shutil.which("gemini") is not None

    if requested == "gemini":
        if gemini_available:
            return "gemini"
        if codex_available:
            return "codex"
        raise RuntimeError("Missing executor: gemini")
    if requested == "codex":
        if codex_available:
            return "codex"
        if gemini_available:
            return "gemini"
        raise RuntimeError("Missing executor: codex")
    if requested == "auto" and gemini_available:
        return "gemini"
    if codex_available:
        return "codex"
    if gemini_available:
        return "gemini"
    raise RuntimeError("Missing executor(s): codex, gemini")


def load_json_object(path_arg: str) -> dict[str, Any]:
    path = Path(path_arg).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise ValueError(f"Request JSON does not exist or is not a file: {path}")
    if is_sensitive_name(path):
        raise ValueError(f"Refusing sensitive-looking request JSON path: {path}")
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Request JSON must be an object.")
    return data


def normalize_request(args: argparse.Namespace) -> DelegateRequest:
    data = load_json_object(args.request_json) if args.request_json else {}

    def pick(name: str, default: Any = None) -> Any:
        value = getattr(args, name, None)
        if value is not None:
            return value
        return data.get(name, default)

    repo = pick("repo")
    if not repo:
        raise ValueError("Missing required repo. Provide --repo or request_json.repo.")

    task = data.get("task")
    task_file_arg = pick("task_file")
    if task_file_arg:
        task_file = Path(str(task_file_arg)).expanduser().resolve()
        if not task_file.exists() or not task_file.is_file():
            raise ValueError(f"Task file does not exist or is not a file: {task_file}")
        if is_sensitive_name(task_file):
            raise ValueError(f"Refusing sensitive-looking task file path: {task_file}")
        task = task_file.read_text(encoding="utf-8")
    check_health = bool(pick("check_health", False))
    if not task or not str(task).strip():
        if check_health:
            task = "Health check"
        else:
            raise ValueError("Missing required task. Provide --task-file or request_json.task.")
    if not task or not str(task).strip():
        raise ValueError("Missing required task. Provide --task-file or request_json.task.")

    executor = pick("executor", "auto")
    mode = pick("mode", "implement")
    requested_quality_mode = str(pick("quality_mode", DEFAULT_QUALITY_MODE))
    quality_mode = resolve_quality_mode(requested_quality_mode, str(task))
    review = pick("review", default_review_policy_for_quality_mode(quality_mode))
    stdout_mode = str(pick("stdout_mode", DEFAULT_STDOUT_MODE))
    max_fixup_rounds_value = pick("max_fixup_rounds", None)
    max_fixup_rounds = (
        default_fixup_rounds_for_quality_mode(quality_mode)
        if max_fixup_rounds_value is None
        else int(max_fixup_rounds_value)
    )
    workspace_mode = normalize_workspace_mode(pick("workspace_mode", DEFAULT_WORKSPACE_MODE))
    if executor not in {"auto", "codex", "gemini"}:
        raise ValueError("executor must be one of: auto, codex, gemini")
    if mode not in {"plan", "implement", "review"}:
        raise ValueError("mode must be one of: plan, implement, review")
    if review not in {"auto", "always", "never"}:
        raise ValueError("review must be one of: auto, always, never")
    if stdout_mode not in {"brief", "summary", "full"}:
        raise ValueError("stdout_mode must be one of: brief, summary, full")
    if quality_mode not in {"fast", "safe"}:
        raise ValueError("quality_mode must resolve to one of: fast, safe")
    if max_fixup_rounds < 0:
        raise ValueError("max_fixup_rounds must be greater than or equal to 0")

    return DelegateRequest(
        repo=str(repo),
        task=str(task),
        executor=executor,
        mode=mode,
        timeout=int(pick("timeout", 1800)),
        codex_model=str(pick("codex_model", DEFAULT_CODEX_MODEL)),
        codex_reasoning_effort=str(pick("codex_reasoning_effort", DEFAULT_CODEX_REASONING_EFFORT)),
        gemini_model=normalize_optional_model(pick("gemini_model", DEFAULT_GEMINI_MODEL)) or DEFAULT_GEMINI_MODEL,
        review_model=normalize_optional_model(pick("review_model", DEFAULT_GEMINI_REVIEW_MODEL)) or DEFAULT_GEMINI_REVIEW_MODEL,
        final_review_model=normalize_optional_model(pick("final_review_model", DEFAULT_GEMINI_FINAL_REVIEW_MODEL))
        or DEFAULT_GEMINI_FINAL_REVIEW_MODEL,
        quality_mode=quality_mode,
        review=review,
        max_fixup_rounds=max_fixup_rounds,
        check_health=check_health,
        stdout_mode=stdout_mode,
        workspace=pick("workspace"),
        workspace_mode=workspace_mode,
    )


def code_changed_files(changed_files: list[dict]) -> list[str]:
    code_suffixes = {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".html",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".swift",
        ".ts",
        ".tsx",
        ".vue",
    }
    return [
        item["path"]
        for item in changed_files
        if Path(item.get("path", "")).suffix.lower() in code_suffixes
    ]


def should_run_review(
    policy: str,
    mode: str,
    changed_files: list[dict],
    tests_failed: bool,
    task: str,
    quality_mode: str,
) -> bool:
    if mode != "implement":
        return False
    if policy == "always":
        return True
    if policy == "never":
        return False
    if tests_failed:
        return True
    lowered = task.lower()
    if quality_mode == "fast":
        risk_words = (
            "security",
            "auth",
            "payment",
            "migration",
            "database",
            "permission",
            "crypto",
            "concurrency",
            "race",
            "crash",
            "deadlock",
            "memory leak",
            "overflow",
            "fork",
            "zombie",
            "data loss",
            "安全",
            "权限",
            "支付",
            "迁移",
            "并发",
        )
        if any(word in lowered for word in risk_words):
            return True
        code_files = code_changed_files(changed_files)
        return len(code_files) >= 5 and len(changed_files) >= 6
    risk_words = (
        "security",
        "auth",
        "payment",
        "migration",
        "database",
        "permission",
        "crypto",
        "安全",
        "权限",
        "支付",
        "迁移",
    )
    if any(word in lowered for word in risk_words):
        return True
    code_files = code_changed_files(changed_files)
    return len(code_files) >= 3


def review_has_findings(text: str) -> bool:
    normalized = " ".join(text.strip().split()).casefold()
    if not normalized:
        return False
    pass_pattern = re.compile(r"^(pass|no findings|no issues|looks good|approved)([.!:]|\s|$)")
    return pass_pattern.match(normalized) is None


def parse_structured_review_findings(text: str) -> list[dict[str, object]]:
    normalized = text.strip()
    if not review_has_findings(normalized):
        return []

    target_pattern = re.compile(r"(?i)^target file\s*[:：]\s*(.+)$")
    line_pattern = re.compile(r"(?i)^line number\s*[:：]\s*(.+)$")
    classification_pattern = re.compile(r"(?i)^error classification\s*[:：]\s*(.+)$")
    action_pattern = re.compile(r"(?i)^required fix action\s*[:：]\s*(.+)$")

    findings: list[dict[str, object]] = []
    current: dict[str, object] = {}

    def flush() -> None:
        nonlocal current
        if current:
            findings.append(current)
            current = {}

    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if current.get("target_file") and target_pattern.match(line):
            flush()

        if match := target_pattern.match(line):
            current["target_file"] = match.group(1).strip()
            continue
        if match := line_pattern.match(line):
            value = match.group(1).strip()
            current["line_number"] = int(value) if value.isdigit() else value
            continue
        if match := classification_pattern.match(line):
            current["error_classification"] = match.group(1).strip()
            continue
        if match := action_pattern.match(line):
            current["required_fix_action"] = match.group(1).strip()
            continue

    flush()
    return findings


def build_final_review_policy() -> str:
    return """If the review fails, you must output the feedback strictly in a structured format containing:
1. Target File
2. Line Number
3. Error Classification
4. Required Fix Action

Do not write conversational prose, long explanations, or chatty filler.
If there are no findings, output exactly PASS.
"""


def build_review_prompt(
    task: str,
    workspace: Path,
    changed_files: list[dict],
    tests: list[dict],
    review_stage: str,
    review_model: str,
) -> str:
    stage_instruction = {
        "step": (
            f"- Review stage: step\n"
            f"- Review model: {review_model}\n"
            "- Return concise findings. If there are no findings, output exactly PASS."
        ),
        "final": (
            f"- Review stage: final\n"
            f"- Review model: {review_model}\n"
            "- This is the final audit after implementation or fixup.\n"
            "- If the review fails, you must output the feedback strictly in a structured format containing: "
            "1. Target File, 2. Line Number, 3. Error Classification, 4. Required Fix Action.\n"
            "- Do not write conversational prose, long explanations, or chatty filler.\n"
            "- If there are no findings, output exactly PASS."
        ),
    }[review_stage]
    return f"""Review the implementation for the Hermes delegated task.

Original user task:
{task.strip()}

Workspace: {workspace}

Changed files:
{json.dumps(changed_files, indent=2, ensure_ascii=False)}

Test results:
{json.dumps(tests, indent=2, ensure_ascii=False)}

Review rules:
- Do not edit files.
- Inspect the current git diff and repository context.
- Focus on correctness bugs, regressions, safety risks, and missing tests.
{stage_instruction}
    """


def build_fixup_prompt(
    task: str,
    workspace: Path,
    changed_files: list[dict],
    tests: list[dict],
    review_feedback: str,
    review_findings: list[dict[str, object]],
    round_index: int,
    max_fixup_rounds: int,
) -> str:
    findings_json = json.dumps(review_findings, indent=2, ensure_ascii=False)
    return f"""Continue fixing the Hermes delegated task.

Original user task:
{task.strip()}

Workspace: {workspace}

Current fixup round: {round_index} of {max_fixup_rounds}

Latest review feedback:
{review_feedback.strip() or 'No textual feedback available.'}

Parsed review findings:
{findings_json}

Changed files so far:
{json.dumps(changed_files, indent=2, ensure_ascii=False)}

Test results so far:
{json.dumps(tests, indent=2, ensure_ascii=False)}

Instructions:
- Fix only the issues identified by the latest review unless a related change is required for correctness.
- Preserve unrelated work.
- Keep the change minimal and targeted.
- Run the most relevant tests again after editing.
    - The helper will re-review this round automatically.
"""


def review_findings_blob(review_findings: list[dict[str, object]]) -> str:
    chunks: list[str] = []
    for finding in review_findings:
        for key in ("target_file", "line_number", "error_classification", "required_fix_action"):
            value = finding.get(key)
            if value is not None:
                chunks.append(str(value))
    return " ".join(chunks).lower()


def review_findings_are_high_risk(review_findings: list[dict[str, object]]) -> bool:
    blob = review_findings_blob(review_findings)
    return any(word in blob for word in PRO_ESCALATION_WORDS)


def should_escalate_review_to_pro(
    quality_mode: str,
    task: str,
    changed_files: list[dict],
    tests_failed: bool,
    review_feedback: str,
    review_findings: list[dict[str, object]],
    review_stage: str,
) -> tuple[bool, str | None]:
    if review_stage == "step" and quality_mode == "fast":
        return False, None

    if review_stage == "final" and quality_mode == "safe":
        return True, "safe mode final review always uses pro"

    if review_findings_are_high_risk(review_findings):
        return True, "flash-lite review surfaced high-risk findings"
    if len(review_findings) >= 2:
        return True, "flash-lite review surfaced multiple findings"
    if tests_failed and review_has_findings(review_feedback):
        return True, "tests failed and flash-lite review also found issues"

    code_files = code_changed_files(changed_files)
    if review_stage == "final":
        if task_has_risk_words(task) and (tests_failed or len(code_files) >= 4 or len(changed_files) >= 6):
            return True, "task or diff looks high-risk enough to justify pro confirmation"
        return False, None

    if task_has_risk_words(task) and (tests_failed or len(code_files) >= 5 or len(changed_files) >= 6):
        return True, "task or diff looks high-risk enough to justify pro confirmation"

    return False, None


def build_pro_escalation_prompt(
    task: str,
    workspace: Path,
    changed_files: list[dict],
    tests: list[dict],
    flash_review_feedback: str,
    flash_review_findings: list[dict[str, object]],
    escalation_reason: str,
) -> str:
    return f"""A first-pass Gemini flash-lite review has already run for the Hermes delegated task.

Escalation reason:
{escalation_reason}

Original user task:
{task.strip()}

Workspace: {workspace}

Changed files:
{json.dumps(changed_files, indent=2, ensure_ascii=False)}

Test results:
{json.dumps(tests, indent=2, ensure_ascii=False)}

Flash-lite review feedback:
{flash_review_feedback.strip() or 'No textual feedback available.'}

Parsed flash-lite findings:
{json.dumps(flash_review_findings, indent=2, ensure_ascii=False)}

Review rules:
- Do not edit files.
- Treat flash-lite as the first-pass reviewer and verify whether its concerns are real, incomplete, or too broad.
- Focus on risky regressions, correctness issues, and missing tests.
- If the review passes, output exactly PASS.
- If the review fails, output the feedback strictly in a structured format containing: 1. Target File, 2. Line Number, 3. Error Classification, 4. Required Fix Action.
- Do not write conversational prose, long explanations, or chatty filler.
{build_final_review_policy()}
"""


def run_implementation_round(
    round_index: int,
    request: DelegateRequest,
    executor: str,
    workspace: Path,
    log_dir: Path,
    prompt: str,
    task: str,
    codex_model: str,
    gemini_model: str,
    review_model: str,
    timeout: int,
) -> dict[str, Any]:
    suffix = "" if round_index == 1 else f"_round_{round_index}"
    prompt_path = log_dir / ("delegate_prompt.md" if round_index == 1 else f"delegate_prompt_round_{round_index}.md")
    write_text(prompt_path, prompt)

    executor_result, last_message_path, executor_warnings = run_executor(
        executor,
        request.mode,
        workspace,
        prompt,
        log_dir,
        timeout,
        codex_model,
        gemini_model,
        None,
        f"implementation_round_{round_index}",
    )
    implementation_stdout_path = log_dir / f"implementation{suffix}.stdout.log"
    implementation_stderr_path = log_dir / f"implementation{suffix}.stderr.log"
    write_text(implementation_stdout_path, executor_result.stdout)
    write_text(implementation_stderr_path, executor_result.stderr)

    tests = run_detected_tests(workspace, timeout) if request.mode == "implement" else []
    tests_path = log_dir / ("tests.json" if round_index == 1 else f"tests_round_{round_index}.json")
    write_text(tests_path, json.dumps(tests, indent=2, ensure_ascii=False))
    changed_files = collect_changed_files(workspace)
    tests_failed = any(test.get("returncode") not in (0, None) for test in tests)
    review_performed = should_run_review(
        request.review,
        request.mode,
        changed_files,
        tests_failed,
        task,
        request.quality_mode,
    )
    step_review_result = None
    step_review_feedback = ""
    step_review_prompt_path = None
    step_review_stdout_path = None
    step_review_stderr_path = None
    flash_step_review_result = None
    flash_step_review_feedback = ""
    flash_step_review_findings: list[dict[str, object]] = []
    step_review_escalation_reason: str | None = None
    step_review_tier: str | None = None
    step_review_model_used: str | None = None
    if request.mode == "review":
        step_review_result = executor_result.to_json()
        step_review_feedback = extract_executor_text(executor_result)
        step_review_model_used = request.final_review_model
        step_review_tier = "pro"
    elif review_performed:
        review_prompt = build_review_prompt(task, workspace, changed_files, tests, "step", review_model)
        step_review_prompt_path = log_dir / ("gemini_review_prompt.txt" if round_index == 1 else f"gemini_review_prompt_round_{round_index}.txt")
        write_text(step_review_prompt_path, review_prompt)
        gemini_review, _, review_warnings = run_executor(
            "gemini",
            "review",
            workspace,
            review_prompt,
            log_dir,
            timeout,
            codex_model,
            review_model,
            None,
            f"step_review_round_{round_index}",
        )
        step_review_stdout_path = log_dir / ("gemini_review.stdout.log" if round_index == 1 else f"gemini_review_round_{round_index}.stdout.log")
        step_review_stderr_path = log_dir / ("gemini_review.stderr.log" if round_index == 1 else f"gemini_review_round_{round_index}.stderr.log")
        write_text(step_review_stdout_path, gemini_review.stdout)
        write_text(step_review_stderr_path, gemini_review.stderr)
        executor_warnings.extend(review_warnings)
        flash_step_review_result = gemini_review.to_json()
        flash_step_review_feedback = extract_executor_text(gemini_review)
        flash_step_review_findings = parse_structured_review_findings(flash_step_review_feedback)
        step_review_result = flash_step_review_result
        step_review_feedback = flash_step_review_feedback
        step_review_model_used = review_model
        step_review_tier = "flash_lite"

        should_escalate, escalation_reason = should_escalate_review_to_pro(
            request.quality_mode,
            task,
            changed_files,
            tests_failed,
            flash_step_review_feedback,
            flash_step_review_findings,
            "step",
        )
        if should_escalate:
            step_review_escalation_reason = escalation_reason
            step_policy_path = log_dir / (
                "gemini_step_review.policy.md" if round_index == 1 else f"gemini_step_review_round_{round_index}.policy.md"
            )
            write_text(step_policy_path, build_final_review_policy())
            pro_review_prompt = build_pro_escalation_prompt(
                task,
                workspace,
                changed_files,
                tests,
                flash_step_review_feedback,
                flash_step_review_findings,
                escalation_reason or "flash-lite review requested pro confirmation",
            )
            pro_prompt_path = log_dir / (
                "gemini_review_pro_prompt.txt" if round_index == 1 else f"gemini_review_pro_prompt_round_{round_index}.txt"
            )
            write_text(pro_prompt_path, pro_review_prompt)
            pro_review_result, _, pro_review_warnings = run_executor(
                "gemini",
                "review",
                workspace,
                pro_review_prompt,
                log_dir,
                timeout,
                codex_model,
                request.final_review_model,
                [step_policy_path],
                f"step_review_escalation_round_{round_index}",
            )
            pro_stdout_path = log_dir / (
                "gemini_review_pro.stdout.log" if round_index == 1 else f"gemini_review_pro_round_{round_index}.stdout.log"
            )
            pro_stderr_path = log_dir / (
                "gemini_review_pro.stderr.log" if round_index == 1 else f"gemini_review_pro_round_{round_index}.stderr.log"
            )
            write_text(pro_stdout_path, pro_review_result.stdout)
            write_text(pro_stderr_path, pro_review_result.stderr)
            executor_warnings.extend(pro_review_warnings)
            step_review_result = pro_review_result.to_json()
            step_review_feedback = extract_executor_text(pro_review_result)
            step_review_model_used = request.final_review_model
            step_review_tier = "pro"

    structured_review_findings = parse_structured_review_findings(step_review_feedback)
    step_review_has_findings = review_has_findings(step_review_feedback)

    return {
        "round": round_index,
        "phase": "initial" if round_index == 1 else "fixup",
        "prompt_path": str(prompt_path),
        "implementation_stdout_path": str(implementation_stdout_path),
        "implementation_stderr_path": str(implementation_stderr_path),
        "tests_path": str(tests_path),
        "step_review_prompt_path": str(step_review_prompt_path) if step_review_prompt_path else None,
        "step_review_stdout_path": str(step_review_stdout_path) if step_review_stdout_path else None,
        "step_review_stderr_path": str(step_review_stderr_path) if step_review_stderr_path else None,
        "step_review_escalation_reason": step_review_escalation_reason,
        "step_review_tier": step_review_tier,
        "step_review_model_used": step_review_model_used,
        "flash_step_review_result": flash_step_review_result,
        "flash_step_review_feedback": flash_step_review_feedback or None,
        "flash_step_review_findings": flash_step_review_findings,
        "executor_result": executor_result.to_json(),
        "last_message_path": str(last_message_path) if last_message_path else None,
        "warnings": executor_warnings,
        "tests": tests,
        "tests_failed": tests_failed,
        "changed_files": changed_files,
        "review_performed": review_performed or request.mode == "review",
        "step_review_result": step_review_result,
        "step_review_feedback": step_review_feedback or None,
        "step_review_findings": structured_review_findings,
        "step_review_has_findings": step_review_has_findings,
        "step_review_ok": step_review_result is None or (
            step_review_result.get("returncode") == 0 and not step_review_has_findings
        ),
    }


def run_final_review_round(
    request: DelegateRequest,
    workspace: Path,
    log_dir: Path,
    task: str,
    changed_files: list[dict],
    tests: list[dict],
    timeout: int,
    codex_model: str,
) -> dict[str, Any]:
    final_policy_path = log_dir / "gemini_final_review.policy.md"
    write_text(final_policy_path, build_final_review_policy())
    if request.quality_mode == "safe":
        final_review_prompt = build_review_prompt(task, workspace, changed_files, tests, "final", request.final_review_model)
        final_prompt_path = log_dir / "gemini_final_review_prompt.txt"
        write_text(final_prompt_path, final_review_prompt)
        final_review_result, _, final_review_warnings = run_executor(
            "gemini",
            "review",
            workspace,
            final_review_prompt,
            log_dir,
            timeout,
            codex_model,
            request.final_review_model,
            [final_policy_path],
            "final_review",
        )
        final_stdout_path = log_dir / "gemini_final_review.stdout.log"
        final_stderr_path = log_dir / "gemini_final_review.stderr.log"
        write_text(final_stdout_path, final_review_result.stdout)
        write_text(final_stderr_path, final_review_result.stderr)
        final_review_feedback = extract_executor_text(final_review_result)
        final_review_findings = parse_structured_review_findings(final_review_feedback)
        return {
            "review_result": final_review_result.to_json(),
            "review_feedback": final_review_feedback or None,
            "review_findings": final_review_findings,
            "review_ok": final_review_result.returncode == 0 and not review_has_findings(final_review_feedback),
            "warnings": final_review_warnings,
            "prompt_path": str(final_prompt_path),
            "stdout_path": str(final_stdout_path),
            "stderr_path": str(final_stderr_path),
            "policy_path": str(final_policy_path),
            "flash_review_result": None,
            "flash_review_feedback": None,
            "flash_review_findings": [],
            "review_model_used": request.final_review_model,
            "review_tier": "pro",
            "escalation_reason": "safe mode final review always uses pro",
        }

    flash_review_prompt = build_review_prompt(task, workspace, changed_files, tests, "final", request.review_model)
    flash_prompt_path = log_dir / "gemini_final_review_flash_prompt.txt"
    write_text(flash_prompt_path, flash_review_prompt)
    flash_review_result, _, flash_review_warnings = run_executor(
        "gemini",
        "review",
        workspace,
        flash_review_prompt,
        log_dir,
        timeout,
        codex_model,
        request.review_model,
        [final_policy_path],
        "final_review_flash",
    )
    flash_stdout_path = log_dir / "gemini_final_review_flash.stdout.log"
    flash_stderr_path = log_dir / "gemini_final_review_flash.stderr.log"
    write_text(flash_stdout_path, flash_review_result.stdout)
    write_text(flash_stderr_path, flash_review_result.stderr)
    flash_review_feedback = extract_executor_text(flash_review_result)
    flash_review_findings = parse_structured_review_findings(flash_review_feedback)

    should_escalate, escalation_reason = should_escalate_review_to_pro(
        request.quality_mode,
        task,
        changed_files,
        any(test.get("returncode") not in (0, None) for test in tests),
        flash_review_feedback,
        flash_review_findings,
        "final",
    )

    final_review_result = flash_review_result
    final_review_feedback = flash_review_feedback
    final_review_findings = flash_review_findings
    final_review_warnings = flash_review_warnings
    review_model_used = request.review_model
    review_tier = "flash_lite"
    if should_escalate:
        pro_review_prompt = build_pro_escalation_prompt(
            task,
            workspace,
            changed_files,
            tests,
            flash_review_feedback,
            flash_review_findings,
            escalation_reason or "flash-lite final review requested pro confirmation",
        )
        pro_prompt_path = log_dir / "gemini_final_review_pro_prompt.txt"
        write_text(pro_prompt_path, pro_review_prompt)
        final_review_result, _, pro_review_warnings = run_executor(
            "gemini",
            "review",
            workspace,
            pro_review_prompt,
            log_dir,
            timeout,
            codex_model,
            request.final_review_model,
            [final_policy_path],
            "final_review",
        )
        final_stdout_path = log_dir / "gemini_final_review.stdout.log"
        final_stderr_path = log_dir / "gemini_final_review.stderr.log"
        write_text(final_stdout_path, final_review_result.stdout)
        write_text(final_stderr_path, final_review_result.stderr)
        final_review_feedback = extract_executor_text(final_review_result)
        final_review_findings = parse_structured_review_findings(final_review_feedback)
        final_review_warnings = flash_review_warnings + pro_review_warnings
        review_model_used = request.final_review_model
        review_tier = "pro"
    else:
        final_stdout_path = flash_stdout_path
        final_stderr_path = flash_stderr_path

    return {
        "review_result": final_review_result.to_json(),
        "review_feedback": final_review_feedback or None,
        "review_findings": final_review_findings,
        "review_ok": final_review_result.returncode == 0 and not review_has_findings(final_review_feedback),
        "warnings": final_review_warnings,
        "prompt_path": str(flash_prompt_path),
        "stdout_path": str(final_stdout_path),
        "stderr_path": str(final_stderr_path),
        "policy_path": str(final_policy_path),
        "flash_review_result": flash_review_result.to_json(),
        "flash_review_feedback": flash_review_feedback or None,
        "flash_review_findings": flash_review_findings,
        "review_model_used": review_model_used,
        "review_tier": review_tier,
        "escalation_reason": escalation_reason if should_escalate else None,
    }


def extract_executor_text(result: CommandResult) -> str:
    text = (result.stdout or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        for key in ("response", "output", "message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return text


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def compact_review_feedback(text: str | None, limit: int = 1200) -> str | None:
    if not text:
        return None
    normalized = " ".join(text.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "... [truncated; read the full handoff for complete feedback]"


def handoff_object(payload: dict[str, Any]) -> dict[str, Any]:
    handoff = payload.get("handoff")
    return handoff if isinstance(handoff, dict) else payload


def handoff_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    handoff = handoff_object(payload)
    metadata = handoff.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def detail_command(handoff_path: Path | str, section: str) -> str:
    script_path = Path(__file__).resolve()
    return (
        f"python3 -B {shlex.quote(str(script_path))} "
        f"--read-handoff {shlex.quote(str(handoff_path))} --section {shlex.quote(section)}"
    )


def build_orchestrator_brief(
    summary: dict[str, Any],
    handoff: dict[str, Any],
    handoff_path: Path,
    summary_path: Path,
    brief_path: Path,
) -> dict[str, Any]:
    metadata = handoff_metadata(handoff)
    review_findings = metadata.get("review_findings") or []
    step_review_findings = metadata.get("step_review_findings") or []
    changed_files = metadata.get("changed_files") or []
    test_results = metadata.get("test_results") or []
    success = summary.get("success")
    if success is None:
        success = handoff.get("handoff_status") == "passed"
    return {
        "success": bool(success),
        "handoff_status": handoff.get("handoff_status"),
        "followup_required": bool(metadata.get("followup_required", summary.get("followup_required", False))),
        "next_recommended_action": metadata.get("next_recommended_action") or summary.get("next_recommended_action"),
        "summary": handoff.get("summary"),
        "counts": {
            "changed_files": len(changed_files) if isinstance(changed_files, list) else 0,
            "tests": len(test_results) if isinstance(test_results, list) else 0,
            "review_findings": len(review_findings) if isinstance(review_findings, list) else 0,
            "step_review_findings": len(step_review_findings) if isinstance(step_review_findings, list) else 0,
            "attempts": metadata.get("attempt_count"),
        },
        "paths": {
            "handoff": str(handoff_path),
            "summary": str(summary_path),
            "brief": str(brief_path),
            "log_dir": metadata.get("log_dir") or summary.get("log_dir"),
        },
        "available_sections": list(HANDOFF_SECTIONS),
        "detail_commands": {
            section: detail_command(handoff_path, section)
            for section in HANDOFF_SECTIONS
            if section != "brief"
        },
    }


def build_stdout_summary(summary: dict[str, Any]) -> dict[str, Any]:
    brief = summary.get("orchestrator_brief", {})
    counts = brief.get("counts", {}) if isinstance(brief, dict) else {}
    return {
        "success": summary.get("success"),
        "executor": summary.get("executor"),
        "mode": summary.get("mode"),
        "quality_mode": summary.get("quality_mode"),
        "review_policy": summary.get("review_policy"),
        "review_performed": summary.get("review_performed"),
        "followup_required": summary.get("followup_required"),
        "handoff_status": summary.get("handoff_status"),
        "models": summary.get("models"),
        "counts": counts,
        "tests_ran": summary.get("tests_ran"),
        "tests_success": summary.get("tests_success"),
        "attempt_count": summary.get("attempt_count"),
        "fixup_rounds_used": summary.get("fixup_rounds_used"),
        "warnings": summary.get("warnings", []),
        "paths": {
            "handoff": summary.get("handoff_path"),
            "brief": summary.get("orchestrator_brief_path"),
            "log_dir": summary.get("log_dir"),
        },
        "next_recommended_action": summary.get("next_recommended_action"),
    }


def build_handoff_section(payload: dict[str, Any], section: str) -> dict[str, Any]:
    handoff = handoff_object(payload)
    metadata = handoff_metadata(payload)
    if section == "full":
        return handoff
    if section == "brief":
        handoff_path = metadata.get("handoff_path") or payload.get("handoff_path") or ""
        summary_path = str(Path(handoff_path).with_name("summary.json")) if handoff_path else ""
        brief_path = str(Path(handoff_path).with_name("orchestrator_brief.json")) if handoff_path else ""
        return build_orchestrator_brief(payload, handoff, Path(handoff_path), Path(summary_path), Path(brief_path))
    if section == "findings":
        return {
            "handoff_status": handoff.get("handoff_status"),
            "followup_required": metadata.get("followup_required"),
            "review_findings": metadata.get("review_findings", []),
            "step_review_findings": metadata.get("step_review_findings", []),
            "review_feedback_excerpt": compact_review_feedback(metadata.get("review_feedback")),
            "step_review_feedback_excerpt": compact_review_feedback(metadata.get("step_review_feedback")),
            "next_recommended_action": metadata.get("next_recommended_action"),
        }
    if section == "tests":
        return {
            "tests_ran": metadata.get("tests_ran"),
            "tests_success": metadata.get("tests_success"),
            "test_results": metadata.get("test_results", []),
        }
    if section == "changed_files":
        return {
            "changed_files": metadata.get("changed_files", []),
            "post_state": metadata.get("post_state"),
        }
    if section == "attempts":
        return {
            "attempt_count": metadata.get("attempt_count"),
            "fixup_rounds_used": metadata.get("fixup_rounds_used"),
            "max_fixup_rounds": metadata.get("max_fixup_rounds"),
            "attempts": metadata.get("attempts", []),
        }
    if section == "logs":
        return {
            "log_dir": metadata.get("log_dir"),
            "handoff_path": metadata.get("handoff_path"),
            "cleanup_log": metadata.get("cleanup_log", []),
            "workspace_path": metadata.get("workspace_path"),
            "workspace_lock": metadata.get("workspace_lock"),
        }
    raise ValueError(f"Unknown handoff section: {section}")


def read_handoff_section(handoff_path: str, section: str) -> int:
    path = Path(handoff_path).expanduser().resolve()
    payload = load_json_object(str(path))
    result = build_handoff_section(payload, section)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def collect_changed_files(workspace: Path) -> list[dict]:
    result = run_command(
        ["git", "-C", str(workspace), "status", "--short", "--", ".", LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
    )
    changed = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        status = line[:2]
        path = line[3:] if len(line) > 3 else ""
        if path.startswith(".hermes/delegate-runs/"):
            continue
        if path and Path(path).suffix == ".lock":
            continue
        if is_generated_path(Path(path)):
            continue
        rel = Path(path)
        full = workspace / rel
        if status.strip() == "??" and full.is_dir():
            for child in sorted(full.rglob("*")):
                if not child.is_file():
                    continue
                child_rel = child.relative_to(workspace)
                if str(child_rel).startswith(".hermes/delegate-runs/"):
                    continue
                if child_rel.suffix == ".lock":
                    continue
                if is_sensitive_name(child_rel) or is_generated_path(child_rel):
                    continue
                changed.append({"status": "??", "path": str(child_rel)})
            continue
        changed.append({"status": status.strip(), "path": path})
    return changed


def run_git_capture(cmd: list[str], workspace: Path, timeout: int, output_path: Path) -> CommandResult:
    result = run_command(cmd, workspace, timeout)
    write_text(output_path, result.stdout + (("\nSTDERR:\n" + result.stderr) if result.stderr else ""))
    return result


def collect_diff(workspace: Path, log_dir: Path) -> dict:
    status = run_git_capture(
        ["git", "-C", str(workspace), "status", "--short", "--", ".", LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
        log_dir / "git_status_short.txt",
    )
    stat = run_git_capture(
        ["git", "-C", str(workspace), "diff", "--stat", "--", ".", LOCK_EXCLUDE_PATHSPEC, LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
        log_dir / "git_diff_stat.txt",
    )
    diff = run_git_capture(
        ["git", "-C", str(workspace), "diff", "--", ".", LOCK_EXCLUDE_PATHSPEC, LOG_EXCLUDE_PATHSPEC],
        workspace,
        30,
        log_dir / "git_diff.patch",
    )

    untracked_patches = []
    for item in collect_changed_files(workspace):
        if item["status"] != "??":
            continue
        rel = Path(item["path"])
        if is_sensitive_name(rel) or rel.match("*.lock"):
            continue
        full = workspace / rel
        if not full.is_file() or full.stat().st_size > 512_000:
            continue
        patch = run_command(["git", "diff", "--no-index", "--", "/dev/null", str(full)], workspace, 30)
        if patch.stdout:
            untracked_patches.append(patch.stdout)

    if untracked_patches:
        with (log_dir / "git_diff.patch").open("a", encoding="utf-8") as handle:
            handle.write("\n".join(untracked_patches))

    return {
        "status": status.to_json(),
        "diff_stat": stat.to_json(),
        "diff": diff.to_json(),
    }


def has_any_file(workspace: Path, patterns: Iterable[str]) -> bool:
    for pattern in patterns:
        if any(workspace.glob(pattern)):
            return True
    return False


def run_detected_tests(workspace: Path, timeout: int) -> list[dict]:
    tests: list[CommandResult] = []

    has_python_tests = has_any_file(workspace, ["tests/**/*.py", "test_*.py", "**/test_*.py"])
    if has_python_tests:
        tests.append(run_command(["python3", "-m", "pytest", "-q"], workspace, timeout))
        if tests[-1].returncode != 0 and "No module named pytest" in (tests[-1].stderr + tests[-1].stdout):
            tests.append(run_command(["python3", "-m", "unittest", "discover", "-v"], workspace, timeout))

    package_json = workspace / "package.json"
    if package_json.exists():
        if (workspace / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
            tests.append(run_command(["pnpm", "test"], workspace, timeout))
        elif shutil.which("npm"):
            tests.append(run_command(["npm", "test"], workspace, timeout))

    if (workspace / "CMakeLists.txt").exists():
        tests.append(CommandResult(
            cmd=["cmake", "--build", "<existing-build-dir>"],
            cwd=str(workspace),
            returncode=None,
            stdout="Skipped: CMake project detected, but no existing build instructions were inferred safely.",
            stderr="",
            duration_sec=0,
        ))

    return [test.to_json() for test in tests]


def cleanup_generated_artifacts(workspace: Path) -> list[str]:
    removed: list[str] = []
    workspace = workspace.resolve()
    for name in CACHE_DIR_NAMES:
        for path in workspace.rglob(name):
            resolved = path.resolve()
            if not path_is_relative_to(resolved, workspace) or not path.is_dir():
                continue
            shutil.rmtree(resolved, ignore_errors=True)
            removed.append(str(resolved.relative_to(workspace)))
    return sorted(set(removed))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delegate coding tasks to local Codex CLI or Gemini CLI.")
    parser.add_argument("--read-handoff", help="Read a compact section from an existing handoff.json or summary.json and exit.")
    parser.add_argument("--section", choices=HANDOFF_SECTIONS, default="brief", help="Section to read with --read-handoff. Default: brief.")
    parser.add_argument("--request-json", help="Hermes/Feishu request JSON file.")
    parser.add_argument("--repo", help="Repository or workspace path.")
    parser.add_argument("--task-file", help="Path to a file containing the user task.")
    parser.add_argument("--executor", choices=["auto", "codex", "gemini"])
    parser.add_argument("--mode", choices=["plan", "implement", "review"])
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--codex-model", help=f"Codex CLI model. Default: {DEFAULT_CODEX_MODEL}")
    parser.add_argument("--codex-reasoning-effort", help=f"Codex reasoning effort. Default: {DEFAULT_CODEX_REASONING_EFFORT}")
    parser.add_argument("--gemini-model", help=f"Gemini CLI implementation model. Default: {DEFAULT_GEMINI_MODEL}")
    parser.add_argument("--review-model", help=f"Gemini CLI step-review model. Default: {DEFAULT_GEMINI_REVIEW_MODEL}")
    parser.add_argument("--final-review-model", help=f"Gemini CLI final-review model. Default: {DEFAULT_GEMINI_FINAL_REVIEW_MODEL}")
    parser.add_argument("--quality-mode", choices=["auto", "fast", "safe"], help="Review strategy. fast minimizes review cost; safe keeps flash-lite review and escalates to pro only when needed.")
    parser.add_argument("--review", choices=["auto", "always", "never"], help="Gemini review policy after implementation.")
    parser.add_argument("--max-fixup-rounds", type=int, help="Maximum automatic fixup rounds after step review findings. Default: 2.")
    parser.add_argument("--check-health", action="store_true", default=None, help="Print a workspace/process health snapshot and exit without delegating work.")
    parser.add_argument("--stdout-mode", choices=["brief", "summary", "full"], help="What to print after a run. Default: brief.")
    parser.add_argument("--workspace-mode", choices=["direct", "worktree", "copy"], default=DEFAULT_WORKSPACE_MODE, help="Workspace handling mode. Default: direct.")
    parser.add_argument("--workspace", help="Exact isolated workspace/worktree path under /tmp. Must not already exist.")
    return parser.parse_args(argv)


def install_signal_handlers(cleanup_callback) -> dict[int, Any]:
    previous_handlers: dict[int, Any] = {}

    def handle_signal(signum: int, _frame: Any) -> None:
        cleanup_callback(f"signal:{signal.Signals(signum).name.lower()}")
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.getsignal(sig)
        signal.signal(sig, handle_signal)
    return previous_handlers


def restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for sig, handler in previous_handlers.items():
        signal.signal(sig, handler)


def error_summary(message: str, **extra: object) -> int:
    payload = {"success": False, "error": message, **extra}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 1


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.read_handoff:
        return read_handoff_section(args.read_handoff, args.section)

    timestamp = now_timestamp()
    workspace_lock_handle: Any | None = None
    workspace_lock_info: dict[str, Any] | None = None
    previous_signal_handlers: dict[int, Any] = {}
    cleanup_log: list[dict[str, Any]] = []

    try:
        reset_runtime_state()
        previous_signal_handlers = install_signal_handlers(cleanup_active_processes)

        request = normalize_request(args)
        repo = validate_repo_path(request.repo)
        if request.check_health:
            health = collect_runtime_health(repo)
            payload = {
                "success": True,
                "mode": "check-health",
                "repo": str(repo),
                "health": health,
                "next_recommended_action": "If the snapshot looks unhealthy, clear stale helper processes before starting a delegate run.",
            }
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return 0
        requested_workspace = validate_workspace_path(request.workspace) if request.workspace else None
        codex_available = shutil.which("codex") is not None
        gemini_available = shutil.which("gemini") is not None
        missing = require_tools()
        if missing:
            if not codex_available and not gemini_available:
                return error_summary(
                    "Missing executor(s).",
                    missing=missing,
                    requires_user_confirmation="No external executor is available; ask the user whether Hermes should implement directly.",
                )
            return error_summary("Missing executor(s).", missing=missing)

        executor = choose_executor(request.executor)

        workspace, branch, workspace_type, warnings, setup_results = create_workspace(
            repo,
            timestamp,
            request.timeout,
            request.workspace_mode,
            requested_workspace,
        )
        workspace_lock_handle, workspace_lock_info = acquire_workspace_lock(workspace, timestamp)
        log_dir = workspace / ".hermes" / "delegate-runs" / timestamp
        log_dir.mkdir(parents=True, exist_ok=True)

        task = request.task
        pre_state = capture_git_state(workspace, log_dir, "pre", include_stat=False)
        changed_files: list[dict] = []
        tests: list[dict] = []
        tests_failed = False
        post_state: dict[str, Any] | None = None
        step_review_performed = False
        step_review_result: dict[str, Any] | None = None
        step_review_feedback = ""
        step_review_findings: list[dict[str, object]] = []
        final_review_performed = False
        final_review_result: dict[str, Any] | None = None
        final_review_feedback = ""
        final_review_findings: list[dict[str, object]] = []
        final_review_ok = False
        final_review_model_used: str | None = None
        final_review_tier: str | None = None
        final_review_escalation_reason: str | None = None
        flash_final_review_result: dict[str, Any] | None = None
        flash_final_review_feedback = ""
        flash_final_review_findings: list[dict[str, object]] = []
        executor_result_json: dict[str, Any] | None = None
        last_message_path: str | None = None
        attempt_history: list[dict[str, Any]] = []
        primary_gemini_model = request.final_review_model if request.mode == "review" else request.gemini_model
        executor_selection_gemini_model = request.final_review_model if request.mode == "review" else request.gemini_model

        if request.mode == "review":
            final_data = run_final_review_round(
                request,
                workspace,
                log_dir,
                task,
                collect_changed_files(workspace),
                [],
                request.timeout,
                request.codex_model,
            )
            warnings.extend(final_data["warnings"])
            final_review_performed = True
            final_review_result = final_data["review_result"]
            final_review_feedback = final_data["review_feedback"] or ""
            final_review_findings = final_data["review_findings"]
            final_review_ok = final_data["review_ok"]
            final_review_model_used = final_data.get("review_model_used")
            final_review_tier = final_data.get("review_tier")
            final_review_escalation_reason = final_data.get("escalation_reason")
            flash_final_review_result = final_data.get("flash_review_result")
            flash_final_review_feedback = final_data.get("flash_review_feedback") or ""
            flash_final_review_findings = final_data.get("flash_review_findings") or []
            changed_files = collect_changed_files(workspace)
            post_state = capture_git_state(workspace, log_dir, "post", include_stat=True)
            cleanup_active_processes("normal_exit")
            cleanup_log = list(CLEANUP_EVENTS)
            executor_result_json = final_review_result
            last_message_path = None
            tests = []
            tests_failed = False
        else:
            current_prompt = build_delegate_prompt(
                task,
                workspace,
                executor,
                request.mode,
                request.quality_mode,
                request.codex_model,
                request.gemini_model,
                request.review_model,
                request.final_review_model,
                request.codex_reasoning_effort,
                request.workspace_mode,
            )
            for round_index in range(1, request.max_fixup_rounds + 2):
                if round_index == 1:
                    round_prompt = current_prompt
                else:
                    previous_attempt = attempt_history[-1]
                    round_prompt = build_fixup_prompt(
                        task,
                        workspace,
                        previous_attempt["changed_files"],
                        previous_attempt["tests"],
                        previous_attempt["step_review_feedback"] or "",
                        previous_attempt["step_review_findings"],
                        round_index,
                        request.max_fixup_rounds,
                    )
                attempt = run_implementation_round(
                    round_index,
                    request,
                    executor,
                    workspace,
                    log_dir,
                    round_prompt,
                    task,
                    request.codex_model,
                    request.gemini_model,
                    request.review_model,
                    request.timeout,
                )
                warnings.extend(attempt["warnings"])
                attempt_history.append(attempt)
                changed_files = attempt["changed_files"]
                tests = attempt["tests"]
                tests_failed = attempt["tests_failed"]
                step_review_performed = step_review_performed or attempt["review_performed"]
                if attempt["step_review_result"] is not None:
                    step_review_result = attempt["step_review_result"]
                    step_review_feedback = attempt["step_review_feedback"] or ""
                    step_review_findings = attempt["step_review_findings"]
                executor_result_json = attempt["executor_result"]
                last_message_path = attempt["last_message_path"]
                if not attempt["review_performed"]:
                    break
                if not attempt["step_review_has_findings"]:
                    break
                if round_index >= request.max_fixup_rounds + 1:
                    break

            if attempt_history:
                final_attempt = attempt_history[-1]
                if final_attempt["implementation_stdout_path"]:
                    write_text(
                        log_dir / "implementation.stdout.log",
                        Path(final_attempt["implementation_stdout_path"]).read_text(encoding="utf-8"),
                    )
                if final_attempt["implementation_stderr_path"]:
                    write_text(
                        log_dir / "implementation.stderr.log",
                        Path(final_attempt["implementation_stderr_path"]).read_text(encoding="utf-8"),
                    )
                if final_attempt["tests_path"]:
                    write_text(
                        log_dir / "tests.json",
                        Path(final_attempt["tests_path"]).read_text(encoding="utf-8"),
                    )
                if final_attempt["step_review_prompt_path"]:
                    write_text(
                        log_dir / "gemini_review_prompt.txt",
                        Path(final_attempt["step_review_prompt_path"]).read_text(encoding="utf-8"),
                    )
                if final_attempt["step_review_stdout_path"]:
                    write_text(
                        log_dir / "gemini_review.stdout.log",
                        Path(final_attempt["step_review_stdout_path"]).read_text(encoding="utf-8"),
                    )
                if final_attempt["step_review_stderr_path"]:
                    write_text(
                        log_dir / "gemini_review.stderr.log",
                        Path(final_attempt["step_review_stderr_path"]).read_text(encoding="utf-8"),
                    )

            final_data = run_final_review_round(
                request,
                workspace,
                log_dir,
                task,
                changed_files,
                tests,
                request.timeout,
                request.codex_model,
            )
            warnings.extend(final_data["warnings"])
            final_review_performed = True
            final_review_result = final_data["review_result"]
            final_review_feedback = final_data["review_feedback"] or ""
            final_review_findings = final_data["review_findings"]
            final_review_ok = final_data["review_ok"]
            final_review_model_used = final_data.get("review_model_used")
            final_review_tier = final_data.get("review_tier")
            final_review_escalation_reason = final_data.get("escalation_reason")
            flash_final_review_result = final_data.get("flash_review_result")
            flash_final_review_feedback = final_data.get("flash_review_feedback") or ""
            flash_final_review_findings = final_data.get("flash_review_findings") or []
            post_state = capture_git_state(workspace, log_dir, "post", include_stat=True)
            cleanup_active_processes("normal_exit")
            cleanup_log = list(CLEANUP_EVENTS)

        if post_state is None:
            post_state = capture_git_state(workspace, log_dir, "post", include_stat=True)
        tests_ran = bool(tests)
        attempt_summaries = [
            {
                "round": attempt["round"],
                "phase": attempt["phase"],
                "review_performed": attempt["review_performed"],
                "step_review_has_findings": attempt["step_review_has_findings"],
                "step_review_ok": attempt["step_review_ok"],
                "tests_failed": attempt["tests_failed"],
                "prompt_path": attempt["prompt_path"],
                "implementation_stdout_path": attempt["implementation_stdout_path"],
                "implementation_stderr_path": attempt["implementation_stderr_path"],
                "tests_path": attempt["tests_path"],
                "step_review_prompt_path": attempt["step_review_prompt_path"],
                "step_review_stdout_path": attempt["step_review_stdout_path"],
                "step_review_stderr_path": attempt["step_review_stderr_path"],
            }
            for attempt in attempt_history
        ]
        if final_review_result is None:
            final_review_result = step_review_result
        if not final_review_feedback and step_review_feedback:
            final_review_feedback = step_review_feedback
            final_review_findings = step_review_findings
        review_ok = final_review_ok if request.mode == "implement" else (
            final_review_result is None or (
                final_review_result.get("returncode") == 0 and not review_has_findings(final_review_feedback)
            )
        )
        followup_required = not review_ok
        review_result = final_review_result
        review_feedback = final_review_feedback or None
        review_performed = step_review_performed or final_review_performed
        fixup_rounds_used = max(0, len(attempt_history) - 1)
        handoff_path = log_dir / "handoff.json"
        summary_path = log_dir / "summary.json"
        brief_path = log_dir / "orchestrator_brief.json"
        handoff_kind = "final_review"
        handoff_status = "passed" if review_ok else "needs_followup"
        handoff_summary = (
            f"Implementation complete after {len(attempt_history) or 1} implementation attempt(s); final review passed with PASS."
            if review_ok
            else f"Final review found {len(final_review_findings) or 1} issue(s); start a new delegate run with the structured handoff."
        )
        next_recommended_action = (
            "Inspect the reported diff and logs, then ask before applying, committing, pushing, or deploying."
            if review_ok
            else f"Start a new delegate run with {handoff_path} as the structured handoff; do not chain an automatic fixup in the same run."
        )
        handoff = {
            "handoff_version": 1,
            "handoff_kind": handoff_kind,
            "handoff_status": handoff_status,
            "summary": handoff_summary,
            "metadata": {
                "task": task.strip(),
                "source_repo": str(repo),
                "workspace_path": str(workspace),
                "workspace_mode": request.workspace_mode,
                "quality_mode": request.quality_mode,
                "branch": branch,
                "executor": executor,
                "mode": request.mode,
                "review_stage": "final" if request.mode == "review" or final_review_performed else "step",
                "review_policy": request.review,
                "models": {
                    "codex": request.codex_model,
                    "implementation": request.gemini_model,
                    "step_review": request.review_model,
                    "final_review": request.final_review_model,
                    "selected": request.codex_model if executor == "codex" else executor_selection_gemini_model,
                },
                "attempt_count": len(attempt_history) or 1,
                "fixup_rounds_used": fixup_rounds_used,
                "max_fixup_rounds": request.max_fixup_rounds,
                "attempts": attempt_summaries,
                "changed_files": changed_files,
                "tests_ran": tests_ran,
                "tests_success": (not tests_failed) if tests_ran else None,
                "test_results": tests,
                "step_review_performed": step_review_performed,
                "step_review_result": step_review_result,
                "step_review_feedback": step_review_feedback or None,
                "step_review_findings": step_review_findings,
                "final_review_performed": final_review_performed,
                "final_review_result": final_review_result,
                "review_result": review_result,
                "review_feedback": review_feedback,
                "review_findings": final_review_findings,
                "review_model_used": final_review_model_used,
                "review_tier": final_review_tier,
                "review_escalation_reason": final_review_escalation_reason,
                "flash_review_result": flash_final_review_result,
                "flash_review_feedback": flash_final_review_feedback or None,
                "flash_review_findings": flash_final_review_findings,
                "followup_required": followup_required,
                "next_recommended_action": next_recommended_action,
                "cleanup_log": cleanup_log,
                "log_dir": str(log_dir),
                "handoff_path": str(handoff_path),
                "workspace_lock": workspace_lock_info,
                "pre_state": pre_state,
                "post_state": post_state,
            },
        }
        summary = {
            "success": executor_result_json is not None and not tests_failed and review_ok,
            "executor": executor,
            "mode": request.mode,
            "quality_mode": request.quality_mode,
            "models": {
                "codex": request.codex_model,
                "implementation": request.gemini_model,
                "step_review": request.review_model,
                "final_review": request.final_review_model,
                "selected": request.codex_model if executor == "codex" else executor_selection_gemini_model,
            },
            "review_policy": request.review,
            "review_performed": review_performed,
            "review_result": review_result,
            "review_feedback": review_feedback,
            "step_review_performed": step_review_performed,
            "step_review_result": step_review_result,
            "step_review_feedback": step_review_feedback or None,
            "step_review_findings": step_review_findings,
            "final_review_performed": final_review_performed,
            "final_review_result": final_review_result,
            "final_review_feedback": final_review_feedback or None,
            "final_review_findings": final_review_findings,
            "final_review_model_used": final_review_model_used,
            "final_review_tier": final_review_tier,
            "final_review_escalation_reason": final_review_escalation_reason,
            "flash_final_review_result": flash_final_review_result,
            "flash_final_review_feedback": flash_final_review_feedback or None,
            "flash_final_review_findings": flash_final_review_findings,
            "followup_required": followup_required,
            "handoff_version": handoff["handoff_version"],
            "handoff_kind": handoff["handoff_kind"],
            "handoff_status": handoff["handoff_status"],
            "cleanup_log": cleanup_log,
            "source_repo": str(repo),
            "workspace_path": str(workspace),
            "workspace_type": workspace_type,
            "workspace_mode": request.workspace_mode,
            "branch": branch,
            "log_dir": str(log_dir),
            "workspace_lock": workspace_lock_info,
            "last_message_path": last_message_path,
            "pre_state": pre_state,
            "post_state": post_state,
            "changed_files": changed_files,
            "tests_ran": tests_ran,
            "tests_success": (not tests_failed) if tests_ran else None,
            "test_results": tests,
            "executor_result": executor_result_json,
            "implementation_result": executor_result_json,
            "setup_results": [result.to_json() for result in setup_results],
            "warnings": warnings,
            "attempt_history": attempt_summaries,
            "attempt_count": len(attempt_history) or 1,
            "fixup_rounds_used": fixup_rounds_used,
            "max_fixup_rounds": request.max_fixup_rounds,
            "handoff": handoff,
            "handoff_path": str(handoff_path),
            "next_recommended_action": next_recommended_action,
        }
        if executor_result_json is None and final_review_result is not None:
            executor_result_json = final_review_result
            summary["executor_result"] = executor_result_json
            summary["implementation_result"] = executor_result_json
        orchestrator_brief = build_orchestrator_brief(summary, handoff, handoff_path, summary_path, brief_path)
        summary["orchestrator_brief"] = orchestrator_brief
        summary["orchestrator_brief_path"] = str(brief_path)
        write_text(handoff_path, json.dumps(handoff, indent=2, ensure_ascii=False))
        write_text(summary_path, json.dumps(summary, indent=2, ensure_ascii=False))
        write_text(brief_path, json.dumps(orchestrator_brief, indent=2, ensure_ascii=False))
        if request.stdout_mode == "brief":
            stdout_payload = orchestrator_brief
        elif request.stdout_mode == "summary":
            stdout_payload = build_stdout_summary(summary)
        else:
            stdout_payload = summary
        print(json.dumps(stdout_payload, indent=2, ensure_ascii=False))
        return 0 if summary["success"] else 1

    except Exception as exc:
        return error_summary(str(exc))
    finally:
        if not cleanup_log:
            cleanup_log.extend(cleanup_active_processes("shutdown"))
        release_workspace_lock(workspace_lock_handle)
        if previous_signal_handlers:
            restore_signal_handlers(previous_signal_handlers)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
