# AGENTS.md

ASA App 前端（React 19 + Vite 8 + TS strict），由 ASA Core（127.0.0.1:8765）的 `/asa-app` 提供 `dist/`。

## 常用命令

- `npm run ci` — 本地 CI 一键：typecheck && lint && test && build && test:contract && check:api-drift。改动后必须全绿。
- `npm run test` — Vitest + React Testing Library（`src/__tests__/`）。
- `npm run test:contract` — Python 源码契约测试（`tests/`，unittest）。
- `npm run generate:api` — 从运行中的 Core 重新生成 `src/generated/api.d.ts`（需 Core 在 8765）。

## 硬性约定

- **禁止 JS 原生对话框**：`prompt()`/`confirm()`/`alert()` 全仓清零，用 React 内对话框（参照 `src/components/RevisePlanDialog.tsx`）。WKWebView 不实现 JS 对话框代理，曾有静默失败事故。
- **`src/main.tsx` 是 663 行巨石，拆分（PRD R4）前**：不重写、不搬家现有组件、不格式化存量压缩 JSX（`.prettierignore` 已排除）；新逻辑放新文件，main.tsx 只做接线。
- 候选人确认层（`role="alertdialog"`、preflight/commit token 链路）的字面量被 `tests/test_candidate_action_dialog.py` 正则断言，改动会打破契约测试。
- 新增代码禁止显式 `any`（eslint 对 main.tsx 以外已设为 error）。
- 工作流状态文案一律走 `src/workflow/statusMapping.ts`，不新增本地映射；`business_outcome` 值不得直接渲染英文原形。
- 提交前确认 `git status` 无 `opencli/chrome-profile/`、Cookie、CDP 会话值、`.env` 等敏感文件。
