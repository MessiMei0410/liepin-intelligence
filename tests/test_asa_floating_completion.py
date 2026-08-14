import unittest
import sys
import tempfile
import sqlite3
import re
import base64
from unittest.mock import patch
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import liepin_workbench_server as server  # noqa: E402
from a_system_agent.privacy import sanitize_context_snapshot  # noqa: E402


class ASAFloatingCompletionTest(unittest.TestCase):
    def test_app_bundle_build_contract_includes_icon_and_version(self) -> None:
        build = (ROOT / "asa-floating-app" / "scripts" / "build.sh").read_text(encoding="utf-8")
        self.assertIn("AppIcon.icns", build)
        self.assertIn("CFBundleIconFile", build)
        self.assertIn("<string>AppIcon</string>", build)
        self.assertIn("-framework Vision", build)
        self.assertIn("ASA Floating Local Code Signing", build)
        self.assertNotIn('grep -q "$SIGN_IDENTITY"', build)
        version = re.search(
            r"<key>CFBundleShortVersionString</key>\s*<string>([0-9.]+)</string>", build
        )
        build_number = re.search(r"<key>CFBundleVersion</key>\s*<string>(\d+)</string>", build)
        self.assertIsNotNone(version)
        self.assertIsNotNone(build_number)
        self.assertGreaterEqual(tuple(map(int, version.group(1).split("."))), (0, 2, 23))
        self.assertGreaterEqual(int(build_number.group(1)), 46)
        self.assertIn("LSMinimumSystemVersion", build)
        self.assertIn('SIGNING_MODE="${ASA_SIGNING_MODE:-stable}"', build)
        self.assertNotIn("&& sign_with_timeout; then", build)
        self.assertIn("NSScreenCaptureUsageDescription", build)
        self.assertIn("NSAppleEventsUsageDescription", build)

    def test_native_shell_exposes_service_recovery_bridge(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        source += (ROOT / "asa-floating-app" / "src" / "DiagnosticsPage.swift").read_text(encoding="utf-8")
        for marker in [
            "startWorkbenchService",
            "openWorkbench",
            "registerGlobalHotKey",
            "Option+Space",
            "Command+Shift+A",
            "Control+Option+A",
            "/tmp/asa_floating_hotkeys.log",
            "registerHotKey",
            "ASA Floating hotkey registered",
            "publishNativeContext",
            "frontmostApplicationDidChange",
            "NSWorkspace.didActivateApplicationNotification",
            "isControlSurfaceApp",
            "preferredContextApplication",
            "ProcessInfo.processInfo.processIdentifier",
            "preferredApplication",
            "presentPanel",
            "frontmostWindowInfo",
            "readWeChatAccessibilityContext",
            "AXIsProcessTrustedWithOptions",
            "VNRecognizeTextRequest",
            "readWeChatWindowOCR",
            "recognizedWeChatMessageBlocks",
            "detectLikelyWeChatImageBubble",
            "requestWeChatImageBubble",
            "VNDetectRectanglesRequest",
            "VNClassifyImageRequest",
            "vision_image_analysis",
            "recognizeWeChatImage",
            "captureWindowImageWithScreencapture",
            'task.arguments = ["-x", "-o", "-l",',
            "NSApp.deactivate()",
            "mouseEventClickState",
            "/usr/sbin/screencapture",
            "vision_ocr",
            "com.tencent.xinwechat",
            "com.tencent.weworkmac",
            "combined_text",
            "visible_text_clean",
            "message_blocks",
            "ocr_quality",
            "text_blocks",
            "permission_debug",
            "CGRequestScreenCaptureAccess",
            "CGPreflightScreenCaptureAccess",
            "screen_capture_authorized",
            "screencapture_status",
            "screencapture_stderr",
            "kCGImageSourceShouldCacheImmediately",
            "surface\": \"native\"",
            "showServiceUnavailablePage",
            "本机 ASA 服务未连接",
            "ASA 服务诊断",
            "启动本机服务",
            "performServiceDiagnostics",
            "coreHealthRetryDelays",
            'run(0, "ASA Core", "api/v1/health", true)',
            'run(1, "Copilot 状态", "api/asa/floating/state", true)',
            'run(2, "Agent 界面", "asa-app", false',
            'mainWebView.customUserAgent = "ASAApp/\\(appVersion)"',
            'type == "reload"',
            'type == "retryServiceConnection"',
            'type == "openWorkbench"',
            'type == "startWorkbenchService"',
            "nativeAttachmentPayloads",
            "sendNativeAttachmentsToWeb",
            'type == "analyzePastedImage"',
            "pasted_clipboard_image",
            "/usr/sbin/screencapture",
        ]:
            self.assertIn(marker, source)
        self.assertNotIn("ScreenshotOverlayView", source)
        self.assertNotIn("captureDisplayImages", source)

    def test_native_windows_keep_a_real_draggable_titlebar(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        self.assertNotIn(".fullSizeContentView", source)
        self.assertNotIn("DragHandleView", source)
        self.assertIn('mainWindow.title = "ASA Agent"', source)
        self.assertIn('panel.title = "ASA Copilot"', source)
        self.assertIn("panel.titleVisibility = .hidden", source)
        self.assertIn('CommandLine.arguments.contains("--compat-copilot")', source)
        self.assertIn("if compatibilityCopilotEnabled", source)

    def test_agent_routes_selected_context_to_the_react_conversation_surface(self) -> None:
        # Agent Conversation Surface v1: compatibility callers dispatch an in-app event only.
        # 旧 src/copilot/bridge.ts 已随死代码清理删除，入口统一在 src/agent/navigation.ts。
        navigation = (ROOT / "asa-web" / "src" / "agent" / "navigation.ts").read_text(encoding="utf-8")
        app = (ROOT / "asa-web" / "src" / "app" / "App.tsx").read_text(encoding="utf-8")
        self.assertFalse((ROOT / "asa-web" / "src" / "copilot").exists())
        self.assertIn("AGENT_NAVIGATE_EVENT", navigation + app)
        self.assertNotIn("publishCopilotContext", navigation + app)
        self.assertNotIn("showFloating", navigation + app)
        self.assertNotIn("/api/asa/floating/context", navigation + app)

    def test_floating_header_has_stable_brand_actions_and_context_rows(self) -> None:
        server = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        self.assertIn('class="header-actions"', server)
        self.assertIn('class="context-line"', server)
        self.assertIn('class="context-copy"', server)
        self.assertNotIn('class="drag-spacer"', server)

    def test_floating_composer_supports_pasted_and_selected_attachments(self) -> None:
        source = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        for marker in [
            'id="attachmentInput"',
            'id="attachmentList"',
            "asaReceiveNativeAttachments",
            "addEventListener('paste'",
            "/api/asa/floating/upload",
            "uploaded_attachments",
            "一次最多添加 3 个附件",
        ]:
            self.assertIn(marker, source)

    def test_floating_upload_extracts_text_without_exposing_path(self) -> None:
        payload = server.prepare_floating_upload(
            {
                "file_name": "候选人说明.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode("8年机械设计经验".encode()).decode(),
            }
        )
        attachment = payload["attachment"]
        self.assertTrue(attachment["content_available"])
        self.assertIn("8年机械设计经验", attachment["extracted_text"])
        self.assertNotIn("path", attachment)

    def test_floating_upload_rejects_unsafe_or_oversized_files(self) -> None:
        encoded = base64.b64encode(b"hello").decode()
        with self.assertRaisesRegex(ValueError, "名称不合法"):
            server.prepare_floating_upload({"file_name": "../secret.txt", "content_base64": encoded})
        with self.assertRaisesRegex(ValueError, "暂不支持"):
            server.prepare_floating_upload({"file_name": "payload.exe", "content_base64": encoded})
        with patch.object(server, "ASA_UPLOAD_MAX_BASE64_CHARS", 4):
            with self.assertRaisesRegex(ValueError, "25 MB"):
                server.prepare_floating_upload({"file_name": "large.txt", "content_base64": encoded})

    def test_workflow_links_navigate_inside_native_agent_window(self) -> None:
        server = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        self.assertIn("native('openWorkbench', {url})", server)
        self.assertIn('let urlString = body["url"] as? String', source)
        self.assertIn("loadWorkbenchURL(urlString)", source)

    def test_message_and_suggestion_job_links_use_native_workbench_bridge(self) -> None:
        source = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        message_actions = source[source.index("function runMessageAction"):source.index("function runProactiveSuggestion")]
        suggestions = source[source.index("function runProactiveSuggestion"):source.index("function floatingMessageContext")]

        self.assertIn("type === 'open_job'", message_actions)
        self.assertIn("openWorkbenchUrl(`/asa-app#job=${encodeURIComponent(id)}`)", message_actions)
        self.assertIn("openWorkbenchUrl(`/asa-app${suffix}`)", message_actions)
        self.assertNotIn("window.open(`/asa-app${suffix}`", message_actions)
        self.assertIn("openWorkbenchUrl(`/asa-app#job=${encodeURIComponent(id)}`)", suggestions)
        self.assertIn("openWorkbenchUrl('/asa-app')", suggestions)

    def test_streaming_empty_message_does_not_render_a_placeholder_reply(self) -> None:
        source = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        self.assertNotIn("<p>暂无回复。</p>", source)
        self.assertIn("if (!body && !actions && !toolSummary && !toolDetails && !workflowCard && !intentCard && !patchBar && !analysisCard && !candidateListCard) return '';", source)
        self.assertIn("renderThinkingMessage()", source)

    def test_floating_r3_card_requires_a_complete_snapshot_and_shows_verbatim_constraints(self) -> None:
        source = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        self.assertIn("typeof item === 'string' ? item : item?.rule || item?.quote", source)
        self.assertIn("snapshot.ready === true", source)
        self.assertIn("channels.length > 0", source)
        self.assertIn("snapshotReady ? '' : ' disabled'", source)
        self.assertIn("原话约束：", source)
        for label in ["待开始", "排队中", "执行中", "待审批", "等待渠道回执", "技术失败", "已完成", "已取消", "已被新修订替代"]:
            self.assertIn(label, source)

    def test_background_context_sync_never_prompts_for_permissions(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        self.assertIn(
            "readWeChatAccessibilityContext(pid: pid, appName: appName, promptForPermission: false)",
            source,
        )
        authorization_guard = source.index("guard screenCaptureAuthorized else")
        screenshot_call = source.index("let capture = captureWindowImageWithScreencapture", authorization_guard)
        self.assertLess(authorization_guard, screenshot_call)

    def test_native_shell_scopes_shortcuts_and_bridge_capabilities(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        policy = (ROOT / "asa-floating-app" / "src" / "WebSecurityPolicy.swift").read_text(encoding="utf-8")
        self.assertIn("event.window === self?.panel", source)
        self.assertIn("NSEvent.removeMonitor(localKeyMonitor)", source)
        self.assertIn("message.frameInfo.isMainFrame", source)
        self.assertIn("message.frameInfo.securityOrigin", source)
        self.assertIn("allowsBridgeAction", source)
        self.assertIn('url.path == "/asa-app"', policy)
        self.assertIn('url.path == "/asa-floating"', policy)

    def test_native_context_does_not_capture_or_persist_clipboard_text(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        context_source = source[source.index("private func publishNativeContext"):source.index("private func preferredContextApplication")]
        self.assertNotIn("clipboardPreview", context_source)
        self.assertNotIn('"preview":', context_source)
        self.assertNotIn("string(forType: .string)", context_source)
        sanitized = sanitize_context_snapshot(
            {
                "surface": "native",
                "clipboard": {
                    "has_text": True,
                    "change_count": 9,
                    "length": 18,
                    "preview": "password=top-secret",
                    "content": "top-secret",
                },
            }
        )
        self.assertEqual(sanitized["clipboard"], {"has_text": True, "change_count": 9})

    def test_floating_state_sanitizes_legacy_native_clipboard_fields(self) -> None:
        legacy_context = {
            "surface": "native",
            "frontmost_app": {"name": "Safari", "bundle_id": "com.apple.Safari"},
            "clipboard": {
                "has_text": True,
                "change_count": 9,
                "length": 18,
                "preview": "password=top-secret",
            },
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

        class FakeRuntimeService:
            db_path = Path("/tmp/asa-floating-test-missing-agent.db")

            def get_runtime_timeline(self, limit: int = 8) -> dict:
                return {
                    "context_snapshots": [{"payload": sanitize_context_snapshot(legacy_context)}],
                    "tool_calls": [],
                    "permission_audit": [],
                }

        with server.ASA_FLOATING_LOCK:
            server.ASA_FLOATING_CONTEXTS.clear()
            server.ASA_FLOATING_CONTEXTS["native"] = legacy_context
        try:
            payload = server.build_floating_state(type("State", (), {"agent_service": FakeRuntimeService()})())
            self.assertEqual(
                payload["bridge"]["context_instances"]["native"]["clipboard"],
                {"has_text": True, "change_count": 9},
            )
            self.assertEqual(
                payload["runtime"]["context_snapshots"][0]["payload"]["clipboard"],
                {"has_text": True, "change_count": 9},
            )
        finally:
            with server.ASA_FLOATING_LOCK:
                server.ASA_FLOATING_CONTEXTS.clear()

    def test_service_diagnostics_are_scoped_to_page_load_generation(self) -> None:
        source = (ROOT / "asa-floating-app" / "src" / "AppDelegate.swift").read_text(encoding="utf-8")
        self.assertIn("mainPageLoadGeneration += 1", source)
        self.assertIn("floatingPageLoadGeneration += 1", source)
        self.assertIn("generation == self.pageLoadGeneration(for: currentTarget)", source)

    def test_floating_page_keeps_actions_available_but_hidden_by_default(self) -> None:
        server = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        for marker in [
            'id="contextActions"',
            'id="runSummary"',
            "renderContextActions()",
            "renderRunSummary()",
            "renderContextSnapshot()",
            "renderToolTimeline()",
            "项执行异常",
            "contextSnapshot",
            "toolTimeline",
            "context_snapshot",
            "runtime?.tool_calls",
            "show_suggested_actions",
            "workbenchTargetUrl",
            "workbenchTargetLabel",
            "openWorkbenchRun",
            "#workflow",
            "floating_context_stale_after",
            "is_asa_floating_native_context",
            "preserve_native_invocation_trigger",
            "native_context_is_control_surface",
            "answerAfterNativeImage",
            "answerAfterNativeAttachment",
            "open_wechat_attachment::",
            "retry_previous",
            "/api/asa/floating/image-detect",
            "native_context_has_wechat_text",
            "native:wechat",
            "微信当前对话",
            "source_label\": \"微信\"",
            "visible_text_clean",
            "ocr_quality",
            "state.data?.show_suggested_actions === true",
            "runSummaryHasAttention",
            "diagnostic-strip",
            "run-summary compact",
            "目标在网页",
            ".context-panels.empty",
            "/Users/messi/Applications/ASA Floating.app",
            "display_mode:'floating_compact'",
            "details.msg-section",
            "renderConnectionError(err)",
            "pending_approvals",
            "active_goals",
            "recent_artifacts",
            "record_context_snapshot",
            "record_tool_call",
            '"native"',
            "/api/asa/floating/action",
            "payload.workflow_id = id",
            "runFloatingAction(type, id, planRef)",
            "open_workflow",
            "/api/asa/floating/state",
            "/api/asa/floating/context",
            "/api/asa/floating/commands",
            "requestStartedAt",
            "请求超时，请重试",
            "上一条消息仍在处理中",
            "上一次请求已中断，可以重新发送",
            "recoverStuckRequest",
            "timeoutMs:45000",
            'id="sendButton"',
        ]:
            self.assertIn(marker, server)

    def test_floating_proposal_cards_use_core_v1_preflight_and_decision_routes(self) -> None:
        source = (ROOT / "scripts" / "liepin_workbench_server.py").read_text(encoding="utf-8")
        for marker in [
            'id="proposalCards"',
            "renderActionCards",
            "/api/v1/agent/proposals?status=pending&limit=4",
            "/api/v1/agent/proposals/${encodeURIComponent(proposalId)}/preflight",
            "/api/v1/agent/proposals/${encodeURIComponent(proposalId)}/decision",
            "confirmation_token",
            "已确认，内部跟进任务已创建。",
            "proposalReceipts",
            "postCheck:postCheck.summary",
            "data-proposal-open-candidate",
            "runMessageAction('open_candidate', button.dataset.proposalOpenCandidate)",
        ]:
            self.assertIn(marker, source)
        for marker in [
            "parseWorkflowHash",
            "parseJobHash",
            "openHashedWorkflow",
            "openHashedJob",
            "showHashNavigationHint",
            "asa-hash-navigation-hint",
            "activateQueueItem",
        ]:
            self.assertIn(marker, source)
        schema = (ROOT / "scripts" / "a_system_agent" / "schema.py").read_text(encoding="utf-8")
        service = (ROOT / "scripts" / "a_system_agent" / "service.py").read_text(encoding="utf-8")
        for marker in [
            "agent_context_snapshots",
            "agent_tool_calls",
            "agent_permissions",
            "def record_context_snapshot",
            "def record_tool_call",
            "def record_permission_request",
            "def get_runtime_timeline",
        ]:
            self.assertTrue(marker in schema or marker in service)
        self.assertNotIn("无待审批</span>", source)
        self.assertNotIn("无执行目标</span>", source)
        self.assertNotIn("const visibleCalls = calls.slice(0, 3)", source)
        self.assertNotIn("工具与权限时间线", source)
        self.assertNotIn("需要注意的工具动作", source)
        self.assertIn("snapshot.show_in_floating !== true", source)
        self.assertIn("permission !== 'read'", source)
        privacy = (ROOT / "scripts" / "a_system_agent" / "privacy.py").read_text(encoding="utf-8")
        self.assertIn('normalized == "wechat"', privacy)
        self.assertIn('"capture_mode" in item', privacy)

    def test_floating_copilot_uses_compact_response_mode(self) -> None:
        llm = (ROOT / "scripts" / "a_system_agent" / "llm.py").read_text(encoding="utf-8")
        service = (ROOT / "scripts" / "a_system_agent" / "service.py").read_text(encoding="utf-8")
        copilot_handler = (ROOT / "scripts" / "a_system_agent" / "copilot_handler.py").read_text(encoding="utf-8")
        # copilot_handler.py 已于 2026-08-12 拆分为 5 个域模块（facade 转发），
        # 实现字符串在拆分后的模块中搜索。
        copilot_impl = "".join(
            (ROOT / "scripts" / "a_system_agent" / f"copilot_{m}.py").read_text(encoding="utf-8")
            for m in ("evidence", "intent", "sessions", "routing", "api", "impl", "skill_routes")
        )
        copilot_runtime = service + copilot_handler + copilot_impl
        self.assertIn("COPILOT_FLOATING_SYSTEM_PROMPT", llm)
        self.assertIn("payload.get(\"response_mode\") == \"floating_compact\"", llm)
        self.assertIn("浮窗空间很小", llm)
        self.assertIn("untrusted_screen_content", copilot_runtime)
        self.assertIn("attachment_content_available", copilot_runtime)
        self.assertIn("visual_understanding_available", copilot_runtime)
        self.assertIn("resolve_wechat_attachments", copilot_runtime)
        self.assertIn("不可信屏幕内容", llm)
        self.assertIn("不得声称已打开、读取或理解附件内容", llm)
        self.assertIn("attachment_evidence", llm)
        self.assertIn("chat_database_accessed=false", llm)
        self.assertIn("page_evidence.image_analysis", llm)
        self.assertIn("page_evidence.ocr_quality.quality", llm)
        self.assertIn("page_evidence.page_type=wechat_visible_window", llm)
        self.assertIn("conversation_history", llm)
        self.assertIn("没有 memory_write_receipt", llm)
        self.assertIn("按这个格式整/参考这个模板", llm)
        self.assertIn("不得默认套用招聘、人选、JD、候选人核验", llm)
        self.assertIn("忽略驾驶舱、岗位、人选、目标队列", llm)
        self.assertIn("floating_compact", copilot_runtime)
        self.assertIn("document_understanding", copilot_runtime)
        self.assertIn("\"response_mode\": \"floating_compact\" if floating_compact else \"default\"", copilot_runtime)

    def test_wechat_native_context_survives_generic_native_focus(self) -> None:
        with server.ASA_FLOATING_LOCK:
            server.ASA_FLOATING_CONTEXTS.clear()

        server.update_floating_context(
            {
                "surface": "native",
                "instance_id": "mac",
                "trigger": "timer",
                "page_focused": True,
                "page_visible": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "window": {"title": "微信"},
                "wechat": {
                    "capture_mode": "vision_ocr",
                    "status": "微信窗口 OCR 成功。",
                    "combined_text": "Mars: minimax 模型 codex 跑你那个不行么？\n我: 把 A 系统升级成 agent 了称之为 ASA",
                    "text_blocks": ["Mars: minimax 模型 codex 跑你那个不行么？"],
                },
            }
        )
        server.update_floating_context(
            {
                "surface": "native",
                "instance_id": "mac",
                "trigger": "hotkey",
                "page_focused": True,
                "page_visible": True,
                "frontmost_app": {"name": "ChatGPT", "bundle_id": "com.openai.codex"},
                "window": {"title": "ChatGPT"},
            }
        )

        with server.ASA_FLOATING_LOCK:
            contexts = {key: dict(value) for key, value in server.ASA_FLOATING_CONTEXTS.items()}
        self.assertIn("native:wechat", contexts)
        self.assertIn("native", contexts)
        active = server.select_floating_active_context(contexts, datetime.now())
        self.assertTrue(server.native_context_has_wechat_text(active))
        self.assertFalse(server.native_context_is_control_surface(active))
        payload = server.build_floating_active_payload(active)
        self.assertEqual(payload["source_label"], "微信")
        self.assertEqual(payload["type"], "wechat")
        self.assertEqual(payload["status"], "已识别")

    def test_wechat_context_prefers_clean_visible_text(self) -> None:
        context = {
            "surface": "native",
            "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
            "window": {"title": "微信"},
            "wechat": {
                "status": "微信窗口 OCR 成功。",
                "combined_text": "左侧联系人噪声\n公众号\nMars: minimax 跑你那个不行么？",
                "visible_text_clean": "Mars: minimax 跑你那个不行么？\n我: ASA 先把识别链路调稳",
                "text_blocks": ["Mars: minimax 跑你那个不行么？", "我: ASA 先把识别链路调稳"],
                "message_blocks": [
                    {"text": "Mars: minimax 跑你那个不行么？", "side": "other", "x": 0.12, "y": 0.55},
                    {"text": "我: ASA 先把识别链路调稳", "side": "self", "x": 0.74, "y": 0.48},
                ],
                "ocr_quality": {"quality": "high", "raw_block_count": 12, "chat_block_count": 2},
            },
        }
        payload = server.build_floating_active_payload(context)
        self.assertIn("Mars: minimax", payload["subtitle"])
        self.assertNotIn("公众号", payload["subtitle"])
        self.assertEqual(payload["confidence"], "high")

    def test_visible_liepin_bridge_wins_when_desktop_reports_chrome_frontmost(self) -> None:
        now = datetime.now()
        contexts = {
            "liepin:tab-1": {
                "surface": "liepin",
                "updated_at": now.isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": False,
                "page_type": "resume_detail",
                "candidate": {"name": "许尧"},
            },
            "native": {
                "surface": "native",
                "updated_at": now.isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
                "window": {"title": "NO.eb622285dcE1d7fb19c0320"},
            },
            "native:wechat": {
                "surface": "native",
                "updated_at": (now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "wechat": {"visible_text_clean": "旧微信对话", "text_blocks": ["旧微信对话"]},
            },
        }

        selected = server.select_floating_active_context(contexts, now)

        self.assertEqual(selected["surface"], "liepin")
        self.assertEqual(selected["candidate"]["name"], "许尧")

    def test_newer_wechat_context_wins_after_user_really_switches_to_wechat(self) -> None:
        now = datetime.now()
        contexts = {
            "liepin:tab-1": {
                "surface": "liepin",
                "updated_at": now.isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": False,
                "candidate": {"name": "许尧"},
            },
            "native": {
                "surface": "native",
                "updated_at": (now - timedelta(seconds=10)).isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
            },
            "native:wechat": {
                "surface": "native",
                "updated_at": now.isoformat(timespec="seconds"),
                "trigger": "activation",
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "wechat": {"visible_text_clean": "当前微信对话", "text_blocks": ["当前微信对话"]},
            },
        }

        selected = server.select_floating_active_context(contexts, now)

        self.assertTrue(server.native_context_has_wechat_text(selected))

    def test_explicit_agent_job_context_wins_over_passive_wechat_refresh(self) -> None:
        now = datetime.now()
        contexts = {
            "a_system": {
                "surface": "a_system",
                "updated_at": now.isoformat(timespec="seconds"),
                "trigger": "selection",
                "explicit": True,
                "user_selected": True,
                "page_visible": True,
                "page_focused": True,
                "context": {"type": "job", "id": 154, "label": "技术市场经理/总监（PC电源）"},
            },
            "native:wechat": {
                "surface": "native",
                "updated_at": now.isoformat(timespec="seconds"),
                "trigger": "timer",
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "wechat": {"visible_text_clean": "后台微信对话", "text_blocks": ["后台微信对话"]},
            },
        }

        selected = server.select_floating_active_context(contexts, now)

        self.assertEqual(selected["surface"], "a_system")
        self.assertEqual(selected["context"]["id"], 154)
        # 2026-07-22 修复：a_system explicit 保鲜期从 900 秒降为 120 秒，
        # 防止过期点击长期压住新鲜浏览器页面；选举契约（explicit 胜过被动微信刷新）不变。
        self.assertEqual(server.floating_context_stale_after(contexts["a_system"]), 120)

    def test_stale_agent_context_does_not_emit_wechat_ocr_warning(self) -> None:
        now = datetime.now()
        context = {
            "surface": "a_system",
            "updated_at": (now - timedelta(seconds=901)).isoformat(timespec="seconds"),
            "explicit": True,
            "user_selected": True,
            "stale": True,
            "context": {"type": "job", "id": 154, "label": "技术市场经理/总监（PC电源）"},
        }

        diagnostics = server.floating_context_diagnostics(context, {"a_system": context}, {}, {}, now)

        self.assertTrue(any(item["code"] == "context_stale" for item in diagnostics))
        self.assertFalse(any(item["code"] == "ocr_low_confidence" for item in diagnostics))

    def test_background_wechat_timer_cannot_steal_visible_liepin_tab(self) -> None:
        now = datetime.now()
        contexts = {
            "liepin:tab-1": {
                "surface": "liepin",
                "updated_at": now.isoformat(timespec="seconds"),
                "page_visible": True,
                "page_focused": False,
                "candidate": {"name": "许尧"},
            },
            "native": {
                "surface": "native",
                "updated_at": (now - timedelta(seconds=12)).isoformat(timespec="seconds"),
                "trigger": "activation",
                "frontmost_app": {"name": "Google Chrome", "bundle_id": "com.google.Chrome"},
            },
            "native:wechat": {
                "surface": "native",
                "updated_at": now.isoformat(timespec="seconds"),
                "trigger": "timer",
                "page_visible": True,
                "page_focused": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "wechat": {"visible_text_clean": "添加朋友", "text_blocks": ["添加朋友"]},
            },
        }

        selected = server.select_floating_active_context(contexts, now)

        self.assertEqual(selected["surface"], "liepin")

    def test_activation_trigger_survives_same_app_timer_refresh(self) -> None:
        now = datetime.now()
        current = {
            "surface": "native",
            "updated_at": now.isoformat(timespec="seconds"),
            "trigger": "activation",
            "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
            "wechat": {"visible_text_clean": "当前微信对话"},
        }
        incoming = {
            "surface": "native",
            "trigger": "timer",
            "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
            "wechat": {"visible_text_clean": "当前微信对话"},
        }

        self.assertEqual(server.preserve_native_invocation_trigger(current, incoming, now), "activation")

    def test_floating_state_exposes_context_quality_diagnostics_and_recent_contexts(self) -> None:
        with server.ASA_FLOATING_LOCK:
            server.ASA_FLOATING_CONTEXTS.clear()
            server.ASA_FLOATING_COMMANDS.clear()
            server.ASA_FLOATING_COMMAND_HISTORY.clear()
            server.ASA_FLOATING_COMMAND_RESULTS.clear()

        server.update_floating_context(
            {
                "surface": "native",
                "instance_id": "mac",
                "trigger": "hotkey",
                "page_focused": True,
                "page_visible": True,
                "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
                "window": {"title": "微信"},
                "wechat": {
                    "capture_mode": "vision_ocr",
                    "combined_text": "候选人推荐报告.pdf\nLeo [图片]",
                    "visible_text_clean": "候选人推荐报告.pdf\nLeo [图片]",
                    "text_blocks": ["候选人推荐报告.pdf", "Leo [图片]"],
                    "ocr_quality": {"quality": "low", "raw_block_count": 4, "chat_block_count": 1},
                    "accessibility_authorized": False,
                    "screen_capture_authorized": True,
                },
            }
        )

        class FakeRuntimeService:
            def __init__(self, db_path: Path) -> None:
                self.db_path = db_path

            def get_runtime_timeline(self, limit: int = 8) -> dict:
                return {"context_snapshots": [], "tool_calls": [], "permission_audit": []}

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "agent.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE agent_goals(goal_id TEXT,objective TEXT,status TEXT,priority INTEGER,created_at TEXT,updated_at TEXT);
                CREATE TABLE agent_workflows(workflow_id TEXT,goal_id TEXT,status TEXT);
                CREATE TABLE agent_approvals(id INTEGER PRIMARY KEY,approval_id TEXT,goal_id TEXT,workflow_id TEXT,step_id INTEGER,action_type TEXT,risk_level TEXT,title TEXT,preflight_json TEXT,status TEXT,created_at TEXT,expires_at TEXT);
                CREATE TABLE agent_artifacts(id INTEGER PRIMARY KEY,artifact_id TEXT,goal_id TEXT,workflow_id TEXT,artifact_type TEXT,title TEXT,mime_type TEXT,validation_status TEXT,created_at TEXT);
                INSERT INTO agent_goals VALUES ('goal_1','处理今日回复','waiting_approval',1,'2026-07-16','2026-07-16');
                INSERT INTO agent_workflows VALUES ('workflow_1','goal_1','waiting_approval');
                INSERT INTO agent_approvals VALUES (1,'approval_1','goal_1','workflow_1',1,'outreach_execute','R3','发送触达','{"object_label":"张航 / 机械高级工程师"}','pending','2026-07-16','2026-07-16 12:30:00');
                INSERT INTO agent_artifacts VALUES (1,'artifact_1','goal_1','workflow_1','recommendation_report','推荐报告','application/vnd.openxmlformats-officedocument.wordprocessingml.document','passed','2026-07-16');
                """
            )
            conn.commit()
            conn.close()
            state = type("State", (), {"agent_service": FakeRuntimeService(db_path)})()
            payload = server.build_floating_state(state)

        self.assertIn("context_quality", payload)
        self.assertIn("diagnostics", payload)
        self.assertIn("recent_contexts", payload)
        self.assertEqual(payload["context_quality"]["source_label"], "微信")
        self.assertEqual(payload["context_quality"]["attachment_status"]["visible_filenames"], ["候选人推荐报告.pdf"])
        self.assertEqual(payload["context_quality"]["image_status"]["status"], "confirmation_required")
        self.assertTrue(any(item["code"] == "ocr_low_confidence" for item in payload["diagnostics"]))
        self.assertTrue(any(item["code"] == "permission_accessibility_authorized" for item in payload["diagnostics"]))
        self.assertTrue(any(item["code"] == "pending_approvals" for item in payload["diagnostics"]))
        self.assertEqual(payload["active_context"]["type"], "wechat")
        self.assertEqual(payload["active_goals"][0]["workflow_id"], "workflow_1")
        self.assertEqual(payload["active_goals"][0]["workflow_status"], "waiting_approval")
        self.assertEqual(payload["pending_approvals"][0]["approval_id"], "approval_1")

    def test_open_workflow_floating_action_opens_workflow_read_only(self) -> None:
        class FakeWorkflowService:
            db_path = Path("/tmp/asa-floating-test-missing-agent.db")

            def __init__(self) -> None:
                self.summary_calls = []

            def get_runtime_timeline(self, limit: int = 8) -> dict:
                return {"context_snapshots": [], "tool_calls": [], "permission_audit": []}

            def get_workflow_summary(self, workflow_id: str) -> dict:
                self.summary_calls.append(workflow_id)
                return {"ok": True, "workflow_id": workflow_id, "status": "planned"}

        with server.ASA_FLOATING_LOCK:
            server.ASA_FLOATING_CONTEXTS.clear()
            server.ASA_FLOATING_COMMANDS.clear()
            server.ASA_FLOATING_COMMAND_HISTORY.clear()
            server.ASA_FLOATING_COMMAND_RESULTS.clear()

        service = FakeWorkflowService()
        state = type("State", (), {"agent_service": service})()
        result = server.route_floating_action(state, {"action": "open_workflow", "workflow_id": "workflow_test123"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "local")
        self.assertEqual(result["open_url"], "/asa-app#workflow=workflow_test123")
        self.assertEqual(result["workflow_summary"]["workflow_id"], "workflow_test123")
        self.assertEqual(service.summary_calls, ["workflow_test123"])

    def test_visible_wechat_attachment_action_opens_exact_path_and_retries(self) -> None:
        context = {
            "surface": "native",
            "wechat": {"text_blocks": ["陈明习 20260609B.docx"]},
        }
        state = type("State", (), {"agent_service": object()})()
        attachment = Path("/tmp/陈明习 20260609B.docx")

        with patch.object(server, "build_floating_state", return_value={"active_context_raw": context}), \
             patch.object(server, "visible_wechat_attachment_path", return_value=attachment) as resolve_path, \
             patch.object(server.subprocess, "Popen") as popen:
            result = server.route_floating_action(
                state,
                {"action": "open_wechat_attachment::陈明习 20260609B.docx"},
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["retry_previous"])
        resolve_path.assert_called_once_with(context, "陈明习 20260609B.docx")
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/open", str(attachment)])

    def test_stale_wechat_context_quality_warns_without_raw_private_text(self) -> None:
        now = datetime.now()
        context = {
            "surface": "native",
            "updated_at": "2000-01-01T00:00:00",
            "frontmost_app": {"name": "微信", "bundle_id": "com.tencent.xinWeChat"},
            "window": {"title": "微信"},
            "wechat": {
                "combined_text": "电话 13800138000\n候选人简历.docx",
                "text_blocks": ["电话 13800138000", "候选人简历.docx"],
                "ocr_quality": {"quality": "high"},
            },
        }
        quality = server.floating_context_quality(context, now)
        diagnostics = server.floating_context_diagnostics(context, {"native:wechat": context}, {}, {}, now)
        recent = server.floating_recent_contexts({"native:wechat": context}, now)
        self.assertEqual(quality["quality"], "stale")
        self.assertEqual(quality["attachment_status"]["visible_filenames"], ["候选人简历.docx"])
        self.assertTrue(any(item["code"] == "context_stale" for item in diagnostics))
        self.assertNotIn("13800138000", recent[0]["subtitle"])


if __name__ == "__main__":
    unittest.main()
