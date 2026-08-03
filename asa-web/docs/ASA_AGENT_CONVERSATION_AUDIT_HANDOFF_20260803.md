# ASA Agent 上下文对话工作区审计交接

日期：2026-08-03

交接对象：Kimi CLI

工作区兼容路径：`/Users/messi/Documents/ASA`

Git 单体仓库根目录：`/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence`

---

## 1. 审计目标

本轮把 ASA 的主要 AI 交互从 Copilot 原生浮窗迁移到 React Agent 对话工作区。请以代码审计和可复现实测为主，不以本文件中的“已完成”描述代替验证。

重点回答以下问题：

1. 正常启动时，用户是否只会进入 `ASA Agent` 主窗口，所有 ASA 业务入口是否都进入同一个 Agent 上下文对话界面。
2. 会话的新建、恢复、切换、搜索、重命名、归档、焦点恢复和焦点清除是否可靠，异步竞态是否会串任务或串消息。
3. SSE 失败、停止、重试是否保持原 `request_id` 和幂等键，是否可能重复创建工作流、重复审批或重复写候选人状态。
4. Core 是否仍是会话、上下文、工作流与审批的唯一事实来源，React 是否只承担交互和严格边界解析。
5. 候选人写操作和 R3 外部寻访审批是否完整保留既有安全链路。
6. 旧浮窗是否仅作为一个版本的兼容回滚实现存在，正常模式下没有可见或可调用入口。

## 2. 审计范围

### 2.1 主范围

前端 Agent：

- `asa-web/src/agent/AgentWorkspace.tsx`
- `asa-web/src/agent/conversationState.ts`
- `asa-web/src/agent/sessionModel.ts`
- `asa-web/src/agent/transport.ts`
- `asa-web/src/agent/AgentObjectEmbed.tsx`
- `asa-web/src/agent/navigation.ts`

前端接入与 API：

- `asa-web/src/app/App.tsx`
- `asa-web/src/main.tsx`
- `asa-web/src/shared/tabs.tsx`
- `asa-web/src/copilot/CopilotSurface.tsx`
- `asa-web/src/copilot/bridge.ts`
- `asa-web/src/api.ts`
- `asa-web/src/generated/api.d.ts`
- `asa-web/AGENTS.md`

Core 会话与接口：

- `scripts/a_system_agent/schema.py`
- `scripts/a_system_agent/copilot_handler.py`
- `scripts/asa_core/app.py`
- `scripts/asa_core/service.py`

原生 App：

- `asa-floating-app/src/AppDelegate.swift`
- `asa-floating-app/src/WebSecurityPolicy.swift`
- `asa-floating-app/scripts/build.sh`
- `asa-floating-app/tests/NativeBoundaryTests.swift`
- `asa-floating-app/README.md`

关键测试：

- `asa-web/src/__tests__/agent-state.test.ts`
- `asa-web/src/__tests__/agent-session-model.test.ts`
- `asa-web/src/__tests__/agent-transport.test.ts`
- `asa-web/src/__tests__/agent-workspace.test.tsx`
- `asa-web/src/__tests__/agent-navigation.test.ts`
- `asa-web/e2e/functional/agent-workspace.spec.ts`
- `tests/test_asa_core_v1.py`
- `tests/test_asa_floating_completion.py`

### 2.2 非主范围

当前工作树还包含分析工作区、寻访漏斗、查询构建器、X-SaaS 等未提交改动。它们只应作为 Agent 集成的回归面检查，不应在本次审计中顺手重构、回滚或格式化。

尤其不要改动或删除未跟踪文件：

`asa-web/text_claude.com_download_Claude_Code_desktop_macOS_official_20260730_100625.json`

## 3. 目标架构与事实来源

`asa-web/AGENTS.md` 已将旧 R12-b 约定替换为 **Agent Conversation Surface v1**：

- React `src/agent/` 是主交互面，负责消息、任务历史、对象卡和确认 UI。
- Core 是上下文仲裁、服务端会话、工作流、审批和业务写入的唯一事实来源。
- 从岗位、人选、工作流进入 Agent 时显式附着上下文；普通业务页面浏览不应悄悄改写已有任务焦点。
- `?surface=copilot` 直接进入主 Agent，不再转发到原生浮窗。
- `asa_floating_html`、原生 panel 和历史数据结构暂不删除，仅保留一个版本用于 `--compat-copilot` 回滚。

主导航预期固定为四项：`Agent / 岗位看板 / 人选进度 / 人选列表`。原“总览”的今日待处理、运行中、已交付、优先待办和固定分析入口已并入 Agent 空任务首页。

