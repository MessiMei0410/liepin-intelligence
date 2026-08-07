import { RefreshCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, type ModelAuditResponse } from "../api";

const EMPTY: ModelAuditResponse = {
  ok: true,
  items: [],
  summary: { total: 0, failed: 0, fallback: 0, avg_duration_ms: 0 },
};

const OPERATION_LABELS: Record<string, string> = {
  assess: "候选人评估",
  assess_percentile_motivation: "动机与时机评估",
  assess_risks: "风险评估",
  assess_trajectory: "职业轨迹评估",
  chat: "对话生成",
  "copilot.message": "Agent 消息生成",
  copilot: "Agent 对话生成",
  copilot_intent: "用户意图识别",
  copilot_summary: "对话摘要生成",
  extract_duty_facts: "岗位职责提取",
  generate_search_strategy: "寻访策略生成",
  plan_workflow: "工作流规划",
  rank_memories: "上下文证据排序",
  review: "评估结果复核",
  role_review: "岗位画像复核",
  strategy_patch: "寻访策略修订",
};

const operationLabel = (operation: string) =>
  OPERATION_LABELS[operation] || "其他模型操作";

const statusLabel = (status: string) =>
  ({
    success: "成功",
    failed: "失败",
    running: "运行中",
    cancelled: "已取消",
  })[status] || status;

const validationLabel = (status: string) =>
  ({
    passed: "结构校验通过",
    failed: "结构校验失败",
    not_applicable: "无需结构校验",
    pending: "待校验",
  })[status] || status;

const PREVIEW_CHAR_LIMIT = 180;

const previewKey = (callId: string, kind: "request" | "response") =>
  `${callId}:${kind}`;
const previewId = (callId: string, kind: "request" | "response") =>
  `model-audit-preview-${callId}-${kind}`;

const truncatedPreview = (text: string) =>
  text.length <= PREVIEW_CHAR_LIMIT
    ? text
    : `${text.slice(0, PREVIEW_CHAR_LIMIT).trimEnd()}…`;

function PreviewText({
  callId,
  kind,
  text,
  expanded,
  onToggle,
}: {
  callId: string;
  kind: "request" | "response";
  text: string;
  expanded: boolean;
  onToggle: (key: string) => void;
}) {
  const long = text.length > PREVIEW_CHAR_LIMIT;
  return (
    <div className="agent-model-audit-preview">
      <p id={previewId(callId, kind)}>
        {long && !expanded ? truncatedPreview(text) : text}
      </p>
      {long && (
        <button
          type="button"
          className="agent-model-audit-toggle"
          aria-expanded={expanded}
          aria-controls={previewId(callId, kind)}
          onClick={() => onToggle(previewKey(callId, kind))}
        >
          {expanded ? "收起全文" : "展开全文"}
        </button>
      )}
    </div>
  );
}

export function ModelAuditPanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;
  return <OpenModelAuditPanel onClose={onClose} />;
}

