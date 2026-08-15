import Cocoa
import ApplicationServices
import Carbon.HIToolbox
import ImageIO
import OSLog
import Vision
import WebKit

final class DraggableDotButton: NSButton {
    override func mouseDragged(with event: NSEvent) {
        window?.performDrag(with: event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler, NSWindowDelegate {
    private var panel: NSPanel!
    private var mainWindow: NSWindow!
    private var mainWebView: WKWebView!
    private var collapsedPanel: NSPanel!
    private var webView: WKWebView!
    private var detachedWindow: NSWindow?
    private var detachedWebView: WKWebView?
    private var detachedListWindow: NSWindow?
    private var detachedListWebView: WKWebView?
    /// 名单窗口待注入的名单 JSON（页面 didFinish 后写入 window.__DETACHED_LIST__）。
    private var pendingDetachedListJSON: String?
    private var collapsedButton: DraggableDotButton!
    private var statusItem: NSStatusItem!
    private var statusTimer: Timer?
    private var nativeContextTimer: Timer?
    private var localKeyMonitor: Any?
    private var screenshotTask: Process?
    private var globalHotKeyRefs: [EventHotKeyRef] = []
    private var globalHotKeyHandlerRef: EventHandlerRef?
    private var panelWasVisibleBeforeScreenshot = false
    private var lastNativeContextSignature = ""
    private var lastContextApplication: NSRunningApplication?
    private var activationContextWorkItem: DispatchWorkItem?
    private var coreRecoveryGeneration = 0
    private var mainPageLoadGeneration = 0
    private var floatingPageLoadGeneration = 0
    private let hotKeyLogPath = "/tmp/asa_floating_hotkeys.log"
    private let webLogger = Logger(subsystem: "local.asa.floating", category: "WebView")
    private let serviceBaseURL = URL(string: "http://127.0.0.1:8765")!
    private lazy var webSecurityPolicy = ASAWebSecurityPolicy(serviceBaseURL: serviceBaseURL)
    private var appVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.2.29"
    }
    private lazy var floatingURL = serviceBaseURL.appendingPathComponent("asa-floating").appending(queryItems: [URLQueryItem(name: "ui", value: appVersion)])
    private lazy var stateURL = serviceBaseURL.appendingPathComponent("api/asa/floating/state")
    private lazy var contextURL = serviceBaseURL.appendingPathComponent("api/asa/floating/context")
    // The desktop Agent uses its own local route so it is not confused with the browser workbench.
    private lazy var workbenchURL = serviceBaseURL.appendingPathComponent("asa-app")
    private lazy var healthURL = serviceBaseURL.appendingPathComponent("api/v1/health")
    private let launchAgentLabel = "ai.hermes.liepin-workbench"
    private let coreHealthRetryDelays: [TimeInterval] = CoreRecoverySchedule.standard.retryDelays
    private let compatibilityCopilotEnabled = CommandLine.arguments.contains("--compat-copilot")

    private func webSurfaceName(for target: WKWebView) -> String {
        target === mainWebView || target === detachedWebView || target === detachedListWebView ? "agent" : "copilot"
    }

    private func loggableWebLocation(_ url: URL?) -> String {
        guard let url else { return "missing-url" }
        guard let scheme = url.scheme, let host = url.host else {
            return url.absoluteString == "about:blank" ? "about:blank" : "non-network-url"
        }
        let port = url.port.map { ":\($0)" } ?? ""
        let origin = "\(scheme)://\(host)\(port)"
        let isLocalService = scheme == serviceBaseURL.scheme
            && host == serviceBaseURL.host
            && url.port == serviceBaseURL.port
        return isLocalService ? "\(origin)\(url.path)" : origin
    }

    private func isNavigationCancellation(_ error: Error) -> Bool {
        let nsError = error as NSError
        return nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
    }

    // Liepin is authenticated in the long-lived CDP Chrome profile, not the
    // browser selected by NSWorkspace. Open those links through CDP so a
    // candidate resume does not unexpectedly land on the login page.
    private func openExternalURL(_ url: URL) {
        let host = (url.host ?? "").lowercased()
        let isLiepin = host == "liepin.com" || host.hasSuffix(".liepin.com")
        guard isLiepin else {
            NSWorkspace.shared.open(url)
            return
        }

        // /json/new treats everything after the first '?' as the target URL;
        // escape target query separators so res_id_encode is not truncated.
        let cdpTarget = url.absoluteString.replacingOccurrences(of: "&", with: "%26")
        var request = URLRequest(url: URL(string: "http://127.0.0.1:9223/json/new?\(cdpTarget)")!)
        request.httpMethod = "PUT"
        request.timeoutInterval = 2
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            guard error == nil, (response as? HTTPURLResponse)?.statusCode == 200 else {
                self?.webLogger.error("Liepin CDP open failed; falling back to default browser")
                NSWorkspace.shared.open(url)
                return
            }
            self?.webLogger.info("Liepin URL opened in CDP Chrome on port 9223")
        }.resume()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMainMenu()
        buildStatusItem()
        buildMainWindow()
        registerGlobalHotKey()
        if compatibilityCopilotEnabled {
            buildPanel()
            buildCollapsedPanel()
            publishNativeContext(trigger: "launch", force: true)
        }
        restoreCoreAndLoad()
        showMainWindow()
        if compatibilityCopilotEnabled {
            showPanel()
            refreshCollapsedStatus()
            statusTimer = Timer.scheduledTimer(withTimeInterval: 6, repeats: true) { [weak self] _ in
                self?.refreshCollapsedStatus()
            }
            nativeContextTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { [weak self] _ in
                self?.publishNativeContext(trigger: "timer", force: false)
            }
            NSWorkspace.shared.notificationCenter.addObserver(
                self,
                selector: #selector(frontmostApplicationDidChange(_:)),
                name: NSWorkspace.didActivateApplicationNotification,
                object: nil
            )
            localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
                let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
                if flags == [.command, .shift], key == "s" {
                    self?.startScreenshotCapture()
                    return nil
                }
                if flags == [.command], key == "v" {
                    guard event.window === self?.panel, self?.panel.isKeyWindow == true else { return event }
                    self?.panel.makeKey()
                    self?.pasteClipboardIntoWebView()
                    return nil
                }
                if flags == [.command], let selector = self?.standardEditSelector(for: key) {
                    guard event.window === self?.panel, self?.panel.isKeyWindow == true else { return event }
                    self?.panel.makeKey()
                    self?.webView.window?.makeFirstResponder(self?.webView)
                    NSApp.sendAction(selector, to: nil, from: self)
                    return nil
                }
                return event
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        statusTimer?.invalidate()
        nativeContextTimer?.invalidate()
        activationContextWorkItem?.cancel()
        if let localKeyMonitor {
            NSEvent.removeMonitor(localKeyMonitor)
        }
        NSWorkspace.shared.notificationCenter.removeObserver(self)
        for globalHotKeyRef in globalHotKeyRefs {
            UnregisterEventHotKey(globalHotKeyRef)
        }
        globalHotKeyRefs.removeAll()
        if let globalHotKeyHandlerRef {
            RemoveEventHandler(globalHotKeyHandlerRef)
        }
    }

    private func registerGlobalHotKey() {
        var eventType = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        let selfPointer = Unmanaged.passUnretained(self).toOpaque()
        let handlerStatus = InstallEventHandler(
            GetApplicationEventTarget(),
            { _, event, userData in
                guard let event, let userData else { return OSStatus(eventNotHandledErr) }
                var hotKeyID = EventHotKeyID()
                let status = GetEventParameter(
                    event,
                    EventParamName(kEventParamDirectObject),
                    EventParamType(typeEventHotKeyID),
                    nil,
                    MemoryLayout<EventHotKeyID>.size,
                    nil,
                    &hotKeyID
                )
                guard status == noErr, hotKeyID.signature == ASAHotKeyRouting.globalHotKeySignature else {
                    return OSStatus(eventNotHandledErr)
                }
                let appDelegate = Unmanaged<AppDelegate>.fromOpaque(userData).takeUnretainedValue()
                DispatchQueue.main.async {
                    appDelegate.handleGlobalHotKey()
                }
                return noErr
            },
            1,
            &eventType,
            selfPointer,
            &globalHotKeyHandlerRef
        )
        guard handlerStatus == noErr else {
            NSLog("ASA Floating hotkey handler failed: \(handlerStatus)")
            notifyWebStatus("ASA 全局快捷键监听注册失败：\(handlerStatus)")
            return
        }

        let registered = ASAHotKeyRouting.defaultHotKeys
            .map { registerHotKey(id: $0.id.rawValue, keyCode: $0.keyCode, modifiers: $0.modifiers, label: $0.label) }
            .filter { $0 }
        if registered.isEmpty {
            notifyWebStatus("ASA 全局快捷键注册失败：请从菜单栏 ASA 显示 Agent。")
        } else {
            notifyWebStatus("ASA Agent 快捷键已启用：Option+Space；备用 Command+Shift+A / Control+Option+A。")
        }
    }

    private func appendHotKeyLog(_ message: String) {
        let line = "\(Date()) \(message)\n"
        guard let data = line.data(using: .utf8) else { return }
        if FileManager.default.fileExists(atPath: hotKeyLogPath),
           let handle = try? FileHandle(forWritingTo: URL(fileURLWithPath: hotKeyLogPath)) {
            handle.seekToEndOfFile()
            handle.write(data)
            try? handle.close()
        } else {
            try? data.write(to: URL(fileURLWithPath: hotKeyLogPath))
        }
    }

    private func registerHotKey(id: UInt32, keyCode: UInt32, modifiers: UInt32, label: String) -> Bool {
        var hotKeyRef: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: ASAHotKeyRouting.globalHotKeySignature, id: id)
        let status = RegisterEventHotKey(
            keyCode,
            modifiers,
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef
        )
        if status == noErr, let hotKeyRef {
            globalHotKeyRefs.append(hotKeyRef)
            NSLog("ASA Floating hotkey registered: \(label)")
            appendHotKeyLog("registered \(label)")
            return true
        }
        NSLog("ASA Floating hotkey failed: \(label), status=\(status)")
        appendHotKeyLog("failed \(label) status=\(status)")
        return false
    }

    private func handleGlobalHotKey() {
        let appBeforeActivation = NSWorkspace.shared.frontmostApplication
        publishNativeContext(trigger: "hotkey", force: true, preferredApplication: appBeforeActivation)
        switch ASAHotKeyRouting.destination(compatibilityCopilotEnabled: compatibilityCopilotEnabled) {
        case .agentMainWindow:
            appendHotKeyLog("triggered hotkey destination=agent")
            showMainWindow()
        case .compatibilityCopilot:
            appendHotKeyLog("triggered hotkey destination=compatibility-copilot")
            if panel.isVisible {
                collapsePanel()
            } else {
                presentPanel()
            }
        }
    }

    @objc private func frontmostApplicationDidChange(_ notification: Notification) {
        guard let app = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication else {
            return
        }
        guard !isControlSurfaceApp(app) else { return }
        lastContextApplication = app
        activationContextWorkItem?.cancel()
        let workItem = DispatchWorkItem { [weak self, weak app] in
            guard let app else { return }
            self?.publishNativeContext(trigger: "activation", force: true, preferredApplication: app)
        }
        activationContextWorkItem = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: workItem)
    }