## 4. 前端实现要点

### 4.1 会话状态机

`conversationState.ts` 使用显式阶段：

- `idle`
- `restoring`
- `streaming`
- `failed`
- `stopped`

每次发送生成 `turnRequestId`。除任务恢复动作外，流式 `text/done/error/stop` 只有在 `requestId === activeRequestId` 时才可修改当前状态，旧请求的迟到事件应被丢弃。

重试不是新增一轮：同一 `request_id` 对应的失败 user/assistant 消息先被替换，再重新流式填充，避免界面重复回合。请重点审计失败发生在“已收到部分文本”和“收到 context 但没有 done”两种情况下的表现。

### 4.2 SSE 与幂等

`transport.ts` 对 `context`、`text`、`done`、`error` 使用 Zod 边界解析。发送请求为：

`POST /api/v1/copilot/stream`

请求体包含 `request_id`、`session_id`、`message`、`context`，Header 包含 `Idempotency-Key`。`createAgentTurn().retry()` 返回同一 turn，必须保持以下两项不变：

- 原 `request_id`
- 原 `Idempotency-Key`

首版仍是“后端完成后分段输出”的 SSE，不是模型逐 token 推理。流在未收到 `done` 时结束，应进入可重试失败态，不能误报成功。

### 4.3 会话与竞态

`AgentWorkspace.tsx` 的预期行为：

- 新任务只在首次发送后由服务端形成会话。
- 最近活跃任务写入 `localStorage` 的 `asaAgentSessionId`，App 重启后恢复。
- 桌面右栏显示任务历史和当前业务焦点；390px 窄屏使用任务抽屉。
- 支持任务搜索、内联重命名、两步归档和解除业务焦点。
- 网络写操作期间禁用冲突操作，防止重复点击。
- 会话恢复使用 generation/identity 防护；快速切换任务时，旧恢复结果不能覆盖新任务。
- 用户显式带着岗位、人选或工作流进入 Agent 时，如果与当前任务焦点冲突，必须显示冲突状态，不得静默合并。

### 4.4 消息内对象卡

`AgentObjectEmbed.tsx` 默认只渲染紧凑摘要，点击后才加载完整对象详情：

- 岗位卡：客户、岗位、优先级、漏斗、策略摘要。
- 人选卡：姓名、公司、职位、阶段及复核/推进/停止。
- 工作流卡：状态、进度、待审批项及批准/拒绝。

工作流状态统一调用 `src/workflow/statusMapping.ts` 的 `mapWorkflowStatus`，不得直接渲染英文 `business_outcome`。

候选人卡写入必须继续走：

`preflight -> 短期 token -> React alertdialog -> commit`

工作流 R3 外部寻访必须继续走既有 approval decision 接口和一次性授权语义。自然语言“可以”“批准”等文本不能被前端当作授权凭证；只有 Core 返回真实 `workflow_id` 时 UI 才能显示任务已启动。

## 5. Core 数据与 API

### 5.1 数据表

`scripts/a_system_agent/schema.py` 新增 `agent_copilot_sessions` 会话元数据表。消息正文仍由既有 `agent_copilot_messages` 保存。元数据用于标题、归档时间和会话生命周期，不应复制一份消息事实。

请检查初始化/迁移对已有正式库是否幂等，并检查外键缺失、孤儿会话、空标题、归档后恢复等边界。

### 5.2 接口

新增或扩展的接口：

```text
GET   /api/v1/copilot/sessions?limit=30&q=&include_archived=false
GET   /api/v1/copilot/sessions/{session_id}?limit=100
PATCH /api/v1/copilot/sessions/{session_id}
POST  /api/v1/copilot/stream
```

列表返回会话标题、预览、消息数、更新时间、最近上下文、`business_focus` 和归档状态。详情返回服务端消息及 `business_focus`。PATCH 支持标题、归档和焦点清除，要求 `request_id` 与 `Idempotency-Key`。

列表实现应为一次查询并 join 最近焦点，不允许每个会话再调用一次 `get_copilot_focus`。已有测试通过将该方法替换为抛错函数来守住 N+1 边界。

FastAPI 路由应有明确 request/response model；OpenAPI 已生成到 `asa-web/src/generated/api.d.ts`，并加入 API drift 锚点。请检查运行中 Core 的 OpenAPI 与当前生成文件，而不是只看静态类型。

## 6. 原生 App 与回滚边界

当前原生版本预期为：

