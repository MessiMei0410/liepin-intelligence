import Foundation

/// Which external links get handed to the long-lived CDP Chrome on port 9223
/// instead of the default browser selected by NSWorkspace.
///
/// Liepin and X-SaaS keep their login sessions in that Chrome profile (the
/// opencli X-SaaS flows run on the same 9223 browser), so sending candidate
/// resume / profile links there avoids landing on a login page in an unrelated
/// browser. Everything else opens in the default browser.
enum ExternalLinkRouting {
    static func viaCDPChrome(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return host == "liepin.com" || host.hasSuffix(".liepin.com")
            || host == "x-saas.com.cn" || host.hasSuffix(".x-saas.com.cn")
    }
}