    private func buildMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        if compatibilityCopilotEnabled {
            appMenu.addItem(NSMenuItem(title: "显示 ASA Copilot", action: #selector(showPanel), keyEquivalent: ""))
        }
        appMenu.addItem(NSMenuItem(title: "显示 ASA Agent", action: #selector(showMainWindow), keyEquivalent: "1"))
        appMenu.addItem(NSMenuItem(title: "启动/检查本机服务", action: #selector(startWorkbenchService), keyEquivalent: ""))
        appMenu.addItem(NSMenuItem(title: "刷新 ASA 页面", action: #selector(reloadASA), keyEquivalent: "r"))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(title: "退出 ASA", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appMenu.items.forEach { $0.target = self }
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(NSMenuItem(title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(title: "重做", action: Selector(("redo:")), keyEquivalent: "Z"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(title: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(title: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a"))
        editItem.submenu = editMenu
        NSApp.mainMenu = mainMenu
    }

    private func standardEditSelector(for key: String) -> Selector? {
        switch key {
        case "x": return #selector(NSText.cut(_:))
        case "c": return #selector(NSText.copy(_:))
        case "a": return #selector(NSText.selectAll(_:))
        default: return nil
        }
    }

    private func javaScriptStringLiteral(_ value: String) -> String {
        // Shared with DiagnosticsPage so any string embedded into JS (including
        // inside <script> markup) is safe against </script> breakout and
        // U+2028/U+2029 line terminators.
        DiagnosticsPage.javaScriptStringLiteral(value)
    }

    private func mimeType(for pathExtension: String) -> String {
        switch pathExtension.lowercased() {
        case "png": return "image/png"
        case "jpg", "jpeg": return "image/jpeg"
        case "webp": return "image/webp"
        case "pdf": return "application/pdf"
        case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case "xlsx": return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        case "xls": return "application/vnd.ms-excel"
        case "pptx": return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        case "csv": return "text/csv"
        case "txt", "md": return "text/plain"
        default: return "application/octet-stream"
        }
    }

    private func cgImage(from data: Data) -> CGImage? {
        guard let image = NSImage(data: data) else { return nil }
        var rect = CGRect(origin: .zero, size: image.size)
        return image.cgImage(forProposedRect: &rect, context: nil, hints: nil)
    }

    private func pngClipboardData() -> Data? {
        let pasteboard = NSPasteboard.general
        if let data = pasteboard.data(forType: .png) {
            return data
        }
        guard let image = NSImage(pasteboard: pasteboard) else { return nil }
        var rect = CGRect(origin: .zero, size: image.size)
        guard let imageRef = image.cgImage(forProposedRect: &rect, context: nil, hints: nil) else { return nil }
        return NSBitmapImageRep(cgImage: imageRef).representation(using: .png, properties: [:])
    }

    private func nativeAttachmentPayloads() -> [[String: Any]] {
        let pasteboard = NSPasteboard.general
        let supported = Set(["docx", "pdf", "txt", "md", "csv", "xls", "xlsx", "pptx", "png", "jpg", "jpeg", "webp"])
        let imageExtensions = Set(["png", "jpg", "jpeg", "webp"])
        let maxBytes = 25 * 1024 * 1024
        let urls = (pasteboard.readObjects(
            forClasses: [NSURL.self],
            options: [.urlReadingFileURLsOnly: true]
        ) as? [URL]) ?? []
        var payloads: [[String: Any]] = []
        for url in urls.prefix(3) {
            let ext = url.pathExtension.lowercased()
            guard supported.contains(ext),
                  let data = try? Data(contentsOf: url, options: .mappedIfSafe),
                  !data.isEmpty,
                  data.count <= maxBytes else { continue }
            var payload: [String: Any] = [
                "file_name": url.lastPathComponent,
                "mime_type": mimeType(for: ext),
                "content_base64": data.base64EncodedString(),
            ]
            if imageExtensions.contains(ext), let image = cgImage(from: data) {
                payload["image_analysis"] = localImageAnalysis(image, source: "pasted_local_image")
            }
            payloads.append(payload)
        }
        if !payloads.isEmpty { return payloads }
        guard let imageData = pngClipboardData(), !imageData.isEmpty, imageData.count <= maxBytes else { return [] }
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        var payload: [String: Any] = [
            "file_name": "clipboard-image-\(formatter.string(from: Date())).png",
            "mime_type": "image/png",
            "content_base64": imageData.base64EncodedString(),
        ]
        if let image = cgImage(from: imageData) {
            payload["image_analysis"] = localImageAnalysis(image, source: "pasted_clipboard_image")
        }
        return [payload]
    }

    private func sendNativeAttachmentsToWeb(_ payloads: [[String: Any]]) {
        guard JSONSerialization.isValidJSONObject(payloads),
              let data = try? JSONSerialization.data(withJSONObject: payloads),
              let literal = String(data: data, encoding: .utf8) else {
            notifyWebStatus("无法读取剪贴板附件。")
            return
        }
        let script = "window.asaReceiveNativeAttachments ? window.asaReceiveNativeAttachments(\(literal)) : false;"
        webView.evaluateJavaScript(script) { [weak self] _, error in
            if let error {
                self?.notifyWebStatus("附件粘贴失败：\(error.localizedDescription)")
            }
        }
    }

    private func pasteClipboardIntoWebView() {
        let attachments = nativeAttachmentPayloads()
        if !attachments.isEmpty {
            sendNativeAttachmentsToWeb(attachments)
            return
        }
        guard let text = NSPasteboard.general.string(forType: .string), !text.isEmpty else {
            notifyWebStatus("剪贴板里没有可粘贴的文字、文件或图片。")
            return
        }
        let literal = javaScriptStringLiteral(text)
        let script = """
        (() => {
          const text = \(literal);
          const active = document.activeElement;
          const el = active && active !== document.body ? active : document.getElementById('input');
          if (!el) return false;
          const tag = String(el.tagName || '').toLowerCase();
          if (tag === 'textarea' || tag === 'input') {
            el.focus();
            const start = Number.isFinite(el.selectionStart) ? el.selectionStart : el.value.length;
            const end = Number.isFinite(el.selectionEnd) ? el.selectionEnd : el.value.length;
            el.value = el.value.slice(0, start) + text + el.value.slice(end);
            const cursor = start + text.length;
            el.selectionStart = cursor;
            el.selectionEnd = cursor;
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste', data: text }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
          }
          if (el.isContentEditable) {
            el.focus();
            document.execCommand('insertText', false, text);
            return true;
          }
          return false;
        })();
        """
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "ASA"
        statusItem.button?.toolTip = "ASA Agent"
        let menu = NSMenu()
        if compatibilityCopilotEnabled {
            menu.addItem(NSMenuItem(title: "显示 ASA Copilot", action: #selector(showPanel), keyEquivalent: ""))
            menu.addItem(NSMenuItem(title: "收起为圆点", action: #selector(collapsePanel), keyEquivalent: ""))
        }
        menu.addItem(NSMenuItem(title: "显示 ASA Agent", action: #selector(showMainWindow), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "启动/检查本机服务", action: #selector(startWorkbenchService), keyEquivalent: ""))
        if compatibilityCopilotEnabled {
            let screenshotItem = NSMenuItem(title: "截图", action: #selector(startScreenshotCapture), keyEquivalent: "s")
            screenshotItem.keyEquivalentModifierMask = [.command, .shift]
            menu.addItem(screenshotItem)
        }
        menu.addItem(NSMenuItem(title: "刷新 ASA 页面", action: #selector(reloadASA), keyEquivalent: "r"))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "退出 ASA", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        menu.items.forEach { $0.target = self }
        statusItem.menu = menu
    }

    private func webConfiguration() -> WKWebViewConfiguration {
        let config = WKWebViewConfiguration()
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        let controller = WKUserContentController()
        controller.add(self, name: "asaNative")
        config.userContentController = controller
        return config
    }

    private func buildMainWindow() {
        let rect = NSRect(x: 0, y: 0, width: 1240, height: 800)
        mainWindow = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        mainWindow.title = "ASA Agent"
        blendTitlebarWithWebUI(mainWindow)
        mainWindow.isReleasedWhenClosed = false
        mainWindow.minSize = NSSize(width: 900, height: 620)
        mainWindow.setFrameAutosaveName("ASA Main Window")
        mainWebView = WKWebView(frame: rect, configuration: webConfiguration())
        mainWebView.customUserAgent = "ASAApp/\(appVersion)"
        mainWebView.navigationDelegate = self
        mainWebView.uiDelegate = self
        mainWebView.autoresizingMask = [.width, .height]
        mainWindow.contentView = mainWebView
    }

    /// Design Language v1：系统深色模式下仍保持浅色标题栏，并与 Web 纸感底色（--bg #f2f3ef）融合，
    /// 避免原生黑标题栏与浅色 Web 内容割裂。
    private func blendTitlebarWithWebUI(_ window: NSWindow, hideTitle: Bool = true) {
        window.appearance = NSAppearance(named: .aqua)
        window.titlebarAppearsTransparent = true
        if hideTitle { window.titleVisibility = .hidden }
        window.backgroundColor = NSColor(calibratedRed: 0.949, green: 0.953, blue: 0.937, alpha: 1)
    }

    private func buildPanel() {
        let rect = NSRect(x: 0, y: 0, width: 430, height: 720)
        panel = NSPanel(
            contentRect: rect,
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "ASA Copilot"
        panel.titleVisibility = .hidden
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        panel.isReleasedWhenClosed = false
        panel.hidesOnDeactivate = false
        blendTitlebarWithWebUI(panel)
        panel.setFrameAutosaveName("ASA Floating Panel")
        panel.minSize = NSSize(width: 360, height: 520)

        let config = webConfiguration()
        webView = WKWebView(frame: rect, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]

        let content = NSView(frame: rect)
        content.autoresizingMask = [.width, .height]
        webView.frame = content.bounds
        content.addSubview(webView)

        panel.contentView = content

        let accessory = NSTitlebarAccessoryViewController()
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 6
        stack.addArrangedSubview(makeTitleButton("截图", action: #selector(startScreenshotCapture)))
        stack.addArrangedSubview(makeTitleButton("收起", action: #selector(collapsePanel)))
        stack.addArrangedSubview(makeTitleButton("刷新", action: #selector(reloadFloatingPage)))
        accessory.view = stack
        accessory.layoutAttribute = .right
        panel.addTitlebarAccessoryViewController(accessory)
    }

    private func buildCollapsedPanel() {
        collapsedPanel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 54, height: 54),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        collapsedPanel.level = .floating
        collapsedPanel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .transient]
        collapsedPanel.isReleasedWhenClosed = false
        collapsedPanel.hidesOnDeactivate = false
        collapsedPanel.backgroundColor = .clear
        collapsedPanel.isOpaque = false
        collapsedPanel.hasShadow = true

        collapsedButton = DraggableDotButton(title: "ASA", target: self, action: #selector(showPanel))
        collapsedButton.toolTip = "ASA Copilot"
        collapsedButton.bezelStyle = .shadowlessSquare
        collapsedButton.isBordered = false
        collapsedButton.wantsLayer = true
        collapsedButton.layer?.cornerRadius = 27
        collapsedButton.layer?.backgroundColor = NSColor(red: 0.15, green: 0.33, blue: 0.25, alpha: 0.95).cgColor
        collapsedButton.attributedTitle = NSAttributedString(
            string: "ASA",
            attributes: [.foregroundColor: NSColor.white, .font: NSFont.systemFont(ofSize: 13, weight: .semibold)]
        )
        collapsedButton.frame = NSRect(x: 0, y: 0, width: 54, height: 54)
        collapsedPanel.contentView = collapsedButton
    }

    private func makeTitleButton(_ title: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.font = NSFont.systemFont(ofSize: 12)
        return button
    }

    @objc private func showPanel() {
        guard compatibilityCopilotEnabled else { return }
        publishNativeContext(trigger: "show", force: true)
        presentPanel()
    }

    @objc private func showMainWindow() {
        mainWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func canonicalAgentURL(from source: URL?) -> URL {
        guard let source,
              ["127.0.0.1", "localhost"].contains(source.host ?? "") else {
            return workbenchURL
        }
        var components = URLComponents(url: source, resolvingAgainstBaseURL: false)
        let fragment = components?.fragment ?? ""
        components?.scheme = serviceBaseURL.scheme
        components?.host = serviceBaseURL.host
        components?.port = serviceBaseURL.port
        components?.path = "/asa-app"
        components?.query = nil
        components?.fragment = ["job=", "workflow=", "sourcing_candidates="].contains { fragment.hasPrefix($0) } ? fragment : nil
        return components?.url ?? workbenchURL
    }

    private func loadMainWindow() {
        var url = workbenchURL
        if let route = UserDefaults.standard.string(forKey: "asa.lastRoute"), !route.isEmpty,
           let restored = URL(string: route) {
            url = canonicalAgentURL(from: restored)
        }
        UserDefaults.standard.set(url.absoluteString, forKey: "asa.lastRoute")
        mainPageLoadGeneration += 1
        mainWebView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20))
    }

    private func restoreCoreAndLoad() {
        coreRecoveryGeneration += 1
        let generation = coreRecoveryGeneration
        checkCoreHealth { [weak self] healthy, detail in
            DispatchQueue.main.async {
                guard let self, generation == self.coreRecoveryGeneration else { return }
                if healthy {
                    self.completeCoreLoad()
                } else {
                    self.kickstartCore()
                    self.retryCoreHealth(attempt: 0, generation: generation, lastDetail: detail)
                }
            }
        }
    }

    private func checkCoreHealth(completion: @escaping (Bool, String) -> Void) {
        var request = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 4)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                completion(false, error.localizedDescription)
                return
            }
            guard let http = response as? HTTPURLResponse else {
                completion(false, "Core 未返回 HTTP 响应")
                return
            }
            guard (200...299).contains(http.statusCode) else {
                completion(false, "Core 返回 HTTP \(http.statusCode)")
                return
            }
            let payload = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let healthy = payload?["ok"] as? Bool ?? false
            completion(healthy, healthy ? "Core 已就绪" : "Core 健康检查未通过")
        }.resume()
    }

    private func retryCoreHealth(attempt: Int, generation: Int, lastDetail: String) {
        guard attempt < CoreRecoverySchedule.standard.maxRetryAttempts else {
            showServiceUnavailablePages("自动恢复未成功（已重试 \(attempt) 次）：\(lastDetail)", generation: generation)
            refreshCollapsedStatus()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + coreHealthRetryDelays[attempt]) { [weak self] in
            guard let self, generation == self.coreRecoveryGeneration else { return }
            self.checkCoreHealth { [weak self] healthy, detail in
                DispatchQueue.main.async {
                    guard let self, generation == self.coreRecoveryGeneration else { return }
                    if healthy {
                        self.completeCoreLoad()
                    } else {
                        self.retryCoreHealth(attempt: attempt + 1, generation: generation, lastDetail: detail)
                    }
                }
            }
        }
    }

    private func completeCoreLoad() {
        loadMainWindow()
        if compatibilityCopilotEnabled {
            loadFloatingPage()
            refreshCollapsedStatus()
        }
    }

    private func kickstartCore() {
        let label = launchAgentLabel
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = ["kickstart", "-k", "gui/\(getuid())/\(label)"]
        let errorPipe = Pipe()
        process.standardError = errorPipe
        process.terminationHandler = { [weak self] process in
            let stderrData = errorPipe.fileHandleForReading.readDataToEndOfFile()
            let stderr = String(data: stderrData, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let status = process.terminationStatus
            self?.appendHotKeyLog("core kickstart status=\(status) stderr=\(stderr)")
            if status != 0 {
                let detail = stderr.isEmpty
                    ? "launchd 服务 \(label) 可能未加载，请检查服务状态。"
                    : stderr
                self?.notifyWebStatus("Core 启动命令失败（launchctl 退出码 \(status)）：\(detail)")
            }
        }
        do {
            try process.run()
        } catch {
            appendHotKeyLog("core kickstart run_error=\(error.localizedDescription)")
            notifyWebStatus("Core 恢复失败：\(error.localizedDescription)")
        }
    }

    private func presentPanel() {
        collapsedPanel?.orderOut(nil)
        if panel.frame.origin == .zero {
            panel.center()
        }
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func frontmostWindowInfo(for pid: pid_t) -> [String: Any] {
        guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return [:]
        }
        for window in windows {
            let ownerPID = window[kCGWindowOwnerPID as String] as? pid_t ?? 0
            let layer = window[kCGWindowLayer as String] as? Int ?? -1
            if ownerPID == pid && layer == 0 {
                let title = window[kCGWindowName as String] as? String ?? ""
                let bounds = window[kCGWindowBounds as String] as? [String: Any] ?? [:]
                return [
                    "title": title,
                    "window_id": window[kCGWindowNumber as String] as? Int ?? 0,
                    "owner_pid": Int(ownerPID),
                    "bounds": bounds,
                ]
            }
        }
        return [:]
    }

    private func publishNativeContext(trigger: String, force: Bool, preferredApplication: NSRunningApplication? = nil) {
        guard let app = preferredContextApplication(preferredApplication) else {
            return
        }
        let appName = app.localizedName ?? ""
        let bundleID = app.bundleIdentifier ?? ""
        let pid = app.processIdentifier
        if pid == ProcessInfo.processInfo.processIdentifier || bundleID == Bundle.main.bundleIdentifier {
            return
        }
        if !isControlSurfaceApp(app) {
            lastContextApplication = app
        }
        let window = frontmostWindowInfo(for: pid)
        let pasteboard = NSPasteboard.general
        let clipboardHasText = pasteboard.availableType(from: [.string]) != nil
        let signature = [
            appName,
            bundleID,
            String(pid),
            String(describing: window["title"] ?? ""),
            String(pasteboard.changeCount),
        ].joined(separator: "|")
        if !force && signature == lastNativeContextSignature && !isWeChatApp(name: appName, bundleID: bundleID) {
            return
        }
        lastNativeContextSignature = signature

        var payload: [String: Any] = [
            "surface": "native",
            "instance_id": Host.current().localizedName ?? "mac",
            "trigger": trigger,
            "page_focused": true,
            "page_visible": true,
            "status": "macOS 上下文已同步",
            "frontmost_app": [
                "name": appName,
                "bundle_id": bundleID,
                "pid": Int(pid),
            ],
            "window": window,
            "clipboard": NativeContextPrivacy.clipboardMetadata(
                hasText: clipboardHasText,
                changeCount: pasteboard.changeCount
            ),
            "context": [
                "type": "page",
                "page": "native",
                "label": (window["title"] as? String) ?? appName,
                "subtitle": appName,
            ],
        ]
        if isWeChatApp(name: appName, bundleID: bundleID) {
            // Background context sync must never trigger macOS permission dialogs.
            payload["wechat"] = readWeChatAccessibilityContext(pid: pid, appName: appName, promptForPermission: false)
        }
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            return
        }
        var request = URLRequest(url: contextURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request).resume()
    }

    private func preferredContextApplication(_ preferred: NSRunningApplication?) -> NSRunningApplication? {
        if let preferred, !isControlSurfaceApp(preferred) {
            return preferred
        }
        if let frontmost = NSWorkspace.shared.frontmostApplication, !isControlSurfaceApp(frontmost) {
            return frontmost
        }
        return lastContextApplication
    }

    private func isControlSurfaceApp(_ app: NSRunningApplication?) -> Bool {
        guard let app else { return true }
        let bundleID = (app.bundleIdentifier ?? "").lowercased()
        let name = (app.localizedName ?? "").lowercased()
        if app.processIdentifier == ProcessInfo.processInfo.processIdentifier {
            return true
        }
        if let ownBundleID = Bundle.main.bundleIdentifier?.lowercased(), bundleID == ownBundleID {
            return true
        }
        return bundleID == "local.asa.floating"
            || bundleID == "com.openai.codex"
            || bundleID == "com.openai.chatgpt"
            || name == "chatgpt"
            || name.contains("codex")
    }

    private func isWeChatApp(name: String, bundleID: String) -> Bool {
        let nameText = name.lowercased()
        let bundleText = bundleID.lowercased()
        return nameText.contains("wechat")
            || name.contains("微信")
            || bundleText.contains("com.tencent.xinwechat")
            || bundleText.contains("com.tencent.weworkmac")
    }

    private func readWeChatAccessibilityContext(pid: pid_t, appName: String, promptForPermission: Bool) -> [String: Any] {
        let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: promptForPermission] as CFDictionary
        let accessibilityAuthorized = AXIsProcessTrustedWithOptions(options)
        appendHotKeyLog("wechat ax_authorized=\(accessibilityAuthorized) prompt=\(promptForPermission)")
        if !accessibilityAuthorized {
            let ocr = readWeChatWindowOCR(pid: pid, promptForPermission: promptForPermission)
            let ocrBlocks = normalizeWeChatTextBlocks(ocr["text_blocks"] as? [String] ?? [])
            let combined = String(ocrBlocks.joined(separator: "\n").prefix(12000))
            return [
                "app": appName,
                "accessibility_authorized": false,
                "capture_mode": ocrBlocks.isEmpty ? "unavailable" : "vision_ocr",
                "text_block_count": ocrBlocks.count,
                "text_blocks": Array(ocrBlocks.prefix(120)),
                "combined_text": combined,
                "visible_text_clean": ocr["visible_text_clean"] ?? combined,
                "raw_text_blocks": ocr["raw_text_blocks"] ?? [],
                "message_blocks": ocr["message_blocks"] ?? [],
                "ocr_quality": ocr["ocr_quality"] ?? [:],
                "permission_debug": [
                    "ax_prompt_requested": promptForPermission,
                    "ax_process_trusted": false,
                    "ocr": ocr["permission_debug"] ?? [:],
                ],
                "status": ocrBlocks.isEmpty
                    ? "需要允许 ASA Floating 使用辅助功能或屏幕录制权限，才能自动读取当前微信窗口文本。\(String(ocr["status"] as? String ?? ""))"
                    : "已通过当前微信窗口截图 OCR 读取文本；辅助功能权限未授权，无法读取 AX 文本。",
            ]
        }

        let appElement = AXUIElementCreateApplication(pid)
        let focusedWindow = axElementAttribute(appElement, kAXFocusedWindowAttribute as String)
        let selectedWindow = focusedWindow ?? firstWindowElement(appElement)
        guard let windowElement = selectedWindow else {
            return [
                "app": appName,
                "accessibility_authorized": true,
                "status": "未找到当前微信窗口。",
                "text_blocks": [],
                "combined_text": "",
            ]
        }

        var visited = Set<UInt>()
        var blocks: [String] = []
        collectAXText(from: windowElement, depth: 0, visited: &visited, blocks: &blocks)
        var filtered = normalizeWeChatTextBlocks(blocks)
        var captureMode = "accessibility"
        var ocrStatus = ""
        if filtered.isEmpty {
            let ocr = readWeChatWindowOCR(pid: pid, promptForPermission: promptForPermission)
            let ocrBlocks = normalizeWeChatTextBlocks(ocr["text_blocks"] as? [String] ?? [])
            if !ocrBlocks.isEmpty {
                filtered = ocrBlocks
                captureMode = "vision_ocr"
            }
            ocrStatus = String(ocr["status"] as? String ?? "")
            let combined = String(filtered.joined(separator: "\n").prefix(12000))
            return [
                "app": appName,
                "accessibility_authorized": true,
                "capture_mode": captureMode,
                "window_title": axStringAttribute(windowElement, kAXTitleAttribute as String) ?? "",
                "text_block_count": filtered.count,
                "text_blocks": Array(filtered.prefix(120)),
                "combined_text": combined,
                "visible_text_clean": ocr["visible_text_clean"] ?? combined,
                "raw_text_blocks": ocr["raw_text_blocks"] ?? [],
                "message_blocks": ocr["message_blocks"] ?? [],
                "ocr_quality": ocr["ocr_quality"] ?? [:],
                "permission_debug": [
                    "ax_prompt_requested": promptForPermission,
                    "ax_process_trusted": true,
                    "ocr_status": ocrStatus,
                    "ocr": ocr["permission_debug"] ?? [:],
                ],
                "status": filtered.isEmpty
                    ? (ocrStatus.isEmpty ? "当前微信窗口没有暴露可读取文本，OCR 也未识别到文字。" : ocrStatus)
                    : "已通过当前微信窗口截图 OCR 读取文本。",
            ]
        }
        let combined = String(filtered.joined(separator: "\n").prefix(12000))
        return [
            "app": appName,
            "accessibility_authorized": true,
            "capture_mode": captureMode,
            "window_title": axStringAttribute(windowElement, kAXTitleAttribute as String) ?? "",
            "text_block_count": filtered.count,
            "text_blocks": Array(filtered.prefix(120)),
            "combined_text": combined,
            "permission_debug": [
                "ax_prompt_requested": promptForPermission,
                "ax_process_trusted": true,
                "ocr_status": ocrStatus,
            ],
            "status": filtered.isEmpty
                ? (ocrStatus.isEmpty ? "当前微信窗口没有暴露可读取文本，OCR 也未识别到文字。" : ocrStatus)
                : (captureMode == "vision_ocr" ? "已通过当前微信窗口截图 OCR 读取文本。" : "已读取当前微信窗口可访问文本。"),
        ]
    }

    private func readWeChatWindowOCR(pid: pid_t, promptForPermission: Bool) -> [String: Any] {
        let screenCapturePreflight = CGPreflightScreenCaptureAccess()
        let screenCaptureAuthorized = screenCapturePreflight || (promptForPermission ? CGRequestScreenCaptureAccess() : false)
        appendHotKeyLog("wechat screen_capture_authorized=\(screenCaptureAuthorized) prompt=\(promptForPermission)")
        guard screenCaptureAuthorized else {
            return [
                "status": "未授权屏幕录制，已跳过微信窗口 OCR。",
                "text_blocks": [],
                "permission_debug": [
                    "screen_capture_authorized": false,
                    "screen_capture_prompt_requested": promptForPermission,
                ],
            ]
        }
        guard let windows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
            return [
                "status": "无法读取窗口列表，可能缺少屏幕录制权限。",
                "text_blocks": [],
                "permission_debug": [
                    "screen_capture_authorized": screenCaptureAuthorized,
                    "screen_capture_prompt_requested": promptForPermission,
                ],
            ]
        }
        guard let window = windows.first(where: {
            ($0[kCGWindowOwnerPID as String] as? pid_t ?? 0) == pid
                && ($0[kCGWindowLayer as String] as? Int ?? -1) == 0
        }) else {
            return [
                "status": "未找到可截图的当前微信窗口。",
                "text_blocks": [],
                "permission_debug": [
                    "screen_capture_authorized": screenCaptureAuthorized,
                    "screen_capture_prompt_requested": promptForPermission,
                ],
            ]
        }
        let windowID = CGWindowID(window[kCGWindowNumber as String] as? Int ?? 0)
        guard windowID != 0 else {
            return [
                "status": "当前微信窗口缺少 window id。",
                "text_blocks": [],
                "permission_debug": [
                    "screen_capture_authorized": screenCaptureAuthorized,
                    "screen_capture_prompt_requested": promptForPermission,
                ],
            ]
        }
        let capture = captureWindowImageWithScreencapture(windowID: windowID)
        guard let image = capture.image else {
            let detail = capture.stderr.isEmpty ? "" : " stderr=\(capture.stderr)"
            return [
                "status": "微信窗口截图失败；请在系统设置中允许 ASA Floating 使用屏幕录制权限。",
                "text_blocks": [],
                "permission_debug": [
                    "screen_capture_authorized": screenCaptureAuthorized,
                    "screen_capture_prompt_requested": promptForPermission,
                    "screencapture_status": capture.status ?? -999,
                    "screencapture_stderr": capture.stderr,
                    "window_id": Int(windowID),
                ],
                "debug": "screencapture_status=\(capture.status ?? -999)\(detail)",
            ]
        }

        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
        request.usesLanguageCorrection = true
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        do {
            try handler.perform([request])
        } catch {
            return ["status": "微信窗口 OCR 失败：\(error.localizedDescription)", "text_blocks": []]
        }
        let observations = request.results ?? []
        let rawBlocks = observations
            .compactMap { $0.topCandidates(1).first?.string }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let messageBlocks = recognizedWeChatMessageBlocks(from: observations)
        let chatBlocks = messageBlocks
            .compactMap { $0["text"] as? String }
            .filter { !$0.isEmpty }
        let blocks = chatBlocks.isEmpty ? rawBlocks : chatBlocks
        let rawCount = rawBlocks.count
        let chatCount = chatBlocks.count
        let quality = chatCount >= 4 ? "high" : chatCount >= 2 ? "medium" : chatCount == 1 ? "low" : "none"
        return [
            "status": blocks.isEmpty ? "微信窗口截图成功，但 OCR 未识别到文字。" : "微信窗口 OCR 成功。",
            "text_blocks": blocks,
            "raw_text_blocks": Array(rawBlocks.prefix(160)),
            "visible_text_clean": String(blocks.joined(separator: "\n").prefix(12000)),
            "message_blocks": Array(messageBlocks.prefix(120)),
            "ocr_quality": [
                "quality": quality,
                "raw_block_count": rawCount,
                "chat_block_count": chatCount,
                "filtered_noise_count": max(0, rawCount - chatCount),
                "chat_region": [
                    "min_x": 0.075,
                    "min_y": 0.12,
                    "max_y": 0.965,
                ],
            ],
            "permission_debug": [
                "screen_capture_authorized": screenCaptureAuthorized,
                "screen_capture_prompt_requested": promptForPermission,
                "screencapture_status": capture.status ?? -999,
                "window_id": Int(windowID),
            ],
        ]
    }

    private func recognizedWeChatMessageBlocks(from observations: [VNRecognizedTextObservation]) -> [[String: Any]] {
        var seen = Set<String>()
        var rows: [[String: Any]] = []
        for observation in observations {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty || text.count > 800 || isWeChatChromeText(text) {
                continue
            }
            let box = observation.boundingBox
            let inMainPane = box.maxX >= 0.075 && box.minY >= 0.12 && box.maxY <= 0.965
            let likelyLeftList = box.maxX < 0.18 && box.minX < 0.075
            let likelyInputBar = box.minY < 0.12
            if !inMainPane || likelyLeftList || likelyInputBar {
                continue
            }
            let normalized = text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            if seen.insert(normalized).inserted {
                let side: String
                if box.minX >= 0.62 {
                    side = "self"
                } else if box.maxX <= 0.46 {
                    side = "other"
                } else {
                    side = "center"
                }
                rows.append([
                    "text": text,
                    "side": side,
                    "x": Double(box.minX),
                    "y": Double(box.minY),
                    "width": Double(box.width),
                    "height": Double(box.height),
                    "confidence": Double(candidate.confidence),
                ])
            }
        }
        return rows.sorted { left, right in
            let ly = left["y"] as? Double ?? 0
            let ry = right["y"] as? Double ?? 0
            if abs(ly - ry) > 0.015 {
                return ly > ry
            }
            return (left["x"] as? Double ?? 0) < (right["x"] as? Double ?? 0)
        }
    }

    private func isWeChatChromeText(_ text: String) -> Bool {
        let chrome = Set([
            "聊天", "通讯录", "收藏", "设置", "搜索", "发送", "表情", "更多", "最小化", "关闭", "全屏",
            "WeChat", "微信", "企业微信",
        ])
        if chrome.contains(text) {
            return true
        }
        if text.range(of: #"^\d{1,2}:\d{2}$"#, options: .regularExpression) != nil {
            return true
        }
        return false
    }

    private func captureWindowImageWithScreencapture(windowID: CGWindowID) -> (image: CGImage?, status: Int32?, stderr: String) {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("asa_wechat_\(UUID().uuidString).png")
        defer { try? FileManager.default.removeItem(at: url) }
        let task = Process()
        let stderrPipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        // Exclude the window shadow so normalized image coordinates map exactly
        // to the bounds returned by CGWindowListCopyWindowInfo.
        task.arguments = ["-x", "-o", "-l", "\(windowID)", url.path]
        task.standardError = stderrPipe
        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            appendHotKeyLog("wechat screencapture run_error=\(error.localizedDescription)")
            return (nil, nil, error.localizedDescription)
        }
        let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
        let stderr = String(data: stderrData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        appendHotKeyLog("wechat screencapture window=\(windowID) status=\(task.terminationStatus) stderr=\(stderr)")
        guard task.terminationStatus == 0,
              let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(
                  source,
                  0,
                  [kCGImageSourceShouldCacheImmediately: true] as CFDictionary
              ) else {
            return (nil, task.terminationStatus, stderr)
        }
        return (image, task.terminationStatus, stderr)
    }

    private func detectLikelyWeChatImageBubble(in image: CGImage) -> CGRect? {
        let request = VNDetectRectanglesRequest()
        request.maximumObservations = 120
        request.minimumSize = 0.015
        request.minimumAspectRatio = 0.2
        request.maximumAspectRatio = 1.0
        request.quadratureTolerance = 20
        request.minimumConfidence = 0.3
        do {
            try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
        } catch {
            appendHotKeyLog("wechat image rectangle_error=\(error.localizedDescription)")
            return nil
        }
        let candidates = (request.results ?? []).filter { item in
            let box = item.boundingBox
            return box.minX > 0.12
                && box.maxX < 0.97
                && box.minY > 0.12
                && box.maxY < 0.92
                && box.width >= 0.035
                && box.height >= 0.03
                && box.width * box.height >= 0.003
        }
        return candidates.min {
            if abs($0.boundingBox.minY - $1.boundingBox.minY) > 0.01 {
                return $0.boundingBox.minY < $1.boundingBox.minY
            }
            return $0.boundingBox.width * $0.boundingBox.height
                > $1.boundingBox.width * $1.boundingBox.height
        }?.boundingBox
    }

    private func localImageAnalysis(_ image: CGImage, source: String = "opened_current_wechat_image") -> [String: Any] {
        let textRequest = VNRecognizeTextRequest()
        textRequest.recognitionLevel = .accurate
        textRequest.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
        textRequest.usesLanguageCorrection = true
        textRequest.minimumTextHeight = 0.003

        let classificationRequest = VNClassifyImageRequest()
        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        var errors: [String] = []
        do {
            try handler.perform([textRequest, classificationRequest])
        } catch {
            errors.append(error.localizedDescription)
        }
        let blocks = (textRequest.results ?? [])
            .compactMap { $0.topCandidates(1).first?.string }
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        let classifications: [[String: Any]] = (classificationRequest.results ?? [])
            .filter { $0.confidence >= 0.03 }
            .prefix(12)
            .map { ["label": $0.identifier, "confidence": Double($0.confidence)] }
        return [
            "source": source,
            "ocr_text": String(blocks.joined(separator: "\n").prefix(12000)),
            "text_blocks": Array(blocks.prefix(120)),
            "classifications": classifications,
            "errors": errors,
        ]
    }

    private func requestWeChatImageBubble(in image: CGImage, completion: @escaping (CGRect?) -> Void) {
        let encoded = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(encoded, "public.png" as CFString, 1, nil) else {
            completion(detectLikelyWeChatImageBubble(in: image))
            return
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            completion(detectLikelyWeChatImageBubble(in: image))
            return
        }
        let payload = ["image_base64": encoded.base64EncodedString(options: [])]
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            completion(detectLikelyWeChatImageBubble(in: image))
            return
        }
        var request = URLRequest(url: serviceBaseURL.appendingPathComponent("api/asa/floating/image-detect"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            var detected: CGRect?
            if error == nil,
               let response = response as? HTTPURLResponse,
               (200...299).contains(response.statusCode),
               let data,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let box = object["box"] as? [String: Any],
               let x = (box["x"] as? NSNumber)?.doubleValue,
               let y = (box["y"] as? NSNumber)?.doubleValue,
               let width = (box["width"] as? NSNumber)?.doubleValue,
               let height = (box["height"] as? NSNumber)?.doubleValue {
                detected = CGRect(x: x, y: 1.0 - y - height, width: width, height: height)
            }
            if detected == nil {
                detected = self?.detectLikelyWeChatImageBubble(in: image)
            }
            DispatchQueue.main.async {
                completion(detected)
            }
        }.resume()
    }

    private func recognizeCurrentWeChatImage() {
        guard AXIsProcessTrusted() else {
            notifyWebStatus("需要辅助功能权限才能打开当前微信图片。")
            return
        }
        guard let app = NSWorkspace.shared.runningApplications.first(where: {
            isWeChatApp(name: $0.localizedName ?? "", bundleID: $0.bundleIdentifier ?? "")
        }) else {
            notifyWebStatus("微信当前未运行。")
            return
        }
        let pid = app.processIdentifier
        let sourceWindow = frontmostWindowInfo(for: pid)
        let windowID = CGWindowID(sourceWindow["window_id"] as? Int ?? 0)
        guard windowID != 0,
              let sourceImage = captureWindowImageWithScreencapture(windowID: windowID).image else {
            notifyWebStatus("当前微信窗口截图失败，请重试。")
            return
        }
        requestWeChatImageBubble(in: sourceImage) { [weak self] imageBox in
            guard let self else { return }
            guard let imageBox else {
                self.notifyWebStatus("未定位到当前聊天里的图片气泡；请让图片完整显示后重试。")
                return
            }
            self.openWeChatImage(app: app, sourceWindow: sourceWindow, imageBox: imageBox)
        }
    }

    private func openWeChatImage(app: NSRunningApplication, sourceWindow: [String: Any], imageBox: CGRect) {
        guard let bounds = sourceWindow["bounds"] as? [String: Any],
              let x = (bounds["X"] as? NSNumber)?.doubleValue,
              let y = (bounds["Y"] as? NSNumber)?.doubleValue,
              let width = (bounds["Width"] as? NSNumber)?.doubleValue,
              let height = (bounds["Height"] as? NSNumber)?.doubleValue else {
            notifyWebStatus("无法读取微信窗口位置，请重试。")
            return
        }

        let clickPoint = CGPoint(
            x: x + (imageBox.midX * width),
            y: y + ((1.0 - imageBox.midY) * height)
        )
        appendHotKeyLog("wechat image bubble=\(imageBox) click=\(clickPoint)")
        panel.orderOut(nil)
        collapsedPanel?.orderOut(nil)
        NSApp.deactivate()
        let activated = app.activate(options: [.activateAllWindows])
        appendHotKeyLog("wechat image activate=\(activated) target_pid=\(app.processIdentifier)")

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self else { return }
            let activePID = NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0
            appendHotKeyLog("wechat image active_pid=\(activePID) before_click")
            if activePID != app.processIdentifier {
                _ = app.activate(options: [.activateAllWindows])
            }
            CGWarpMouseCursorPosition(clickPoint)
            let eventSource = CGEventSource(stateID: .hidSystemState)
            let mouseDown = CGEvent(
                mouseEventSource: eventSource,
                mouseType: .leftMouseDown,
                mouseCursorPosition: clickPoint,
                mouseButton: .left
            )
            let mouseUp = CGEvent(
                mouseEventSource: eventSource,
                mouseType: .leftMouseUp,
                mouseCursorPosition: clickPoint,
                mouseButton: .left
            )
            mouseDown?.setIntegerValueField(.mouseEventClickState, value: 1)
            mouseUp?.setIntegerValueField(.mouseEventClickState, value: 1)
            mouseDown?.post(tap: .cghidEventTap)
            mouseUp?.post(tap: .cghidEventTap)
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                self?.captureOpenedWeChatImage(app: app)
            }
        }
    }

    private func captureOpenedWeChatImage(app: NSRunningApplication) {
        let window = frontmostWindowInfo(for: app.processIdentifier)
        appendHotKeyLog("wechat image capture_window=\(window)")
        let windowID = CGWindowID(window["window_id"] as? Int ?? 0)
        guard windowID != 0,
              let image = captureWindowImageWithScreencapture(windowID: windowID).image else {
            presentPanel()
            notifyWebStatus("微信图片打开后截图失败，请重试。")
            return
        }
        let analysis = localImageAnalysis(image)
        let blocks = analysis["text_blocks"] as? [String] ?? []
        let classifications = analysis["classifications"] as? [[String: Any]] ?? []
        let payload: [String: Any] = [
            "surface": "native",
            "instance_id": Host.current().localizedName ?? "mac",
            "trigger": "image_action",
            "page_focused": true,
            "page_visible": true,
            "status": "微信图片已在本机完成识别",
            "frontmost_app": [
                "name": app.localizedName ?? "微信",
                "bundle_id": app.bundleIdentifier ?? "com.tencent.xinWeChat",
                "pid": Int(app.processIdentifier),
            ],
            "window": window,
            "context": [
                "type": "page",
                "page": "native",
                "label": "微信图片",
                "subtitle": app.localizedName ?? "微信",
            ],
            "wechat": [
                "app": app.localizedName ?? "微信",
                "accessibility_authorized": true,
                "capture_mode": "vision_image_analysis",
                "window_title": window["title"] as? String ?? "",
                "text_block_count": blocks.count,
                "text_blocks": blocks,
                "combined_text": String(blocks.joined(separator: "\n").prefix(12000)),
                "image_analysis": analysis,
                "status": "已打开当前微信图片，并在本机完成 OCR 与图像分类。",
            ],
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload, options: []) else {
            presentPanel()
            notifyWebStatus("微信图片识别结果序列化失败。")
            return
        }
        var request = URLRequest(url: contextURL)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            DispatchQueue.main.async {
                guard let self else { return }
                CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: true)?.post(tap: .cghidEventTap)
                CGEvent(keyboardEventSource: nil, virtualKey: 53, keyDown: false)?.post(tap: .cghidEventTap)
                self.presentPanel()
                if let error {
                    self.notifyWebStatus("图片已识别，但同步失败：\(error.localizedDescription)")
                    return
                }
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                if !(200...299).contains(status) {
                    self.notifyWebStatus("图片已识别，但同步服务返回 HTTP \(status)。")
                    return
                }
                let summary = blocks.isEmpty && classifications.isEmpty
                    ? "图片已打开，但本机没有识别到可用文字或类别。"
                    : "图片已在本机识别，ASA 正在整理结果。"
                self.notifyWebStatus(summary, action: "imageAnalysisReady")
            }
        }.resume()
    }

    private func firstWindowElement(_ appElement: AXUIElement) -> AXUIElement? {
        guard let windows = axArrayAttribute(appElement, kAXWindowsAttribute as String) else { return nil }
        return windows.compactMap { $0 as! AXUIElement? }.first
    }

    private func axElementAttribute(_ element: AXUIElement, _ attribute: String) -> AXUIElement? {
        var raw: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute as CFString, &raw)
        guard status == .success, let raw else { return nil }
        return (raw as! AXUIElement)
    }

    private func axArrayAttribute(_ element: AXUIElement, _ attribute: String) -> [Any]? {
        var raw: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute as CFString, &raw)
        guard status == .success, let raw else { return nil }
        return raw as? [Any]
    }

    private func axStringAttribute(_ element: AXUIElement, _ attribute: String) -> String? {
        var raw: CFTypeRef?
        let status = AXUIElementCopyAttributeValue(element, attribute as CFString, &raw)
        guard status == .success, let raw else { return nil }
        if let text = raw as? String {
            return text
        }
        if let attributed = raw as? NSAttributedString {
            return attributed.string
        }
        if let number = raw as? NSNumber {
            return number.stringValue
        }
        return nil
    }

    private func collectAXText(from element: AXUIElement, depth: Int, visited: inout Set<UInt>, blocks: inout [String]) {
        guard depth <= 9, blocks.count < 240 else { return }
        let key = CFHash(element)
        if visited.contains(key) { return }
        visited.insert(key)

        let role = axStringAttribute(element, kAXRoleAttribute as String) ?? ""
        let shouldCollect = [
            kAXStaticTextRole as String,
            kAXTextAreaRole as String,
            kAXTextFieldRole as String,
            kAXButtonRole as String,
            kAXGroupRole as String,
        ].contains(role)
        if shouldCollect {
            for attribute in [kAXValueAttribute as String, kAXTitleAttribute as String, kAXDescriptionAttribute as String] {
                if let text = axStringAttribute(element, attribute) {
                    blocks.append(text)
                }
            }
        }

        guard let children = axArrayAttribute(element, kAXChildrenAttribute as String) else { return }
        for child in children.prefix(900) {
            let childElement = child as! AXUIElement
            collectAXText(from: childElement, depth: depth + 1, visited: &visited, blocks: &blocks)
            if blocks.count >= 240 { break }
        }
    }

    private func normalizeWeChatTextBlocks(_ blocks: [String]) -> [String] {
        var seen = Set<String>()
        var result: [String] = []
        let chrome = Set([
            "聊天", "通讯录", "收藏", "设置", "搜索", "发送", "表情", "更多", "最小化", "关闭", "全屏",
            "WeChat", "微信", "企业微信",
        ])
        for block in blocks {
            let text = block
                .replacingOccurrences(of: "\u{00a0}", with: " ")
                .replacingOccurrences(of: "\r", with: "\n")
                .split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                .filter { !$0.isEmpty }
                .joined(separator: "\n")
            if text.isEmpty || chrome.contains(text) || text.count > 2000 {
                continue
            }
            if seen.insert(text).inserted {
                result.append(text)
            }
        }
        return result
    }

    @objc private func collapsePanel() {
        guard compatibilityCopilotEnabled else { return }
        if let screen = panel.screen ?? NSScreen.main {
            let visible = screen.visibleFrame
            collapsedPanel.setFrameOrigin(NSPoint(x: visible.maxX - 72, y: visible.midY))
        }
        panel.orderOut(nil)
        collapsedPanel.orderFrontRegardless()
    }

    @objc private func reloadASA() {
        loadMainWindow()
        if compatibilityCopilotEnabled {
            loadFloatingPage()
            refreshCollapsedStatus()
        }
    }

    @objc private func reloadFloatingPage() {
        loadFloatingPage()
        refreshCollapsedStatus()
    }

    @objc private func openWorkbench() {
        showMainWindow()
    }

    /// 将 WebView 内的弹窗"弹出"为独立的 macOS 窗口（普通级别，可自由拖出屏幕）。
    /// body: { title, url } —— url 为 asa-app 详情页 hash 链接（如 /asa-app#candidate=137）
    /// 或   { title, candidates: [{id,name,company,title,stage}] } —— 名单数据，原生渲染列表。
    private func openDetachedDialog(_ body: [String: Any], source: WKWebView?) {
        let title = body["title"] as? String ?? "ASA"
        let anchor: DetachedAnchor? = parseAnchor(body["anchor"])
        if let list = body["list"] as? [String: Any] {
            presentDetachedCandidateList(title: title, list: list, anchor: anchor, sourceWindow: source?.window)
        } else if let urlString = body["url"] as? String, !urlString.isEmpty,
           let url = URL(string: urlString, relativeTo: serviceBaseURL)?.absoluteURL,
           webSecurityPolicy.isTrustedServiceURL(url) {
            let appURL = canonicalAgentURL(from: url)
            presentDetachedWindow(title: title, webURL: appURL, anchor: anchor, sourceWindow: source?.window)
        } else if let candidates = body["candidates"] as? [[String: Any]], !candidates.isEmpty {
            // 旧版前端只发扁平 candidates：包一层 groups 走同一条 Web 名单窗口路径。
            let list: [String: Any] = ["title": title, "groups": [["key": "all", "label": title, "candidates": candidates]]]
            presentDetachedCandidateList(title: title, list: list, anchor: anchor, sourceWindow: source?.window)
        }
    }

    private struct DetachedAnchor {
        let x: CGFloat
        let y: CGFloat
        let edge: String
    }

    private func parseAnchor(_ value: Any?) -> DetachedAnchor? {
        guard let dict = value as? [String: Any],
              let x = dict["x"] as? Double,
              let y = dict["y"] as? Double else { return nil }
        return DetachedAnchor(x: CGFloat(x), y: CGFloat(y), edge: dict["edge"] as? String ?? "center")
    }

    /// 独立窗口的初始位置：优先锚点（源窗口视口坐标 → 屏幕坐标换算），否则屏幕中心。
    /// 锚点相对发消息的源窗口（主窗口/浮窗/独立窗口）frame 换算到屏幕坐标系；edge 决定窗口靠锚点哪一侧展开。
    /// 注意不能假设浮窗 panel 存在：正常启动只创建主窗口，panel 为 nil，直接引用会闪退。
    private func anchorFrame(for anchor: DetachedAnchor?, size: NSSize, sourceWindow: NSWindow?) -> NSRect? {
        guard let anchor, let sourceWindow, sourceWindow.isVisible else { return nil }
        let sourceFrame = sourceWindow.frame
        // WebView 内容坐标 → 源窗口屏幕坐标（窗口顶部有标题栏，粗略减 30px）
        let contentX = sourceFrame.minX + anchor.x
        let contentY = sourceFrame.maxY - anchor.y - 30
        let screen = sourceWindow.screen ?? NSScreen.main
        guard let screenFrame = screen?.visibleFrame else { return nil }
        let w = min(size.width, screenFrame.width * 0.9)
        let h = min(size.height, screenFrame.height * 0.9)
        var x = contentX
        var y = contentY
        switch anchor.edge {
        case "left":
            x = contentX - w
        case "right":
            x = contentX
        case "top":
            y = contentY - h
        case "bottom":
            y = contentY
        default:
            x = contentX - w / 2
            y = contentY - h / 2
        }
        // 钳制在屏幕可见区域内
        x = max(screenFrame.minX + 8, min(x, screenFrame.maxX - w - 8))
        y = max(screenFrame.minY + 8, min(y, screenFrame.maxY - h - 8))
        return NSRect(x: x, y: y, width: w, height: h)
    }

    private func presentDetachedWindow(title: String, webURL: URL, anchor: DetachedAnchor?, sourceWindow: NSWindow?) {
        if detachedWindow == nil {
            let rect = NSRect(x: 0, y: 0, width: 760, height: 640)
            let window = NSWindow(
                contentRect: rect,
                styleMask: [.titled, .closable, .resizable, .miniaturizable],
                backing: .buffered,
                defer: false
            )
            window.title = title
            blendTitlebarWithWebUI(window, hideTitle: false)
            window.isReleasedWhenClosed = false
            window.setFrameAutosaveName("ASA Detached Dialog")
            window.minSize = NSSize(width: 480, height: 360)
            let config = webConfiguration()
            let web = WKWebView(frame: rect, configuration: config)
            // 与主窗口一致：/asa-app 按 UA 放行，缺了会被 Core 当作禁用的浏览器入口。
            web.customUserAgent = "ASAApp/\(appVersion)"
            web.navigationDelegate = self
            web.uiDelegate = self
            web.autoresizingMask = [.width, .height]
            window.contentView = web
            window.delegate = self
            detachedWindow = window
            detachedWebView = web
        }
        detachedWebView?.load(URLRequest(url: webURL, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20))
        detachedWindow?.title = title
        let wasVisible = detachedWindow?.isVisible ?? false
        if !wasVisible {
            if let frame = anchorFrame(for: anchor, size: NSSize(width: 760, height: 640), sourceWindow: sourceWindow) {
                detachedWindow?.setFrame(frame, display: true)
            } else {
                detachedWindow?.center()
            }
        }
        detachedWindow?.makeKeyAndOrderFront(nil)
        if !wasVisible {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    /// 独立名单窗口：WKWebView 加载 /asa-app#candidate_list=bare，页面加载完成后注入名单 JSON，
    /// 由前端同一个名单组件渲染，UI 与应用内完全一致；点人选在同窗口打开纯净详情页（bare=1）。
    private func presentDetachedCandidateList(title: String, list: [String: Any], anchor: DetachedAnchor?, sourceWindow: NSWindow?) {
        if detachedListWindow == nil {
            let rect = NSRect(x: 0, y: 0, width: 480, height: 600)
            let window = NSWindow(
                contentRect: rect,
                styleMask: [.titled, .closable, .resizable, .miniaturizable],
                backing: .buffered,
                defer: false
            )
            window.title = title
            blendTitlebarWithWebUI(window, hideTitle: false)
            window.isReleasedWhenClosed = false
            window.setFrameAutosaveName("ASA Detached Candidate List")
            window.minSize = NSSize(width: 380, height: 300)
            let web = WKWebView(frame: rect, configuration: webConfiguration())
            // 与主窗口一致：/asa-app 按 UA 放行，缺了会被 Core 当作禁用的浏览器入口。
            web.customUserAgent = "ASAApp/\(appVersion)"
            web.navigationDelegate = self
            web.uiDelegate = self
            web.autoresizingMask = [.width, .height]
            window.contentView = web
            window.delegate = self
            detachedListWindow = window
            detachedListWebView = web
        }
        if let data = try? JSONSerialization.data(withJSONObject: list),
           let json = String(data: data, encoding: .utf8) {
            pendingDetachedListJSON = json
        }
        var components = URLComponents(url: serviceBaseURL.appendingPathComponent("asa-app"), resolvingAgainstBaseURL: false)
        components?.fragment = "candidate_list=1&bare=1"
        if let url = components?.url {
            detachedListWebView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20))
        }
        detachedListWindow?.title = title
        let wasVisible = detachedListWindow?.isVisible ?? false
        if !wasVisible {
            if let frame = anchorFrame(for: anchor, size: NSSize(width: 480, height: 600), sourceWindow: sourceWindow) {
                detachedListWindow?.setFrame(frame, display: true)
            } else {
                detachedListWindow?.center()
            }
        }
        detachedListWindow?.makeKeyAndOrderFront(nil)
        if !wasVisible {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    private func loadWorkbenchURL(_ urlString: String) {
        guard let url = URL(string: urlString, relativeTo: serviceBaseURL)?.absoluteURL,
              ["127.0.0.1", "localhost"].contains(url.host ?? ""),
              url.path == "/asa-app" || url.path == "/workbench" || url.path == "/asa" || url.path == "/" else {
            notifyWebStatus("已拒绝非本机 ASA 页面导航。")
            return
        }
        let appURL = canonicalAgentURL(from: url)
        UserDefaults.standard.set(appURL.absoluteString, forKey: "asa.lastRoute")
        mainPageLoadGeneration += 1
        mainWebView.load(URLRequest(url: appURL, cachePolicy: .reloadIgnoringLocalAndRemoteCacheData, timeoutInterval: 20))
        // 浮窗是 floating level 常驻最前：跳详情页前先收起浮窗（DOM 保留），
        // 主窗口的人选详情页才能置顶可见；用户可点边缘小圆点随时展开浮窗继续切换人选。
        if compatibilityCopilotEnabled {
            collapsePanel()
        }
        showMainWindow()
    }

    @objc private func startWorkbenchService() {
        coreRecoveryGeneration += 1
        let generation = coreRecoveryGeneration
        kickstartCore()
        notifyWebStatus("正在通过 launchd 恢复 ASA Core...")
        retryCoreHealth(attempt: 0, generation: generation, lastDetail: "正在等待 Core 启动")
    }

    @objc private func retryServiceConnection() {
        notifyWebStatus("正在重试 ASA Core 连接...")
        restoreCoreAndLoad()
    }

    @objc private func startScreenshotCapture() {
        guard screenshotTask == nil else { return }
        panelWasVisibleBeforeScreenshot = panel.isVisible
        notifyWebStatus("进入截图模式：拖拽选择区域，Esc 取消。")
        panel.orderOut(nil)
        collapsedPanel?.orderOut(nil)

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.16) { [weak self] in
            self?.runSystemScreenshot()
        }
    }

    private func runSystemScreenshot() {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        task.arguments = ["-i", "-c", "-x"]
        task.terminationHandler = { [weak self] process in
            DispatchQueue.main.async {
                guard let self else { return }
                self.screenshotTask = nil
                let message = process.terminationStatus == 0
                    ? "截图已复制到剪贴板，可以直接粘贴到微信或 ASA 对话。"
                    : "已取消截图。"
                self.notifyWebStatus(message)
                if self.panelWasVisibleBeforeScreenshot {
                    self.showPanel()
                } else {
                    self.collapsedPanel.orderFrontRegardless()
                }
            }
        }
        do {
            screenshotTask = task
            try task.run()
        } catch {
            screenshotTask = nil
            notifyWebStatus("截图启动失败：\(error.localizedDescription)")
            if panelWasVisibleBeforeScreenshot {
                showPanel()
            } else {
                collapsedPanel.orderFrontRegardless()
            }
        }
    }

    private func notifyWebStatus(_ message: String, action: String = "") {
        let literal = javaScriptStringLiteral(message)
        let actionLiteral = javaScriptStringLiteral(action)
        let script = """
        window.dispatchEvent(new CustomEvent('asa-native-status', { detail: { message: \(literal), action: \(actionLiteral) } }));
        """
        let target = compatibilityCopilotEnabled ? webView : mainWebView
        target?.evaluateJavaScript(script, completionHandler: nil)
    }

    private func notifyWebAttachmentAnalysis(attachmentID: String, analysis: [String: Any]? = nil, error: String = "") {
        var detail: [String: Any] = ["attachment_id": attachmentID]
        if let analysis { detail["analysis"] = analysis }
        if !error.isEmpty { detail["error"] = error }
        guard JSONSerialization.isValidJSONObject(detail),
              let data = try? JSONSerialization.data(withJSONObject: detail),
              let literal = String(data: data, encoding: .utf8) else { return }
        let script = "window.dispatchEvent(new CustomEvent('asa-native-attachment-analysis', { detail: \(literal) }));"
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    private func loadFloatingPage() {
        let request = URLRequest(
            url: floatingURL,
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 20
        )
        floatingPageLoadGeneration += 1
        webView.load(request)
    }

    private func diagnosticRequest(
        order: Int,
        title: String,
        path: String,
        requiresJSONOK: Bool,
        userAgent: String? = nil,
        completion: @escaping (ServiceDiagnostic) -> Void
    ) {
        var request = URLRequest(
            url: serviceBaseURL.appendingPathComponent(path),
            cachePolicy: .reloadIgnoringLocalAndRemoteCacheData,
            timeoutInterval: 4
        )
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        if let userAgent {
            request.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        }
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                completion(ServiceDiagnostic(order: order, title: title, path: "/\(path)", ok: false, status: error.localizedDescription))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                completion(ServiceDiagnostic(order: order, title: title, path: "/\(path)", ok: false, status: "无 HTTP 响应"))
                return
            }
            var ok = (200...299).contains(http.statusCode)
            if ok && requiresJSONOK {
                let payload = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                ok = payload?["ok"] as? Bool ?? false
            }
            let status = ok ? "HTTP \(http.statusCode)" : "HTTP \(http.statusCode) 未通过"
            completion(ServiceDiagnostic(order: order, title: title, path: "/\(path)", ok: ok, status: status))
        }.resume()
    }

    private func performServiceDiagnostics(completion: @escaping ([ServiceDiagnostic]) -> Void) {
        let group = DispatchGroup()
        let lock = NSLock()
        var diagnostics: [ServiceDiagnostic] = []

        func run(_ order: Int, _ title: String, _ path: String, _ requiresJSONOK: Bool, _ userAgent: String? = nil) {
            group.enter()
            diagnosticRequest(order: order, title: title, path: path, requiresJSONOK: requiresJSONOK, userAgent: userAgent) { result in
                lock.lock()
                diagnostics.append(result)
                lock.unlock()
                group.leave()
            }
        }

        run(0, "ASA Core", "api/v1/health", true)
        run(1, "Copilot 状态", "api/asa/floating/state", true)
        run(2, "Agent 界面", "asa-app", false, "ASAApp/\(appVersion)")
        group.notify(queue: .main) {
            completion(diagnostics.sorted { $0.order < $1.order })
        }
    }

    private func showServiceUnavailablePages(_ detail: String, generation: Int) {
        let mainGeneration = mainPageLoadGeneration
        let floatingGeneration = floatingPageLoadGeneration
        performServiceDiagnostics { [weak self] diagnostics in
            guard let self, generation == self.coreRecoveryGeneration else { return }
            if mainGeneration == self.mainPageLoadGeneration {
                self.renderServiceUnavailablePage(detail, diagnostics: diagnostics, in: self.mainWebView)
            }
            if self.compatibilityCopilotEnabled && floatingGeneration == self.floatingPageLoadGeneration {
                self.renderServiceUnavailablePage(detail, diagnostics: diagnostics, in: self.webView)
            }
        }
    }

    private func showServiceUnavailablePage(_ detail: String, in target: WKWebView? = nil) {
        guard let resolvedTarget = target ?? webView else { return }
        let generation = pageLoadGeneration(for: resolvedTarget)
        performServiceDiagnostics { [weak self, weak target] diagnostics in
            guard let self, let currentTarget = target ?? self.webView else { return }
            guard generation == self.pageLoadGeneration(for: currentTarget) else { return }
            self.renderServiceUnavailablePage(detail, diagnostics: diagnostics, in: currentTarget)
        }
    }

    private func pageLoadGeneration(for target: WKWebView) -> Int {
        target === mainWebView ? mainPageLoadGeneration : floatingPageLoadGeneration
    }

    private func renderServiceUnavailablePage(_ detail: String, diagnostics: [ServiceDiagnostic], in target: WKWebView) {
        let diagnosticsTarget: DiagnosticsPageTarget = target === mainWebView ? .mainWindow : .floatingPanel
        let html = DiagnosticsPage.html(
            detail: detail,
            diagnostics: diagnostics,
            compatibilityCopilotEnabled: compatibilityCopilotEnabled,
            target: diagnosticsTarget
        )
        target.loadHTMLString(
            html,
            baseURL: DiagnosticsPage.baseURL(target: diagnosticsTarget, mainWindowURL: workbenchURL, floatingURL: floatingURL)
        )
    }

    private func refreshCollapsedStatus() {
        URLSession.shared.dataTask(with: stateURL) { [weak self] data, _, error in
            var color = NSColor(red: 0.10, green: 0.32, blue: 0.92, alpha: 0.95)
            var tooltip = "ASA Copilot"
            if error != nil || data == nil {
                color = NSColor(red: 0.71, green: 0.14, blue: 0.10, alpha: 0.96)
                tooltip = "ASA 服务未连接"
            } else if let data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                let approvals = json["pending_approvals"] as? [[String: Any]] ?? []
                let active = json["active_context"] as? [String: Any] ?? [:]
                let connected = active["connected"] as? Bool ?? false
                if !approvals.isEmpty {
                    color = NSColor(red: 0.71, green: 0.28, blue: 0.03, alpha: 0.96)
                    tooltip = "ASA 有待审批动作"
                } else if connected {
                    color = NSColor(red: 0.03, green: 0.45, blue: 0.26, alpha: 0.96)
                    tooltip = "ASA 已同步当前对象"
                } else {
                    color = NSColor(red: 0.71, green: 0.28, blue: 0.03, alpha: 0.96)
                    tooltip = "ASA 等待页面同步"
                }
            }
            DispatchQueue.main.async {
                self?.collapsedButton?.layer?.backgroundColor = color.cgColor
                self?.collapsedButton?.toolTip = tooltip
                self?.statusItem?.button?.toolTip = tooltip
            }
        }.resume()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func windowWillClose(_ notification: Notification) {
        if let window = notification.object as? NSWindow {
            if window === detachedWindow {
                detachedWebView?.stopLoading()
            } else if window === detachedListWindow {
                detachedListWebView?.stopLoading()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            webLogger.error("navigation deny surface=\(self.webSurfaceName(for: webView), privacy: .public) reason=missing-url")
            decisionHandler(.cancel)
            return
        }
        if url.absoluteString == "about:blank" {
            webLogger.debug("navigation allow surface=\(self.webSurfaceName(for: webView), privacy: .public) url=about:blank")
            decisionHandler(.allow)
            return
        }
        let surface: ASAWebSurface = (webView === mainWebView || webView === detachedWebView || webView === detachedListWebView) ? .agent : .copilot
        if webSecurityPolicy.allowsNavigation(to: url, on: surface) {
            webLogger.info("navigation allow surface=\(self.webSurfaceName(for: webView), privacy: .public) url=\(self.loggableWebLocation(url), privacy: .public)")
            if webView === mainWebView {
                UserDefaults.standard.set(canonicalAgentURL(from: url).absoluteString, forKey: "asa.lastRoute")
            }
            decisionHandler(.allow)
            return
        }
        if webSecurityPolicy.allowsExternalURL(url), navigationAction.navigationType == .linkActivated {
            openExternalURL(url)
        }
        webLogger.error("navigation deny surface=\(self.webSurfaceName(for: webView), privacy: .public) url=\(self.loggableWebLocation(url), privacy: .public)")
        decisionHandler(.cancel)
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        webLogger.info("navigation start surface=\(self.webSurfaceName(for: webView), privacy: .public) url=\(self.loggableWebLocation(webView.url), privacy: .public)")
    }

    func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
        webLogger.info("navigation commit surface=\(self.webSurfaceName(for: webView), privacy: .public) url=\(self.loggableWebLocation(webView.url), privacy: .public)")
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webLogger.info("navigation finish surface=\(self.webSurfaceName(for: webView), privacy: .public) url=\(self.loggableWebLocation(webView.url), privacy: .public)")
        // 独立名单窗口：页面加载完成后注入名单 JSON，前端轮询读取并渲染。
        if webView === detachedListWebView, let json = pendingDetachedListJSON {
            webView.evaluateJavaScript("window.__DETACHED_LIST__ = \(json);") { _, _ in }
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil,
              let url = navigationAction.request.url else {
            return nil
        }
        guard webSecurityPolicy.allowsExternalURL(url) else { return nil }
        openExternalURL(url)
        return nil
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let openPanel = NSOpenPanel()
        openPanel.canChooseFiles = true
        openPanel.canChooseDirectories = parameters.allowsDirectories
        openPanel.allowsMultipleSelection = parameters.allowsMultipleSelection
        openPanel.resolvesAliases = true

        let finish: (NSApplication.ModalResponse) -> Void = { response in
            completionHandler(response == .OK ? openPanel.urls : nil)
        }
        if let window = webView.window {
            openPanel.beginSheetModal(for: window, completionHandler: finish)
        } else {
            finish(openPanel.runModal())
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if isNavigationCancellation(error) {
            webLogger.debug("navigation cancelled surface=\(self.webSurfaceName(for: webView), privacy: .public)")
            return
        }
        webLogger.error("navigation provisional-failure surface=\(self.webSurfaceName(for: webView), privacy: .public) domain=\(nsError.domain, privacy: .public) code=\(nsError.code)")
        showServiceUnavailablePage(error.localizedDescription, in: webView)
        refreshCollapsedStatus()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        let nsError = error as NSError
        if isNavigationCancellation(error) {
            webLogger.debug("navigation cancelled-after-commit surface=\(self.webSurfaceName(for: webView), privacy: .public)")
            return
        }
        webLogger.error("navigation failure surface=\(self.webSurfaceName(for: webView), privacy: .public) domain=\(nsError.domain, privacy: .public) code=\(nsError.code)")
        showServiceUnavailablePage(error.localizedDescription, in: webView)
        refreshCollapsedStatus()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "asaNative",
              let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,
              webSecurityPolicy.isTrustedOrigin(scheme: origin.protocol, host: origin.host, port: origin.port),
              let sourceWebView = message.webView else { return }
        let surface: ASAWebSurface = (sourceWebView === mainWebView || sourceWebView === detachedWebView || sourceWebView === detachedListWebView) ? .agent : .copilot
        guard sourceWebView === mainWebView || sourceWebView === webView || sourceWebView === detachedWebView || sourceWebView === detachedListWebView,
              webSecurityPolicy.allowsBridgeAction(type, on: surface) else { return }
        if type == "screenshot" {
            startScreenshotCapture()
        } else if type == "recognizeWeChatImage" {
            recognizeCurrentWeChatImage()
        } else if type == "analyzePastedImage" {
            let attachmentID = body["attachment_id"] as? String ?? ""
            let encoded = body["content_base64"] as? String ?? ""
            guard !attachmentID.isEmpty,
                  let data = Data(base64Encoded: encoded),
                  data.count <= 25 * 1024 * 1024,
                  let image = cgImage(from: data) else {
                notifyWebAttachmentAnalysis(attachmentID: attachmentID, error: "无法解码粘贴的图片。")
                return
            }
            notifyWebAttachmentAnalysis(
                attachmentID: attachmentID,
                analysis: localImageAnalysis(image, source: "pasted_clipboard_image")
            )
        } else if type == "reload" {
            guard compatibilityCopilotEnabled else { return }
            reloadFloatingPage()
        } else if type == "openWorkbench" {
            if let urlString = body["url"] as? String, !urlString.isEmpty {
                loadWorkbenchURL(urlString)
            } else {
                openWorkbench()
            }
        } else if type == "showFloating" {
            guard compatibilityCopilotEnabled else { return }
            presentPanel()
        } else if type == "hideFloating" {
            collapsePanel()
        } else if type == "openDetachedDialog" {
            openDetachedDialog(body, source: sourceWebView)
        } else if type == "openExternal", let urlString = body["url"] as? String,
                  let url = URL(string: urlString), webSecurityPolicy.allowsExternalURL(url) {
            openExternalURL(url)
        } else if type == "startWorkbenchService" {
            startWorkbenchService()
        } else if type == "retryServiceConnection" {
            retryServiceConnection()
        }
    }
}
