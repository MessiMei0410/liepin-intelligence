# 任务卡 OC1：OpenCLI 升主渠道——证据修复 + 单岗位试点（2026-07-27）

> 批处理模板：改动范围 / 红线 / 验收标准 / 门禁层级 / 是否需部署
> 背景：A/B（`opencli/READONLY_AB_RESULT_2026-07-21.md`）证明 OpenCLI 与生产 runner 质量平齐（成功率/一致性 100%、重合 10/10、30/30）且快 37%/18%；迁移规则"严格更优"在双满分下永远不可达，经用户 2026-07-27 拍板修订为"质量平齐 + 更快 + 故障可回退"。
> 执行：Codex。协同纪律同 S7-3：他人未提交改动不碰；git add 逐文件显式；共享文件只追加。

## 迁移规则修订（用户已批准）

业务动作迁移到 OpenCLI 的条件由"严格更优"改为同时满足：

1. 受控 A/B 质量平齐（成功率/一致性/相对召回不劣于生产基线）——已满足（07-21）；
2. 必填字段完整度不劣于生产——已满足；
3. 速度不劣于生产——已满足（快 37%/18%）；
4. 故障时可自动回退生产 runner——本期 Phase B 落地；
5. ASA 审批/去重/入库/归因/审计层保持权威——全程不变。

## 本期范围（两个阶段）

### Phase A：影子证据层修复

1. 影子对比补充"分数门之前"的原始计数（`shadow_raw_count`/`shadow_gated_out`/`baseline_file_count`），让 0/0 对比可解读（是召回空还是分数线筛空）；
2. 采样策略从"固定第一个 query"改为"优先选基线非空的 query"（`--queries-json` 传入全量词表，脚本内逐词探测基线，全空时回退第一词）；artifact 记录选词依据；
3. 契约测试跟进（`tests/test_opencli_sourcing_shadow.py`）。

### Phase B：单岗位试点"OpenCLI 主跑 + 生产兜底"

1. `capability_runtime` 增加 opt-in 开关 `opencli_primary`（请求级）/ `ASA_OPENCLI_PRIMARY`（环境级），默认关闭；
2. 开启时：该渠道先由 OpenCLI 适配器召回（分数门/详情采集/完整度校验复用生产逻辑），失败、被阻断或 0 行时**自动回退生产 runner**；
3. 渠道产物文件形状与下游完全兼容（intake/归因/审计零改动）；`channel_runs` 与结果负载记录 `recall_engine`（opencli / production_fallback / production）；
4. 契约测试新增 `tests/test_opencli_primary_recall.py`：默认关闭、回退触发、行形状过入库 normalize、红线负向扫描。

## 红线

- OpenCLI 只接管**召回**；详情采集用生产 `capture_resume_details`/`capture_candidate_details`；入库 dry-run→apply、去重、归因、审计链路零改动；
- 不自动触达：本卡不涉及任何对外动作；R3 审批语义不变；
- 完整度门槛不松：`resume_capture_status != complete` 一律不入库（生产同标准）；
- 影子契约不破：`affects_intake=false` / `affects_outreach=false` 保持，影子产物不进合并文件；
- 证据强约束：对比 artifact 只写计数与单向 hash，不落候选人明文。

## 验收标准

1. Phase A：契约测试全绿；新一次真实寻访后影子 artifact 出现 raw/gated 计数与非空基线采样记录；
2. Phase B：`opencli_primary=true` 时对试点岗位（士兰微｜技术市场经理/总监（PC电源））完成一轮真实寻访——OpenCLI 召回成功则 `recall_engine=opencli`，人为制造失败则回退 `production_fallback`；入库/归因/审计产物与生产路径一致；
3. 门禁全绿（见下）。

## 门禁层级

- L1 `npm run ci:fast` + L2 `npm run test:contract`（asa-web）+ 仓根 `python3 -m unittest discover -s tests`（677 项基线：1 failed / 36 errors 为预存，不得新增）；
- 真实验证：验收 2 真跑（CDP 9223 在线）。

## 部署

- 代码落仓即可；默认关闭，不影响生产路径；试点数据达标后再议 Phase C（切默认 + 生产反向影子两周），另开任务卡。
