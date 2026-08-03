# ASA Agent

macOS 系统级 ASA 应用。

- `ASA Agent` 主窗口加载 App 专用本机路由 `http://127.0.0.1:8765/asa-app`。
- 菜单栏显示 `ASA`，用于重新打开 Agent 和检查本机服务。
- 旧 `ASA Copilot` 面板保留一个版本作为回滚实现，仅在从命令行显式传入 `--compat-copilot` 时初始化，正常启动无入口也不创建窗口。

构建：

```bash
bash asa-floating-app/scripts/build.sh
open "asa-floating-app/build/ASA.app"
```
