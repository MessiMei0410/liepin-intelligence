# Chrome CDP LaunchAgent 配置

Chrome CDP 在 macOS 上频繁崩溃或退出，导致猎聘搜索中断。解决方案：用 LaunchAgent 守护，崩溃自动重启。

## plist 模板

保存为 `~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist`：

```xml
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
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
```

**注意**：`KeepAlive > SuccessfulExit = false` 意味着正常退出（exit 0）不重启，异常退出（crash/SIGTERM）自动重启。Chrome 偶尔被 macOS kill，这个配置让它 5 秒后自动回升。

## 安装

```bash
cp chrome-cdp.plist ~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist
launchctl load ~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist
```

## 验证

```bash
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json;print(json.load(sys.stdin)['Browser'])"
```
