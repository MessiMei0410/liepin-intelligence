# ASA 分发依赖盘点（路 B：同事自部署）

> P1 交付物。目标：每位同事一台 Mac、一份空库、一把自己的 DeepSeek Key、自己的猎聘登录态，
> 从分发包一键装出完整 ASA。理念继承 `a-system-portable/`（A 系统 V2.5 时代）：空系统、无数据、Key 走 Keychain。
>
> 状态标注：✅ 已由 `install.sh` 自动化；⚠️ 半自动（脚本检查+中文指引）；❌ 纯手动。

## 1. 系统依赖

| 依赖 | 要求 | 状态 | 说明 |
| --- | --- | --- | --- |
| macOS | 任一近年版本 | ✅ 检查 | install.sh 校验 `uname` |
| Python | ≥ 3.11（代码用到 `tomllib`） | ✅ 检查+指引 | 缺失提示 `brew install python@3.11` |
| Node.js + npm | 18+（DSH 工具链用） | ✅ 检查+指引 | 缺失提示 `brew install node` |
| Google Chrome | 最新版 | ⚠️ 检查+提醒 | 两个扩展需要；猎聘登录态在同事自己的 Chrome |
| Homebrew | — | ❌ 手动 | 装不上 Python/Node 时的前置 |

## 2. Python 依赖（Core，venv 内 pip 安装）

Core 启动硬依赖（`scripts/asa_core/app.py` 顶层 import）：

- `fastapi`、`uvicorn[standard]`、`pydantic` — ✅ install.sh 装进 `$ASA_HOME/venv`
  （默认 PyPI 失败时自动回退清华镜像；npm 同理回退 npmmirror）

运行时按需（懒加载）第三方包，**P1 不装**，用到对应功能再 `pip install`：
`openpyxl`、`python-docx`、`python-pptx`、`pypdf`、`pymupdf`(fitz)、`xlrd`、`numpy`、`opencv-python`(cv2)、`certifi`
（分布在 `scripts/a_system_agent/` 的报告导出/附件解析路径）。

其余全部为标准库（sqlite3 等）。仓库根无 requirements/pyproject——本清单即事实源。

## 3. 前端

- `asa-web/`（React 19 + Vite）：**同事侧不需要 Node 构建**——`build_package.py` 在打包含预构建
  `dist/`（`--build-web` 或自动检测缺失时跑 `npm install && npm run build`）。✅ 自动化
- Core 以 `/asa-app` 伺服 dist，UA 前缀 `ASAApp/` 门控（无 UA → 403）。✅ 随 Core 就绪

## 4. DSH 编排层（127.0.0.1:8891）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| 工具链 `~/.dsh/asa-server-toolchain` | ✅ | install.sh 用 `dsh/package.json`+`package-lock.json`（`@deepseek-ai/dsh` 锁定 `0.1.0-rc.6`）`npm ci` |
| profile `~/.dsh/profiles/asa-server` | ✅ | install.sh 按 `dsh/README.md` 快速开始装配：profile 四件套 + `@asa/dsh-asa-server` / `@asa/dsh-asa-tools` 实体拷贝 |
| 工作目录护栏 `~/.dsh/asa-workspace/AGENTS.md` | ✅ | install.sh 同步（护栏不在该目录则 agent-instructions 不加载） |
| 默认模型 `~/.dsh/settings.yaml` | ✅ | 缺失才写：`deepseek-official` / `deepseek-v4-flash` |
| DeepSeek Key `~/.dsh/.credentials.yaml` | ✅ 引导 | `DEEPSEEK_API_KEY: sk-...`，0600；交互输入或 `ASA_DEEPSEEK_KEY` 环境变量 |

注意（继承自 dsh/README 的坑）：工具链必须在 `~/.dsh` 下，launchd 进程没有 `~/Documents` 的
TCC 授权；profile 里不得出现 `file:` 依赖。install.sh 的布局已规避。

## 5. 服务与端口（launchd）

| 服务 | Label | 端口 | 状态 |
| --- | --- | --- | --- |
| ASA Core（FastAPI） | `ai.hermes.liepin-workbench` | 127.0.0.1:8765 | ✅ 模板 `distribution/launchd/ai.hermes.liepin-workbench.plist.template` |
| DSH 常驻服务器 | `com.asa.dsh-server` | 127.0.0.1:8891 | ✅ 模板 `distribution/launchd/com.asa.dsh-server.plist.template` |

模板占位符 `__ASA_HOME__`/`__HOME__` 由 install.sh 渲染；Core 的环境变量（`A_SYSTEM_DB`、
`ASA_WEB_DIST`、Keychain service/account、模型）都在 plist 内。健康检查：`/api/v1/health`、
`/asa-app`（带 UA）、DSH `/health`。✅ install.sh 自动验证。

## 6. 文件与数据

| 项 | 位置 | 状态 |
| --- | --- | --- |
| 安装目录 | `~/ASA`（可传参改） | ✅ |
| 空库 | `~/ASA/data/asa.db` | ✅ 见下「空库初始化」 |
| 日志 | `~/ASA/logs/`、`~/.dsh/asa-server*.log` | ✅ |
| 桥接密钥 | `~/.dsh/asa-bridge-token`（0600，openssl rand） | ✅ 缺失才生成 |
| DeepSeek Key（Core 侧） | macOS Keychain：service `a-system-agent-deepseek` / account `api.deepseek.com` | ✅ 引导写入 |
| 猎聘登录态 | 同事自己的 Chrome | ❌ 手动登录 |

### 空库初始化（P1 已验证）

