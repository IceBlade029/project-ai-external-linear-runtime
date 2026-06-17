# External Linear Runtime

External Linear Runtime（`elr`）是一个文件驱动、单线程、可恢复的外部调度运行时，用来协调面向人类的 Codex Coordinator，以及被调度执行的 Codex / Claude workers。

权力结构是：

```text
人类 <-> Codex Coordinator <-> elr runtime <-> Codex/Claude workers
```

- 人类主要和 Codex Coordinator 对话。
- runtime 是唯一状态权威和唯一调度器。
- workers 只接收 handoff 并写入要求的产物，不能自行推进流程。

## 快速开始

```powershell
python bin/elr init --profile product_tdd
python bin/elr status --json
python bin/elr plan --json
python bin/elr step --json
```

`elr init` 会创建 `.elr/`：

- `.elr/runtime/state.json`
- `.elr/workflow.json`
- `.elr/config.json`
- `.elr/templates/codex/`
- `.elr/templates/claude/`
- `.elr/handoffs/`
- `.elr/human_decisions/`
- `.elr/agent_outputs/`
- `.elr/logs/`
- `.elr/tasks/`
- `.elr/task_reports/`
- `.elr/tdd/`
- `.elr/gates/manifests/`
- `.elr/feedback/`
- `.elr/iteration_reports/`

如果只想运行最小三步示例，可以使用：

```powershell
python bin/elr init --profile minimal
```

## product_tdd 流程

`product_tdd` profile 的 phase 是：

```text
product_discovery -> planning -> execution -> iteration_review -> iteration_polishing -> backlog_update -> done
```

`execution` 是 task loop。worker turns 跑完以后，任务仍然必须通过硬门禁：

```powershell
python bin/elr task complete I1_T01 --json
```

## 任务和门禁命令

```powershell
python bin/elr task sync --json
python bin/elr task next --json
python bin/elr task context I1_T01 --json
python bin/elr task complete I1_T01 --json
```

```powershell
python bin/elr gate seal I1_T01 test_writer --json
python bin/elr gate check-integrity I1_T01 --json
python bin/elr gate check-boundary I1_T01 --json
python bin/elr gate run-validation I1_T01 --json
```

正常 `elr step` 会在带 `gate_phase` 的 worker turn 成功后自动 seal。

## 人类决策

当 runtime 停在 `waiting_human` 时，Codex Coordinator 应该根据人类对话创建一个 decision JSON，然后提交给 runtime：

```powershell
python bin/elr decide --file decision.json --json
```

最小 decision 结构：

```json
{
  "id": "approve-plan-001",
  "type": "approval",
  "phase": "codex_planning",
  "task_id": null,
  "summary": "人类已批准当前计划。",
  "decision": "approved",
  "created_by": "codex_coordinator",
  "source": "human_conversation"
}
```

## Agent 命令

默认情况下，runtime 会调用：

- `codex exec <prompt_file>`
- `claude -p <prompt>`

项目级 agent 启动策略写在 `.elr/config.json`。查看当前配置：

```powershell
python bin/elr agent show --json
```

如果希望 Claude worker 在当前项目中自动跳过 Claude Code 的交互式权限确认，可以显式开启 autonomous 模式：

```powershell
python bin/elr agent configure claude --mode autonomous --json
```

这会让 runtime 后续使用：

```powershell
claude -p --dangerously-skip-permissions "<worker prompt>"
```

恢复默认策略：

```powershell
python bin/elr agent configure claude --mode default --json
```

建议只在可信项目目录、最好已经初始化 git 的情况下开启 autonomous 模式。开启后，主要安全边界来自 ELR 的 handoff、`allowed_write_paths`、validation、gate 和 human review。

测试或本地适配时，可以用 JSON 命令数组临时覆盖项目配置。环境变量优先级高于 `.elr/config.json`：

```powershell
$env:ELR_CODEX_CMD_JSON='["python","fake_agent.py","{prompt_file}"]'
$env:ELR_CLAUDE_CMD_JSON='["python","fake_agent.py","{prompt_file}"]'
```

可用占位符：`{prompt_file}`、`{prompt}`、`{handoff_id}`、`{output_dir}`。

## Codex Coordinator 工作流示例

### 人类说“继续”

Coordinator 先查看状态：

```powershell
python bin/elr status --json
```

如果返回 `status: "idle"`，可以运行：

```powershell
python bin/elr step --json
```

如果返回 `status: "waiting_human"`，不要运行 `step`，先收集人类 decision。

### 人类批准计划

当 runtime 停在 `waiting_human`，并且当前 `phase` 接受 `approval` 时，Coordinator 创建 decision JSON：

```json
{
  "id": "approve-codex-planning-001",
  "type": "approval",
  "phase": "codex_planning",
  "task_id": null,
  "summary": "人类批准 Codex 规划产物。",
  "decision": "批准当前规划，进入下一 phase。",
  "created_by": "codex_coordinator",
  "source": "human_conversation"
}
```

然后提交：

```powershell
python bin/elr decide --file approve-codex-planning-001.json --json
```

### worker blocked

当 `status` 为 `blocked` 时，Coordinator 先解释 `last_error` 和相关日志，不要手改 `.elr/runtime/state.json`。如果需要人类修正范围或补充信息，把反馈写成 `scope_change`、`polishing_feedback` 或 `clarification` decision，再交给 runtime 处理。
