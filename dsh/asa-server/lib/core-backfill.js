// DSH 轮次服务端回填 Core（dogfood R2-1：流式中途刷新/关页/断网时，done 事件根本
// 不到达前端，前端 recordDshTurn / recordDshIncompleteTurn 都不会发生，新任务首轮
// 整会话从任务列表消失）。asa-server 持有轮次完整数据，轮末（无论 completed 还是
// aborted/超时）自己 POST Core /api/v1/copilot/sessions/record-turn：
// - 幂等键与前端回填相同（前端经 /turn payload 带上来的 request_id）；Core 按
//   (session_id, request_id) 原子去重（INSERT…WHERE NOT EXISTS），两条回填通道
//   先到先赢，绝不双份；
// - 前端 recordDshTurn / recordDshIncompleteTurn 保留（同一幂等键下谁快谁落）；
// - 请求体未带 request_id（旧版前端 bundle）时跳过服务端回填，避免双键双份。
// 纯函数模块（不依赖 dsh 包），便于 node --test。

const CORE_URL = () => process.env.ASA_CORE_URL || "http://127.0.0.1:8765";
const UA = "asa-dsh-server/1.0";
const REQUEST_TIMEOUT_MS = Number(process.env.ASA_DSH_BACKFILL_TIMEOUT_MS || 5000);

const isRecord = (value) => value && typeof value === "object" && !Array.isArray(value);

/**
 * 组装 record-turn 请求体（与前端 transport.recordDshTurn / recordDshIncompleteTurn 同构）。
 * doneData 为轮末 done 载荷（ok/session_id/answer/error + 结构化卡片透传字段）；
 * completed=false（aborted/超时/error）时只带 message/answer/turn_error，
 * 与前端 recordDshIncompleteTurn 的最小回填语义一致。
 */
export function buildBackfillBody({ sessionId, requestId, message, context, done }) {
  const data = isRecord(done) ? done : {};
  const body = {
    session_id: String(data.session_id || sessionId),
    request_id: String(requestId),
    message: String(message || ""),
    answer: String(data.answer || ""),
    context: isRecord(context) ? context : {},
    source: "dsh",
  };
  if (data.ok === false) {
    body.turn_error = String(data.error || "turn did not complete");
    return body;
  }
  if (isRecord(data.action_card) && Object.keys(data.action_card).length) body.action_card = data.action_card;
  if (Array.isArray(data.action_cards) && data.action_cards.length) body.action_cards = data.action_cards;
  if (Array.isArray(data.subagents) && data.subagents.length) body.subagents = data.subagents;
  if (Array.isArray(data.suggested_actions) && data.suggested_actions.length) body.suggested_actions = data.suggested_actions;
  if (Array.isArray(data.references) && data.references.length) body.references = data.references;
  if (isRecord(data.confirm_request)) {
    // 与前端一致注入 client_request_id：恢复会话后确认/取消的终态回写靠它定位同轮消息。
    body.confirm_request = { ...data.confirm_request, client_request_id: String(requestId) };
  }
  for (const key of ["understanding_card", "execution_receipt", "analysis_card", "business_focus", "model_participation", "workflow_progress"]) {
    if (isRecord(data[key])) body[key] = data[key];
  }
  if (typeof data.workflow_id === "string" && data.workflow_id) body.workflow_id = data.workflow_id;
  return body;
}

/**
 * POST Core record-turn，指数退避重试（默认 3 次，同前端 recordDshTurn）。
 * 全部失败返回 false（只告警——前端回填仍是并行通道；本回填的价值恰在
 * 前端已不在的场景，失败时无人能补，只能留日志）。
 */
export async function backfillTurnToCore(
  body,
  { attempts = 3, baseDelayMs = 400, coreUrl = CORE_URL(), fetchImpl = fetch } = {},
) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const response = await fetchImpl(`${coreUrl}/api/v1/copilot/sessions/record-turn`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": UA },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (response.ok) return true;
      console.warn(`[asa-resident] 轮次回填 Core 返回 ${response.status}（第 ${attempt}/${attempts} 次）`);
    } catch (error) {
      console.warn(`[asa-resident] 轮次回填 Core 失败（第 ${attempt}/${attempts} 次）`, error instanceof Error ? error.message : error);
    }
    if (attempt < attempts) await sleep(baseDelayMs * 2 ** (attempt - 1));
  }
  return false;
}
