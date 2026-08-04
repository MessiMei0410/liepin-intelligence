import Foundation

@main
struct NativeBoundaryTests {
    static func main() {
        let policy = ASAWebSecurityPolicy(serviceBaseURL: URL(string: "http://127.0.0.1:8765")!)
        let agentURL = URL(string: "http://127.0.0.1:8765/asa-app#job=154")!
        let floatingURL = URL(string: "http://127.0.0.1:8765/asa-floating?ui=0.2.20")!

        precondition(policy.allowsNavigation(to: agentURL, on: .agent))
        precondition(policy.allowsNavigation(to: floatingURL, on: .copilot))
        precondition(!policy.isTrustedServiceURL(URL(string: "http://localhost:8765/asa-app")!))
        precondition(!policy.isTrustedServiceURL(URL(string: "http://127.0.0.1:9999/asa-app")!))
        precondition(!policy.isTrustedServiceURL(URL(string: "https://127.0.0.1:8765/asa-app")!))
        precondition(!policy.allowsNavigation(to: floatingURL, on: .agent))
        precondition(!policy.allowsBridgeAction("showFloating", on: .agent))
        precondition(!policy.allowsBridgeAction("screenshot", on: .agent))
        precondition(policy.allowsBridgeAction("screenshot", on: .copilot))
        precondition(policy.allowsExternalURL(URL(string: "https://openai.com")!))
        precondition(!policy.allowsExternalURL(URL(string: "file:///tmp/private")!))
        precondition(!policy.allowsExternalURL(URL(string: "x-apple.systempreferences:privacy")!))

        let clipboard = NativeContextPrivacy.clipboardMetadata(hasText: true, changeCount: 7)
        precondition(clipboard["has_text"] as? Bool == true)
        precondition(clipboard["change_count"] as? Int == 7)
        precondition(clipboard["preview"] == nil)
        precondition(clipboard["length"] == nil)

        // Diagnostics page: Copilot recovery button only exists in --compat-copilot mode.
        precondition(DiagnosticsPage.copilotButtonMarkup(compatibilityCopilotEnabled: false).isEmpty)
        precondition(DiagnosticsPage.copilotButtonMarkup(compatibilityCopilotEnabled: true).contains("showFloating"))

        // Diagnostics page baseURL: main window uses /asa-app, floating panel uses /asa-floating.
        let mainWindowURL = URL(string: "http://127.0.0.1:8765/asa-app")!
        let floatingPanelURL = URL(string: "http://127.0.0.1:8765/asa-floating?ui=0.2.23")!
        precondition(DiagnosticsPage.baseURL(target: .mainWindow, mainWindowURL: mainWindowURL, floatingURL: floatingPanelURL).path == "/asa-app")
        precondition(DiagnosticsPage.baseURL(target: .floatingPanel, mainWindowURL: mainWindowURL, floatingURL: floatingPanelURL).path == "/asa-floating")

        // Diagnostics page auto-reload: poll /api/v1/health and jump back per target.
        let sampleDiagnostics = [
            ServiceDiagnostic(order: 0, title: "ASA Core", path: "/api/v1/health", ok: false, status: "未连接")
        ]
        let mainPageHTML = DiagnosticsPage.html(
            detail: "",
            diagnostics: sampleDiagnostics,
            compatibilityCopilotEnabled: false,
            target: .mainWindow
        )
        precondition(!mainPageHTML.contains("showFloating"))
        precondition(mainPageHTML.contains("fetch('/api/v1/health'"))
        precondition(mainPageHTML.contains("}, 5000)"))
        precondition(mainPageHTML.contains("location.href = '/asa-app'"))
        precondition(!mainPageHTML.contains("location.href = '/asa-floating'"))

        let panelPageHTML = DiagnosticsPage.html(
            detail: "本机 ASA 服务未连接",
            diagnostics: sampleDiagnostics,
            compatibilityCopilotEnabled: true,
            target: .floatingPanel
        )
        precondition(panelPageHTML.contains("showFloating"))
        precondition(panelPageHTML.contains("fetch('/api/v1/health'"))
        precondition(panelPageHTML.contains("location.href = '/asa-floating'"))
        precondition(!panelPageHTML.contains("location.href = '/asa-app'"))
    }
}
