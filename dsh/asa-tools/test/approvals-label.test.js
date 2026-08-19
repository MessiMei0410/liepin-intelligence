// asa_approvals 工具单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：presentationMeta 的 object_refs label 优先用 goal 标题——同名 R3 审批
// （审批 title 都是"执行多渠道寻访"）的轮末操作芯片由此可区分（2026-08-19 dogfood：
// 士兰微/长越两条审批芯片文案完全相同）；原审批 title 降为命中别名，保住
// asa-server 相关性过滤（#71/#78 按 label 命中 answer）的命中语义。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { apply } from "../lib/index.js";

function registerTools() {
  const tools = new Map();
  const ctx = { tools: { register: (definition) => tools.set(definition.name, definition) } };
  apply(ctx);
  return tools;
}

describe("asa_approvals presentationMeta", () => {
  it("label 优先 goal 标题区分同名审批，审批 title 作命中别名", () => {
    const tool = registerTools().get("asa_approvals");
    const meta = tool.output.presentationMeta({}, {
      ok: true,
      items: [
        { approval_id: "approval_1", workflow_id: "workflow_sl", title: "执行多渠道寻访", goal_title: "士兰微 / 电源专家｜第3轮寻访 · 10" },
        { approval_id: "approval_2", workflow_id: "workflow_cy", title: "执行多渠道寻访", goal_title: "长越科技 / 机械高级工程师｜第5轮寻访 · 15" },
      ],
    });
    assert.deepEqual(meta.object_refs, [
      {
        type: "workflow", id: "workflow_sl",
        label: "士兰微 / 电源专家｜第3轮寻访 · 10",
        aliases: ["执行多渠道寻访"],
        approval_id: "approval_1",
      },
      {
        type: "workflow", id: "workflow_cy",
        label: "长越科技 / 机械高级工程师｜第5轮寻访 · 15",
        aliases: ["执行多渠道寻访"],
        approval_id: "approval_2",
      },
    ]);
  });

  it("缺 goal_title 时退回审批 title，且不产生冗余别名；无 workflow_id 的条目剔除", () => {
    const tool = registerTools().get("asa_approvals");
    const meta = tool.output.presentationMeta({}, {
      ok: true,
      items: [
        { approval_id: "approval_3", workflow_id: "workflow_x", title: "执行多渠道寻访" },
        { approval_id: "approval_4", title: "无工作流条目" },
      ],
    });
    assert.deepEqual(meta.object_refs, [
      { type: "workflow", id: "workflow_x", label: "执行多渠道寻访", approval_id: "approval_3" },
    ]);
  });

  it("列表为空/异常形态时退化安全", () => {
    const tool = registerTools().get("asa_approvals");
    assert.deepEqual(tool.output.presentationMeta({}, { ok: true, items: [] }), { object_refs: [] });
    assert.deepEqual(tool.output.presentationMeta({}, null), { object_refs: [] });
  });
});
