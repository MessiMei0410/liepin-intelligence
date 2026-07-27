---
name: resume-export
description: 猎聘简历一键导出为 .docx — CDP 注入按钮 + 结构化解析 + 专业排版 + LaunchAgent 自动守护
version: 1.0.0
category: productivity
---

# 猎聘简历 .docx 导出

在猎聘简历详情页自动注入「📄 导出docx」按钮，点击采集页面数据，CDP 回传后生成专业排版 .docx 存入 `~/Desktop/简历存档/`。

## 触发条件

- "导出简历"、"导出docx"、"存成word"
- 用户在猎聘简历页点「📄 导出docx」按钮后说"生成"
- 首次使用需注入按钮

## 架构

```
猎聘简历页 → [注入按钮] → 用户点击 → 采集 body.innerText
                                            ↓
                              CDP Runtime.evaluate 读取 window.__he
                                            ↓
                              generate_resume_docx.py 解析 + 排版
                                            ↓
                              ~/Desktop/简历存档/resume_{姓名}_{时间}.docx
```

## Step 1：注入按钮

使用 CDP 向猎聘标签页注入按钮脚本。按钮在页面右下角，渐变色圆角样式，hover 有缩放效果。

```bash
CDP="$HOME/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py"
WS=$(curl -s http://127.0.0.1:9222/json/list | python3 -c "import sys,json;t=json.load(sys.stdin);print([x['webSocketDebuggerUrl'] for x in t if 'liepin.com/resume/showresumedetail' in x.get('url','')][0])")

# 注入按钮
python3 "$CDP" "$WS" "Runtime.evaluate" "{\"expression\": $(python3 -c "import json;print(json.dumps(open('/tmp/hermes_btn.js').read()))"), \"returnByValue\": true}"
```

按钮脚本模板见 `scripts/inject_button.js`。

## Step 2：用户点击后生成 .docx

```bash
# 读取采集数据
python3 "$CDP" "$WS" "Runtime.evaluate" '{"expression":"window.__he","returnByValue":true}' > /tmp/rs_data.json

# 生成 docx
python3 ~/.hermes/scripts/generate_resume_docx.py
```

## Step 3：LaunchAgent 自动守护

脚本：`~/.hermes/scripts/resume_saver_daemon.py`
plist：`~/Library/LaunchAgents/ai.hermes.resume-saver.plist`

守护进程每 8-12 秒扫描一次猎聘标签页，自动注入按钮。使用成熟的 `cdp_client.py` 做 CDP 通信（不要自己写 WebSocket）。

```xml
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>RunAtLoad</key><true/>
```

日志：`/tmp/resume_saver.log`

## 解析逻辑

`generate_resume_docx.py` 中的 `parse()` 函数针对猎聘简历页文本格式：

- **姓名**：匹配「今天活跃」后的名字或「男/女」前的名字
- **基本信息**：正则提取年龄、城市、学历、工作年限
- **求职意向**：「求职意向」到「工作经历」之间的内容
- **工作经历**：按「公司名\n（时间）」分块，提取职位、职责描述
- **教育经历**：学校·专业·学位·时间段

## 排版规范

- 字体：微软雅黑
- 标题：22pt 深蓝 (#1a478a) 加粗
- 章节标题：14pt 深蓝 + 底部分割线
- 正文：10.5pt，公司名 12pt 加粗
- 职责描述：9.5pt 灰色 + 缩进 + 圆点列表
- 页边距：上下 2cm，左右 2.5cm

## 防封策略

猎聘会检测异常自动化行为。守护已内置：
- 扫描间隔 8-12 秒随机
- 注入前随机延迟 1-3 秒
- 不修改页面 DOM 结构（仅添加一个 div）
- 不使用自动化点击，由用户手动点击按钮

## Pitfalls

- 按钮注入仅在当前标签页生效。新开标签页需守护重新扫描注入（~10秒内自动完成）
- `window.__he` 数据在用户点击按钮后 2.5 秒自动清除（按钮恢复初始状态）
- 解析依赖猎聘页面文本格式，如果猎聘改版需要更新正则
- 工作经历中同公司的不同项目可能被误判为独立公司——需人工复核
- python-docx 需要预装：`pip install python-docx`
- 守护使用 cdp_client.py 做 CDP 通信，不要自己实现 WebSocket 连接
