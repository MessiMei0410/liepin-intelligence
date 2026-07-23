# 任务卡 S5-1：Mapping 直挖 —— 目标团队定位 + 名单生成（2026-07-23）

> 批处理模板：改动范围 / 验收标准 / 门禁层级 / 是否需部署
> 完整 PRD：`docs/ASA_PRD_S5_mapping_direct_sourcing_2026-07-23.md`（本期只做 §6 的 S5-1 行）
> 前置已就绪：S4-3c 已完成（N3 扩池决策树已上线）；公司图谱 v1 在 `knowledge_base/`（233 家客户画像 + 589 家公司图谱）

## 本期范围（只做这些，别扩）

1. **数据模型**：新 artifact 类型 `mapping_task`（schema_version `mapping_v1`），复用 agent_artifacts 体系，不建新表。字段按 PRD §2：trigger / job_id / strategy_ref / target_teams[]（公司、团队、地域、evidence[] 带 type+ref+as_of、confidence）/ candidates[]（name 允许遮罩或姓氏+职务、current_role、team_ref、**source_urls 必填**、confidence、reason、status、consultant_note）/ stats。
2. **目标团队定位器**：输入策略 v2 的 T1/T2 公司池，输出每家公司的目标团队（产品线/部门/地域），每条带证据来源标注；公司图谱已有的团队信息优先复用。
3. **线索采集器（只读）**：官网/公众号/招聘 JD 公开页只读抓取 + 专利/论文公开库按公司+技术词检索作者。脉脉通道本期只做接口预留，不接。采集失败/页面变动记入 stats，不静默。
4. **名单生成**：线索 → 候选目标人两步走，每条线索保留原始出处；`source_urls` 为空的人名**拒绝写入**（防编造，硬约束，契约测试锚死）。
5. **入口**：扩池决策树末端"发起 Mapping 直挖"先只做后端触发（创建 artifact 并写入 job 时间线），前端任务卡视图是 S5-2 的事，本期不做。

## 红线（写死，违反即返工）

- 不自动触达、不自动发消息；Mapping 只产名单和任务卡数据。
- 人名必须有公开来源 URL，无来源不进名单；遮罩名合并规则不放松。
- 禁挖名单照常生效（禁挖公司的人不进名单）。
- 猎聘/X-SaaS 不用于 Mapping 采集（避免与 N1 方言层重复）。
- 入库链路不开旁路（本期不涉及入库，但 schema 里 status=intaken 的后续路径必须指向现有 preflight/commit）。

## 验收标准

1. 用 #154（七轮枯竭、排重率 99% 那个岗位）真实生成一份 `mapping_task`：≥3 家 T1 公司、每家 ≥1 个目标团队、候选目标人全部带 source_urls；teams/candidates 计数进 stats。
2. 契约测试：构造无 source_urls 的人名写入 → 必须被拒；编造检查通过。
3. 禁挖名单测试：禁挖公司出现在 T1 池时，其人不进名单。
4. UI 文案如出现新板块，遵循 UX-1 原则（业务语言，无技术词）。

## 门禁层级

- L1 `npm run ci:fast` 必过；
- L2 `npm run test:contract` 必过（动了 artifact 数据模型）；
- 真实生成验证：#154 跑一次（见验收 1）；
- 本期不动 UI 主流程，不跑 e2e。

## 部署：完成后执行 `bash scripts/ship.sh`
