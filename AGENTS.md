# AGENTS.md

> **2026-07-23 仓库合并**：原前端仓库（/Users/messi/Documents/ASA）已并入本仓为 `asa-web/` 子树（双方历史完整保留），原路径为符号链接。本仓现为唯一主仓：后端 `scripts/` + 前端 `asa-web/`。

- 修改 `liepin-reply-assistant-extension` 扩展代码后，必须同步升级 `manifest.json` 的 `version`，并在交付前确认页面标题显示新版本号。

## 双机双 Codex 协作规范（2026-08-14 加入）

- 本仓库由两台 Mac（A/B）+ 两个 Codex 协同开发，GitHub 远程仓库为唯一同步枢纽。
- **责任区分工（2026-08-14 确认）**：
  - Mac A（本机）→ **前端 `asa-web/`**
  - Mac B（另一台）→ **后端 `scripts/` + `tests/`**
  - 双方各改各的目录；确需跨区时先与对方协商，禁止静默跨区改动。
- **一切变更走 PR**：`main` 受保护，禁止直接 push；A 的 PR 由 B 审，B 的 PR 由 A 审。
- 开工前 `git switch main && git pull --rebase`；收工前 commit + push（只 add 自己负责的文件，绝不 `git add .` 全仓）。
- 冲突处理：`git pull --rebase` → 逐块解决 → `git add 冲突文件 && git rebase --continue`。
- `git stash` 只在本机，跨机器接力用 `wip:` 前缀 commit。
- 分支命名：`feature/<模块>-<简述>` / `fix/<模块>-<简述>` / `wip/<模块>-<简述>`。
- Commit 规范：`feat:` `fix:` `refactor:` `docs:` `chore:`。
- 合并前必须通过 CI（见 `.github/workflows/ci.yml`）。
