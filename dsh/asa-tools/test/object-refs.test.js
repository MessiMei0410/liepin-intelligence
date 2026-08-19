// 只读工具 object_refs 投影单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：asa_approvals/asa_workflow/asa_candidates/asa_jobs 的 presentationMeta 把结果里的
// 业务对象 ID 投到 meta.object_refs（常驻服务器轮末据此聚合 suggested_actions/references），
// 且无结果时恒为 []（presentationMeta 不得返回 undefined）。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

describe("object_refs 投影", () => {
  it("asa_approvals：items → workflow 引用（带 approval_id）；label 优先 goal 标题、审批 title 降为命中别名", () => {
    const tool = registerTools().get("asa_approvals");
    const value = {
      ok: true,
      items: [
        { approval_id: "approval_1", workflow_id: "workflow_aaa", title: "R3 外部寻访审批", goal_title: "寻访" },
        { approval_id: "approval_2", workflow_id: "workflow_bbb", title: "", goal_title: "发布岗位" },
        { approval_id: "approval_3", workflow_id: "" }, // 无 workflow_id 剔除
      ],
    };
    // label 优先 goal 标题（同名审批可区分）；label 换成 goal 标题时原审批 title 作命中别名，
    // 空 title 不产生别名（专项见 approvals-label.test.js）。
    assert.deepEqual(tool.output.presentationMeta({}, value), {
      object_refs: [
        { type: "workflow", id: "workflow_aaa", label: "寻访", aliases: ["R3 外部寻访审批"], approval_id: "approval_1" },
        { type: "workflow", id: "workflow_bbb", label: "发布岗位", approval_id: "approval_2" },
      ],
    });
  });

  it("asa_workflow：value.workflow.workflow_id 提取，label 用 goal.title，args 兜底 id", () => {
    const tool = registerTools().get("asa_workflow");
    const value = {
      ok: true,
      goal: { title: "士兰微电源专家寻访" },
      workflow: { workflow_id: "workflow_aaa", current_stage: "寻访中" },
    };
    assert.deepEqual(tool.output.presentationMeta({ workflow_id: "workflow_aaa" }, value), {
      object_refs: [{ type: "workflow", id: "workflow_aaa", label: "士兰微电源专家寻访" }],
    });
    // 返回值缺 workflow 字段时 args 兜底，label 退化为默认文案
    assert.deepEqual(tool.output.presentationMeta({ workflow_id: "workflow_bbb" }, { ok: true }), {
      object_refs: [{ type: "workflow", id: "workflow_bbb", label: "工作流" }],
    });
  });

  it("asa_candidates：items → candidate 引用（id 为 job_candidates 关系 ID）", () => {
    const tool = registerTools().get("asa_candidates");
    const value = { ok: true, items: [{ id: 531, name: "张三", current_company: "某半导体" }, { name: "无 id" }] };
    assert.deepEqual(tool.output.presentationMeta({}, value), {
      object_refs: [{ type: "candidate", id: 531, label: "张三", subtitle: "某半导体" }],
    });
  });

  it("asa_jobs：items → job 引用；空结果/异常值恒为 []", () => {
    const tool = registerTools().get("asa_jobs");
    assert.deepEqual(tool.output.presentationMeta({}, { ok: true, items: [{ id: 142, title: "电源专家", client: "士兰微" }] }), {
      object_refs: [{ type: "job", id: 142, label: "电源专家", subtitle: "士兰微" }],
    });
    assert.deepEqual(tool.output.presentationMeta({}, { ok: true, items: [] }), { object_refs: [] });
    assert.deepEqual(tool.output.presentationMeta({}, null), { object_refs: [] });
    assert.deepEqual(tool.output.presentationMeta({}, undefined), { object_refs: [] });
  });

  it("投影上限 8 条", () => {
    const tool = registerTools().get("asa_approvals");
    const value = {
      ok: true,
      items: Array.from({ length: 20 }, (_, index) => ({
        approval_id: `approval_${index}`, workflow_id: `workflow_${index}`, title: `审批 ${index}`,
      })),
    };
    const meta = tool.output.presentationMeta({}, value);
    assert.equal(meta.object_refs.length, 8);
    assert.equal(meta.object_refs[7].id, "workflow_7");
  });
});
