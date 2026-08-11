# ASA 轻量工作流浮层：KimiCode 审计交接

更新时间：2026-08-11  
交接对象：KimiCode  
范围：`asa-web/` React 前端工作流弹框

## 1. 目标

当前工作流弹框信息密度和视觉重量过高。此次改动将默认入口改成轻量浮层，首层只回答：

1. 当前执行到哪一步；
2. 整体进度是多少；
3. 是否需要用户操作。

策略、人选名单、渠道漏斗、执行动态、产物和完整详情均改为用户点击“查看”后按需挂载。工作流业务逻辑、Core API、SSE、摘要轮询、审批 token 链路和 `#workflow=` 路由保持不变。

## 2. 已实现内容

### 默认入口

- `src/app/App.tsx` 不再直接渲染 `WorkflowPanel`，改为渲染 `WorkflowSurface`。
- `src/workflows/WorkflowSurface.tsx` 负责在摘要层和完整详情层之间切换。
- 默认层为 `CompactWorkflowDialog`，完整 `WorkflowPanel` 保留为二级详情。

### 轻量摘要层

文件：`src/workflows/CompactWorkflowDialog.tsx`

- 默认宽度约 `560px`，桌面端可拖动，移动端居中并限制为局部滚动。
- 只渲染标题、映射后的工作流状态、紧凑步骤列表、进度 footer 和阻塞操作。
- 状态图标复用 Lucide，并使用 Core 状态映射：
  - 完成/跳过：对勾；
  - 执行中/排队/等待外部：旋转环；
  - 待审批：琥珀色审批图标；
  - 失败/阻塞：红色警告；
  - 未开始：空心圆；
  - 暂停/取消：对应操作图标。
- 待审批、确认计划、失败重试直接显示。
- 暂停、继续、停止、归档收进“更多”菜单。
- “查看”菜单提供：寻访策略、人选名单、渠道漏斗、执行动态、结果与产物、完整详情。
- 保留 `useWorkflowEventStream`、摘要轮询、耗时更新和 Core 状态事实来源，不在前端推断执行状态。
- 默认不挂载策略、人选、漏斗、动态和产物模块，避免触发这些模块的附加请求。

### 二级详情

文件：`src/workflows/WorkflowPanel.tsx`

- 新增可选 `initialSection`。
- 从“查看策略”“查看人选”等入口进入时，直接滚动定位到对应模块。
- 详情层的关闭按钮实际表现为返回摘要层；原有完整业务能力保留。

### 基础设施与样式

- `src/shared/Dialog.tsx`：`DialogPanel` 增加可选 `ariaLabel`。
- `src/styles.css`：新增 `.compact-workflow-*` 样式、桌面拖动布局和 `390x700` 移动/浮窗适配。
- `e2e/support/nav.ts` 及相关工作流入口测试更新为识别摘要层。

## 3. 验证结果

已通过：

- `npm run ci:fast`
  - typecheck、lint、Vitest、build、API drift 均通过；
  - 60 个测试文件，488 个测试通过。
- `npm test -- --run src/__tests__/compact-workflow-dialog.test.tsx`
  - 4 个测试通过。
- 工作流紧凑浮层 E2E：`e2e/functional/workflow-compact.spec.ts` 通过。
- 工作流桌面截图和浮窗截图回归通过，基线为 `1440x900` 与 `390x700`。

已知但与本次改动无关：完整 `npm run ci:e2e-functional` 中已有两条 `candidate-stop.spec.ts` 失败，涉及共享候选人状态/mock stop commit；工作流相关用例通过。审计时请不要将其误判为本次浮层回归。

## 4. KimiCode 审计重点

### 交互与视觉

- 默认打开工作流时，策略/人选/漏斗/动态/产物是否确实未挂载。
- “查看”进入二级详情后，返回是否回到同一份摘要状态，而不是重新打开重型首层。
- 桌面拖动是否稳定；`390x700` 下是否存在横向溢出、footer 截断、菜单被裁切或按钮换行异常。
- 待审批、确认计划、失败重试是否始终可见；低频操作是否只出现在“更多”。
- 状态图标和文案是否完全来自 `src/workflow/statusMapping.ts` / `src/workflows/utils.ts`，不得新增本地状态映射。

### 数据与请求

- 首层渲染不应请求策略、人选、漏斗、动态和产物数据。
- SSE 断开时应退回摘要轮询；Core 暂时不可用时保留当前快照，不伪造状态。
- 审批应继续使用现有 approval token 链路；启动计划应携带 `plan_ref` 的版本/hash 校验。
- 重试、暂停、继续、停止、归档的权限和状态约束不得因 UI 收缩而改变。

### 可访问性与兼容

- `DialogPanel` 的 `role="dialog"`、`aria-label`、Escape 关闭和焦点行为不能回归。
- 菜单按钮应保持 `aria-haspopup` / `aria-expanded`；图标按钮要有可读 `title` / `aria-label`。
- `#workflow=` 路由、审批 token 和 Core schema 不改名、不改字段。

## 5. 建议审计命令

```bash
cd /Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/asa-web
npm run ci:fast
npx playwright test e2e/functional/workflow-compact.spec.ts --project=functional
npx playwright test e2e/shots/views.spec.ts --project=shots-desktop --project=shots-floating
```

本地预览：`http://127.0.0.1:5173/asa-app`。打开已有工作流或使用 `#workflow=<workflow_id>` 验证摘要层和二级详情跳转。

## 6. 交接边界

- 本次目标是降低默认信息密度，不删除任何已有工作流业务能力。
- 不要把策略、人选等详情重新放回首层，也不要为了视觉优化改动 Core API 或工作流 schema。
- 当前仓库原本存在其他未提交改动；审计和后续优化只应围绕本交接文档列出的工作流文件，避免覆盖无关修改。
- 若需要调整视觉，优先改 `.compact-workflow-*` 样式和摘要组件；若需要调整业务行为，先核对现有 `WorkflowPanel`、`api.ts`、`statusMapping.ts` 和 workflow hooks。

