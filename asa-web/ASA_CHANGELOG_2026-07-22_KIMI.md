# ASA 2026-07-22 Kimi 会话变更记录

> 供 Kimi Code 接手后续优化使用。本记录基于《ASA_APP_KIMI_HANDOFF_2026-07-22.md》之后的一个工作会话，全部改动已通过测试与 A 系统回归守卫。

## 0. 基线

- `/Users/messi/Documents/ASA` 首个 git commit：`8b8f32e620bc809cf1dce8f8dd404b993b9ea2c2`（35 文件）。
- `.gitignore` 已补全：`opencli/chrome-profile/`、`node_modules/`、`dist/`、`work/`、日志、缓存、`.env`、`*.pem` 等。
- 敏感扫描通过，无密钥/Cookie/简历明文入库。

## 1. 浮窗上下文选举与 Copilot 焦点修复

**问题**：A 系统一次显式点击（explicit 上下文）保鲜 900 秒、选举 +1300 分，碾压新鲜浏览器页面，导致浮窗一直显示旧候选人；Copilot 焦点被 continuation 规则无限续命。

**改动**（liepin-intelligence 非 git，改前备份 `.bak-kimi-20260722`）：

- `scripts/liepin_workbench_server.py`
  - `floating_context_stale_after()`：a_system explicit 900 → **120 秒**。
  - `select_floating_active_context()`：`browser_bridge_preferred` 分支末尾 `elif explicit_workbench: score -= 1000`。
  - 浮窗内联 JS 新增 `floatingMessageContext()`：active 为 a_system 且比最新鲜 liepin/xsaas 上下文旧 60 秒以上时，发消息不附 `job_candidate_id`（sendMessage 与 answerAfterNativeImage 两处统一走该函数）。
- `scripts/a_system_agent/service.py`
  - `_copilot_context_from_focus()`：selected 候选人与旧焦点不同 → 直接用 selected，continuation 不复活旧焦点。
  - `_persist_copilot_focus()`：候选人冲突且新候选人未入库（无 facts）→ 清空候选人焦点（context=global）、confidence 降至 0.4；已入库则切换到新候选人（confidence 1.0）。

**测试**：`tests/test_asa_floating_context.py`（8 项）。既有对齐：`test_asa_floating_completion.py:480` 的 900 → 120。

## 2. 人才库储备入库（pool-only）

**问题**：`talent_system_sync.py` intake 强制 candidate+client+job 三齐全，缺岗位即 blocked（"未唯一定位：未选择客户"），无法先入储备。产品裁决：入库不强制选岗位。

**改动**：

- `/Users/messi/Documents/Codex/2026-06-26/re/work/talent_system_sync.py`（已备份）
  - `upsert_candidate_library()` 只强制 candidate，client/position 可空。
  - `process_action()` intake 分支：`pool_only:true` 或缺 client/job → pool-only 写入：people（fingerprint 排重）+ source_profiles + candidates（`talent_pool='猎聘/待分配'`（xsaas 为 `X-SaaS/待分配`）、status=pool、client/position 空）+ person 级 candidate_events（job_id=NULL，event_status=pool_intake）。**不写 job_candidates、不调 get_job_id()**。幂等复用 existing_event_count。返回 reason `pool_intake: 已入人才库储备，未挂岗位`。
- `scripts/liepin_workbench_server.py`：candidate_intake 动作透传 `pool_only`（约 4112-4132 行）。
- `liepin-reply-assistant-extension/`（**0.3.9 → 0.3.10**）：缺客户/岗位时确认弹窗提供"入人才库储备（不挂岗位）"；`lookupIssueMessage` 透传 sync 真实 reason；summarize 文案区分预检/写入。
- `service.py` Copilot 文案："未选岗位时将先入库为人才库储备（不挂岗位），之后可再补选客户和岗位"。

**测试**：`tests/test_talent_pool_intake.py`（6 项）。

