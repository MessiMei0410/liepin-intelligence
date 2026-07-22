# AGENTS.md

ASA App 前端（React 19 + Vite 8 + TS strict），由 ASA Core（127.0.0.1:8765）的 `/asa-app` 提供 `dist/`。

## 常用命令

- `npm run ci` — 本地 CI 一键：typecheck && lint && test && build && test:contract && test:e2e && check:api-drift。改动后必须全绿。
- `npm run test` — Vitest + React Testing Library（`src/__tests__/`）。
- `npm run test:contract` — Python 源码契约测试（`tests/`，unittest）。
- `npm run test:e2e` — Playwright E2E + 截图回归（`e2e/`）。global setup 把正式库**只读**复制到 /tmp 新鲜副本，拉起隔离 Core（127.0.0.1:8876，`A_SYSTEM_DB`/`--db` 双指向副本），跑完回收；正式 Core（8765）与正式库绝不作为目标。缺依赖（后端仓库/python/dist）时整套降级 skip。基线在 `e2e/snapshots/`（桌面 1440×900 + 浮窗 390×700 两组），变更 UI 后用 `npx playwright test --project=shots-desktop --project=shots-floating --update-snapshots` 重生成。
- `npm run generate:api` — 从运行中的 Core 重新生成 `src/generated/api.d.ts`（需 Core 在 8765）。

## 硬性约定

- **禁止 JS 原生对话框**：`prompt()`/`confirm()`/`alert()` 全仓清零，用 React 内对话框（参照 `src/components/RevisePlanDialog.tsx`）。WKWebView 不实现 JS 对话框代理，曾有静默失败事故。
- **Copilot 架构（R12-b 收敛）**：原生浮窗是唯一 Copilot 交互界面（UI/会话/发送/R9 确认卡都在后端仓库 `asa_floating_html` 内联 JS）；React 侧只保留两个角色——上下文生产者（`publishCopilotContext` 发进服务端仲裁层）与唤起浮窗（`openCopilotWindow` → native `showFloating`）。`?surface=copilot` 是纯转发器/只读提示页（`src/copilot/CopilotSurface.tsx`），不得再加回对话 UI 或 URL 上下文通道。
- **PRD R4 拆分已完成**：`src/main.tsx` 只剩入口装配（surface 判定 + createRoot）；组件分布在 `src/app/`（App/Diagnostics）、`src/pages/`、`src/panels/`、`src/workflows/`（面板 + utils）、`src/copilot/`、`src/shared/`。搬运来的存量压缩 JSX 逐字节保留，其路径列入 `.prettierignore`，不重排、不格式化、不"修"存量 lint warn；新逻辑放新文件。
- 候选人确认层（`role="alertdialog"`、preflight/commit token 链路）的字面量被 `tests/test_candidate_action_dialog.py` 正则断言——正向锚定 `src/panels/CandidatePanel.tsx`，负向 `confirm(` 扫描 `src/**/*.tsx` 拼接文本，改动会打破契约测试。
- 新增代码禁止显式 `any`（eslint 对 main.tsx 以外已设为 error）。
- 工作流状态文案一律走 `src/workflow/statusMapping.ts`，不新增本地映射；`business_outcome` 值不得直接渲染英文原形。
- 提交前确认 `git status` 无 `opencli/chrome-profile/`、Cookie、CDP 会话值、`.env` 等敏感文件。
