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

    /// Percent-encode a URL for embedding into `CDP /json/new?<target>`.
    ///
    /// CDP percent-decodes its query string exactly once and treats the result
    /// as the target URL. Percent-encoding every URL-reserved character
    /// (':', '/', '?', '=', '&', '#', '%' …) therefore keeps query separators,
    /// the X-SaaS SPA hash route (`#/app/candidate/info/<id>`), and any literal
    /// '%' sequences intact instead of truncating them at the HTTP layer.
    static func cdpTargetEncoding(_ url: URL) -> String {
        let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
        return url.absoluteString.addingPercentEncoding(withAllowedCharacters: unreserved) ?? url.absoluteString
    }
}