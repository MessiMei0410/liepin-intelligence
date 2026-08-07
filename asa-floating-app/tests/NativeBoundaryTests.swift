import Foundation
import Carbon.HIToolbox

@main
struct NativeBoundaryTests {
    static func main() {
        let policy = ASAWebSecurityPolicy(serviceBaseURL: URL(string: "http://127.0.0.1:8765")!)
        let agentURL = URL(string: "http://127.0.0.1:8765/asa-app#job=154")!
        let floatingURL = URL(string: "http://127.0.0.1:8765/asa-floating?ui=0.2.20")!

        precondition(policy.allowsNavigation(to: agentURL, on: .agent))
        precondition(policy.allowsNavigation(to: URL(string: "http://127.0.0.1:8765/asa-app#workflow=12")!, on: .agent))
        precondition(policy.allowsNavigation(to: floatingURL, on: .copilot))
        precondition(policy.allowsNavigation(to: URL(string: "http://127.0.0.1:8765/asa-floating?ui=0.2.27#x")!, on: .copilot))
        precondition(!policy.isTrustedServiceURL(URL(string: "http://localhost:8765/asa-app")!))
        precondition(!policy.isTrustedServiceURL(URL(string: "http://127.0.0.1:9999/asa-app")!))
        precondition(!policy.isTrustedServiceURL(URL(string: "https://127.0.0.1:8765/asa-app")!))
        // Agent surface pins the exact route: query strings are rejected,
        // fragments are the supported route payload.
        precondition(!policy.allowsNavigation(to: URL(string: "http://127.0.0.1:8765/asa-app?tab=jobs")!, on: .agent))
        precondition(!policy.allowsNavigation(to: URL(string: "http://127.0.0.1:8765/asa-floating")!, on: .agent))
        precondition(!policy.allowsNavigation(to: URL(string: "http://127.0.0.1:8765/workbench")!, on: .agent))
        precondition(!policy.allowsNavigation(to: URL(string: "http://user:pass@127.0.0.1:8765/asa-app")!, on: .agent))
        precondition(!policy.allowsNavigation(to: floatingURL, on: .agent))

        // Bridge action allow lists: the agent surface only exposes recovery
        // actions; the legacy copilot surface additionally gets the floating
        // panel and capture actions.
        for action in ["openWorkbench", "retryServiceConnection", "startWorkbenchService"] {
            precondition(policy.allowsBridgeAction(action, on: .agent))
        }
        for action in ["showFloating", "hideFloating", "screenshot", "recognizeWeChatImage",
                       "analyzePastedImage", "openExternal", "reload"] {
            precondition(!policy.allowsBridgeAction(action, on: .agent))
            precondition(policy.allowsBridgeAction(action, on: .copilot))
        }
        precondition(!policy.allowsBridgeAction("showFloating", on: .agent))
        precondition(!policy.allowsBridgeAction("reload", on: .agent))
        precondition(!policy.allowsBridgeAction("openExternal", on: .agent))

        precondition(policy.allowsExternalURL(URL(string: "https://openai.com")!))
        precondition(policy.allowsExternalURL(URL(string: "HTTPS://openai.com/path?q=1")!))
        precondition(policy.allowsExternalURL(URL(string: "http://127.0.0.1:9999")!))
        precondition(!policy.allowsExternalURL(URL(string: "file:///tmp/private")!))
        precondition(!policy.allowsExternalURL(URL(string: "x-apple.systempreferences:privacy")!))
        precondition(!policy.allowsExternalURL(URL(string: "javascript:alert(1)")!))
        precondition(!policy.allowsExternalURL(URL(string: "about:blank")!))
        precondition(!policy.allowsExternalURL(URL(string: "http://user:pass@example.com")!))

        let clipboard = NativeContextPrivacy.clipboardMetadata(hasText: true, changeCount: 7)
        precondition(clipboard["has_text"] as? Bool == true)
        precondition(clipboard["change_count"] as? Int == 7)
        precondition(clipboard["preview"] == nil)
        precondition(clipboard["length"] == nil)

        // Diagnostics page: Copilot recovery button only exists in --compat-copilot mode.
        precondition(DiagnosticsPage.copilotButtonMarkup(compatibilityCopilotEnabled: false).isEmpty)
        precondition(DiagnosticsPage.copilotButtonMarkup(compatibilityCopilotEnabled: true).contains("showFloating"))

        // Global shortcuts open the Agent by default; the old panel is compatibility-only.
        precondition(ASAHotKeyRouting.destination(compatibilityCopilotEnabled: false) == .agentMainWindow)
        precondition(ASAHotKeyRouting.destination(compatibilityCopilotEnabled: true) == .compatibilityCopilot)

        // The global shortcut table is the single source of truth: primary
        // Option+Space, two distinct backups, no duplicate ids or combos.
        let hotKeys = ASAHotKeyRouting.defaultHotKeys
        precondition(hotKeys.count == 3)
        precondition(hotKeys.contains {
            $0.id == .primaryOptionSpace
                && $0.label == "Option+Space"
                && $0.keyCode == UInt32(kVK_Space)
                && $0.modifiers == UInt32(optionKey)
        })
        precondition(hotKeys.contains {
            $0.id == .backupCommandShiftA
                && $0.label == "Command+Shift+A"
                && $0.keyCode == UInt32(kVK_ANSI_A)
                && $0.modifiers == UInt32(cmdKey | shiftKey)
        })
        precondition(hotKeys.contains {
            $0.id == .backupControlOptionA
                && $0.label == "Control+Option+A"
                && $0.keyCode == UInt32(kVK_ANSI_A)
                && $0.modifiers == UInt32(controlKey | optionKey)
        })
        precondition(Set(hotKeys.map(\.id)).count == hotKeys.count)
        precondition(Set(hotKeys.map { "\($0.keyCode)-\($0.modifiers)" }).count == hotKeys.count)
        precondition(hotKeys.allSatisfy { !$0.label.isEmpty })
        precondition(ASAHotKeyRouting.globalHotKeySignature != 0)

        // Core-restart recovery cadence: deterministic schedule with strictly
        // increasing positive backoff and a bounded attempt budget.
        let schedule = CoreRecoverySchedule.standard
        precondition(schedule.retryDelays == [0.4, 0.8, 1.5, 2.5, 4.0])
        precondition(schedule.maxRetryAttempts == 5)
        precondition(schedule.retryDelays.allSatisfy { $0 > 0 })
        precondition(zip(schedule.retryDelays.dropFirst(), schedule.retryDelays).allSatisfy { $0 > $1 })
        precondition(abs(schedule.totalBackoff - 9.2) < 0.0001)

        // Diagnostics page baseURL: main window uses /asa-app, floating panel uses /asa-floating.
        let mainWindowURL = URL(string: "http://127.0.0.1:8765/asa-app")!
        let floatingPanelURL = URL(string: "http://127.0.0.1:8765/asa-floating?ui=0.2.23")!
        precondition(DiagnosticsPage.baseURL(target: .mainWindow, mainWindowURL: mainWindowURL, floatingURL: floatingPanelURL).path == "/asa-app")
        precondition(DiagnosticsPage.baseURL(target: .floatingPanel, mainWindowURL: mainWindowURL, floatingURL: floatingPanelURL).path == "/asa-floating")
        precondition(DiagnosticsPage.homePath(target: .mainWindow) == "/asa-app")
        precondition(DiagnosticsPage.homePath(target: .floatingPanel) == "/asa-floating")

        // Diagnostics page literals live inside a <script> block: JSON escaping
        // alone would let "</script>" or U+2028/U+2029 break out of the page.
        let dangerousDetail = "</script><script>alert(1)</script>\u{2028}payload"
        let escapedDetail = DiagnosticsPage.javaScriptStringLiteral(dangerousDetail)
        precondition(!escapedDetail.contains("<"))
        precondition(!escapedDetail.contains(">"))
        precondition(!escapedDetail.contains("\u{2028}"))
        precondition(escapedDetail.contains(#"\u003C\/script"#))
        precondition(escapedDetail.contains(#"\u2028"#))

        // Diagnostics page auto-reload: poll /api/v1/health and jump back per target.
        let sampleDiagnostics = [
            ServiceDiagnostic(order: 0, title: "ASA Core</b><script>", path: "/api/v1/health", ok: false, status: "未连接</script>")
        ]
        let mainPageHTML = DiagnosticsPage.html(
            detail: dangerousDetail,
            diagnostics: sampleDiagnostics,
            compatibilityCopilotEnabled: false,
            target: .mainWindow
        )
        precondition(!mainPageHTML.contains("showFloating"))
        precondition(mainPageHTML.contains("fetch('/api/v1/health'"))
        precondition(mainPageHTML.contains("}, 5000)"))
        precondition(mainPageHTML.contains("location.href = '/asa-app'"))
        precondition(!mainPageHTML.contains("location.href = '/asa-floating'"))
        // The injected "</script>" must be escaped; the page's own closing
        // script tag is the only raw occurrence left.
        precondition(mainPageHTML.components(separatedBy: "</script>").count - 1 == 1)
        precondition(mainPageHTML.contains(#"\u003C\/script"#))
        precondition(!mainPageHTML.contains("\u{2028}"))

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
