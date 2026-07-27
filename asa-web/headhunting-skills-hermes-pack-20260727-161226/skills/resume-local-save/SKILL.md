---
name: resume-local-save
description: 猎聘简历一键保存到本地——CDP 注入按钮，点击即存 HTML 到桌面。
version: 1.0.0
category: productivity
---

# 简历本地存档

在猎聘简历详情页注入「💾 存本地」浮动按钮，点击后抓取完整页面 HTML 保存到 `~/Desktop/客户项目/`。

## 触发条件

- "简历存本地"、"保存简历"、"存到本地"
- 用户表示想脱离公司数据库独立保存简历
- 与 `liepin-cdp-search` 配合使用：搜到候选人 → 打开详情页 → 点按钮保存

## 工作原理

CDP `Runtime.evaluate` 注入 JS → 创建浮动按钮 → 点击时 `document.documentElement.outerHTML` → Python 守护轮询 `window.__rs_pending` → 保存文件。

## 部署步骤

### Step 1: 创建守护脚本

写入 `~/.hermes/scripts/resume_saver_daemon.py`（完整代码见 `scripts/resume_saver_daemon.py`）。

核心逻辑：
- 轮询检测猎聘标签页（`/json/list`）
- 注入按钮 JS（`Runtime.evaluate`）
- 检测 `window.__rs_pending` 待保存数据
- 保存 HTML 到 `~/Desktop/客户项目/resume_{姓名}_{时间}.html`
- 提取姓名存入 `talent_pool.db`

### Step 2: 注册 LaunchAgent

```bash
cat > ~/Library/LaunchAgents/ai.hermes.resume-saver.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hermes.resume-saver</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Library/Frameworks/Python.framework/Versions/3.11/Resources/Python.app/Contents/MacOS/Python</string>
        <string>/Users/USER/.hermes/scripts/resume_saver_daemon.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/tmp/resume_saver.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/ai.hermes.resume-saver.plist
```

### Step 3: 前置条件

- Chrome CDP 运行中（`curl http://127.0.0.1:9222/json/version`）
- 已在 Chrome 中登录猎聘

## 使用方式

1. 打开任意猎聘简历详情页
2. 等待 2-5 秒，右下角出现蓝色「💾 存本地」按钮
3. 点击按钮 → 按钮变黄「⏳」→ 变绿「✅」
4. 文件保存到 `~/Desktop/客户项目/resume_{姓名}_{时间}.html`

## Pitfalls

1. **CDP 标签页冲突** — 如果 `liepin-cdp-search` 正在使用 CDP 搜索，守护无法同时连接同一标签页。搜索结束后守护自动接管。
2. **猎聘账号限制** — 基础版账号可能看不到完整简历内容（页面显示"简历异常"），但按钮注入和 HTML 保存不受此限制。
3. **按钮延迟** — 猎聘是 SPA，URL 变化后按钮需 2-5 秒重新注入。
