# ASA 工作流优化方案 v1 落地交接（给 Codex）

交接时间：2026-08-05
交接来源：Kimi Code（接手 Codex session `019fcb46-55f3-7b71-9a1a-a526df458f9d` 后完成）
范围：`ASA 猎头工作流优化方案 v1` 一期（可信推荐闭环）+ 二期（顾问知识飞轮）+ 三期（项目交付驾驶舱）全部落地

> 本文件只交接"做了什么、在哪、怎么验证、有什么坑"。方案原文见 Codex session 中的 proposed_plan；模块全景见 `docs/ASA_MODULE_CATALOG_20260804.md` 第十七节。

## 一、验证状态（交接时全部通过）

| 门禁 | 结果 |
| --- | --- |
| 后端全量 `PYTHONPATH=scripts python3 -m pytest tests/ -q` | 1108 passed + 116 subtests |
| 前端 L1 `npm run ci:fast`（typecheck/lint/test/build/api-drift） | 全绿（371+ vitest） |
| L2 契约 `npm run test:contract` | 58/58 |
| L2 e2e 功能 `npm run ci:e2e-functional` | 17/17 |
| e2e 截图基线 | 已按 UI 里程碑 `--update-snapshots` 重生成并复跑 14/14（Agent 首页/岗位详情/工作流详情的变化是预期内的新 UI） |
| 策略回放 `PYTHONPATH=scripts python3 scripts/strategy_replay_eval.py --json` | 两个真实 case 七指标无倒退 |
| Core（8765） | 已通过 `launchctl kickstart -k gui/$(id -u)/ai.hermes.liepin-workbench` 重启，OpenAPI 90 条路径含全部新端点；`asa-web/src/generated/api.d.ts` 已重生成且 check-api-drift 一致 |

**重要：所有改动均未 git 提交**（工作区约 150 个文件，含 Codex session 的八轮打磨 + 本次 v1 全部产出）。当前是一个合理的提交点。

## 二、一期：可信推荐闭环

### 1. 寻访策略按项编辑（最大缺口，已补）

- 新模块 `scripts/a_system_agent/strategy_editor.py`：
  - `apply_item_edits(v2, edits)` 纯函数，8 种 op：`add/update/delete_keyword_group`、`add/update/delete_company`（按 tier）、`update_accepted_levels`（同步 `evaluation_constraints.levels`）、`update_consultant_constraints`；单次 ≤20 项。
  - `validate_edited_strategy(v2)`：先 `strategy_v2.validate_strategy_v2`，再镜像 query_builders 质量规则链（公司词不两两成对、≤2 词/组、组数封顶、电源岗禁裸公司词复用 `_requires_power_evidence`）。
  - `apply_strategy_item_edits(workflow_id, edits, note)`：状态闸门与 `revise_workflow` 一致（寻访已开始 → 409"不能原地替换"）；落**新** artifact（旧 artifact 不改写）；重编译 `query_plan_v1`；R3 不绕过——waiting_approval 时旧审批卡置 superseded 并经 `_create_approval` 换新（新卡带新快照 hash）。
- 端点：`POST /api/v1/workflows/{workflow_id}/strategy/edits`（app.py，走 `idem()` 幂等；LookupError→404、ValueError→409 中文 detail）。
- 前端：`asa-web/src/workflows/WorkflowStrategy.tsx` 新增可选 props（`strategyV2/workflowId/editable/onEdited`，不传则行为与旧版完全一致）；每组关键词显示目标画像（targets）与预期召回（step5 `expected_recall_per_tier`），按项编辑入口 + 两步确认删除；`WorkflowPanel.tsx` 提取 strategyV2 传入并加 `strategyEditable` 闸门。
- 测试：`tests/test_strategy_item_edits.py`（16）、`asa-web/src/__tests__/strategy-item-edit.test.tsx`（8）。
- 坑：①编辑后 `golden_candidate_replay_v1=None`（旧回放失效，快照按"无回放"视为有效）；②状态闸门与 revise_workflow 有同样的固有竞态（revise 用二次校验兜底，本实现未加）；③测试里模拟"寻访已开始"必须用 `waiting_external` 而非 `running`（`_recover_interrupted` 会把 running 改写为 paused/pending）。

### 2. 版本化推荐包 + 客户反馈闭环

