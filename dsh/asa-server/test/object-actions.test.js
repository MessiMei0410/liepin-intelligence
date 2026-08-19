// 轮末对象操作入口聚合单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：tool/result meta.object_refs → done 的 suggested_actions/references
// 提取、type+id 去重保序、上限（suggested_actions ≤4 / references ≤8）、非法项剔除、
// candidate_list 名单卡在场时候选人引用/芯片抑制、候选人引用按 answer 文本命中过滤。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  createObjectRefCollector,
  REFERENCES_MAX,
  SAME_TYPE_ACTIONS_MAX,
  SUGGESTED_ACTIONS_MAX,
} from "../lib/object-actions.js";

describe("createObjectRefCollector", () => {
  it("无对象时 outputs 返回空数组（done 不携带字段）", () => {
    const collector = createObjectRefCollector();
    assert.deepEqual(collector.outputs(), { suggested_actions: [], references: [] });
  });

  it("workflow_id 提取为 open_workflow 操作 + workflow reference（label 缺省有兜底）", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批", approval_id: "approval_1" },
    ]);
    const { suggested_actions, references } = collector.outputs();
    // 芯片 label 带对象标题，多个审批/工作流芯片可区分。
    assert.deepEqual(suggested_actions, [
      { type: "open_workflow", id: "workflow_aaa", label: "查看并审批：R3 外部寻访审批" },
    ]);
    // references 面向前端对象卡：approval_id/action_label 为 asa-server 内部
    // 附加信息，不下发。
    assert.deepEqual(references, [
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
  });

  it("候选人与岗位分别映射 open_candidate/open_job，芯片 label 带标题与 subtitle", () => {
    const collector = createObjectRefCollector();
    collector.add([{ type: "candidate", id: 531, label: "张三", subtitle: "某半导体" }]);
    collector.add([{ type: "job", id: 142, label: "电源专家", subtitle: "士兰微" }]);
    // 候选人 reference 要求 label 在 answer 中命中（相关性过滤见专项用例）。
    const { suggested_actions, references } = collector.outputs({ answer: "候选人 张三 匹配电源专家岗位。" });
    assert.deepEqual(suggested_actions, [
      { type: "open_candidate", id: 531, label: "打开人选：张三", subtitle: "某半导体" },
      { type: "open_job", id: 142, label: "打开岗位：电源专家", subtitle: "士兰微" },
    ]);
    assert.deepEqual(references, [
      { type: "candidate", id: 531, label: "张三", subtitle: "某半导体" },
      { type: "job", id: 142, label: "电源专家", subtitle: "士兰微" },
    ]);
  });

  it("label 缺省时芯片退回通用文案", () => {
    const collector = createObjectRefCollector();
    collector.add([{ type: "job", id: 142 }]);
    const { suggested_actions, references } = collector.outputs();
    assert.deepEqual(suggested_actions, [
      { type: "open_job", id: 142, label: "打开岗位" },
    ]);
    assert.deepEqual(references, [{ type: "job", id: 142, label: "岗位" }]);
  });

  it("同类对象芯片只保留前 2 个，references 保留完整列表", () => {
    // 2026-08-19 验收：asa_jobs 列表结果曾生成一排一模一样的“打开岗位”芯片。
    const collector = createObjectRefCollector();
    collector.add([
      { type: "job", id: 137, label: "机械高级工程师", subtitle: "长越科技" },
      { type: "job", id: 111, label: "技术市场经理", subtitle: "士兰微" },
      { type: "job", id: 154, label: "电源专家", subtitle: "士兰微" },
      { type: "job", id: 160, label: "固晶设备专家", subtitle: "长越科技" },
    ]);
    const { suggested_actions, references } = collector.outputs();
    assert.equal(suggested_actions.length, SAME_TYPE_ACTIONS_MAX);
    assert.deepEqual(suggested_actions, [
      { type: "open_job", id: 137, label: "打开岗位：机械高级工程师", subtitle: "长越科技" },
      { type: "open_job", id: 111, label: "打开岗位：技术市场经理", subtitle: "士兰微" },
    ]);
    // references 不截断同类：完整列表由对象卡承载。
    assert.deepEqual(references.map((ref) => ref.id), [137, 111, 154, 160]);
  });

  it("同类上限按类型独立计数：岗位占满名额不影响人选/工作流芯片", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "job", id: 137, label: "机械高级工程师" },
      { type: "job", id: 111, label: "技术市场经理" },
      { type: "job", id: 154, label: "电源专家" },
      { type: "candidate", id: 531, label: "张三" },
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
    const { suggested_actions } = collector.outputs();
    assert.deepEqual(
      suggested_actions.map((action) => `${action.type}:${action.id}`),
      ["open_job:137", "open_job:111", "open_candidate:531", "open_workflow:workflow_aaa"],
    );
  });

  it("type+id 去重：多次工具结果重复出现只保留首个", () => {
    const collector = createObjectRefCollector();
    collector.add([{ type: "workflow", id: "workflow_aaa", label: "首次" }]);
    collector.add([{ type: "workflow", id: "workflow_aaa", label: "重复" }]);
    collector.add([{ type: "workflow", id: "workflow_bbb" }]);
    const { suggested_actions, references } = collector.outputs();
    assert.equal(suggested_actions.length, 2);
    assert.deepEqual(references.map((ref) => ref.id), ["workflow_aaa", "workflow_bbb"]);
    assert.equal(references[0].label, "首次");
    // label 缺省兜底
    assert.equal(references[1].label, "工作流");
  });

  it("非法项剔除：未知 type / 空 id / 非对象 不入列", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "goal", id: "goal_1" },
      { type: "workflow", id: "" },
      { type: "workflow" },
      null,
      "workflow_ccc",
      { type: "workflow", id: "workflow_ccc" },
    ]);
    collector.add(undefined);
    collector.add("not-an-array");
    const { suggested_actions } = collector.outputs();
    assert.deepEqual(suggested_actions, [
      { type: "open_workflow", id: "workflow_ccc", label: "查看并审批" },
    ]);
  });

  it("上限：suggested_actions ≤4（同类 ≤2）、references ≤8（超出截断、保序）", () => {
    const collector = createObjectRefCollector();
    // 混类型验证总上限：同类芯片先被 SAME_TYPE_ACTIONS_MAX 截断。
    collector.add(Array.from({ length: 12 }, (_, index) => ({
      type: "workflow", id: `workflow_${index}`, label: `审批 ${index}`,
    })));
    collector.add(Array.from({ length: 3 }, (_, index) => ({
      type: "job", id: index + 1, label: `岗位 ${index + 1}`,
    })));
    collector.add(Array.from({ length: 3 }, (_, index) => ({
      type: "candidate", id: index + 1, label: `人选 ${index + 1}`,
    })));
    const { suggested_actions, references } = collector.outputs();
    assert.equal(suggested_actions.length, SUGGESTED_ACTIONS_MAX);
    assert.equal(references.length, REFERENCES_MAX);
    assert.equal(suggested_actions[0].id, "workflow_0");
    // 同类只留前 2 个，随后轮到下一类型的首个对象。
    assert.deepEqual(
      suggested_actions.map((action) => action.id),
      ["workflow_0", "workflow_1", 1, 2, 1, 2].slice(0, SUGGESTED_ACTIONS_MAX),
    );
    assert.equal(references[0].id, "workflow_0");
    assert.equal(references[REFERENCES_MAX - 1].id, `workflow_${REFERENCES_MAX - 1}`);
  });

  it("candidate_list 名单卡在场：candidate references 与 open_candidate 芯片全抑制，workflow/job 保留", () => {
    // 2026-08-19 验收：名单类回答 action_card 已承载名单，下面再嵌 8 张候选人对象卡全是噪音。
    const collector = createObjectRefCollector();
    collector.add([
      { type: "candidate", id: 531, label: "张三", subtitle: "某半导体" },
      { type: "candidate", id: 532, label: "李四", subtitle: "某电子" },
      { type: "job", id: 142, label: "电源专家", subtitle: "士兰微" },
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
    const { suggested_actions, references } = collector.outputs({
      answer: "电源专家岗位候选人 张三、李四 的名单如下，关联 R3 外部寻访审批。",
      candidateListCard: true,
    });
    assert.deepEqual(suggested_actions, [
      { type: "open_job", id: 142, label: "打开岗位：电源专家", subtitle: "士兰微" },
      { type: "open_workflow", id: "workflow_aaa", label: "查看并审批：R3 外部寻访审批" },
    ]);
    assert.deepEqual(references, [
      { type: "job", id: 142, label: "电源专家", subtitle: "士兰微" },
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
  });

  it("candidate 引用按 answer 文本命中过滤：未提及的全弃（job/workflow 同规则，见专项）", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "candidate", id: 531, label: "张三" },
      { type: "candidate", id: 532, label: "李四" },
      { type: "candidate", id: 533 },
      { type: "job", id: 142, label: "电源专家" },
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
    // 回答只提到张三：李四（无文本命中）、无名候选人（无真实 label 可命中）、
    // 未提及的 job/workflow（2026-08-19 起同规则过滤）全部剔除；芯片与 references 同一判定。
    const { suggested_actions, references } = collector.outputs({ answer: "推荐候选人 张三。" });
    assert.deepEqual(references, [{ type: "candidate", id: 531, label: "张三" }]);
    assert.deepEqual(
      suggested_actions.map((action) => `${action.type}:${action.id}`),
      ["open_candidate:531"],
    );
  });
});

