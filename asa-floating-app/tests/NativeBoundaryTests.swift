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
    }
}
