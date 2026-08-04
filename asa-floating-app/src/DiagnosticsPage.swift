import Foundation

struct ServiceDiagnostic {
    let order: Int
    let title: String
    let path: String
    let ok: Bool
    let status: String
}

enum DiagnosticsPageTarget {
    case mainWindow
    case floatingPanel
}

// Pure helpers for the service-unavailable diagnostics page so the markup,
// baseURL selection, and recovery script stay covered by NativeBoundaryTests.
enum DiagnosticsPage {
    // Only the --compat-copilot mode exposes the legacy floating panel,
    // so the Copilot recovery button is rendered conditionally.
    static func copilotButtonMarkup(compatibilityCopilotEnabled: Bool) -> String {
        compatibilityCopilotEnabled
            ? #"<button onclick="native('showFloating')">显示 Copilot</button>"#
            : ""
    }

    // loadHTMLString 的 baseURL 会成为这次替代加载的导航 URL，必须通过
    // WebSecurityPolicy 的导航白名单：主窗口用 /asa-app，浮窗 panel 用 /asa-floating。
    // 直接用服务根地址会被白名单拒绝，诊断页将渲染为白屏。
    static func baseURL(target: DiagnosticsPageTarget, mainWindowURL: URL, floatingURL: URL) -> URL {
        switch target {
        case .mainWindow: return mainWindowURL
        case .floatingPanel: return floatingURL
        }
    }

    // 服务恢复后诊断页自动跳回的目标路径，与 baseURL 同源、按 target 区分。
    static func homePath(target: DiagnosticsPageTarget) -> String {
        switch target {
        case .mainWindow: return "/asa-app"
        case .floatingPanel: return "/asa-floating"
        }
    }

    static func html(
        detail: String,
        diagnostics: [ServiceDiagnostic],
        compatibilityCopilotEnabled: Bool,
        target: DiagnosticsPageTarget
    ) -> String {
        let detailLiteral = javaScriptStringLiteral(detail)
        let diagnosticPayload = diagnostics.map {
            ["title": $0.title, "path": $0.path, "ok": $0.ok, "status": $0.status] as [String: Any]
        }
        let diagnosticsLiteral = (try? JSONSerialization.data(withJSONObject: diagnosticPayload))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        let copilotButtonMarkup = copilotButtonMarkup(compatibilityCopilotEnabled: compatibilityCopilotEnabled)
        let homePath = homePath(target: target)
        return """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            :root { color-scheme: light; --text:#111827; --muted:#667085; --line:#d8dee8; --blue:#1f5eff; --red:#b42318; }
            * { box-sizing:border-box; }
            body { margin:0; min-height:100vh; display:grid; place-items:center; padding:28px; background:#f7f8fb; color:var(--text); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
            main { width:min(400px, 100%); display:grid; gap:16px; }
            .mark { width:52px; height:52px; border-radius:8px; background:#111827; color:white; display:grid; place-items:center; font-weight:800; font-size:20px; }
            h1 { margin:0; font-size:20px; letter-spacing:0; }
            p { margin:0; color:var(--muted); }
            .status { border-left:3px solid var(--red); background:#fff5f4; color:var(--red); padding:9px 11px; word-break:break-word; }
            .checks { border-top:1px solid var(--line); }
            .check { min-height:52px; display:grid; grid-template-columns:10px 1fr auto; align-items:center; gap:10px; border-bottom:1px solid var(--line); }
            .dot { width:8px; height:8px; border-radius:50%; background:var(--red); }
            .dot.ok { background:#15803d; }
            .check b { display:block; font-size:13px; }
            .path { color:var(--muted); font-size:11px; }
            .result { color:var(--red); font-size:12px; text-align:right; max-width:150px; }
            .result.ok { color:#166534; }
            .actions { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
            button { min-height:38px; border:1px solid var(--line); background:white; color:#1f2937; border-radius:7px; font:inherit; cursor:pointer; }
            button.primary { background:var(--blue); color:white; border-color:var(--blue); }
            .hint { font-size:12px; color:var(--muted); }
          </style>
        </head>
        <body>
          <main>
            <div class="mark">ASA</div>
            <div>
              <h1>ASA 服务诊断</h1>
              <p>正在检查 Core、Copilot 状态和 Agent 界面。</p>
            </div>
            <div class="status" id="detail"></div>
            <div class="checks" id="checks"></div>
            <div class="actions">
              <button class="primary" onclick="native('startWorkbenchService')">启动本机服务</button>
              <button onclick="native('retryServiceConnection')">重试连接</button>
              <button onclick="native('openWorkbench')">打开 ASA Agent</button>
              \(copilotButtonMarkup)
            </div>
            <div class="hint">ASA 会在本机服务恢复后自动重新加载，不需要打开浏览器。</div>
          </main>
          <script>
            const detail = \(detailLiteral);
            const diagnostics = \(diagnosticsLiteral);
            document.getElementById('detail').textContent = detail || '本机 ASA 服务未连接';
            document.getElementById('checks').innerHTML = diagnostics.map(item => `
              <div class="check">
                <span class="dot ${item.ok ? 'ok' : ''}"></span>
                <span><b>${escapeHTML(item.title)}</b><span class="path">${escapeHTML(item.path)}</span></span>
                <span class="result ${item.ok ? 'ok' : ''}">${escapeHTML(item.status)}</span>
              </div>`).join('');
            function escapeHTML(value) {
              return String(value || '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
            }
            function native(type) {
              window.webkit?.messageHandlers?.asaNative?.postMessage({type});
            }
            // 轮询本机服务健康状态，恢复后自动跳回对应界面；失败静默重试。
            setInterval(() => {
              fetch('/api/v1/health', { cache: 'no-store' })
                .then(response => {
                  if (response.ok) location.href = '\(homePath)';
                })
                .catch(() => {});
            }, 5000);
          </script>
        </body>
        </html>
        """
    }

    private static func javaScriptStringLiteral(_ value: String) -> String {
        guard let data = try? JSONEncoder().encode(value),
              let literal = String(data: data, encoding: .utf8) else {
            return "''"
        }
        return literal
    }
}
