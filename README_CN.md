<div align="right">
  <sub>
    <a href="README.md">English</a> |
    <strong>中文</strong>
  </sub>
</div>

# Bounded Coding Delegation

Bounded Coding Delegation 是一个 Hermes skill 和本地 Python helper，用来把仓库内的编码任务委派给 Gemini CLI、Codex CLI 这类 AI 编码 CLI。

它围绕一个核心原则设计：

**Orchestrator 负责决定任务边界，helper 负责执行循环。**

也就是说，昂贵的父级 agent 不应该盯着每一次实现、测试、review、重试和清理。helper 在本地跑一个有边界的流程，写入结构化产物，并返回一个很小的 JSON brief，方便父级 orchestrator 低成本解析。

## 解决什么问题

本地 coding-agent 工作流经常栽在一些无聊但很痛的地方：

| 问题 | 常见故障 | 这个 helper 的做法 |
|:--|:--|:--|
| 进程泄漏 | 子 CLI 在中断、超时或 pipe 卡住后残留 | 跟踪进程组，并在 timeout、shutdown、cleanup 时回收 |
| token 膨胀 | orchestrator 读完整日志、review 输出和大 handoff JSON | stdout 只打印紧凑 brief，细节留在磁盘 |
| 缺少可见性 | 长时间运行时终端看起来像卡死 | 把 progress heartbeat 写到 stderr，不污染 stdout JSON |
| 弱模型循环 | fast/flash 模型反复修同一个 review 问题 | 到达 fixup 边界后停止，并向父级发出 `needs_escalation` |
| review 策略临时化 | 每一步都让 orchestrator 重新判断 | `quality_mode` 每轮解析一次，然后执行确定性策略 |

## 核心行为

helper 把职责拆开：

- **实现**：默认使用 Gemini CLI 的 `gemini-3-flash-preview`；也可以显式选择 Codex CLI。
- **step review**：默认使用 `gemini-3.1-flash-lite-preview`，用于常规检查和 fixup 决策。
- **final review**：safe mode 使用 `gemini-3.1-pro-preview`；fast mode 先走轻量路径，只有风险足够时才升级。
- **父级升级**：如果 fixup 次数耗尽但 review 仍失败，helper 不会自己升级模型，而是返回 `needs_escalation`，让 Hermes 用更强模型重新提交。

## 执行流程

1. 校验目标仓库和 workspace 模式。
2. 创建或选择受控 workspace。
3. 构建受限 delegation prompt。
4. 通过 Gemini CLI 或 Codex CLI 执行实现。
5. 在合理范围内运行检测到的测试。
6. 按策略执行 step review。
7. 在 review 发现可修复问题时，执行有边界的 fixup 轮次。
8. 如果 fixup 次数耗尽且 review 仍失败，返回 `needs_escalation`。
9. 否则根据 `quality_mode` 执行 final review。
10. 写入 `orchestrator_brief.json`、`summary.json`、`handoff.json` 和日志。
11. 将最终 stdout payload 作为合法 JSON 打印出来。

## Progress Heartbeat

当 `stdout_mode` 是 `brief` 时，stdout 必须保持可机器解析。因此进度消息只写到 stderr：

```text
[Heartbeat] Starting Workspace: Preparing direct workspace for /path/to/repo.
[Heartbeat] Generating Implementation: Round 1: running gemini implementation.
[Heartbeat] Running Tests: Round 1: detecting and running configured tests.
[Heartbeat] Step Review [1]: Running first-pass review with gemini-3.1-flash-lite-preview.
[Heartbeat] Fixup Attempt [1]: Running targeted fixup with gemini.
[Heartbeat] Final Review: Running final review in safe mode.
[Heartbeat] Cleanup: Reaping active child processes after implementation run.
```

这样人和日志都能看到实时进度，同时 stdout 仍然只留给下游 JSON 解析。

## 两级交接

默认 stdout payload 很小：

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

完整产物保存在：

```text
.hermes/delegate-runs/<timestamp>/
```

orchestrator 只读取自己需要的 section：

```bash
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section findings
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section tests
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section changed_files
```

可用 section 包括 `brief`、`findings`、`tests`、`changed_files`、`attempts`、`logs` 和 `full`。

## 运行产物

每次 helper run 都会把中间文件写到：

```text
<workspace>/.hermes/delegate-runs/<timestamp>/
```

最终 stdout brief 里会包含准确的 `paths.log_dir`、`paths.summary` 和 `paths.handoff`。最常用的文件如下：

