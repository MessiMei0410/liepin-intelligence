# ASA ← DeepSeek Harness (DSH) 编排层

把 [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) 嵌入 ASA 作为**编排层（路 2）**：
DSH 负责多步编排 / 子代理 / goal / workflow，领域情报继续留在现有 Python Copilot，
写动作走 typed 工具（preflight → commit + 幂等）。

完整设计、验证证据与决策记录见
[`docs/ASA_DSH_嵌入方案_方案A_2026-08-17.md`](../docs/ASA_DSH_嵌入方案_方案A_2026-08-17.md)。

## 目录

- `asa-tools/` — Cordis 工具插件 `@asa/dsh-asa-tools`（8 个工具，见下）。
- `asa-profile/` — `asa` profile 源（headless 一次性）：persona + 12 条业务护栏（`AGENTS.md`）+ 插件装配。
- `asa-server/` — 常驻服务器 bundle `@asa/dsh-asa-server`：HTTP `POST /turn` + 会话复用（多轮记忆）。
- `asa-server-profile/` — `asa-server` profile 源（bundles = `dsh-base` + `@asa/dsh-asa-server`）。
- `bridge/` — `asa_dsh_bridge.py`：per-turn 子进程桥接（v0 遗留，已被常驻服务器取代）。

## 工具面（8 个）

| 类 | 工具 | 说明 |
| --- | --- | --- |
| 只读 | `asa_dashboard` / `asa_jobs` / `asa_candidates` / `asa_workflow` | 直读 ASA Core（GET） |
| 受控写 | `asa_candidate_preflight` / `asa_candidate_commit` / `asa_approval_decision` | preflight→commit + `Idempotency-Key` |
| 领域情报委托 | `asa_copilot_ask` | 转发 `/api/v1/copilot/stream`，取现有 Copilot 富答案 |

## 快速开始（常驻服务器，推荐）

```bash
# 1. 安装 profile 到 ~/.dsh/profiles/asa-server（bundles = dsh-base + @asa/dsh-asa-server）
mkdir -p ~/.dsh/profiles/asa-server
cp asa-server-profile/{cordis.patch.yml,AGENTS.md,pnpm-workspace.yaml} ~/.dsh/profiles/asa-server/
#   并把 package.json 里的 file: 相对路径改成绝对路径后，在该目录 pnpm install

# 2. 起常驻服务器（默认 8891，env ASA_DSH_RESIDENT_PORT 可改）
dsh --profile asa-server

# 3. 验证（同 session_id 复用会话 → 多轮记忆）
curl -s http://127.0.0.1:8891/health
curl -s -X POST http://127.0.0.1:8891/turn -H 'Content-Type: application/json' \
  -d '{"message":"用 asa_dashboard 读数据，回答：现在有多少活跃岗位？"}'
```

前端走 DSH：ASA 界面 URL 加 `?surface=copilot&brain=dsh`（默认仍走 `/copilot/stream`）。

> per-turn 子进程桥接（`dsh/bridge/asa_dsh_bridge.py`，8890）仍可用作 headless 一次性回退，
> 但无跨轮记忆；常驻服务器（8891）是当前前端 DSH 路径。

## 关键坑（已记录，勿重踩）

- 插件 `file:` 安装是**拷贝**：改 `asa-tools/lib/index.js` 后需在 profile 目录重新 `pnpm install`（或 `dsh plugin` 重装）。
- `link:` 会让 `@deepseek-ai/dsh-tools`（peerDep）从软链 realpath 解析不到 → 插件树 `ERR_MODULE_NOT_FOUND`；回退 `file:` 时记得清 `pnpm-lock.yaml` 里残留的 `link:` 条目。
- 工具参数里 `object` 类型必须显式 `additionalProperties: true/false`，否则 `UNSUPPORTED_SCHEMA`。
- 写动作正式库零接触：测试一律用 `/tmp` DB 副本 + 独立端口 Core（见 e2e `global-setup.ts` 的隔离模式）。
