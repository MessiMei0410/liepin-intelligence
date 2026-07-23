# ASA 实施 PRD（S4）：寻访策略 Agent —— 资深顾问级策略生成与迭代闭环

日期：2026-07-23
撰写：Kimi（顾问已确认交接）
执行方：Kimi CLI
优先级：P1（在 ROUND2 任务包 T1-T5 全部完成后启动）
前置阅读：`docs/ASA_sourcing_strategy_capability_2026-07-23.md`（方法论）、`docs/ASA_PRD_strategy_agent_boundary_and_vertical_moat_2026-07-23.md`（战略）、`AGENTS.md`
数据资产（已入库，本 PRD 的直接输入）：

| 文件 | 角色 |
| --- | --- |
| `knowledge_base/seed_silan_tme_v1.json`（v1.1 定稿） | L1 校准基准 / 岗位原型 tme_computing_power |
| `knowledge_base/cases/case_changyue_equipment_v1.json`（v1.2） | L3 校准基准（锚点缺失推断 + 提问清单样例） |
| `knowledge_base/cases/case_pengxinxu_fab_v1.json` | L1.5 fab 基准 / restricted 层样例 |
| `knowledge_base/kb_seed_jiachi_equipment_v1.json` | 分类法 + 五类负向规则类型学 + 30 家目标池锚点 |
| `knowledge_base/kb_client_profiles_v1.json`（233 家） | 客户画像库 |
| `knowledge_base/kb_company_graph_jsj_v1.json`（589 家） | 公司图谱底图 |

---

## 0. 目标（可度量）

让 ASA 对任何新岗位产出"资深顾问级"寻访策略，并每轮自动复盘迭代：

1. **策略质量可对标**：Agent 生成的目标公司池与顾问/客户资料定的池子可测重合度（recall/precision），基线用士兰微、长越两个定稿 case。
2. **策略过程可干预**：策略 = 五步显式判断的结构化输出，每步可见、可被顾问逐条推翻，推翻动作自动成为学习信号。
3. **策略迭代有闭环**：每轮寻访结束自动诊断"策略错还是执行错"并给出 diff 式修订建议，作为"调整条件再搜"按钮的数据源。

**明确不做**：不改现有寻访执行链路（runner/审批/intake 不动）；不做任何形式的自动触达；复盘器 v1 用规则版，不上模型。

---

## 1. 输入分级（L1/L2/L3）

策略生成前必须先定级，定级结果写入策略对象 `input_level`：

| 级别 | 定义 | 处理流程 |
| --- | --- | --- |
| L1 | 有客户一手锚点资料（需求梳理表/项目管理表） | 直接结构化锚点，缺项才问 |
| L2 | 结构化 JD + 顾问口述补充 | Agent 抽锚点 → 缺锚点向顾问提问 → 补齐后生成 |
| L3 | 只有 JD | Agent 用知识库推断锚点，**每个推断标记 `inferred: true, confidence`**，并向顾问输出提问清单，顾问确认或修改后才执行寻访 |

**L3 主动提问（本 PRD 最高优先单点）**：当前系统等于永远 L3 裸跑。实现位置：Copilot 创建工作流前的策略生成环节（A System Agent 侧，`scripts/a_system_agent/`）。提问清单模板（按四锚点）：

- 这个岗位"客户的客户"是谁？（服务什么客户/终端）
- 对标友商/目标公司您有名单吗？
- 产线/工艺代际有没有硬过滤？（如"只看12寸"）
- 有没有禁挖名单/竞业限制/背景限制？

规则：四锚点中缺失 ≥2 且知识库无对应岗位原型时，**不得直接执行外部寻访**，先出提问清单；顾问说"直接搜"才允许带 `inferred` 标记继续，且策略对象记录 `consultant_override: true`。

## 2. 五步判断树与策略对象 v2

现有 `search_strategy` artifact（`agent_artifacts`，实测工作流中有 `artifact_type: "search_strategy"`）升级为 v2 schema；每步结构化、可 diff、可被顾问逐条推翻：

```json
{
  "schema_version": "strategy_v2",
  "input_level": "L1|L2|L3",
  "step1_job_essence": {"statement": "岗位本质一段话", "value_chain_role": "...", "confirmed_by": "consultant|inferred"},
  "step2_target_pool": [
    {"path": "same_layer|reverse|adjacent", "tier": "T1|T2|T3",
     "companies": [{"name": "...", "source": "client_doc|kb_graph|kb_profile|llm_inferred", "confidence": "high|medium|low"}],
     "rationale": "..."}
  ],
  "step3_level_mapping": {"accepted_levels": ["..."], "calibration_rule": "..."},
  "step4_keyword_groups": [{"group": "...", "targets": "绑定哪个画像", "terms": ["..."]}],
  "step5_expectation": {"expected_recall_per_tier": {"T1": 0}, "fallback_plan": "若 T1 召回<X 则放宽 Y"},
  "negative_rules": [{"type": "五类之一", "rule": "...", "source": "..."}],
  "consultant_edits": []
}
```

硬性约束：