| 文件 | 含义 |
|:--|:--|
| `delegate_prompt.md` | 发给 executor 的初始实现 prompt。 |
| `delegate_prompt_round_N.md` | 第 `N` 轮 fixup 的实现 prompt。 |
| `implementation.stdout.log` / `implementation.stderr.log` | 最新一轮 implementation executor 输出。 |
| `implementation_round_N.stdout.log` / `implementation_round_N.stderr.log` | 第 `N` 轮 fixup 的 implementation executor 输出。 |
| `tests.json` | 最新一轮检测到的测试结果。 |
| `tests_round_N.json` | 第 `N` 轮 fixup 的测试结果。 |
| `gemini_review_prompt.txt` | 最新 step review prompt。 |
| `gemini_review.stdout.log` / `gemini_review.stderr.log` | 最新 step review 输出，通常来自 `gemini-3.1-flash-lite-preview`。 |
| `gemini_review_round_N.stdout.log` / `gemini_review_round_N.stderr.log` | 第 `N` 轮 fixup 的 step review 输出。 |
| `gemini_review_pro.stdout.log` / `gemini_review_pro.stderr.log` | step review 升级到 pro 确认时的输出。 |
| `gemini_review_pro_round_N.stdout.log` / `gemini_review_pro_round_N.stderr.log` | 第 `N` 轮 fixup 的 step review pro 确认输出。 |
| `gemini_final_review_flash.stdout.log` / `gemini_final_review_flash.stderr.log` | fast mode 下 final flash-lite review 的输出。 |
| `gemini_final_review.stdout.log` / `gemini_final_review.stderr.log` | safe mode 的 final pro review 输出，或 fast final review 升级到 pro 后的输出。 |
| `orchestrator_brief.json` | 给父级 orchestrator 消费的紧凑 payload。 |
| `summary.json` | 完整运行摘要，包含模型路由和产物路径。 |
| `handoff.json` | 用于 follow-up、escalation 或细节检查的结构化 handoff。 |

## Dynamic Escalation Signal

helper 是有边界的。如果 step review 在 fixup 预算耗尽后仍然失败，helper 会停止并返回：

```json
{
  "success": false,
  "handoff_status": "needs_escalation",
  "followup_required": true,
  "escalation_required": true,
  "next_recommended_action": "Task exceeded max fixup attempts with current executor. Recommend escalating to a stronger model (e.g., gemini-3.1-pro-preview) and resubmitting the task."
}
```

具体触发原因会写入 `summary.json` 的 `error`、`escalation_reason` 和 `escalation_error`。helper 不在内部自动升级模型；这个判断属于父级 orchestrator。

## 质量模式

| 模式 | 行为 |
|:--|:--|
| `auto` | 根据任务风险信号一次性解析成 `fast` 或 `safe` |
| `fast` | 尽量少做 step review；final review 先走 flash-lite，只有风险足够才调用 pro |
| `safe` | 默认运行 step review，允许高风险 step review 找 pro 确认，并且 final review 默认使用 pro，除非 circuit breaker 先停止 |

## 安装

克隆仓库后，把 skill 安装到 Hermes：

```bash
mkdir -p ~/.hermes/skills/devops
cp -R . ~/.hermes/skills/devops/delegate-coding-cli
chmod +x ~/.hermes/skills/devops/delegate-coding-cli/scripts/delegate_coding_cli.py
```

依赖：

- Python 3
- Git
- Gemini CLI 和/或 Codex CLI

至少需要一个 executor 可用。

## 使用

创建 request JSON：

```json
{
  "repo": "/path/to/repo",
  "task": "修复登录失败后没有清理 loading 状态的问题",
  "executor": "auto",
  "mode": "implement",
  "quality_mode": "auto",
  "review": "auto",
  "stdout_mode": "brief",
  "workspace_mode": "direct"
}
```

运行 helper：

```bash
python3 -B scripts/delegate_coding_cli.py --request-json /tmp/delegate-request.json
```

`stdout_mode` 只控制最终 stdout payload：

| 取值 | 输出 |
|:--|:--|
| `brief` | 最小 orchestrator JSON，默认且推荐 |
| `summary` | 紧凑运行摘要，不包含完整 handoff 或 executor payload |
| `full` | 旧版详细 summary，仅建议调试使用 |

常用 request 字段：

| 字段 | 取值 |
|:--|:--|
| `mode` | `plan`, `implement`, `review` |
| `executor` | `auto`, `gemini`, `codex` |
| `quality_mode` | `auto`, `fast`, `safe` |
| `workspace_mode` | `direct`, `worktree`, `copy` |
| `review` | `auto`, `always`, `never` |
| `max_fixup_rounds` | 非负整数 |

## 安全边界

helper 默认保守运行：

- 拒绝过大或敏感的仓库路径。
- 不 push、merge、deploy 或 publish。
- 在调用子 CLI 前清理敏感环境变量。
- prompt 先写入文件，再用 subprocess 调用，不拼 shell 字符串。
- 使用 workspace lock，避免两个 helper 同时改同一个 workspace。
- 跟踪子进程组，并在 timeout、interrupt、shutdown 时清理。

## 仓库结构

```text
.
├── SKILL.md
├── README.md
├── README_CN.md
├── LICENSE
└── scripts
    └── delegate_coding_cli.py
```

## 项目状态

这是从活跃 Hermes 工作流里抽出来的 MVP。目标不是替代高层 orchestrator，而是把本地执行层做得有边界、可观察、易交接。