- 新表（`scripts/asa_core/database.py` migration 5）：`recommendation_packages`（`UNIQUE(job_candidate_id, version)`，summary/evidence/risks/verification_questions 四个 JSON 块）+ `recommendation_package_feedback`（`UNIQUE(package_id, request_id)` 表级幂等；feedback_type: approved/interview/rejected/hold/other）。
- `scripts/asa_core/service.py`：`consultant_recommendation_commit` 成功后同事务生成推荐包 v1（无当前评估时 `evidence.status='no_current_assessment'` 如实标注）；重复确认/并发 IntegrityError 回读现有包不重复生成；历史已确认缺包的再次确认时补齐。新增 `list_recommendation_packages / get_recommendation_package / record_package_feedback`。
- 端点：`GET /api/v1/candidates/{id}/recommendation-packages`、`GET /api/v1/recommendation-packages/{package_id}`、`POST /api/v1/recommendation-packages/{package_id}/feedback`（写反馈并回写 `candidate_events` event_type=client_feedback）。
- 前端：`asa-web/src/panels/RecommendationPackages.tsx`（新组件挂 CandidatePanel aside）；`RecommendationDecision.tsx` 回执区分"已生成/生成中"。
- 测试：`tests/test_recommendation_packages.py`、`asa-web/src/__tests__/recommendation-packages.test.tsx`。
- 坑：①只有 v1，无"重新生成/升版"入口；②客户反馈走新表，未接旧 `client_feedback_events`（旧报表口径如需可见要做视图或双写）；③`feedback_time` 未做格式校验。

### 3. 评估证据三桶视图

- `asa-web/src/panels/CandidateAssessment.tsx`：默认三桶视图（直接证据=certain+带 ref / 合理推断=inferred+带 ref / 未知项=缺 ref、置信度缺失、全部 risks 条目），按维度视图保留可切换；UI 内如实写明归桶口径。
- 测试：`candidate-assessment.test.tsx` 新增 5 例。
- 坑：证据只有维度级 confidence（无条目级），归桶是保守规则；若其他测试钉死默认视图需注意。

### 4. 工作流摘要"预计产出"

- `asa-web/src/workflows/BusinessDeliverySummary.tsx`：新增「预计产出」行。非终态"预计召回 N 条 · 目标 M 人"（step5 expected_recall_per_tier 求和 + external_request.target_count，回落审批 preflight.target_count）；终态"实际召回 A（预期 N）· 实际入库 B（目标 M）"；无数据"未设定预期产出"。
- 坑：目标人数口径是寻访目标，终态对齐"实际入库"；想对齐"实际评估人数"需后端补字段。

### 5. 岗位 Brief（Codex session 已完成，本次只修了重复渲染）

- `asa-web/src/panels/JobBrief.tsx`：岗位要解决什么/必须先满足/优先从哪里找/明确不找什么 + 启动前待确认。**顾问约束不在 Brief 里重复展示**（下方策略区已有，重复会让 `getByText` 撞多元素）。

## 三、二期：顾问知识飞轮

### 1. 技能本体 + 职级映射（新知识文件）

- `kb_skill_ontology_semiconductor_v1.json`（47 技能/5 族：power_supply、motion_control、packaging_equipment、precision_mechanical、technical_marketing；每条含 aliases/related/evidence）。
- `kb_level_mapping_v1.json`（6 职级带 + 4 体系对照：互联网 P/M 序列、半导体原厂、设备厂）。
- 两目录 `asa-web/knowledge_base/` 与 `/Users/messi/Documents/ASA/knowledge_base` 是硬链接镜像（P2-d 核实为同一目录 inode 24699526），单点写入即同步。
- `scripts/a_system_agent/knowledge_base.py`：`load_skill_ontology / normalize_skill / related_skills / load_level_mapping / map_level`，缺文件优雅降级。
- 消费：策略 step3 职级优先查映射库（`level_source=kb_level`）；step4 关键词 canonical 归一去重（保留首个原词不破坏召回，组上挂 `skill_ontology{source=kb_skill}`）；评估 `build_llm_payload` 注入归一技能词。不改评分门槛。
- 测试：`tests/test_skill_ontology_level_mapping.py`（19）。
- 坑：step3 kb_level 命中会覆盖 LLM fragment 的 accepted_levels（需求指定语义）；本体未覆盖 fab 工艺/质量/YE/FPGA 族（原型里这些 nodes 置空待扩族）。

