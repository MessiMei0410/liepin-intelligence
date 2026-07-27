---
name: resume-docx-export
description: 猎聘简历→docx一键导出：CDP注入按钮，自动采集页面文本，生成结构化.docx到桌面。
version: 1.0.0
category: productivity
---

# 猎聘简历 docx 导出

猎聘简历详情页右下角注入「📄 导出docx」按钮，点击采集页面文本，Python 解析为结构化数据并生成 .docx 文件。

## 触发条件

- "导出简历"、"生成docx"、"存简历"
- 用户在猎聘简历详情页点了按钮
- 按钮自动注入失败时手动触发

## 架构

```
CDP 注入按钮 → 用户点击 → 采集 body.innerText → window.__he
                                                    ↓
                            守护进程轮询检测 → 读取数据
                                                    ↓
                            generate_resume_docx.py → 解析 → .docx
                                                    ↓
                                          ~/Desktop/简历存档/
```

## 组件

| 组件 | 路径 | 作用 |
|------|------|------|
| 守护进程 | `~/.hermes/scripts/resume_saver_daemon.py` | 每8-12秒扫描猎聘标签页，注入按钮，检测数据，触发生成 |
| docx生成器 | `~/.hermes/scripts/generate_resume_docx.py` | 解析页面文本，生成结构化 .docx |
| 按钮脚本 | `/tmp/hermes_btn.js` | 守护自动写入，注入到页面 |
| LaunchAgent | `~/Library/LaunchAgents/ai.hermes.resume-saver.plist` | 开机自启守护 |
| Chrome守护 | `~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist` | Chrome CDP 自启+自动重启 |
| cdp_client | `~/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py` | 稳定 CDP 通信（不要自己写 WebSocket 连接） |

## 工作流

### 自动模式（推荐）

守护进程运行中时，打开猎聘简历详情页 → 8-12秒内按钮自动出现 → 点击 → 自动生成 docx。

### 手动模式（兜底）

```bash
CDP="$HOME/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py"
WS=$(curl -s http://127.0.0.1:9222/json/list | python3 -c "import sys,json;t=json.load(sys.stdin);print([x['webSocketDebuggerUrl'] for x in t if 'resume/showresumedetail' in x.get('url','')][0])")

# 注入按钮
python3 "$CDP" "$WS" "Runtime.evaluate" '{"expression":"(function(){...})()", "returnByValue":true}'

# 用户点击后读取数据
python3 "$CDP" "$WS" "Runtime.evaluate" '{"expression":"window.__he", "returnByValue":true}' > /tmp/rs_data.json

# 生成 docx
python3 ~/.hermes/scripts/generate_resume_docx.py
```

## 输出格式

.docx 文件包含：
- 📋 基本信息（姓名/年龄/城市/学历/年限/求职意向）
- 💼 工作经历（公司/职位/时间/职责）
- 🎓 教育经历（学校/专业/学位/时间）

保存路径：`~/Desktop/简历存档/resume_{姓名}_{时间戳}.docx`

## 反封策略

- 守护扫描间隔 8-12 秒（含随机1-3秒抖动）
- 注入前随机延迟 1-3 秒
- 页面内容 < 500 字符跳过生成（受限页面不浪费）
- 单次 CDP 操作超时 12 秒

## Pitfalls

1. **CDP 连接要用成熟的 cdp_client.py** — 不要自己写 WebSocket 连接。守护 v1/v2 自己实现 CDP 连接导致超时，v3 改用 subprocess 调用 cdp_client.py 后稳定。
2. **猎聘账号限制** — 基础版/免费账号查看简历详情页显示"简历异常"（NO.xxx），`body.innerText` 几乎为空。按钮会采集但生成的 docx 是空的。确认页面有完整内容再导出。
3. **按钮只注入简历详情页** — 只在 URL 含 `resume/showresumedetail` 的页面注入。搜索结果页、首页不注入。
4. **按钮持久化** — `Runtime.evaluate` 只在当前页面生效，换页后消失。守护会重新注入。`Page.addScriptToEvaluateOnNewDocument` 可在同标签页内持久化但不跨标签页。
5. **LaunchAgent 日志** — Python 的 `print` 在 launchd 下被缓冲。使用 `open(LOG,"a").write()` 直接写文件，或加 `-u` 参数。
6. **生成器解析依赖页面格式** — 猎聘简历页文本格式变化会导致解析失败。解析器按实际格式匹配（公司名\n（时间）\n行业\n职位\n职责业绩：\n描述），不同猎聘版本可能不同。