- `CFBundleShortVersionString = 0.2.21`
- `CFBundleVersion = 44`
- 安装路径：`/Users/messi/Applications/ASA.app`

普通启动预期只创建标题为 `ASA Agent` 的主窗口，不注册 Copilot 菜单、全局热键或 Agent 页面可调用的浮窗 bridge。兼容启动方式：

```bash
open -na /Users/messi/Applications/ASA.app --args --compat-copilot
```

兼容模式允许旧 panel 被构建和显示，仅用于一个版本内回滚，不是主流程。

### 6.1 必查风险：诊断页残留入口

源码中仍可检索到诊断页 HTML 的 `native('showFloating')` 按钮和 native message handler 的 `showFloating` 分支。请不要因存在字符串就直接判定失败，也不要忽略它：

1. 在**不带** `--compat-copilot` 的普通模式触发服务不可用诊断页。
2. 确认页面不显示“显示 Copilot”入口，或点击后无法创建/显示 panel。
3. 检查 handler 本身是否受 `compatibilityCopilotEnabled` 保护。

若普通模式可见或可调用，该问题应按 P0/P1 报告；目标契约是“旧实现可回滚，但正常模式没有可见入口”。

## 7. 安全与一致性不变量

审计时请逐项给出“通过/失败/证据”：

- 全仓无 JS 原生 `prompt()`、`confirm()`、`alert()`。
- 候选人写入没有绕过 preflight/token/commit。
- preflight token 短期、单用途，失败或重复提交不会写两次。
- R3 外部寻访审批保持一次性授权，拒绝与批准不可双重生效。
- 重试沿用原 request/idempotency，不产生重复工作流或重复消息。
- 会话 PATCH 使用幂等键，快速双击重命名/归档/清焦点不会重复副作用。
- `business_focus` 以服务端恢复值为准，前端本地状态不成为第二事实来源。
- 未返回真实 `workflow_id` 时不显示“已启动”。
- 结构化对象卡数据经 TypeScript/Zod 边界解析；非法 SSE 不被当作成功内容。
- 正常 ASA 页面不调用 native `showFloating`，也不发布旧浮窗上下文。

## 8. 已有验证证据

实现收官时记录的结果如下，Kimi 应抽样或完整复跑，不要仅采信数字：

| 验证 | 记录结果 |
| --- | --- |
| `npm run ci` | 通过；200 个前端测试、58 个 contract 测试、13 个 functional E2E、14 个截图测试 |
| Core 定向回归 | 155 passed |
| 原生边界测试 | 通过 |
| `test_asa_floating_completion.py` | 32 passed |
| `git diff --check` | 通过 |
| 正式 Core health | 通过 |
| 正式 Core API drift | 通过 |
| App 签名 | `codesign --verify --deep --strict` 通过 |
| 正式 App | `0.2.21 (44)`，普通模式辅助功能窗口列表仅 `ASA Agent`，菜单无 Copilot 项 |

当前正式运行信息：

```text
Core: 127.0.0.1:8765
DB: /Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db
DB backup: /Users/messi/.hermes/backups/asa_v3/asa_v3_20260803_190515_manual.db
App: /Users/messi/Applications/ASA.app
```

本交接文档生成前再次读取 `/api/v1/health`，返回上述正式 DB；同时再次读取 App bundle 版本为 `0.2.21 (44)`，签名校验无错误输出。

## 9. 建议审计步骤

### 9.1 先读约束和工作树

```bash
cd /Users/messi/Documents/Codex/2026-06-18/liepin-intelligence
sed -n '1,220p' asa-web/AGENTS.md
git status --short
git diff --check
git diff --stat
```

不要执行 `git reset --hard`、`git checkout --`、全仓格式化或清理未跟踪文件。

### 9.2 聚焦查看本次 diff

```bash
git diff -- \
  asa-web/AGENTS.md \
  asa-web/src/agent \
  asa-web/src/app/App.tsx \
  asa-web/src/main.tsx \
  asa-web/src/shared/tabs.tsx \
  asa-web/src/copilot \
  asa-web/src/api.ts \
  asa-web/src/generated/api.d.ts \
  scripts/a_system_agent/schema.py \
  scripts/a_system_agent/copilot_handler.py \
  scripts/asa_core/app.py \
  scripts/asa_core/service.py \
  asa-floating-app/src/AppDelegate.swift \
  asa-floating-app/src/WebSecurityPolicy.swift \
  asa-floating-app/tests/NativeBoundaryTests.swift
```

注意：`src/agent/` 与部分测试仍是未跟踪文件，单独用 `sed`/`rg` 阅读；`git diff` 默认不会显示未跟踪文件内容。

