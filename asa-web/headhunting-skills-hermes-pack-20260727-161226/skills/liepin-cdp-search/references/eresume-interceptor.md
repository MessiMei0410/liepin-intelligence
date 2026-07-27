# E简历插件拦截器

配合 E简历 Chrome 插件（`bffcfnaphpneefffmnfmmhpaefoboabh`）使用的本地简历拦截器。

## 工作原理

E简历插件点「一键导入」时将简历 HTML 发送到公司数据库（如 `http://headhunt.x-saas.com.cn/handler.aspx`）。拦截器通过 CDP 在猎聘简历页注入 JS 钩子，截取 `chrome.runtime.sendMessage` 中的简历数据，**同时存入本地**而不影响正常流程。

## 拦截器脚本

`~/.hermes/scripts/resume_interceptor.py` — v3 版本采用轮询检测方案：
- 每 2 秒检查 Chrome CDP 是否有猎聘简历详情页 (`/resume/showresumedetail/`) 
- 检测到后通过 `Runtime.evaluate` 提取页面结构化数据
- 保存文本到 `~/Desktop/客户项目/resume_{姓名}_{时间}.txt`
- 存入 SQLite 人才库

## LaunchAgent

```xml
<!-- ~/Library/LaunchAgents/ai.hermes.resume-interceptor.plist -->
<!-- RunAtLoad + KeepAlive，与 Chrome CDP 协同 -->
```

## 使用方法

1. Chrome CDP 已启动（`ai.hermes.chrome-cdp` LaunchAgent）
2. 拦截器已启动（`ai.hermes.resume-interceptor` LaunchAgent）
3. 在猎聘简历页点击 E简历「一键导入」
4. 简历同步进公司系统 + 本地 `~/Desktop/客户项目/`

## 注意事项

- 拦截器 v1-v2 尝试过 `Fetch.enable`（浏览器级 WS 不支持）和 `chrome.runtime.sendMessage` 钩子（需扩展 content script 已注入），均不够稳健
- v3 的轮询 DOM 方案最简单可靠，但需要简历页加载完成后再捕获
- Chrome 重启后猎聘登录态会丢失，需手动重新登录
