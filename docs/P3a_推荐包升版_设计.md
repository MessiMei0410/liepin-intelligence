# P3-a 推荐包升版后端合同设计（2026-08-14）

> 状态：设计稿（未实施）。P2-1 capability_runtime 拆分完成后实施（避免 service.py 并发改动冲突）。
> 对应 docs/ASA_优化方案_20260814.md P3 第 1 项：**推荐包升版入口**。

## 现状

- `_ensure_recommendation_package(conn, candidate_id, recommendation_id)`（service.py:3137）：确认推荐后幂等生成推荐包 **v1**（`version=1, status='generated'`），已存在则回读，不重复生成。
- 证据快照：`evidence_json` 存 `assessment_id / fit_score / fit_level / recommendation / confidence / evidence_coverage / criteria / strengths / gaps / assessed_at`。
- 唯一约束：`UNIQUE(job_candidate_id, version)`（并发兜底 IntegrityError → 回读现有版本）。
- 前端（asa-web）已补**只读证据过期提示**（评估更新后显示"证据已过期"），但无升版写入入口。

## 需求

评估更新后（新 assessment 生成、is_current=1），顾问可**重新生成/升版推荐包**：新版本继承 v1 的 summary（人岗/推荐事实不变），证据快照换成新评估，version 自增，旧版本保留只读。

## 后端合同设计

### 1. 评估指纹（判定"评估已更新"）

- 服务端计算当前有效评估的指纹：`assessment_id + fit_score + fit_level + evidence_coverage + created_at`（或 hash(assessment 关键字段)）。
- `GET /api/v1/recommendation-packages/{package_id}` 响应增加：
  ```json
  {
    "evidence": { "...既有字段...", "fingerprint": "sha256:..." },
    "upgradeable": true,          // 存在更新的有效评估（fingerprint ≠ 包内快照 fingerprint）
    "latest_assessment_id": 12345
  }
  ```
- **upgradeable 判定**：当前有效评估的 fingerprint ≠ 包 evidence 快照的 fingerprint。

### 2. 升版 POST（preflight + commit 幂等）

复用确认层模式（preflight token + 幂等创建）：

- `POST /api/v1/recommendation-packages/{package_id}/upgrade/preflight`
  - body: `{request_id, package_id}`
  - 校验：包存在（404）；`upgradeable=true`（409 "无更新的评估，无需升版"）
  - 返回 `{token, package_id, current_version, latest_fingerprint}`
- `POST /api/v1/recommendation-packages/{package_id}/upgrade/commit`
  - headers: `Idempotency-Key`
  - body: `{request_id, package_id, preflight_token}`
  - 服务层：查当前有效评估 → 新 fingerprint 对比 → 一致则生成 `version=旧+1` 新包（继承 summary，新 evidence）
  - 并发语义：`UNIQUE(job_candidate_id, version)` 幂等；预检 token 过期/不匹配 → 409 中文 detail
  - 响应：`{ok, package: {package_id(新), version, status:'generated'}, previous_version, upgraded: true}`

### 3. 列表/详情兼容

- `GET /api/v1/candidates/{id}/recommendation-packages`：保持返回全部版本（含旧版本，前端标"历史版本"）。
- 详情：`GET /api/v1/recommendation-packages/{package_id}` 支持任意版本（含历史）。

### 4. 表结构

无需新表：`recommendation_packages` 已有 `version`、`UNIQUE(job_candidate_id, version)`、`evidence_json`。可加索引 `(job_candidate_id, version DESC)` 优化列表。

## 边界与降级

- 无当前有效评估 → `upgradeable=false`，detail 说明"暂无更新的判人评估"。
- 升版时评估恰好更新（并发）→ 以 commit 时刻的 fingerprint 为准，幂等回读。
- 历史包只读：不允许对历史版本升版（只允许对最新版本升版）。

## 测试要点（实施时）

1. 确认推荐 → v1；更新评估（新 is_current）→ `upgradeable=true` → 升版 → v2 存在、v1 保留。
2. 无更新评估 → preflight 409。
3. 幂等：同 Idempotency-Key 重放 → 同 package_id，不重复生成。
4. 历史版本详情可读。
5. 评估无变化时升版 → 409（fingerprint 一致）。