### 2. 岗位原型库 3→11 + 评估直接消费

- 新增 8 个原型（均带 skills_ontology_nodes + level_mapping.level_band 互引）：seed_silan_power_expert_v1、seed_pengxinxu_fab_process/equipment/yield/quality_v1、seed_changyue_electrical_v1、seed_changyue_failure_analysis_v1、seed_sukesi_fpga_v1。来源全部是真实 P0 缺口与 cases/。顺带修了 seed_silan_tme 的 4 个非 canonical 词。
- `strategy_v2.py`：`_ARCHETYPE_TITLE_TOKENS` 新增 8 组匹配 token（刻意避开裸词，有回归测试锁住）；输出新增 `archetype_matched` 布尔 + 未命中时 `archetype_note`（"无可用原型"显式说明）。
- `candidate_assessment.py`：`build_archetype_reference()`（:835）+ `build_llm_payload` 新 `archetype` 参数，命中注入 `archetype_reference`（source=kb_archetype），无命中完全不注入；`signal_stats.archetype_reference` 留痕。
- 测试：`tests/test_job_archetype_expansion_kb2.py`（14，含全部 seed schema 校验 + 三库引用一致性 + 11 个真实 P0 标题命中）。
- 坑：新原型目标公司池多为"行业常识推导、待顾问校准"（confidence medium/low，文件内已标注）。

### 3. knowledge_proposal 提案链路

- 新模块 `scripts/asa_core/knowledge_proposals.py`；新表 migration 6 `knowledge_proposals`（`UNIQUE(proposal_type, content_key)` 幂等，content_key=sha256(type|canonical content)，已拒绝的同内容不会复活）。
- 三条确定性生成规则：停止原因聚类（客户×原因 ≥3 且可结构化五枚举）→ negative_rule；包反馈 rejected/hold 聚类（≥2）→ negative_rule；确认推荐现职公司聚类（≥2 且不在图谱）→ company_graph_entry。低于阈值只进 candidates 不生成提案。
- 确认链：preflight（300s 内存令牌 + 内容签名）→ decision（一次性令牌，漂移/过期 409，reject 必填 note）。accept 落库：company_graph_entry → 图谱 JSON 追加（带 proposed_by=consultant_confirmed）；其余 → `kb_agent_confirmed_rules_v1.json`。写知识文件：tmp + os.replace 原子替换，**写后重建硬链接镜像**（原子替换会断链）。
- 端点：`GET /api/v1/knowledge-proposals[-/{id}]`、`POST .../generate`、`POST .../{id}/preflight`、`POST .../{id}/decision`。
- 前端：`asa-web/src/panels/KnowledgeProposals.tsx`，挂 Agent 首页工具行（未新增主 tab）。
- 测试：`tests/test_knowledge_proposals.py`（10）。
- 坑：确认令牌内存存储，Core 重启失效（与 consultant_recommendation 同口径）。

### 4. 公司校准工具

- 新模块 `scripts/asa_core/company_calibration.py`；新表 migration 7 `company_calibrations`（company_key=normalize_client_name(图谱条目名) UNIQUE；字段 track/product_lines/skill_tags/level_system/no_poach/non_compete/note/status/version）。
- 覆盖层：`knowledge_base.load_calibration_overlay(db_path)` + `apply_calibration_overlay(graph, overlay)`。图谱 JSON 保持原始名单不动；仅 status='calibrated' 进覆盖层；命中标注 source=consultant_calibrated；孤儿校准只留痕不新建条目；缺库/缺表/失败 → 空覆盖层，输出与现状逐字节一致。
- 端点：`GET /api/v1/company-calibrations[-/progress|-/{company_key}]`、`POST /api/v1/company-calibrations`（同内容重提不 bump version）。
- 前端：`asa-web/src/panels/CompanyCalibration.tsx`，挂 Agent 首页工具行第三个开关（没挂 Radar 是因为 Radar 加载失败会 early-return 连带隐藏；校准队列是全量 589 家与雷达榜单不是一个集合）。
- 测试：`tests/test_company_calibration.py`（6）+ `company-calibration.test.tsx`（7）。

### 5. 消费侧闭环（最后接入，关键）