function OpenModelAuditPanel({ onClose }: { onClose: () => void }) {
  const [payload, setPayload] = useState<ModelAuditResponse>(EMPTY);
  const [status, setStatus] = useState("");
  const [operation, setOperation] = useState("");
  const [operations, setOperations] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [requestVersion, setRequestVersion] = useState(0);
  const [expandedPreviews, setExpandedPreviews] = useState<
    Record<string, boolean>
  >({});
  const requestSequence = useRef(0);

  useEffect(() => {
    const requestId = requestSequence.current + 1;
    requestSequence.current = requestId;
    api
      .modelAudit(60, operation, status)
      .then((value) => {
        if (requestSequence.current !== requestId) return;
        setPayload(value);
        setExpandedPreviews({});
        setOperations((current) =>
          Array.from(
            new Set([...current, ...value.items.map((item) => item.operation)]),
          ).sort((left, right) =>
            operationLabel(left).localeCompare(operationLabel(right), "zh-CN"),
          ),
        );
        setLoading(false);
      })
      .catch((value) => {
        if (requestSequence.current !== requestId) return;
        const detail = value instanceof Error ? value.message : String(value);
        setError(`模型审计加载失败：${detail}`);
        setLoading(false);
      });
    return () => {
      requestSequence.current += 1;
    };
  }, [operation, requestVersion, status]);

  const beginLoad = () => {
    // 先同步作废在途请求，避免慢响应在新筛选/重试后仍覆盖结果。
    requestSequence.current += 1;
    setLoading(true);
    setError("");
    setPayload(EMPTY);
  };

  const changeStatus = (value: string) => {
    beginLoad();
    setStatus(value);
  };

  const changeOperation = (value: string) => {
    beginLoad();
    setOperation(value);
  };

  const reload = () => {
    beginLoad();
    setRequestVersion((current) => current + 1);
  };

  const togglePreview = (key: string) => {
    setExpandedPreviews((current) => ({ ...current, [key]: !current[key] }));
  };

  const summary = loading || error ? null : payload.summary;

  return (
    <aside className="agent-model-audit" aria-label="模型输出审计">
      <header>
        <span>
          <b>模型输出审计</b>
          <small>最近 24 小时汇总</small>
        </span>
        <button
          className="icon-btn"
          aria-label="关闭模型输出审计"
          title="关闭"
          onClick={onClose}
        >
          <X />
        </button>
      </header>
      <section
        className="agent-model-audit-summary"
        aria-label="模型调用汇总"
        aria-busy={loading}
      >
        <div>
          <span>调用</span>
          <b>{summary?.total ?? "-"}</b>
        </div>
        <div>
          <span>失败</span>
          <b>{summary?.failed ?? "-"}</b>
        </div>
        <div>
          <span>已降级</span>
          <b>{summary?.fallback ?? "-"}</b>
        </div>
        <div>
          <span>平均耗时</span>
          <b>{summary ? `${summary.avg_duration_ms} ms` : "-"}</b>
        </div>
      </section>
      <div className="agent-model-audit-filters">
        <label htmlFor="model-audit-status-filter">
          <span>状态</span>
          <select
            id="model-audit-status-filter"
            value={status}
            onChange={(event) => changeStatus(event.target.value)}
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failed">失败</option>
            <option value="running">运行中</option>
            <option value="cancelled">已取消</option>
          </select>
        </label>
        <label htmlFor="model-audit-operation-filter">
          <span>动作</span>
          <select
            id="model-audit-operation-filter"
            value={operation}
            onChange={(event) => changeOperation(event.target.value)}
          >
            <option value="">全部</option>
            {operations.map((item) => (
              <option key={item} value={item}>
                {operationLabel(item)}（{item}）
              </option>
            ))}
          </select>
        </label>
        <button
          className="icon-btn"
          aria-label="刷新模型审计"
          title="刷新"
          disabled={loading}
          onClick={reload}
        >
          <RefreshCw className={loading ? "spin" : ""} />
        </button>
      </div>
      <div className="agent-model-audit-list">
        {loading && (
          <p className="agent-model-audit-empty" role="status">
            <RefreshCw className="spin" aria-hidden="true" />{" "}
            正在加载模型审计...
          </p>
        )}
        {!loading && error && (
          <div className="agent-model-audit-error" role="alert">
            <p>{error}</p>
            <button className="button secondary" onClick={reload}>
              <RefreshCw aria-hidden="true" />
              重试加载
            </button>
          </div>
        )}
        {!loading && !error && payload.items.length > 0 && (
          <p className="agent-model-audit-count">
            共 {payload.items.length} 条
          </p>
        )}
        {!loading &&
          !error &&
          payload.items.map((item) => (
            <article
              key={item.call_id}
              aria-labelledby={`model-audit-record-${item.call_id}`}
            >
              <header>
                <span>
                  <b id={`model-audit-record-${item.call_id}`}>
                    {operationLabel(item.operation)}
                  </b>
                  <small>审计标识：{item.operation}</small>
                  <small>
                    {item.model} · {item.provider}
                  </small>
                </span>
                <em className={item.status}>{statusLabel(item.status)}</em>
              </header>
              <dl>
                <div>
                  <dt>参与方式</dt>
                  <dd>
                    {item.fallback_used ? "模型失败，已规则降级" : "模型生成"}
                  </dd>
                </div>
                <div>
                  <dt>校验</dt>
                  <dd>{validationLabel(item.validation_status)}</dd>
                </div>
                <div>
                  <dt>耗时</dt>
                  <dd>{item.duration_ms} ms</dd>
                </div>
                <div>
                  <dt>Token</dt>
                  <dd>
                    {item.input_tokens} / {item.output_tokens}
                  </dd>
                </div>
              </dl>
              <PreviewText
                callId={item.call_id}
                kind="request"
                text={item.request_preview}
                expanded={Boolean(
                  expandedPreviews[previewKey(item.call_id, "request")],
                )}
                onToggle={togglePreview}
              />
              {item.response_preview && (
                <PreviewText
                  callId={item.call_id}
                  kind="response"
                  text={item.response_preview}
                  expanded={Boolean(
                    expandedPreviews[previewKey(item.call_id, "response")],
                  )}
                  onToggle={togglePreview}
                />
              )}
              {item.error && <p className="error">{item.error}</p>}
              <footer>
                <time>{item.created_at}</time>
                <code title={item.request_hash}>
                  {item.request_hash.slice(0, 12)}
                </code>
              </footer>
            </article>
          ))}
        {!loading && !error && !payload.items.length && (
          <p className="agent-model-audit-empty" role="status">
            {operation || status
              ? "没有符合当前筛选条件的模型调用"
              : "最近 24 小时暂无模型调用"}
          </p>
        )}
      </div>
    </aside>
  );
}