`asa_core.database.migrate()` 要求库文件已存在，且 13 个 migration（版本 1-11/14/15）里的
数据迁移 SQL 引用 v3 基座表（`candidate_events`/`job_candidates`/`positions`…）——这些表由
仓库外的 v3 流水线创建，**仓库内没有建表语句**。因此空库初始化分三步（install.sh 第 4 步）：

1. `distribution/base_schema.sql` — v3 基座 DDL（57 个对象，仅结构无数据），由
   `distribution/generate_base_schema.py` 从主库 sqlite_master 差集生成（剔除运行时代码
   `CREATE IF NOT EXISTS` 自建的对象及其从属索引/视图）。
2. `a_system_agent.schema.ensure_schema(conn)` — agent 层表。
3. `migrate(db)` — asa_core 迁移（幂等，带 checksum 校验与外键检查）。

实测（2026-08-20）：全新空文件 → 三步跑通，`applied=[1..11,14,15]`，`foreign_key_issues=[]`，
共 100 张表；Core 在该空库上启动，`/api/v1/health` 返回 ok。

## 7. 应用与扩展（P1 为"文档+指引"级，P2 深化）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| ASA.app（`asa-floating-app/` Swift 壳） | ❌ P2 | 需 Xcode/swiftc 编译+签名分发；P1 在 install.sh 结尾打印临时访问方式 |
| 猎聘回复助手扩展 | ❌ 手动 | `chrome://extensions` 开发者模式加载 `extensions/liepin-reply-assistant-extension` |
| X-SaaS 候选人助手扩展 | ❌ 手动 | 同上，`extensions/xsaas-candidate-assistant-extension` |

## 8. 分发包内容（`build_package.py` 产出 `distribution/dist/ASA-<date>/`）

- `app/` — Python 闭包（`asa_core`、`a_system_agent` 全包 + `liepin_workbench_server` 等 19 个扁平模块，
  import 闭包自动解析；剔除 tests、`__pycache__`、`*.bak`）+ `base_schema.sql`
- `web/dist/` — asa-web 预构建产物
- `dsh/` — asa-tools / asa-server / asa-profile / asa-server-profile / bin / launchd /
  package.json / package-lock.json / README.md（不含 node_modules）
- `extensions/` — 两个扩展源码
- `launchd/` — plist 模板；`install.sh`；`INVENTORY.md`；`MANIFEST.txt`

## 9. 脱敏清单（P2 处理，本轮只列不改）

### 9.1 硬编码私人路径 `/Users/messi`（多数有环境变量兜底，plist env 已覆盖关键项）

- `scripts/asa_core/database.py:17` — `A_SYSTEM_DB` 默认值指向主库（install.sh 在 plist 里覆盖）
- `scripts/asa_core/knowledge_proposals.py:82` — `/Users/messi/Documents/ASA/knowledge_base`（业务知识库路径）
- `scripts/asa_core/app.py:1923` — 403 提示页写死 `/Users/messi/Applications/ASA.app`
- `scripts/a_system_agent/service.py:49`、`_shared.py:44` — `A_SYSTEM_OPENCLI_BIN` 默认 `~/.hermes/node/bin/opencli`
- `scripts/a_system_agent/copilot_impl.py:2431` — 文案含本机 opencli 路径
- `scripts/a_system_agent/knowledge_base.py` — 知识库默认路径同上
- `scripts/liepin_workbench_server.py:99,109,112` — `A_SYSTEM_ROOT`（v3 流水线根）、
  多渠道脚本 `~/.codex/skills/...`、`A_SYSTEM_CODEX_BIN`
- `scripts/xsaas_candidate_search.py:14` — `ASA_CDP_SKILL_DIR` 默认 `~/.codex/skills/liepin-cdp-search/scripts`
- `scripts/ensure_project_confirmation_schema.py`、`generate_position_dashboard.py`、
  `generate_workflow_status_report.py`、`record_candidate_reply.py`、`record_client_feedback.py`、
  `record_outreach_event.py`、`record_search_experiment.py`、`sync_reply_assistant_samples.py`、
  `sync_reply_assistant_outreach_events.py` — 各自的 DB/输出目录默认值
- `dsh/launchd/com.asa.dsh-server.plist` — 仓库内这份是本机实例（分发用 `distribution/launchd/` 模板，已脱敏）
- `liepin-reply-assistant-extension/安装说明.md`、`打开安装页.command`、
  `xsaas-candidate-assistant-extension/安装说明.md` — 安装指引含本机路径

### 9.2 真实客户名/业务知识（grep 命中：长越/士兰微/杰理/雅特力/峰岹/纳芯微/南芯/英集芯 等）

- `scripts/a_system_agent/`：`copilot_evidence.py`、`capability_runtime_base.py`、`query_builders.py`、
  `mapping_task.py`、`llm.py`、`strategy_v2.py`、`copilot_tools.py`、`copilot_impl.py`、
  `workflow_handler.py`、`copilot_intent.py`、`copilot_sessions.py`（prompt 示例/别名表/行业词典）
- `dsh/asa-tools/lib/index.js`、`dsh/asa-server/lib/object-actions.js` 及各自 `test/*.test.js`（fixture 含真实客户名）
- `liepin-reply-assistant-extension/match-profiles.js`、`content.js`、`recommendation-copy.js`（话术模板）
- `dsh/asa-profile/AGENTS.md`、`dsh/asa-server-profile/AGENTS.md`（业务护栏，含客户语境——P2 需评审是否泛化）

### 9.3 业务数据（不进分发包，仅确认隔离）

- 主库 `talent_system_v3_*.db`、`outputs/`、`knowledge_base/`（asa-web 下）、`scripts/company_kb/` 产出
  均**不在** `build_package.py` 收集范围内；base_schema.sql 只含 DDL 不含数据。
- 同事侧从空库起步，客户/候选人数据由各自使用累积。
