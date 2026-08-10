import Foundation

enum ASAWebSurface {
    case agent
    case copilot
}

struct ASAWebSecurityPolicy {
    private let scheme: String
    private let host: String
    private let port: Int

    init(serviceBaseURL: URL) {
        guard let scheme = serviceBaseURL.scheme,
              let host = serviceBaseURL.host,
              let port = serviceBaseURL.port else {
            preconditionFailure("ASA service URL must include scheme, host, and port")
        }
        self.scheme = scheme.lowercased()
        self.host = host.lowercased()
        self.port = port
    }

    func isTrustedOrigin(scheme: String, host: String, port: Int) -> Bool {
        scheme.lowercased() == self.scheme
            && host.lowercased() == self.host
            && port == self.port
    }

    func isTrustedServiceURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme,
              let host = url.host,
              let port = url.port else { return false }
        return isTrustedOrigin(scheme: scheme, host: host, port: port)
    }

    func allowsNavigation(to url: URL, on surface: ASAWebSurface) -> Bool {
        guard isTrustedServiceURL(url), url.user == nil, url.password == nil else { return false }
        switch surface {
        case .agent:
            return url.path == "/asa-app" && url.query == nil
        case .copilot:
            return url.path == "/asa-floating"
        }
    }

    func allowsBridgeAction(_ action: String, on surface: ASAWebSurface) -> Bool {
        switch surface {
        case .agent:
            return [
                "openDetachedDialog", "openWorkbench", "retryServiceConnection", "startWorkbenchService",
            ].contains(action)
        case .copilot:
            return [
                "analyzePastedImage", "hideFloating", "openDetachedDialog", "openExternal", "openWorkbench", "recognizeWeChatImage",
                "reload", "retryServiceConnection", "screenshot", "showFloating", "startWorkbenchService",
            ].contains(action)
        }
    }

    func allowsExternalURL(_ url: URL) -> Bool {
        guard url.user == nil, url.password == nil else { return false }
        return url.scheme?.lowercased() == "https" || url.scheme?.lowercased() == "http"
    }
}
