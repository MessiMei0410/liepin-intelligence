# S4-4 策略回放评测基线（士兰微 v1.1 / 长越 v1.2）

- 生成日期：2026-07-23（首次基线，S4-4 交付）
- 生成方式：`PYTHONPATH=scripts /usr/local/bin/python3 scripts/strategy_replay_eval.py --json`（确定性模式，FakeLLM deterministic_fallback，临时库 + 真实 KB 只读）
- 口径来源：PRD `docs/ASA_PRD_S4_sourcing_strategy_agent_2026-07-23.md` §6；指标口径与公司名归一规则的完整定义见 `scripts/strategy_replay_eval.py` 模块 docstring
- 回归门槛：`tests/test_strategy_replay_s4.py` 的 `REPLAY_BASELINE` 常量与本文件数值一致，指标不许倒退

## 口径摘要

- **回放方式**：每个 case 按文件内容构造岗位上下文（士兰微 L1 客户一手语料 / 长越 L3 仅 JD），跑 `capability_runtime.run_search_strategy` 全链（确定性模式），对产出的 strategy_v2 评分。
- **① 目标池重合度**：Agent step2 的 T1/T2 公司池 vs case T1/T2 参考池（T3 不计入；泛化条目剔除；归一：大小写/空白/括号别名/公司后缀剥离/斜杠拆分，包含匹配短键 ≥3，可走图谱全名归一）。
- **② 关键词有效率**：case 标注有效关键词组被 Agent step4 覆盖的组级比例（组内词覆盖率 ≥0.5 记命中）。
- **③ 锚点完整率**：四锚点逐锚 0（缺失）/0.5（锚定偏差）/1（与参考答案重合），取均值。
- **汇总**：两 case 指标的算术均值。

## 基线数值（2026-07-23 实跑）

| case | 定级 | ① recall | ① precision | ② 关键词 | ③ 锚点 |
| --- | --- | --- | --- | --- | --- |
| case_silan_tme（士兰微 TME，v1.1，L1） | L1 ✓ | 1.000（15/15） | 0.652（15/23） | 1.000（3/3 组） | 1.000 |
| case_changyue_equipment（长越 自动化软件高工，v1.2，L3） | L3 ✓ | 0.000（0/20） | 0.000（0/8） | 0.000（0/2 组） | 0.375 |
| **汇总（均值）** | — | 0.500 | 0.326 | 0.500 | 0.688 |

生成模式均为 `deterministic_fallback`；士兰微 step2 来源 `kb_profile=22 + kb_graph=8`，长越 `kb_graph=8`。

## 未命中明细（后续改进的输入，不许只报分）

### case_silan_tme（士兰微）

- case 池未覆盖：**无**（T1 4 家 + T2 11 家全部由岗位原型 kb_profile 池命中）。
- Agent 池多出 8 家（全部 source=kb_graph，拉低 precision 至 0.652）：上海巴玛克电气技术、冠礼控制科技（上海）、杭州慧翔电液技术开发、江阴市天马电源制造、苏州佰控传感技术、上扬软件（上海）、上海喆塔信息科技、上海微电子装备（集团）。
  → 图谱按「控制/电源」bigram 召回设备类公司，与器件原厂 TME 池语义不符；图谱召回 query 构造（title+ability_keywords）是后续改进点。
- 关键词组、四锚点：全部命中（L1 客户一手语料 + 原型关键词组直挂）。

### case_changyue_equipment（长越，L3 裸跑）

- case 池未覆盖 **20/20**：ASMPT、K&S（库力索法）、BESI、新益昌、凯格精机、华封科技、恩纳基智能装备、普莱信、大族封测、快克智能、拓荆科技、晶盛机电、世禹/景焱、浙江达仕科技、上海光键、苏州艾科瑞思、常州铭赛机器人、芯钛科（上海）、博纳半导体（浙江）、嘉兴景焱。
- Agent 池 8 家全部多出（kb_graph）：上海果纳半导体技术、尊芯（上海）半导体科技、无锡芯享信息科技、江苏泰治科技股份、上海中艺自动化系统、上海凌恒工业自动化、上海喆塔信息科技、上海孤波科技。
  → L3 仅 JD 时图谱 query 为「自动化软件高级工程师 + 运动控制/EtherCAT/TwinCAT/RTOS/多轴」，按 自动化/软件/平台 召回，与顾问校准的键合/固晶设备池零重合——缺的正是「键合/固晶/封装设备」场景词，即 L3 提问清单要补的锚点。
- 参考池剔除泛化条目 4 条（不计分母）：其他 die bonder/wire bonder 设备商、拓荆/中微/北方华创等设备商运动控制岗、机器人/直线电机平台公司、精密测量设备公司。
- 关键词组 0/2 命中：motion_control_core、equipment_scene 均未覆盖——确定性回退计划无渠道查询，step4 为空（L3 无顾问搜索词输入）。
- 锚点：客户的客户 0（缺失，参考=封测厂/先进封装产线）；竞争格局 0（缺失，参考=T1 公司池）；产品/技术线 1（运动控制/EtherCAT 等与参考重合）；场景/赛道 0.5（Agent 锚到「机器人」，参考「半导体封装设备」，锚定偏差）。

