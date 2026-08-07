# ASA Workflow Blocking Incident：workflow_8a57e861a20d

## 一、现象

- 2026-08-06 用户发现 ASA 工作流 `workflow_8a57e861a20d`（士兰微｜电源专家｜第3轮寻访）在前端/看板上仍显示为 **阻塞（blocked）** 状态。
- 后端数据库/API 查询显示 workflow 实际为 **`running`**，`current_stage=assessment`，`active_step_id=299`。

## 二、根因

### 1. 代码逻辑过严

`scripts/a_system_agent/workflow_handler.py` 中 `candidate_batch_assessment` 能力的阻塞判定逻辑过严：

- 旧逻辑：本轮评估中 **只要有候选人评估失败**，就把整轮标记为 `blocked=True`。
- 实际运行中，step 299 完成 42 人评估，8 人因为模型未返回合法 JSON 而失败，旧逻辑直接将该步骤标为 `blocked`，workflow 状态随之变成 `blocked`。

### 2. 前端缓存/状态刷新延迟

修复代码并发起重试后，后端状态已变为 `running`，但前端/看板若未刷新，仍可能展示旧的 `blocked` 状态。

## 三、修复

### 3.1 代码修复

文件：`scripts/a_system_agent/workflow_handler.py`（`candidate_batch_assessment` 能力）

变更：只有 **全部候选人都评估失败** 时，才将步骤标记为 `blocked`。

```python
all_failed = len(completed) == 0 and len(failed) > 0
return {
    ...,
    "blocked": all_failed,
    "missing_inputs": ["检查模型连接后重试失败评估"] if all_failed else [],
}
```

### 3.2 服务恢复

- 停止自动重启的 `launchd` 服务：`ai.hermes.liepin-workbench`（plist 位于 `/Users/messi/Library/LaunchAgents/ai.hermes.liepin-workbench.plist`）。
- 手动启动稳定的 `asa_core.app` 实例，监听 `127.0.0.1:8765`。
- 调用 resume API 恢复 workflow 执行。

## 四、验证

- 后端 API 返回 workflow 状态为 `running`：`curl http://127.0.0.1:8765/api/v1/dashboard`
- `agent_runs` 表持续有新的 `workflow_candidate_batch_assessment` run 产生，说明 step 299 正在实际执行评估。
- Step 299 状态为 `running`，尚未完成；output_json 仍为旧内容，待本次执行完成后会更新。

## 五、最终结果（2026-08-06 16:20）

- Workflow `workflow_8a57e861a20d` 状态变为 **`completed`**，`progress=1.0`。
- Step 299 状态变为 **`completed`**，`blocked=False`。
- 本轮评估结果：50 人中 **47 人成功评估，3 人失败**，不再被错误标为 blocked。
- 岗位当前累计已有 **138 位** 评估结果。

修复确认生效：代码修改后，部分评估失败不再导致整轮 workflow 阻塞。

## 六、后续注意

1. **阻塞判定标准**：`candidate_batch_assessment` 之后遇到部分失败不应再整体 blocked，只有全部失败才 blocked。
2. **服务重启影响**：`launchd` 自动重启会在启动时触发 `_recover_interrupted()`，将 running workflow/steps 重置为 paused/pending。排查 running workflow 异常时，先确认是否有外部服务在不断重启。
3. **前端状态刷新**：修复后若前端仍显示阻塞，优先检查缓存或手动刷新 dashboard。
