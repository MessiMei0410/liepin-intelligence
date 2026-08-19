/**
 * @asa/dsh-asa-server — 子代理生命周期 → SSE `subagent` 事件透传（纯函数，node:test 可测）。
 *
 * DSH 侧机制（侦察自 @deepseek-ai/dsh-subagent）：主 agent 经 `subagent`/`subagent_fork`
 * 工具派生子代理时，dsh-subagent 的 SubagentRuntime 以派生父 agent 为 scope 载体发
 * Cordis 事件 `subagent/start`（SubagentRunInfo：runId/provider/id/local）与
 * `subagent/end`（SubagentRunEndInfo：+ stopReason/lastAssistantMessage?）。agent.ctx
 * 上的监听器按 scope 链收到本 agent（及其后代）的委派事件——asa-server 在 runTurn 里
 * 与 session/event 同一订阅点挂这两个事件，无需全局订阅。
 *
 * 事件 payload 本身不带任务描述：label 先从子 session 的 `subagent/descriptor`
 * （durable label）读取，读不到时退到主 session 火线 tool/call（name=subagent*）的
 * arguments.description 按序兜底（同轮多次委派按调用顺序配对）。
 *
 * SSE 增量形态：{event:'start', id, label, status:'running'} /
 * {event:'end', id, status, summary?}；轮末 done 另携带 subagents 终态数组用于回填。
 */

// 状态词汇：running（start 后未结）→ done / failed / stopped（end 按 stopReason 映射）。
const TERMINAL_STATUS = {
  completed: "done",
  error: "failed",
  "max-tokens": "failed",
  refusal: "failed",
  aborted: "stopped",
};

/** stopReason → 前端状态词汇（未知原因按 failed 兜底，宁报坏不漏报）。 */
function subagentTerminalStatus(stopReason) {
  return TERMINAL_STATUS[stopReason] || "failed";
}

const SUMMARY_MAX_CHARS = 500;

/** subagent/end 的 lastAssistantMessage（ContentBlock[]）→ 结果摘要纯文本（截断）。 */
function subagentSummaryText(blocks, maxChars = SUMMARY_MAX_CHARS) {
  if (!Array.isArray(blocks)) return "";
  const text = blocks
    .filter((block) => block && block.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("")
    .trim();
  if (text === "") return "";
  return text.length > maxChars ? `${text.slice(0, maxChars)}…` : text;
}

/** tool/call 的 name 是否为子代理委派工具（subagent / subagent_fork）。 */
function isSubagentToolCall(toolName) {
  return toolName === "subagent" || toolName === "subagent_fork";
}

/** tool/call arguments（未解析 JSON 字符串）→ 委派描述（label 兜底来源）。 */
function subagentToolCallLabel(argumentsJson) {
  if (typeof argumentsJson !== "string" || argumentsJson === "") return "";
  try {
    const args = JSON.parse(argumentsJson);
    return typeof args.description === "string" ? args.description.trim() : "";
  } catch {
    return "";
  }
}

/**
 * 本轮子代理聚合器：start/end 增量转 SSE data，轮末 list() 出终态数组随 done 下发。
 * label 解析顺序：start 时调用方给的 descriptor label → tool/call 顺序队列 → 空串
 * （前端对空 label 显示「子代理任务」）。
 */
function createSubagentTracker() {
  const runs = new Map(); // runId → {id, label, status, summary?}
  const queuedLabels = []; // tool/call 顺序兜底队列
  return {
    /** 主 session 火线 tool/call：登记委派工具的描述，按序兜底配对后续的 start。 */
    noteToolCall(toolName, argumentsJson) {
      if (!isSubagentToolCall(toolName)) return;
      queuedLabels.push(subagentToolCallLabel(argumentsJson));
    },
    /** subagent/start → SSE data。descriptorLabel 为子 session descriptor 读到的 label。 */
    start(info, descriptorLabel) {
      if (!info || !info.runId) return null;
      const label = String(descriptorLabel || "").trim() || queuedLabels.shift() || "";
      const run = { id: String(info.runId), label, status: "running" };
      runs.set(run.id, run);
      return { event: "start", id: run.id, label, status: run.status };
    },
    /** subagent/end → SSE data（未知 runId 也兜底成终态行，不让结束事件丢失）。 */
    end(info) {
      if (!info || !info.runId) return null;
      const id = String(info.runId);
      const status = subagentTerminalStatus(info.stopReason);
      const summary = subagentSummaryText(info.lastAssistantMessage);
      const run = runs.get(id) || { id, label: queuedLabels.shift() || "", status: "running" };
      run.status = status;
      if (summary) run.summary = summary;
      runs.set(id, run);
      return { event: "end", id, status, ...(summary ? { summary } : {}) };
    },
    /** 轮末终态数组（含仍 running 的：后台子代理可能在轮结束后才 settle）。 */
    list() {
      return [...runs.values()];
    },
  };
}

export {
  createSubagentTracker,
  isSubagentToolCall,
  subagentSummaryText,
  subagentTerminalStatus,
  subagentToolCallLabel,
};