> 长越三指标为 0 是该 case 声明的 L3 裸跑真实基线（meta：友商/客户锚点全部缺失），不是评测 bug；
> 回归门槛锚定的是「不许倒退」，改进（如 L3 提问清单答案回填、图谱 query 加场景词）应推动指标上行后再更新基线。

## 基线更新流程（策略生成逻辑改动后）

1. 跑 `PYTHONPATH=scripts /usr/local/bin/python3 scripts/strategy_replay_eval.py --json` 拿新指标与明细；
   也可用 `--compare <旧基线.json>` 自动逐项 diff（按指标方向判倒退，有倒退退出码 1）；
2. 对比 `tests/test_strategy_replay_s4.py` 的 `REPLAY_BASELINE` 与本文件，逐项核对未命中明细变化；
3. 确认是能力提升（非口径漂移/倒退洗白）后，手动更新测试常量（改注释日期）并同步本文件数值与明细；
4. 指标下降一律视为回归——修策略生成逻辑，不得改基线放行。

## 二期扩展指标（2026-08-05，additive）

新增三指标，原①②③口径与门槛语义不变；指标方向登记在 `scripts/strategy_replay_eval.py` 的
`METRIC_DIRECTIONS`（noise_rate 越低越好，其余越高越好），回归门槛与 `--compare` 均按方向判定。

### 口径定义

- **④ 证据覆盖率（evidence_coverage）**：选择「策略要素来源标注」口径而非 scoring.py 的
  候选人级 evidence_coverage（回放 case 无候选人评分数据，无法对齐该口径）。
  统计 strategy_v2 产物中带 source 字段的三类要素里有依据来源的占比：
  - 目标池公司（T1/T2）：有依据 = source ∈ {client_doc, kb_profile, kb_graph}；llm_inferred 无依据；
  - present 锚点（missing 锚点不计分母）：有依据 = source ∈ {client_doc, jd, consultant, kb_archetype}；
  - 约束（negative_rules 中 rule 文本非空条目；空 rule「不适用」留痕条目不计入）：
    有依据 = source ∈ {client_doc, jd, consultant, kb_profile, restricted_client}；
  - step4 关键词组无 source 字段，无法确定性判定来源，显式不计入。
- **⑤ 噪音率（noise_rate）**：= 1 − pool_precision，显式输出；precision 语义不变。方向：越低越好。
- **⑥ 推荐率 proxy（recommendation_rate_proxy）**：回放 case 无「顾问确认可推荐」结果数据，
  真实推荐率无法确定性回放，故输出 proxy 并明确标注，不伪造口径：
  Agent T1/T2 池中「命中 case 参考池 且 source ≠ llm_inferred」的公司占比。
  顾问确认口径接入后应替换本 proxy。

### 首版基线数值（2026-08-05 实跑，首次以当日真实值入基线，非历史目标值）

| case | ④ 证据覆盖率 | ⑤ 噪音率（↓好） | ⑥ 推荐率 proxy |
| --- | --- | --- | --- |
| case_silan_tme（士兰微 TME，v1.1，L1） | 1.000（31/31：池 23/23、锚点 4/4、约束 4/4） | 0.3478（= 1 − 0.6522） | 0.6522（15/23） |
| case_changyue_equipment（长越 自动化软件高工，v1.2，L3） | 1.000（10/10：池 8/8、锚点 2/2、约束 0/0） | 1.000（= 1 − 0） | 0.000（0/8） |
| **汇总（均值）** | 1.000 | 0.6739 | 0.3261 |

说明：两 case 证据覆盖率均为 1.0——当前确定性模式下全部要素来自 KB/JD/客户语料，
无 llm_inferred 要素；该指标的真实约束力在引入 LLM 生成要素后体现（llm_inferred 出现即拉低）。
长越约束 0/0：L3 裸跑五类排除清单全部不适用（rule 为空），无计入要素，不拉低覆盖率。

### 基线 diff 工具

`PYTHONPATH=scripts python3 scripts/strategy_replay_eval.py --compare <baseline.json>`：
baseline 为本脚本 `--out`/`--json` 产出的报告文件；逐 case × 指标输出 baseline/current/delta
与倒退标记（按 METRIC_DIRECTIONS 方向），基线缺的新指标标 "new"，当前缺基线已有指标按倒退处理；
有倒退退出码 1、无倒退 0、文件错误 2；与 `--json` 同用时 diff 并入 JSON 的 "compare" 键。

### 评估链指标化回放（assessment_replay.py --metrics）

`scripts/assessment_replay.py` 新增 `--metrics` 可选模式（盲评 markdown 导出不变），
对生成结果输出确定性指标 JSON：五维覆盖率（dimension_coverage，有非空 verdict 的维度占比）、
证据条数分布（evidence_distribution：kept 的 min/max/avg + stripped 总数）、
unknown 占比（unknown_ratio：枚举字段 == "unknown" 的比例，字段清单见脚本 docstring）、
推测维度占比（inferred_ratio，沿用原口径）。评估回放不进 CI 门槛，供变更前后对比使用。
