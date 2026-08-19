// asa-server Copilot 委托载荷 → done 字段单测（node --test，本机跑；CI 暂无 dsh JS 测试门禁）。
// 验证：meta.copilot_payload 的透传字段并入 done，workflow 进度原料按 Core bridge
// （service_copilot_bridge.py）结构组装成 workflow_progress。
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildWorkflowProgress, delegateDoneFields } from "../lib/copilot-payload.js";

describe("buildWorkflowProgress", () => {
  it("按 bridge 结构组装 workflow_progress", () => {
    const progress = buildWorkflowProgress({
      workflow_id: "workflow_aaa",
      workflow: { status: "running", current_stage: "外部寻访", business_outcome: "" },
      progress: { completed: 2, total: 5 },
      plan_summary: [{ title: "step1" }],
      approvals: [{ approval_id: "a1", status: "pending" }],
      goal: { business_outcome: "已产生推荐" },
    });
    assert.deepEqual(progress, {
      workflow_id: "workflow_aaa",
      status: "running",
      business_outcome: "已产生推荐",
      completed: 2,
      total: 5,
      label: "外部寻访",
      pending_approvals: [{ approval_id: "a1", status: "pending" }],
    });
  });

  it("缺 progress.total 时用 plan_summary 长度兜底；缺 status/label 用默认值", () => {
    const progress = buildWorkflowProgress({
      workflow_id: "workflow_bbb",
      workflow: {},
      plan_summary: [{}, {}, {}],
    });
    assert.equal(progress.total, 3);
    assert.equal(progress.completed, 0);
    assert.equal(progress.status, "queued");
    assert.equal(progress.label, "准备执行");
    assert.equal(progress.business_outcome, null);
    assert.deepEqual(progress.pending_approvals, []);
  });

  it("无 workflow_id 返回 null（不组装半截进度卡）", () => {
    assert.equal(buildWorkflowProgress({ workflow: { status: "running" } }), null);
    assert.equal(buildWorkflowProgress({}), null);
    assert.equal(buildWorkflowProgress(null), null);
    assert.equal(buildWorkflowProgress(undefined), null);
  });
});

describe("delegateDoneFields", () => {
  it("透传理解卡/执行回执/焦点/模型参与/复数卡片/上下文，并组装工作流进度", () => {
    const fields = delegateDoneFields({
      understanding_card: { show: true, summary: "我理解为…" },
      execution_receipt: { state: "已生成建议" },
      business_focus: { client: "士兰微", action: "寻访" },
      model_participation: { mode: "model_tools", label: "模型生成 + 工具证据", model: "deepseek-v4" },
      action_cards: [{ type: "candidate_list" }],
      context: { type: "job", id: 142 },
      workflow_id: "workflow_aaa",
      workflow: { status: "running", current_stage: "寻访中" },
      progress: { completed: 1, total: 4 },
    });
    assert.deepEqual(fields, {
      understanding_card: { show: true, summary: "我理解为…" },
      execution_receipt: { state: "已生成建议" },
      business_focus: { client: "士兰微", action: "寻访" },
      model_participation: { mode: "model_tools", label: "模型生成 + 工具证据", model: "deepseek-v4" },
      action_cards: [{ type: "candidate_list" }],
      context: { type: "job", id: 142 },
      workflow_id: "workflow_aaa",
      workflow_progress: {
        workflow_id: "workflow_aaa",
        status: "running",
        business_outcome: null,
        completed: 1,
        total: 4,
        label: "寻访中",
        pending_approvals: [],
      },
    });
  });

  it("字段缺失时不携带对应键（done 不出 null 字段）", () => {
    assert.deepEqual(delegateDoneFields({}), {});
    assert.deepEqual(delegateDoneFields(null), {});
    assert.deepEqual(delegateDoneFields("junk"), {});
    // 无 workflow_id 时不带 workflow_id/workflow_progress
    assert.deepEqual(delegateDoneFields({ workflow: { status: "running" } }), {});
    // action_cards 空数组不携带
    assert.deepEqual(delegateDoneFields({ action_cards: [] }), {});
  });
});