- `capability_runtime.run_search_strategy`：`load_company_graph` 后、`derive_graph_pool` 前合并校准覆盖层（非空才合并，trace 标注）；`candidate_assessment.graph_hits` 同样合并后匹配（只增强命中信息不改评分）。
- 确认规则消费：`negative_rules.load_confirmed_negative_rules`（进五类清单，source=consultant_confirmed + proposal_id）；`normalize_skill` 确认别名优先于内置；`map_level` 确认规则优先于内置库。全部"有则增强、无则现状"，坏文件/缺文件不炸。
- 无进程内缓存：每次运行直接读小表/JSON，校准提交后下一次运行即生效。
- `strategy_v2._POOL_SOURCES` 增加 `consultant_calibrated`（否则 validate 拒绝）。
- 测试：`tests/test_kb_confirmed_consumption_s7.py`（12）。
- 坑：skill_alias/level_mapping 类提案目前没有写入方（生成器只产出 negative_rule/company_graph_entry），loader 已按宽容多键兼容预留。

### 6. 回放集扩展

- `scripts/strategy_replay_eval.py`：新增 evidence_coverage（策略要素来源标注率，step4 关键词组无 source 字段显式不计入）、noise_rate（=1−precision，方向感知）、recommendation_rate_proxy（明确标注 proxy）；`--compare <baseline.json>` 基线 diff（倒退退出码 1，缺指标按倒退处理宁严勿宽）。
- `scripts/assessment_replay.py`：`--metrics` 确定性指标模式（五维覆盖率/证据条数分布/unknown 占比/inferred_ratio），不进 CI。
- 基线：`tests/test_strategy_replay_s4.py` REPLAY_BASELINE 纳入新指标（首版真实值）；`docs/ASA_strategy_replay_baseline_s4-4_2026-07-23.md` 追加二期扩展指标一节。
- 坑：两 case evidence_coverage 都是 1.0（确定性模式无 llm_inferred），约束力要等 LLM 要素引入后才体现；推荐率 proxy 接入真实顾问确认数据后应替换。

## 四、三期：项目交付驾驶舱

### 1. 今日工作台五分组 + 首页挂载

- `scripts/asa_core/analytics.py` workbench() 五 lane：`decision 待判断`（待复核/待核验/待联系/已回复 + R2/R3 待审批）、`running 运行中`（queued/running/waiting_external）、`waiting_client 待客户`（进行中且 stage/事件命中客户等待 token）、`risk 风险/逾期`（超时/异常队列）、`delivered 最近交付`。summary 保留 pending 作 decision 兼容别名。普通"进行中"人选不进工作台（不以纯数量驱动）。
- `app.py` 一处修改：workbench flow 拉取从 queue="今日待办" 改为 "全部进行中"（让已推荐待反馈人选可被推导）。
- 前端：`TodayWorkbench.tsx` 重构五 lane；`AgentWorkspace.tsx` AgentHome 实际挂载五 lane（这是真实首页；Overview.tsx 是死代码但 overview-status.test.tsx 依赖，保留并复用新组件）。
- 测试：`tests/test_asa_workbench_lanes.py`（3）。
- 坑：①**待客户 lane 靠文本 token 推导，真实库目前为 0**，需用真实推荐数据复核 token 覆盖度；②Core 重启后 running 工作流被 `_recover_interrupted` 置 paused，运行中 lane 主要靠 queued/waiting_external 填充（既有行为）。

### 2. 岗位自动周报

- 新模块 `scripts/asa_core/job_weekly_report.py`（零 LLM）。五区块口径：漏斗（与上一期周报 artifact 快照对比，无基线如实标注）、有效推荐（本周确认+累计+包反馈五类计数）、渠道质量（agent_sourcing_funnel 本周窗口）、风险（逾期待办/待评估积压/触达≥5 且 0 回复）、建议（纯规则阈值，无触发如实"未触发任何规则建议"）。
- 端点：`POST /api/v1/jobs/{id}/weekly-report`（同周幂等 upsert 同 artifact、version 自增、history 上限 10；跨周新建）、`GET /api/v1/jobs/{id}/weekly-reports`。
- 前端：`asa-web/src/panels/JobWeeklyReport.tsx` 挂 JobPanel（RecommendationMetricsCard 之后），复用 WorkflowArtifactDialog 查看。
- 测试：`tests/test_job_weekly_report.py`（5）。
- 坑：触达/回复口径依赖 candidate_events 的 liepin_outreach/candidate_outreach/candidate_message_received 事件类型，新触达通道不写这些类型会漏报（偏保守，不误报）。

