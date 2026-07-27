# Chrome CDP 守护（macOS LaunchAgent）

Chrome 长时间运行容易崩溃，需要用 launchd 守护自动重启。

## 创建 LaunchAgent

```bash
cat > ~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.hermes.chrome-cdp</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Applications/Google Chrome.app/Contents/MacOS/Google Chrome</string>
        <string>--remote-debugging-port=9222</string>
        <string>--user-data-dir=/Users/USER/.hermes/chrome_profile_xhs</string>
        <string>--no-first-run</string>
        <string>--no-default-browser-check</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
EOF
```

## 注册

```bash
launchctl load ~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist
launchctl list ai.hermes.chrome-cdp  # 验证
```

## 验证 CDP

```bash
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json;print(json.load(sys.stdin)['Browser'])"
# 输出: Chrome/149.0.7827.54
```

## CDP 标签页连接冲突

Chrome 每个标签页只允许一个调试器连接。同时有两个进程（搜索+守护）连同一标签页会互相阻塞。

**规避**: 搜索结束后断开连接；或守护优先连接非搜索页的标签页。
