# ASA 阶段 0 验证基线报告

执行时间：2026-07-22 22:42–22:47 CST
执行人：Kimi（阶段 0，未做任何业务代码改动）
基线提交：`3e08e51`（其前为 `c1a5c85`，首个基线提交 `8b8f32e`）

## 结果总览

| # | 验证项 | 结果 | 耗时 | 警告 |
| --- | --- | --- | --- | --- |
| 1 | `npm run typecheck` | ✅ 通过（exit 0） | ~1s | 1 条 npm 配置警告（`node_sass_mirror` 未知配置，npm 自身 .npmrc 遗留，与代码无关） |
| 2 | `npm run build` | ✅ 通过（exit 0） | ~1s（vite 114ms） | 无；产物 `dist/assets/index-*.js` 331 kB（gzip 98.6 kB） |
| 3 | `python3 -m unittest discover -s tests -p 'test_*.py' -v` | ✅ 通过，**22 项全 OK** | <1s（0.007s） | 无 |
| 4 | `pytest -q .../liepin-intelligence/tests/test_asa_core_v1.py` | ✅ 通过，**31 passed** | 26.2s | 1 条 `StarletteDeprecationWarning`（httpx→httpx2，与交接文档记录的存量警告一致） |
| 5 | `a_system_regression_guard.py` | ✅ 通过，`failure_count: 0` | <1s | 无 |

**五项全部通过。**

## 与交接文档基线的差异说明

- 验证 3 实际为 **22 项**（交接文档记录 17 项）。差异原因：2026-07-22 后续会话新增了 `tests/test_opencli_shadow_trend.py`（5 项，commit `292cb9c`），全部通过，非回归。
- 验证 4 的 1 条警告为交接文档已记录的存量警告（FastAPI TestClient/httpx2 弃用），未扩大。
- 交接文档称"仓库无首个提交、依赖为 `latest`"——该状态已由 2026-07-22 早些时候会话解决（首个基线 `8b8f32e`，依赖锁定 `244e494`）。本次阶段 0 为核实 + 加固 + 留档，非从零建立。

## 本次阶段 0 实际变更（仅 2 个文件，均已提交）

1. `.gitignore` 防御性加固：
   - 新增 `coverage/`、`playwright-report/`、`test-results/`、`*.har`（HAR 可能含 Cookie）。
   - 敏感配置区新增 `*.key`、`*.crt`、`*.p12`，并为 `.env.example`/`.env.sample` 模板加豁免。
   - 已用 `git check-ignore` 复核：`opencli/chrome-profile/`（磁盘上真实存在，含浏览器登录态）、`node_modules/`、`dist/`、`work/`、`.env` 均命中忽略。
   - 已跟踪文件精确扫描：无 `sk-` 密钥、无 Bearer token、无 Cookie 会话值。
2. `VERSIONS.md` 新建：逐项实测版本（原生 App 0.2.18/41、asa-web 1.0.0、猎聘扩展 **0.3.11**（交接文档为 0.3.9，已按实际 manifest 更正）、X-SaaS 扩展 0.1.22、OpenCLI 扩展 1.0.22、Core 1.0.0）、Core/数据库/App 路径与工具链版本。

## 依赖锁定核实

- `package.json` 中 `latest` 出现次数：**0**（全部 `^` 语义化范围）。
- `npm install` 复跑：正常完成，`package-lock.json` 与 `package.json` **零漂移**（`git diff` 为空）。

## 红线遵守确认

- v3 SQLite 数据库未做任何写入（守卫与测试均为只读或走隔离副本）。
- `src/` 下业务代码零改动；正式执行链路未触碰。
- 未提交任何登录 profile、Cookie、API key、简历明文（chrome-profile 确认被忽略，已跟踪文件扫描无敏感值）。
- 五项验证全部通过，无需修复任何业务代码。
