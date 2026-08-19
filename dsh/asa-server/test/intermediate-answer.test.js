import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  CANDIDATE_LIST_FOLLOWUP_PROMPT,
  isIntermediateAnswer,
  mergeCopilotPayload,
  shouldFollowupCandidateList,
} from "../lib/index.js";

describe("候选名单中间态收尾识别", () => {
  it("命中子代理中间态", () => {
    assert.equal(isIntermediateAnswer("名单已返回，子代理仍在执行，稍后给出最终结果"), true);
    assert.equal(isIntermediateAnswer("等它返回后我再给你结论"), true);
  });

  it("正常最终答复不命中", () => {
    assert.equal(isIntermediateAnswer("已确认 3 人，2 人为相邻经验，下一步核验 1 人"), false);
    assert.equal(isIntermediateAnswer(""), false);
  });

  it("只有名单卡与中间态同时存在才触发收尾", () => {
    assert.equal(shouldFollowupCandidateList("子代理仍在执行", true), true);
    assert.equal(shouldFollowupCandidateList("子代理仍在执行", false), false);
    assert.equal(shouldFollowupCandidateList("已完成分档结论", true), false);
  });

  it("收尾提示要求基于证据且不重复调用名单工具", () => {
    assert.match(CANDIDATE_LIST_FOLLOWUP_PROMPT, /真实候选名单工具证据/);
    assert.match(CANDIDATE_LIST_FOLLOWUP_PROMPT, /不要重复调用名单工具/);
  });

  it("收尾 payload 缺字段时保留首次分析与名单卡", () => {
    const first = {
      analysis_card: { headline: "候选人分档" },
      action_cards: [{ type: "candidate_list" }],
      context: { type: "job", id: 137 },
    };
    assert.deepEqual(mergeCopilotPayload(first, { business_focus: { action: "筛选" } }), {
      ...first,
      business_focus: { action: "筛选" },
    });
  });
});
