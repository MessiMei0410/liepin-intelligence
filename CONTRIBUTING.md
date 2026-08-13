# CONTRIBUTING.md — liepin-intelligence 协作流程

本仓库由两台 Mac + 两个 Codex 协同开发（见 `AGENTS.md` 双机协作规范）。遵守以下流程。

## 协作流程

1. 从 `main` 拉最新：`git switch main && git pull --rebase`
2. 建分支：`git switch -c feature/<模块>-<简述>`
3. 只修改自己负责的目录（后端 `scripts/` / 前端 `asa-web/`）
4. 提交（用规范前缀），推送分支
5. 开 PR，模板必填：关联 Issue、改动说明、验收清单
6. 等对方 Codex 审查 → 批准 → CI 通过 → 合并 → 删除分支

## 接受什么

- 符合责任区划分的功能/修复
- 有测试或验证命令可证明的改动（本项目后端 pytest、前端 vitest）
- PR 描述完整、可独立审查的改动

## 不接受什么

- 未验证的改动（CI 没跑或本地验证命令没跑）
- 跨责任区的静默改动（先协商）
- 直接 push main 的改动（会被分支保护拦截）

## 冲突时

```
git pull --rebase
# 手动逐块解决冲突
git add 冲突文件
git rebase --continue
git push --force-with-lease
```

## 验证命令

```bash
# 后端
cd scripts && python3 -m pytest tests/ -x -q
# 前端
cd asa-web && npx vitest run
```

## 检查清单（PR 模板同步使用）

- [ ] 关联的 Issue 已链接（Closes: #xxx）
- [ ] 只改了责任区内的文件
- [ ] 本地验证命令通过
- [ ] 无敏感信息（密钥/token/密码）进入仓库