- 公司池每家必须标 `path`（同层/逆向/相邻）与 `source`；`llm_inferred` 的公司在 UI 显示"待确认"标。
- 关键词组禁止不与公司池或产品技术词锚定的孤立方向词（防"PC电源"式偏差）。
- 研发岗默认逆向路径关闭（长越 v1.1 规律），启用需顾问逐岗确认；市场岗逆向默认开（士兰微规律）。
- 顾问在前端或 Copilot 推翻任一条目 → 追加进 `consultant_edits` 并写 `explicit_corrections` 学习信号（机制已存在，需接通）。

## 3. 知识库消费接口

**3.1 客户画像挂载（intake 钩子）**：岗位/工作流创建时按客户名查 `kb_client_profiles_v1.json`，命中则把画像（赛道/卖点/面试流程/用人偏好/目标池/注意事项）注入策略生成上下文与岗位详情上下文。长川科技在库（17 字段富画像）可作为首个联调样本。客户名匹配规则：精确 → 去括号/别名 → 模糊需人工确认。

**3.2 公司图谱查询**：`kb_company_graph_jsj_v1.json` 提供按赛道/主营业务/四分类标签的公司检索，供第 2 步公司池推导与"待确认"标记。**遵守图谱 governance：公司命中只用于召回和排序，必须回候选人详情核验本人证据**（写进策略 Agent 的系统约束）。

**3.3 restricted 层**：禁挖名单、话术红线、费率、顾问手机号、offer 金额只进策略约束与内部判断，**绝不进入 Copilot 通用回答、推荐报告或任何对外输出**。鹏新旭 case 的 `restricted` 字段为格式基准。

## 4. 排除规则引擎

策略生成的第 4 步之后强制过五类检查清单（`kb_seed_jiachi_equipment_v1.json` 的 `negative_rule_typology`）：

1. 在职保护名单（客户级禁挖名单合并）
2. 学历门槛
3. 身份/背景限制
4. 竞业协议排除
5. 稳定性筛选（如"五年三跳"）

每类输出"适用/不适用 + 依据"，写入 `negative_rules`。客户级禁挖名单（如鹏新旭 7 家）从 restricted 层读取，按客户持久化，新岗位自动继承。

## 5. 策略复盘器（v1 规则版）

每轮寻访收尾后（接 ROUND2 已建成的 `agent_sourcing_funnel`），生成结构化复盘：

- **策略错 vs 执行错判定**：
  - 召回量 < `step5_expectation` 的 50% → 策略（关键词/池太窄）
  - 召回正常但 detail_failed 占比高 / zero_attribution ∈ {session_expired, page_structure_changed, loading_incomplete} → 执行/渠道，不改策略
  - 入库正常但高分率 < 阈值 → 画像偏差（策略）或评分偏差（评估，转评估问题单）
- **修订建议**：以 strategy_v2 的 diff 形式输出（如"step2 增列 T2 公司 X、Y；step4 替换关键词组 B"），前端"调整条件再搜"按钮点击后展示该 diff，顾问可逐项采纳。
- 复盘对象持久化（新 artifact 类型 `strategy_review`），供回放评测与横向萃取。

## 6. 回放评测

新脚本（建议 `scripts/strategy_replay_eval.py`）+ 测试：

- 输入：`knowledge_base/cases/*.json` 中已定稿 case（当前：士兰微 v1.1、长越 v1.2）
- 指标：① 目标池重合度（Agent T1/T2 池 vs case 池的 recall/precision，公司名走图谱别名归一）；② 关键词有效率（case 标注有效组是否被覆盖）；③ 锚点完整率（四锚点命中比例）
- 门槛：策略生成逻辑（prompt/判断树/知识库接入）任何改动，回放三项指标不许倒退；纳入 Core 测试体系。

## 7. 分期与验收

| 期 | 内容 | 验收 |
| --- | --- | --- |
| S4-1 | 输入分级 + L3 提问清单 + strategy_v2 schema 落库 | L3 场景下 Copilot 先出四锚点问题；策略对象按 schema 存 artifact；`npm run ci` + Core 测试绿 |
| S4-2 | 知识库消费：客户画像挂载 + 图谱查询 + restricted 层边界 | 长川科技岗位自动挂画像；图谱公司带 source/confidence；restricted 字段不出现在任何 API 通用响应（契约测试断言） |
| S4-3 | 排除规则引擎 + 复盘器 v1 + "调整条件再搜"接 diff | 五类清单逐类留痕；#154 类 blocked 工作流产出复盘 artifact；前端可逐项采纳修订 |
| S4-4 | 回放评测进回归 | 两 case 三指标跑出基线并文档化；改动回归可执行 |

每期独立 commit、独立过守卫（`npm run ci` + Core 测试 + A 系统回归守卫），顺序执行不并行。

## 8. 约束（继承并强调）

- 候选人真实写入继续走 preflight/commit、幂等、审计、A 系统同步；OpenCLI 保持 read_only_shadow。
- 策略对象是学习资产不是执行旁路：外部寻访仍必须过一次性 R3 审批。
- 知识库 JSON 文件为事实源，运行时只读；知识库 Agent 的写维护流程另行立项（S5）。
- restricted 层边界违反视为 P0 事故：任何 restricted 字段出现在对外输出即回滚。

## 9. 待顾问决策（不阻塞 S4-1/S4-2）

1. 13 份客户手册飞书链接是否授权知识库消化（顾问登录态）。
2. 士兰微 T3 池初始排序（南芯>希荻微>TI>…）在首轮复盘后是否调整。
