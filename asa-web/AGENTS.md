# AGENTS.md

> **2026-07-23 仓库合并**：本目录现为单体仓库 `liepin-intelligence` 的 `asa-web/` 子树；`/Users/messi/Documents/ASA` 是指向该子树的符号链接（路径兼容层）。提交在主仓 `liepin-intelligence` 进行，历史双方完整保留。

ASA App 前端（React 19 + Vite 8 + TS strict），由 ASA Core（127.0.0.1:8765）的 `/asa-app` 提供 `dist/`。

## 常用命令

- 验证分三层门禁（2026-07-23 提速方案，详见 `docs/ASA_提速方案_v1_20260723.md`）：
  - **L1 快速门禁（每个 commit / 小任务必跑）**：`npm run ci:fast`（typecheck && lint && test && build && check:api-drift）。纯文案、样式、prompt、docs 改动只跑 L1。
  - **L2 模块门禁（按需追加）**：动 Core API/数据结构 → `npm run test:contract`；动前端交互流程 → `npm run ci:e2e-functional`（不跑截图）；动寻访策略/池逻辑 → 单岗位真实寻访验证一次。
  - **L3 全量门禁（里程碑收官 + 每日收工前一次）**：`npm run ci` 完整链。截图基线只在 UI 变更的里程碑重生成。
- `npm run test` — Vitest + React Testing Library（`src/__tests__/`）。
- `npm run test:contract` — Python 源码契约测试（`tests/`，unittest）。
- `npm run test:e2e` — Playwright E2E + 截图回归（`e2e/`）。global setup 把正式库**只读**复制到 /tmp 新鲜副本，拉起隔离 Core（127.0.0.1:8876，`A_SYSTEM_DB`/`--db` 双指向副本），跑完回收；正式 Core（8765）与正式库绝不作为目标。缺依赖（后端仓库/python/dist）时整套降级 skip。基线在 `e2e/snapshots/`（桌面 1440×900 + 浮窗 390×700 两组），变更 UI 后用 `npx playwright test --project=shots-desktop --project=shots-floating --update-snapshots` 重生成。
- `npm run generate:api` — 从运行中的 Core 重新生成 `src/generated/api.d.ts`（需 Core 在 8765）。

## 硬性约定

- **禁止 JS 原生对话框**：`prompt()`/`confirm()`/`alert()` 全仓清零，用 React 内对话框（参照 `src/components/RevisePlanDialog.tsx`）。WKWebView 不实现 JS 对话框代理，曾有静默失败事故。
- **Agent Conversation Surface v1**：React `src/agent/` 是 Agent 的主交互面，负责会话、发送、对象卡和确认 UI；Core 仍是上下文仲裁、会话、工作流与审批的唯一事实来源。所有业务入口必须显式附着上下文后进入 Agent，不得调用 native `showFloating` 或浮窗上下文通道。旧 `asa_floating_html` 仅保留一个版本作为兼容回滚实现，本期不删除且不提供可见入口；`?surface=copilot` 直接进入主 Agent 界面。AgentWorkspace 在 App 层常驻挂载（`.agent-keepalive` 仅 `hidden` 切换显隐），切 Tab/打开分析不再卸载，进行中的会话与任务栏状态不丢失。
- **PRD R4 拆分已完成**：`src/main.tsx` 只剩入口装配（surface 判定 + createRoot）；组件分布在 `src/app/`（App/Diagnostics）、`src/pages/`、`src/panels/`、`src/workflows/`（面板 + utils）、`src/shared/`。搬运来的存量压缩 JSX 逐字节保留，其路径列入 `.prettierignore`，不重排、不格式化、不"修"存量 lint warn；新逻辑放新文件。
- 候选人确认层（`role="alertdialog"`、preflight/commit token 链路）的字面量被 `tests/test_candidate_action_dialog.py` 正则断言——正向锚定 `src/panels/CandidatePanel.tsx`，负向 `confirm(` 扫描 `src/**/*.tsx` 拼接文本，改动会打破契约测试。
- 新增代码禁止显式 `any`（eslint 对 main.tsx 以外已设为 error）。
- 工作流状态文案一律走 `src/workflow/statusMapping.ts`，不新增本地映射；`business_outcome` 值不得直接渲染英文原形。

## 设计令牌（Design Language v1，2026-08-15）

- `src/styles.css` 中部「ASA Design Language v1」`:root` 块是唯一权威色板：松绿 `--green`（品牌/主按钮/完成态）、`--info`（进行态/链接）、`--amber`（待办警示）、`--red`（危险）、中性阶 `--bg/--surface/--surface-sub/--line/--line-soft/--muted/--text`、阴影/圆角/z 轴/easing。文件顶部 L1 旧 `:root` 仅作回退层，不再新增变量。
- 新增颜色一律走语义变量，禁止新增硬编码 hex；近似灰用 `--surface-sub`/`--line-soft` 收口。
- 文件末尾「视觉覆写层」只覆盖视觉属性（颜色/圆角/阴影/字阶/动效），不得改布局网格；消息 3 列契约（`grid-column:3`）与浮窗 composer 3 列修复不在其覆盖范围。
- 圆角语义：控件 `var(--r-control)`=8px、卡片 12px、dialog `var(--r-dialog)`=12px、badge/tag 全圆角 pill。
- 侧边导航为深松绿主题（`--nav-*` 令牌），其内 focus 环用 `#9cc4ac` 而非 `--focus`。
- 提交前确认 `git status` 无 `opencli/chrome-profile/`、Cookie、CDP 会话值、`.env` 等敏感文件。
