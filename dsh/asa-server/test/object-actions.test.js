// 轮末对象操作入口聚合单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：tool/result meta.object_refs → done 的 suggested_actions/references
// 提取、type+id 去重保序、上限（suggested_actions ≤4 / references ≤8）、非法项剔除。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  createObjectRefCollector,
  REFERENCES_MAX,
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
    assert.deepEqual(suggested_actions, [
      { type: "open_workflow", id: "workflow_aaa", label: "查看并审批" },
    ]);
    // references 面向前端对象卡：approval_id 为 asa-server 内部附加信息，不下发。
    assert.deepEqual(references, [
      { type: "workflow", id: "workflow_aaa", label: "R3 外部寻访审批" },
    ]);
  });

  it("候选人与岗位分别映射 open_candidate/open_job，按出现顺序", () => {
    const collector = createObjectRefCollector();
    collector.add([{ type: "candidate", id: 531, label: "张三", subtitle: "某半导体" }]);
    collector.add([{ type: "job", id: 142, label: "电源专家", subtitle: "士兰微" }]);
    const { suggested_actions, references } = collector.outputs();
    assert.deepEqual(suggested_actions, [
      { type: "open_candidate", id: 531, label: "打开人选" },
      { type: "open_job", id: 142, label: "打开岗位" },
    ]);
    assert.deepEqual(references, [
      { type: "candidate", id: 531, label: "张三", subtitle: "某半导体" },
      { type: "job", id: 142, label: "电源专家", subtitle: "士兰微" },
    ]);
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

  it("上限：suggested_actions ≤4、references ≤8（超出截断、保序）", () => {
    const collector = createObjectRefCollector();
    collector.add(Array.from({ length: 12 }, (_, index) => ({
      type: "workflow", id: `workflow_${index}`, label: `审批 ${index}`,
    })));
    const { suggested_actions, references } = collector.outputs();
    assert.equal(suggested_actions.length, SUGGESTED_ACTIONS_MAX);
    assert.equal(references.length, REFERENCES_MAX);
    assert.equal(suggested_actions[0].id, "workflow_0");
    assert.equal(suggested_actions[SUGGESTED_ACTIONS_MAX - 1].id, `workflow_${SUGGESTED_ACTIONS_MAX - 1}`);
    assert.equal(references[REFERENCES_MAX - 1].id, `workflow_${REFERENCES_MAX - 1}`);
  });
});
