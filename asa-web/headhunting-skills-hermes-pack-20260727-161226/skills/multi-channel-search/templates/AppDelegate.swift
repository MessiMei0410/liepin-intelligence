import Cocoa
import WebKit

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let webView = WKWebView()
        webView.navigationDelegate = self

        let windowRect = NSRect(x: 0, y: 0, width: 1200, height: 800)
        window = NSWindow(contentRect: windowRect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)

        window.title = "鹏新旭候选人搜索"
        window.minSize = NSSize(width: 900, height: 600)
        window.contentView = webView
        window.center()

        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.appearance = NSAppearance(named: .darkAqua)

        if let resPath = Bundle.main.resourcePath {
            let htmlPath = (resPath as NSString).appendingPathComponent("report.html")
            let htmlDir = resPath as String
            let htmlURL = URL(fileURLWithPath: htmlPath)
            let baseURL = URL(fileURLWithPath: htmlDir, isDirectory: true)
            webView.loadFileURL(htmlURL, allowingReadAccessTo: baseURL)
        }

        window.makeKeyAndOrderFront(nil)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = navigationAction.request.url,
           navigationAction.navigationType == .linkActivated {
            if url.absoluteString.hasPrefix("https://h.liepin.com/") {
                NSWorkspace.shared.open(url)
                decisionHandler(.cancel)
                return
            }
        }
        decisionHandler(.allow)
    }
}
