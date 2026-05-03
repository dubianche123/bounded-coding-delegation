<div align="right">
  <sub>
    <a href="README.md">English</a> |
    <strong>中文</strong>
  </sub>
</div>

# Bounded Coding Delegation

## 一个面向本地 AI 编码 CLI 的受控委派引擎

**版本**：1.0 MVP  
**作者**：Leo  
**状态**：Hermes skill / 本地 CLI helper

Bounded Coding Delegation 是一个 Hermes skill 和 Python helper，用来把仓库内的编码任务委派给 Gemini CLI、Codex CLI 这类本地编码工具。它专注处理 agentic coding 里最容易变乱的部分：进程清理、review 节奏、重试边界，以及返回给 orchestrator 的结构化交接。

核心原则很简单：

**Orchestrator 负责决定任务边界，helper 负责执行循环。**

与其让高成本的 orchestrator 逐步监督每一次实现和 review，不如让 Hermes 一次性启动一个有边界的 helper run。helper 会准备 workspace、执行实现、在可行时运行测试、做 step review 和 fixup 轮次、跑 final review，最后只向 stdout 输出很小的 orchestrator brief，并把完整的 `summary.json` 和 `handoff.json` 写到磁盘。

## 为什么要做这个

很多本地 agent 工作流会把 orchestrator 变成进程监督器。这既贵又脆弱。顶层 agent 要不断决定是否 review、是否重试、是否切更强的模型、子进程是不是还活着。会话一长，token 消耗会明显上升，进程泄漏也更难排查。

Bounded Coding Delegation 把可重复的执行策略下沉到一个小而确定的 helper 里：

| 问题 | 临时式委派 | Bounded Coding Delegation |
|:--|:--|:--|
| Review 节奏 | orchestrator 一步一步决定 | `quality_mode` 每轮只解析一次，得到 `fast` 或 `safe` |
| 模型路由 | 贵的 reviewer 容易被过度调用 | flash-lite 负责常规 step review，pro 留给 safe final review 和高风险升级 |
| 重试控制 | orchestrator 手动反复拉起任务 | helper 内部执行有边界的 fixup 轮次 |
| 进程清理 | 子 CLI 超时或中断后可能残留 | helper 跟踪进程组，并在超时、中断、退出时回收 |
| 交接方式 | 下一位 agent 读自然语言或整份大 JSON | helper 只输出最小 brief，细节按 section 按需读取 |

## 执行模型

helper 把三种职责分开：

- **实现**：默认使用 Gemini CLI 的 `gemini-3-flash-preview`，也可显式切换到 Codex CLI。
- **step review**：默认使用 `gemini-3.1-flash-lite-preview`，用于常规检查和有边界的 fixup 决策。
- **final review**：safe mode 下默认使用 `gemini-3.1-pro-preview`；fast mode 则先走轻量路径，只有 helper 判断风险足够高时才升级。

orchestrator 仍然重要，但它主要负责选择 repo、任务、模式和质量策略。之后，helper 接管执行循环，直到返回结构化 handoff。

## 质量模式

`quality_mode` 决定 review 预算：

| 模式 | 行为 |
|:--|:--|
| `auto` | helper 根据任务风险信号一次性判断为 `fast` 或 `safe` |
| `fast` | 尽量减少 step review；final review 先走 flash-lite，只有风险足够时才升级 |
| `safe` | 默认运行 step review，风险较高的 step review 可升级到 pro，final review 必定使用 pro |

这样能保留常见路径的速度，同时避免把每个任务都当成同一个 review 预算来处理。

## 执行流程

1. 校验仓库路径和 workspace 模式。
2. 创建或选择受控 workspace。
3. 构建受限的 delegation prompt。
4. 通过 Gemini CLI 或 Codex CLI 执行实现。
5. 在合理范围内运行测试。
6. 按策略执行 step review。
7. 在 review 发现可修复问题时，执行有边界的 fixup 轮次。
8. 根据质量模式执行 final review。
9. 写入日志、`orchestrator_brief.json`、`summary.json` 和 `handoff.json`。

## 两级交接

每次运行默认只向 stdout 输出最小 payload：

```json
{
  "success": true,
  "handoff_status": "passed",
  "followup_required": false,
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

orchestrator 需要细节时，只读取对应 section：

```bash
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section findings
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section tests
python3 -B scripts/delegate_coding_cli.py --read-handoff .hermes/delegate-runs/<timestamp>/handoff.json --section changed_files
```

可用 section 包括 `brief`、`findings`、`tests`、`changed_files`、`attempts`、`logs` 和 `full`。

完整 handoff 包含：

- 任务与 workspace 元数据
- 选用的 executor 和模型路由
- 变更文件
- 测试结果
- step review 的反馈和 findings
- final review 的反馈和 findings
- cleanup log
- `followup_required`
- `next_recommended_action`

下游 orchestrator 应该把 helper 执行视为无状态过程：启动 helper 后丢掉临时执行日志上下文，等 helper 结束后只读取 brief。只有 brief 指向确实需要细节时，再按 section 读取完整 JSON 的局部内容。

## 安装

先克隆仓库，再把 skill 安装到 Hermes 目录：

```bash
mkdir -p ~/.hermes/skills/devops
cp -R . ~/.hermes/skills/devops/delegate-coding-cli
chmod +x ~/.hermes/skills/devops/delegate-coding-cli/scripts/delegate_coding_cli.py
```

helper 需要：

- Python 3
- Git
- Gemini CLI 和/或 Codex CLI

至少要有一个 executor 可用。

## 使用

先写一个 request JSON 文件：

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

然后运行 helper：

```bash
python3 -B scripts/delegate_coding_cli.py --request-json /tmp/delegate-request.json
```

默认 stdout 是压缩后的 orchestrator brief。`stdout_mode: "summary"` 会输出不包含完整 handoff 和 executor payload 的紧凑运行摘要；`stdout_mode: "full"` 保留旧版大 stdout 行为，仅建议调试时使用。

常用字段：

| 字段 | 取值 |
|:--|:--|
| `mode` | `plan`, `implement`, `review` |
| `executor` | `auto`, `gemini`, `codex` |
| `quality_mode` | `auto`, `fast`, `safe` |
| `workspace_mode` | `direct`, `worktree`, `copy` |
| `review` | `auto`, `always`, `never` |
| `stdout_mode` | `brief`, `summary`, `full` |

## 安全边界

helper 会尽量保守地运行：

- 拒绝过大或敏感的仓库路径。
- 不 push、merge、deploy 或 publish。
- 在调用子 CLI 前清理敏感环境变量。
- prompt 先写入文件，再用 subprocess 调用，不拼 shell 字符串。
- 用 workspace lock 避免两个 helper 同时改同一个 workspace。
- 跟踪子进程组，并在超时或退出时清理。

## 仓库结构

```text
.
├── README.md
├── README_CN.md
├── LICENSE
└── scripts
    └── delegate_coding_cli.py
```

## 项目状态

这是从 Hermes 工作流里抽出来的 MVP。目标不是替代高层 orchestrator，而是把执行层做得可控、可检查、可复用。
