# ASA ← DeepSeek Harness (DSH) 编排层

把 [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) 嵌入 ASA 作为**编排层（路 2）**：
DSH 负责多步编排 / 子代理 / goal / workflow，领域情报继续留在现有 Python Copilot，
写动作走 typed 工具（preflight → commit + 幂等）。

完整设计、验证证据与决策记录见
[`docs/ASA_DSH_嵌入方案_方案A_2026-08-17.md`](../docs/ASA_DSH_嵌入方案_方案A_2026-08-17.md)。

## 目录

- `asa-tools/` — Cordis 工具插件 `@asa/dsh-asa-tools`（8 个工具，见下）。
- `asa-profile/` — `asa` profile 源（headless 一次性）：persona + 12 条业务护栏（`AGENTS.md`）+ 插件装配。
- `asa-server/` — 常驻服务器 bundle `@asa/dsh-asa-server`：HTTP `POST /turn`（SSE 流式）+ 会话复用（多轮记忆）。
- `asa-server-profile/` — `asa-server` profile 源（bundles = `dsh-base` + `@asa/dsh-asa-server`）。
- （`bridge/` v0 per-turn 子进程桥接已于 2026-08-19 删除——无任何引用，headless 一次性需求用 `dsh --profile asa` 直跑。）

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

前端走 DSH：**2026-08-18 起 DSH 为默认大脑**（所有入口：桌面 APP `/asa-app`、浮窗），
URL 加 `?brain=copilot` 可临时回退 Python Copilot 直连。
DSH 轮次完成后前端自动回填 Core（`POST /api/v1/copilot/sessions/record-turn`，按 `request_id` 幂等），
会话出现在任务列表、可刷新恢复。

## v1.4 收敛（2026-08-18）

- 默认模型切到 `deepseek-v4-flash`（`~/.dsh/settings.yaml` 的 `agent-default-model`，本机配置、不在仓库内）。
- 工作目录从 `/tmp/asa-dsh-spike`（会被系统清理）迁到 `~/.dsh/asa-workspace`；**业务护栏
  `AGENTS.md` 必须在该目录才会被 agent-instructions 加载**——此前 /tmp 下没有该文件，护栏实际未生效。
  `deploy-asa-server.sh` 现在同步 `cordis.patch.yml` → profile、`AGENTS.md` → 工作目录。
- 工具面收敛：`tool-bash` / `tool-pwsh` / `tool-fs` / `tool-fs-search` / `tool-skill` 在
  `cordis.patch.yml` 中 `disabled`——业务问答只用 8 个 `asa_*` 工具，消除模型绕路探索文件系统
  （实测同问 120s → 77s）并收紧 `danger-full-access` 暴露面。副作用：Agent 不再能导出文件。
- 常驻服务器每轮 stdout 打一行观测日志（session/成败/答案长度/耗时）；`tool/call` 事件
  转发为 SSE progress，前端可见工具执行进度。

> headless 一次性回退：直接 `dsh --profile asa "<任务>"`（无跨轮记忆）；
> 常驻服务器（8891）是当前前端 DSH 路径。

## 部署与守护

profile 的 `file:` 安装是**拷贝**：改完 `asa-server/` 或 `asa-tools/` 必须同步到
`~/.dsh/profiles/asa-server/node_modules/@asa/` 并重启常驻服务器才生效。一键完成：

```bash
dsh/bin/deploy-asa-server.sh   # 同步源码 → 重启 → 健康检查
```

常驻服务器建议挂 launchd 守护（崩溃 / 重启自动拉起，此前手动 nohup 无守护、挂了就静默死亡）：

```bash
cp dsh/launchd/com.asa.dsh-server.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.asa.dsh-server.plist
```

装了 launchd 后 `deploy-asa-server.sh` 自动改走 `launchctl kickstart`。

## v1.3 加固（2026-08-17 审计后）

- 事件订阅 finally dispose：异常路径不再残留监听器（此前同 session 后续请求会叠加重复推流）。
- Agent 池持句柄：空闲 TTL 30 分钟（`ASA_DSH_AGENT_IDLE_TTL_MS`）+ 上限 20 个（`ASA_DSH_MAX_AGENTS`）LRU 回收，常驻内存不再只增不减。
- 客户端断连（前端 AbortController）即时 cancel 本轮：止损 LLM 调用、释放会话队列。
- 单轮总超时 300s（`ASA_DSH_TURN_TIMEOUT_MS`），超时 cancel 并回 `done ok:false`。
- 请求体上限 1MB（`ASA_DSH_MAX_BODY_BYTES`），超限 413。
- token 比较改恒定时间（`timingSafeEqual` / `hmac.compare_digest`）。
- CORS 从 `*` 收紧为白名单回显（Core 8765 + vite dev 5173），8891/8890 一致。
- 前端 dsh-config 失败不再缓存负结果：Core 未就绪时一次失败不再导致整页 401。
- 会话串行队列排空后删除条目；最终答案改为事件流增量聚合，不再每轮全量扫 `session.events`。

## 关键坑（已记录，勿重踩）

- 插件 `file:` 安装是**拷贝**：改 `asa-tools/lib/index.js` 后需在 profile 目录重新 `pnpm install`（或 `dsh plugin` 重装）。
- `link:` 会让 `@deepseek-ai/dsh-tools`（peerDep）从软链 realpath 解析不到 → 插件树 `ERR_MODULE_NOT_FOUND`；回退 `file:` 时记得清 `pnpm-lock.yaml` 里残留的 `link:` 条目。
- 工具参数里 `object` 类型必须显式 `additionalProperties: true/false`，否则 `UNSUPPORTED_SCHEMA`。
- 写动作正式库零接触：测试一律用 `/tmp` DB 副本 + 独立端口 Core（见 e2e `global-setup.ts` 的隔离模式）。