### 3. 面试/Offer/入职一等事件

- `scripts/asa_core/service.py` `LIFECYCLE_EVENT_TYPES` 六枚举：interview_scheduled/interview_completed/offer_extended/offer_accepted/offer_declined/onboarded，各带默认状态与跟进待办口径（task_type + 天数）。
- `record_lifecycle_event()`：写 candidate_events + 自动生成 followup_tasks 待办；不对外发任何消息。
- 端点：`POST /api/v1/candidates/{id}/lifecycle-events`（idem 幂等 + request_id 表级去重双层）。
- 前端：`asa-web/src/panels/LifecycleEventForm.tsx` 挂 CandidatePanel 业务时间线；`shared/format.ts` eventStatusLabel 扩展 + lifecycleEventLabel/Tone。
- 测试：`tests/test_candidate_lifecycle_events.py`（5）+ `candidate-lifecycle-events.test.tsx`（8）。
- 坑：表单未暴露 event_status 选择（面试通过/未通过靠备注表达）；request_id 去重是先查后插（本地单顾问无并发风险，多客户端需加唯一索引）。

### 4. 交付记分卡（固定分析指标）

- `analytics.py` CATALOGS 新增 `delivery_scorecard 交付记分卡`，7 条指标：有效推荐率（与 consultant_recommendation_metrics 同口径）、推荐至面试转化（确认后面试信号两路：package feedback interview / client_feedback event_status；确认前事件不计）、渠道质量×2（复用 channel_performance 口径）、岗位关闭周期×2（jobs.closed_at − created_at 中位/平均；archived 但 closed_at 空的不计）、复盘完成率（终局寻访工作流中有 strategy_review artifact 的比例；cancelled/superseded 不计分母）。
- 每项带 value/sample_size/note 中文口径；4 个 per-job 钻取 section。
- 真实库冒烟：有效推荐率 0.0%（样本 190）、复盘完成率 81.2%（样本 16）、关闭周期 21.9 天（样本 1）、转化 null 如实空态。
- 测试：`tests/test_asa_delivery_scorecard.py`（6）+ `delivery-scorecard.test.tsx`（3）。
- 坑：复盘口径是 SQL 近似（search_strategy artifact 或 funnel 记录作代理）；面试转化短期会是空态（确认推荐刚起步），这是如实呈现。

## 五、遗留事项（按优先级）

1. **git 提交**：约 150 个文件未提交，当前是合理提交点（用户尚未授权提交）。
2. **待客户 lane 真实数据复核**（token 覆盖度）。
3. 推荐包升版入口（评估更新后 regenerate）。
4. 客户反馈新旧表口径统一（视图或双写）。
5. 技能本体扩族（fab 工艺/质量/YE/FPGA），回填新原型的空 nodes。
6. 回放推荐率 proxy → 真实口径替换。
7. 策略编辑的状态闸门二次校验（对齐 revise_workflow）。
8. 一期验收未做真实 P0 岗位端到端寻访（外部渠道动作需顾问 R3 触发，未自动执行）。

## 六、协作约定提醒（本项目既有红线）

- 写操作一律预检 + 幂等（Idempotency-Key + request_id）+ 结果回读 + 冲突 409 中文文案；R3 单次审批不可绕过。
- 知识库两目录硬链接镜像：写 JSON 知识文件必须原子替换 + 重建镜像（参考 knowledge_proposals.py 的实现）。
- 测试运行方式：后端 `PYTHONPATH=scripts python3 -m pytest`（不是 unittest）；隔离 DB fixture 照抄 `tests/test_consultant_recommendation.py`（SOURCE_DB backup 到 tmp_path）；知识库测试用 tmp 目录 + ASA_KNOWLEDGE_BASE_DIR 覆盖，绝不写真实知识库。
- 前端共享文件（api.ts/styles.css/app.py）追加式编辑；不引入 any/prompt/confirm/alert；主 tab 保持四个。
- 验证三层门禁见 `docs/ASA_提速方案_v1_20260723.md` 与 AGENTS.md；UI 变更里程碑需重生成截图基线。
