# ASA Agent

macOS 系统级 ASA 应用。

- `ASA Agent` 主窗口加载 App 专用本机路由 `http://127.0.0.1:8765/asa-app`。
- 菜单栏显示 `ASA`，用于重新打开 Agent 和检查本机服务。
- 全局快捷键 `Option+Space` 显示 Agent；备用组合为 `Command+Shift+A` 和 `Control+Option+A`。
- 旧 `ASA Copilot` 面板保留一个版本作为回滚实现，仅在从命令行显式传入 `--compat-copilot` 时初始化，正常启动无入口也不创建窗口。

构建：

```bash
bash asa-floating-app/scripts/build.sh
open "$HOME/Applications/ASA.app"
```

构建脚本会先在 `build/` 中完成编译和签名验证，再替换用户应用目录中的唯一安装副本；`build/` 不保留可被系统识别的 `.app`。

测试：

```bash
bash asa-floating-app/scripts/test.sh
```

编译并运行确定性边界测试（Web 安全策略、快捷键路由、Core 恢复调度、诊断页转义），随后对全部源码做完整 typecheck。