describe("job/workflow 引用相关性过滤（2026-08-19 验收：长越名单下出现电源专家芯片）", () => {
  it("未在回答中命中的岗位从芯片和 references 同步剔除；客户名命中可救回", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "job", id: 154, label: "电源专家", subtitle: "士兰微" },
      { type: "job", id: 137, label: "机械高级工程师", subtitle: "长越科技" },
    ]);
    // 回答只讲长越机械岗，未提电源专家/士兰微
    const out = collector.outputs({ answer: "长越科技机械高级工程师岗的存量名单已筛完，A-核心 20 人。" });
    assert.deepEqual(out.suggested_actions, [
      { type: "open_job", id: 137, label: "打开岗位：机械高级工程师", subtitle: "长越科技" },
    ]);
    assert.deepEqual(out.references, [
      { type: "job", id: 137, label: "机械高级工程师", subtitle: "长越科技" },
    ]);
    // 岗位名命中则精确过滤：回答点名"机械高级工程师"→ 只留 137，154（电源专家）剔除
    const byTitle = collector.outputs({ answer: "长越科技机械高级工程师岗的存量名单已筛完。" });
    assert.deepEqual(byTitle.references.map((ref) => ref.id), [137]);
    // 只提客户名、任何岗位名都未命中 → 触发兜底整组放回（宁可多不可丢）
    const byClient = collector.outputs({ answer: "长越科技这个岗位的名单已出。" });
    assert.deepEqual(byClient.references.map((ref) => ref.id), [154, 137]);
  });

  it("无回答文本时不过滤（兼容无 answer 调用方）", () => {
    const collector = createObjectRefCollector();
    collector.add([{ type: "job", id: 154, label: "电源专家", subtitle: "士兰微" }]);
    const out = collector.outputs({});
    assert.equal(out.references.length, 1);
  });
});

describe("相关性兜底：全未命中时整组放回 job/workflow", () => {
  it("回答以别的方式指代（无任何对象名命中）时放回非 candidate 对象", () => {
    const collector = createObjectRefCollector();
    collector.add([
      { type: "job", id: 154, label: "电源专家", subtitle: "士兰微" },
      { type: "candidate", id: 531, label: "张三" },
    ]);
    const out = collector.outputs({ answer: "这个岗位的情况如上所述。" });
    // 岗位名未命中 → 兜底放回 job；candidate 不参与兜底（维持全弃）
    assert.deepEqual(out.references, [{ type: "job", id: 154, label: "电源专家", subtitle: "士兰微" }]);
  });
});
