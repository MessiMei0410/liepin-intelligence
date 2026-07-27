# 猎头工作站 App 集成模式

将候选人匹配分析功能嵌入本地 macOS 桌面应用的标准模式。

## 架构

```
工作站 App（Swift/Cocoa / HTML）
    │  POST http://127.0.0.1:18901/match
    │  GET  /result?candidate=... (轮询直到报告生成)
    ▼
Python HTTP Server (matching_server.py)
    │  写入请求 JSON
    ▼
~/.hermes/matching_queue/
    │  cron job 每 1 分钟扫描
    ▼
Hermes Agent + candidate-matching-report skill
    │  生成 .docx，删除请求文件
    ▼
~/Desktop/人选匹配_{name}_{company}_{position}.docx
```

## 部署清单

### 1. Python HTTP 服务
- 脚本: `~/.hermes/scripts/matching_server.py`
- 端口: `18901`
- LaunchAgent: `~/Library/LaunchAgents/ai.hermes.matching-server.plist`
- 队列目录: `~/.hermes/matching_queue/`
- 启动: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.matching-server.plist`

**API 端点**：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/match` | 提交匹配请求。Body: `{candidate, company, position, resume_path, jd_text}`。服务器阻塞等待最多 60 秒，报告就绪则返回 `{success, output}`，否则返回 `{success, processing: true}` |
| GET | `/status` | 查询队列状态 + 最近完成报告。返回 `{status, queue, pending[], completed[{name, path, mtime}]}` |
| GET | `/result?candidate=X&company=X&position=X` | 查询指定候选人的报告是否已生成。返回 `{done: true, path}` 或 `{done: false}` |

**前端轮询模式**：POST `/match` 返回 `processing: true` 后，启动 Timer 每 3 秒 GET `/result`，实时显示等待秒数，最多 90 秒超时。轮询到结果后激活「打开报告」按钮。

### 2. Cron 后台任务
- 名称: `候选人匹配报告生成器`
- 间隔: `every 1m`
- 载入技能: `candidate-matching-report`
- 功能: 扫描队列目录 → 提取简历 → AI 分析 → 生成报告 → 删除请求文件

### 3. 前端集成

#### Swift/Cocoa 原生 App
参考 `pnx_app/src/MatchingAnalysisViewController.swift`：
- 新建 ViewController 类，用 `NSTabViewItem` 挂到 `NSTabViewController`
- 表单字段: 候选人姓名、公司、岗位、简历路径（NSOpenPanel 选择）、JD（NSTextView）
- 提交: `URLSession.shared.dataTask` POST 到 `http://127.0.0.1:18901/match`，timeout 70s
- 轮询: 若返回 `processing: true`，启动 `Timer.scheduledTimer` 每 3 秒 GET `/result?candidate=...`，实时显示等待秒数，最多 90s 超时。轮询到结果后激活「打开报告」按钮，并发送 `Notification.Name("MatchingReportCompleted")` 通知 AppDelegate 刷新候选人列表
- 工具箱: `toolbarLabels` 数组追加 `"📊 匹配分析"`，`toolbarTabIdentifiers` 追加 `"tab3"`
- Build: `swiftc` 参数追加 `"$SRC_DIR/MatchingAnalysisViewController.swift"`

#### HTML 单页应用
- 顶部导航: `<nav class="top-nav">` + `<button class="nav-tab" data-tab="matching">`
- Tab 切换: `document.querySelectorAll('.nav-tab')` 监听 click，切换 `.tab-content.active`
- 表单: 输入框 + 文件选择（`<input type="file" accept=".docx">` → 注意 WebView 可能不提供完整路径，需提示用户手动粘贴）
- 提交: `fetch('http://127.0.0.1:18901/match', { method: 'POST', body: JSON.stringify(...) })`
- 轮询: 每 2 秒 GET `/status`，队列归零表示处理完成

## 关键陷阱

### 不要改 HTML 却不改源文件
macOS `.app` 的 `Contents/Resources/report.html` 可能是 Swift 源码编译时从别处 copy 的静态资源。如果修改后运行 `build.sh`，改动会被覆盖。**先检查 Contents/MacOS/ 下有无原生二进制**（如 `pnx_search`），有则找到源码目录（如 `pnx_app/src/`），改 Swift 源码再重新编译。

### macOS LaunchAgent 权限
LaunchAgent 运行的 Python 脚本可能无法访问 `~/Desktop/`（sandbox 限制）。将队列目录放在 `~/.hermes/matching_queue/` 而非桌面目录。

### 文件路径输入
WebView 中的 `<input type="file">` 出于安全限制不返回完整路径。对于 HTML 前端，提供文本输入框让用户手动粘贴路径；对于 Swift 原生 App，用 `NSOpenPanel` 获取完整路径。