### 9.3 静态守卫

```bash
cd /Users/messi/Documents/ASA
rg -n 'openCopilotWindow|publishCopilotContext|showFloating' src
rg -n '\b(prompt|confirm|alert)\s*\(' src --glob '*.{ts,tsx,js,jsx}'
rg -n 'business_outcome' src/agent src/workflows src/workflow
rg -n 'Idempotency-Key|request_id|preflight|commit|approval' src/agent ../scripts/asa_core ../scripts/a_system_agent
```

预期：React 业务入口不再使用旧浮窗 API；原生兼容实现中仍会存在 `showFloating`，需结合可达性判断。

### 9.4 自动化验证

```bash
cd /Users/messi/Documents/ASA
npm run ci:fast
npm run test:contract
npm run ci:e2e-functional
npm run ci
```

Core 定向回归可从单体仓库根执行：

```bash
cd /Users/messi/Documents/Codex/2026-06-18/liepin-intelligence
python3 -m pytest -q tests/test_asa_core_v1.py tests/test_asa_floating_completion.py
```

原生边界测试请按 `asa-floating-app/README.md` 和 `asa-floating-app/scripts/build.sh` 的现有方式执行，不要先修改版本号或覆盖已安装 App。

### 9.5 正式 App 手工/E2E 检查

1. 普通启动 `/Users/messi/Applications/ASA.app`，确认只有 `ASA Agent` 窗口且菜单无 Copilot。
2. 默认落在 Agent，导航正好四项。
3. 新建任务，发送后刷新 App，确认会话和焦点恢复。
4. 连续快速切换两个历史任务，确认消息和焦点不串线。
5. 模拟 SSE 中断，重试后检查 network 中 `request_id` 和 `Idempotency-Key` 未变。
6. 分别从岗位、人选、工作流点击“交给 Agent”，检查显式上下文和冲突提示。
7. 展开人选卡，验证预检确认后才 commit；快速双击不能重复写入。
8. 展开工作流卡，验证 R3 批准/拒绝和一次性授权。
9. 390x700 检查任务抽屉、输入框、对象卡和长标题，无横向溢出或遮挡。
10. 普通模式触发 Core 离线诊断页，重点验证第 6.1 节残留入口。
11. 使用 `--compat-copilot` 启动一次，确认旧浮窗只在兼容模式可用且不破坏 Agent 主窗口。

## 10. 建议补充的对抗性用例

- A 任务恢复请求较慢，切到 B 后 A 才返回。
- A 正在流式输出时新建 B，A 的 text/done/error 迟到。
- context 事件声明 session A，done 事件错误返回 session B。
- context/text 后连接正常关闭但没有 done。
- SSE JSON 合法但 schema 不合法；多行 `data:`、CRLF 和分块刚好切在事件边界。
- 重试第一次已在 Core 落库、前端却因断网未收到 done。
- 新任务首次发送成功但刷新任务列表失败，随后 App 重启恢复。
- PATCH 重命名已成功但响应丢失，再用同一幂等键重试。
- 已归档任务仍保存在 localStorage，启动恢复返回 404 或归档态。
- 人选 preflight token 过期、已消费、跨人选复用。
- R3 approval 重复点击、先拒绝后批准、两个窗口同时决策。
- `business_focus` 对象缺字段、引用对象已删除、历史消息含未知卡片类型。

## 11. 预期审计输出格式

请按严重级别先列问题，再给总体结论：

```text
P0/P1/P2/P3 - 标题
文件:行号
复现步骤
实际结果
预期结果
风险
建议修复
```

若未发现问题，也请明确写出：

- 实际执行过的命令及结果。
- 未执行的验证及原因。
- 剩余风险，尤其是原生兼容路径、正式库迁移和并发幂等。
- 是否建议合并/发布，或需要先修哪些问题。

## 12. 给 Kimi CLI 的简短任务提示

```text
请审计 /Users/messi/Documents/ASA/docs/ASA_AGENT_CONVERSATION_AUDIT_HANDOFF_20260803.md 所述 ASA Agent 上下文对话工作区。先读 AGENTS.md 和当前 dirty worktree，不要修改或回滚无关改动。以 findings-first 方式输出，重点检查会话竞态、SSE 完整性、request_id/幂等、候选人 preflight/commit、R3 一次性审批、Core 事实来源，以及普通原生启动是否仍存在可见或可调用的 showFloating 入口。先审计，不要自动修复；每个问题给文件行号、复现和风险，最后列已运行测试与未覆盖风险。
```
