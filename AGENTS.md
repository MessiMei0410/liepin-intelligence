# AGENTS.md

> **2026-07-23 仓库合并**：原前端仓库（/Users/messi/Documents/ASA）已并入本仓为 `asa-web/` 子树（双方历史完整保留），原路径为符号链接。本仓现为唯一主仓：后端 `scripts/` + 前端 `asa-web/`。

- 修改 `liepin-reply-assistant-extension` 扩展代码后，必须同步升级 `manifest.json` 的 `version`，并在交付前确认页面标题显示新版本号。

## 单人开发规范（2026-08-17 起：Mac B 停用，双机协作改为单人）

- 本仓库由单机（本 Mac）+ Codex 开发，GitHub 远程仓库为唯一同步枢纽。
- 不设目录责任区：可修改仓库内任意目录（含 `asa-web/`、`scripts/`、`tests/`、`docs/`）。
- **一切变更走 PR**：`main` 受保护，禁止直接 push；PR 由提交者自查 + CI 作为质量闸门。
- 开工前 `git switch main && git pull --rebase`；收工前 commit + push（只 add 自己本次改动的文件，绝不 `git add .` 全仓）。
- 冲突处理：`git pull --rebase` → 逐块解决 → `git add 冲突文件 && git rebase --continue`。
- `git stash` 只在本机，跨会话接力用 `wip:` 前缀 commit。
- 分支命名：`feature/<模块>-<简述>` / `fix/<模块>-<简述>` / `wip/<模块>-<简述>`。
- Commit 规范：`feat:` `fix:` `refactor:` `docs:` `chore:`。
- 合并前必须通过 CI（见 `.github/workflows/ci.yml`）。
