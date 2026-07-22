# ASA Agent + Copilot

macOS 系统级 ASA 应用。

- `ASA Agent` 主窗口加载 App 专用本机路由 `http://127.0.0.1:8765/asa-app`。
- `ASA Copilot` 使用 AppKit `NSPanel` 常驻置顶，加载 `http://127.0.0.1:8765/asa-floating`。
- 菜单栏显示 `ASA`，支持分别打开 Agent、Copilot，以及将 Copilot 收起为圆点。
- Copilot 展开态可拖动顶部区域，收起态可按住 `ASA` 圆点拖动。

构建：

```bash
bash asa-floating-app/scripts/build.sh
open "asa-floating-app/build/ASA.app"
```
