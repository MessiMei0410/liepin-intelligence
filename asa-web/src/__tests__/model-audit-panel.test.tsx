import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModelAuditPanel } from "../agent/ModelAuditPanel";
import { api, type ModelAuditResponse } from "../api";

const auditResponse = (
  overrides: Partial<ModelAuditResponse> = {},
): ModelAuditResponse => ({
  ok: true,
  summary: { total: 7, failed: 1, fallback: 2, avg_duration_ms: 842 },
  items: [
    {
      call_id: "llm-1",
      operation: "assess_risks",
      provider: "api.deepseek.com",
      model: "deepseek-v4-flash",
      status: "success",
      validation_status: "passed",
      fallback_used: 0,
      duration_ms: 920,
      input_tokens: 120,
      output_tokens: 18,
      request_hash: "abcdef1234567890",
      request_preview: "JSON 对象；字段：candidate, job",
      response_preview: "文本；8 字符",
      error: null,
      created_at: "2026-08-04 10:00:00",
      finished_at: "2026-08-04 10:00:01",
    },
  ],
  ...overrides,
});

describe("ModelAuditPanel", () => {
  afterEach(() => vi.restoreAllMocks());

  it("首次加载显示明确 loading，成功后再展示汇总", async () => {
    let resolveRequest: (value: ModelAuditResponse) => void = () => undefined;
    vi.spyOn(api, "modelAudit").mockReturnValue(
      new Promise((resolve) => {
        resolveRequest = resolve;
      }),
    );

    render(<ModelAuditPanel open onClose={() => undefined} />);

    expect(screen.getByRole("status")).toHaveTextContent("正在加载模型审计");
    expect(
      within(screen.getByRole("region", { name: "模型调用汇总" })).queryByText(
        "7",
      ),
    ).not.toBeInTheDocument();

    resolveRequest(auditResponse());
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
    expect(
      within(screen.getByRole("region", { name: "模型调用汇总" })).getByText(
        "7",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("共 1 条")).toBeInTheDocument();
  });

  it("中文展示操作名称，同时保留原 operation 作为筛选值和审计标识", async () => {
    const request = vi
      .spyOn(api, "modelAudit")
      .mockResolvedValue(auditResponse());
    render(<ModelAuditPanel open onClose={() => undefined} />);

    expect(await screen.findByText("风险评估")).toBeInTheDocument();
    expect(screen.getByText("审计标识：assess_risks")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "风险评估（assess_risks）" }),
    ).toHaveValue("assess_risks");
    expect(
      screen.queryByRole("button", { name: "展开全文" }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("动作"), {
      target: { value: "assess_risks" },
    });
    await waitFor(() =>
      expect(request).toHaveBeenLastCalledWith(60, "assess_risks", ""),
    );
  });

  it("筛选变更后，先前的慢响应不会覆盖新筛选的结果", async () => {
    let resolveOldRequest!: (value: ModelAuditResponse) => void;
    const oldRequest = new Promise<ModelAuditResponse>((resolve) => {
      resolveOldRequest = resolve;
    });
    const request = vi
      .spyOn(api, "modelAudit")
      .mockResolvedValueOnce(auditResponse())
      .mockReturnValueOnce(oldRequest)
      .mockResolvedValueOnce(
        auditResponse({
          items: [
            {
              call_id: "llm-chat",
              operation: "chat",
              provider: "api.deepseek.com",
              model: "deepseek-v4-flash",
              status: "success",
              validation_status: "passed",
              fallback_used: 0,
              duration_ms: 500,
              input_tokens: 10,
              output_tokens: 5,
              request_hash: "fedcba9876543210",
              request_preview: "对话输入预览",
              response_preview: "对话输出预览",
              error: null,
              created_at: "2026-08-04 11:00:00",
              finished_at: null,
            },
          ],
        }),
      );
    render(<ModelAuditPanel open onClose={() => undefined} />);

    await screen.findByText("风险评估");
    fireEvent.change(screen.getByLabelText("动作"), {
      target: { value: "assess_risks" },
    });
    await waitFor(() => expect(request).toHaveBeenCalledTimes(2));
    fireEvent.change(screen.getByLabelText("状态"), {
      target: { value: "failed" },
    });

    resolveOldRequest(auditResponse());

    expect(await screen.findByText("对话生成")).toBeInTheDocument();
    expect(screen.queryByText("风险评估")).not.toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("超长预览默认截断，可展开并收起全文", async () => {
    const longPreview =
      "很长很长的模型输入预览内容，用于验证审计记录的长文本展示不会撑爆面板。".repeat(
        8,
      );
    vi.spyOn(api, "modelAudit").mockResolvedValue(
      auditResponse({
        items: [
          {
            ...auditResponse().items[0],
            request_preview: longPreview,
            response_preview: longPreview,
          },
        ],
      }),
    );
    render(<ModelAuditPanel open onClose={() => undefined} />);

    const expandButtons = await screen.findAllByRole("button", {
      name: "展开全文",
    });
    expect(expandButtons).toHaveLength(2);
    expect(expandButtons[0]).toHaveAttribute("aria-expanded", "false");
    expect(expandButtons[0]).toHaveAttribute(
      "aria-controls",
      "model-audit-preview-llm-1-request",
    );
    expect(screen.queryByText(longPreview)).not.toBeInTheDocument();
    expect(screen.getAllByText(/…$/)).toHaveLength(2);

    fireEvent.click(expandButtons[0]);
    expect(expandButtons[0]).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText(longPreview)).toBeInTheDocument();

    const collapseButton = screen.getByRole("button", { name: "收起全文" });
    fireEvent.click(collapseButton);
    expect(collapseButton).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(longPreview)).not.toBeInTheDocument();
  });

  it("加载失败清除旧汇总并可点击重试", async () => {
    const empty = auditResponse({
      items: [],
      summary: { total: 0, failed: 0, fallback: 0, avg_duration_ms: 0 },
    });
    const request = vi
      .spyOn(api, "modelAudit")
      .mockResolvedValueOnce(auditResponse())
      .mockRejectedValueOnce(new Error("网络不可用"))
      .mockResolvedValueOnce(empty);
    render(<ModelAuditPanel open onClose={() => undefined} />);

    const summary = screen.getByRole("region", { name: "模型调用汇总" });
    expect(await within(summary).findByText("7")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "刷新模型审计" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "模型审计加载失败：网络不可用",
    );
    expect(within(summary).queryByText("7")).not.toBeInTheDocument();
    expect(screen.queryByText("风险评估")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重试加载" }));
    expect(
      await screen.findByText("最近 24 小时暂无模型调用"),
    ).toBeInTheDocument();
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("成功但无结果时区分全量空态和筛选空态", async () => {
    vi.spyOn(api, "modelAudit").mockResolvedValue(
      auditResponse({
        items: [],
        summary: { total: 0, failed: 0, fallback: 0, avg_duration_ms: 0 },
      }),
    );
    render(<ModelAuditPanel open onClose={() => undefined} />);

    expect(
      await screen.findByText("最近 24 小时暂无模型调用"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("状态"), {
      target: { value: "failed" },
    });
    expect(
      await screen.findByText("没有符合当前筛选条件的模型调用"),
    ).toBeInTheDocument();
  });
});