## 3. 岗位状态过滤与自动降级

**改动**：

- 新建 `scripts/a_system_agent/job_status.py`：唯一名单定义。黑名单关键词（子串、忽略大小写）：待启动、暂停、关闭、closed、只读快照、已拆分、误归属、归档。未列出新状态默认可入库。
- `talent_system_sync.py` 内置同名单副本（有防漂移测试）+ `find_job_status()`（只读）：intake 三要素齐全但岗位命中黑名单 → 自动降级 pool-only，reason 含岗位名与状态（如 `岗位"XXX"状态为"待启动"，先入人才库储备，岗位启动后可再挂回`），dry_run 同样返回。
- `service.py _mentioned_jobs_for_copilot()`：过滤黑名单岗位。
- 扩展 content.js（**0.3.10 → 0.3.11**）：`dynamicProjectOptions()` 过滤黑名单（`detectDynamicProject` 自动采用随之避开）；`projectPriorityRank()` 黑名单返回 99；下拉标注"（状态·不可入库）"。岗位 status 由 `/api/context` positions 已带，Core 无改动。

**测试**：`tests/test_job_status_filter.py`（7 项）。既有对齐：`test_a_system_agent_workflow.py` 一处 fixture 岗位 status "待启动" → "已发布/推进中"（该用例验证 #id 解析契约，status 是附带数据）。

## 4. 验证基线（2026-07-22 收尾时）

- 全量测试：**264 passed, 2 failed（存量）, 1 warning**。
  - 存量失败 1：`test_a_system_agent_v15.py::...v15_workbench_contract`（修复前代码同样失败）。
  - 存量失败 2：`test_wechat_attachment_reading.py::...image_bubble_detector...`（ModuleNotFoundError，环境问题）。
- A 系统回归守卫：`failure_count: 0`。
- Core 已多次 `launchctl kickstart` 重启，health `{"ok":true}`；ASA.app 未动（仍 0.2.18/41）。
- 受管 Python 运行时补装了测试工具包：pytest、httpx、fastapi、uvicorn。

## 5. 待办与注意（交给 Kimi Code）

1. **扩展需手动重载到 0.3.11**：`chrome://extensions` 重载"猎聘专业回复助手" + 刷新猎聘页面（用户已重载过 0.3.10，0.3.11 待确认）。MV3 service worker 休眠导致 CDP 无法自动重载。
2. **P0 未做**（交接文档 11.3/11.4）：工作流"修改计划"仍用 `prompt()`（WKWebView 无效），需改 React 对话框；业务 blocked（人数不足）与技术 failed 需分开展示，建议加业务终态与"复核现有人选/调整条件再搜/结束本轮"动作。
3. **微信 OCR 上下文抢选举**：trigger=activation 的微信上下文 +420 分且不受 `browser_bridge_preferred` 惩罚（豁免集合含 activation），用户切微信发截图时浮窗会显示"微信当前对话"。建议：放宽 `browser_bridge_preferred`（如 latest_browser_age ≤ 90 且豁免集合移除 activation），或降低 activation 微信上下文加分。见 `liepin_workbench_server.py:812-870`。
4. **鹏新旭 #22（分析设备专家）**：用户口头确认已暂停，DB status 仍为"已发布/推进中"（jobs.status 无"暂停"值）。未代改，业务事实需走 A 系统渠道更新；过滤逻辑已兼容将来出现"暂停"字样。
5. 沙先生（猎聘简历，应聘"分析设备专家"）会话时尚未入库；pool-only 链路上线后由用户在 App 内操作入库储备。
6. 非 git 目录（liepin-intelligence、re/work）改动均有 `.bak-kimi-20260722` 备份；建议 Kimi Code 先给这些目录建 git 基线再大规模改动。
7. P1/P2 技术债详见交接文档第 11 节（main.tsx 拆分、类型落地、轮询、工作流输出瘦身、X-SaaS 0 结果可解释性等）。
