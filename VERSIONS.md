# ASA 版本快照（阶段 0 基线）

记录时间：2026-07-22 22:42 CST
记录方式：逐项从安装源 / 运行中服务实际读取，非转自交接文档。

## 组件版本

| 组件 | 版本 | 来源（验证方式） |
| --- | --- | --- |
| 原生 macOS App | `0.2.18 (41)` | `/Users/messi/Applications/ASA.app` Info.plist（CFBundleShortVersionString / CFBundleVersion） |
| React 前端 package | `asa-web@1.0.0` | `package.json` |
| 猎聘专业回复助手扩展 | `0.3.11` | `liepin-intelligence/liepin-reply-assistant-extension/manifest.json`（交接文档为 0.3.9，2026-07-22 会话已升至 0.3.11） |
| X-SaaS 人选推进助手扩展 | `0.1.22` | `liepin-intelligence/xsaas-candidate-assistant-extension/manifest.json` |
| OpenCLI 私有扩展 | `1.0.22` | `opencli/opencli-extension-v1.0.22/manifest.json` |
| ASA Core | `asa-core 1.0.0` | `GET http://127.0.0.1:8765/api/v1/health` 返回 `{"ok":true}` |

## 关键路径

| 项 | 路径 |
| --- | --- |
| React 前端仓库 | `/Users/messi/Documents/ASA` |
| 原生 App 源码 | `/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/asa-floating-app` |
| 原生 App 安装位置 | `/Users/messi/Applications/ASA.app` |
| ASA Core 源码 | `/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts/asa_core` |
| v3 业务数据库（唯一事实源，只读） | `/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db`（144 MB，2026-07-22 22:33 mtime，health 接口确认 Core 正连接此库） |
| Core 服务管理 | LaunchAgent `ai.hermes.liepin-workbench` @ `127.0.0.1:8765` |
| Core 日志 | `/Users/messi/.hermes/logs/liepin_workbench_server.log`（错误日志同目录 `_error.log`） |

## 工具链

| 工具 | 版本 |
| --- | --- |
| Node.js | v24.15.0 |
| npm | 11.12.1 |
| Python（系统） | 3.12.13 |
| Git | 2.50.1 (Apple Git-155) |

## Git 基线

- 首个基线提交：`8b8f32e`（基线：Kimi 接手前现状存档，交接文档 2026-07-22）。
- 本快照记录时 HEAD：`c1a5c85`（阶段 4 R12-b：React Copilot 降级为纯转发）。
- 依赖锁定状态：`package.json` 无 `latest`，全部 `^` 语义化范围（commit `244e494` 完成）；`npm install` 复跑后 `package-lock.json` 零漂移。
