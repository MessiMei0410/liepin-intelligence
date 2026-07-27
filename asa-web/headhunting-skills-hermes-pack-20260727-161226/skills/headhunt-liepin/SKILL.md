---
name: headhunt-liepin
description: "Use when executing headhunting search workflows on 猎聘 (Liepin) — search candidates via Chrome CDP automation, extract resume HTML/JS, generate Excel, and send."
version: 2.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [headhunting, recruitment, liepin, chrome-cdp, safari]
    related_skills: [macos-computer-use]
---

# 猎聘寻访工作流 (macOS Chrome CDP 版)

**首选：Chrome + CDP (Chrome DevTools Protocol)** — 完全程序化控制，支持导航、JS执行、HTML捕获、截图。
**备选：Safari + osascript** — 仅当 Chrome 不可用时使用，交互可靠性差。

## Overview

```
加载寻访策略 → 启动Chrome(CDP) → 多轮关键词搜索 → JS提取/截图候选人 → 生成Excel → 发送
```

## When to Use

- 策略文档已生成（来自 `headhunting-search-strategy`），需要开始在猎聘上搜索
- 需要批量处理岗位寻访任务
- 需要生成标准化候选人 Excel 报告

## 环境要求

### Chrome + CDP（首选）

macOS + Google Chrome。CDP 端口可任意选择（9222/9223），与其他 Chrome CDP 应用（如小红书）可共享同一 Chrome 实例。

启动 Chrome 并开启远程调试：

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9223 \
  --user-data-dir=~/.hermes/chrome_profile_xhs \
  --no-first-run \
  --no-default-browser-check &
```

验证 CDP 连通：
```bash
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json; print(json.load(sys.stdin)['Browser'])"
# Chrome/148.0...
```

CDP 客户端脚本：`/tmp/cdp_client.py`（标准库 WebSocket 实现，无需 pip 安装依赖）。

核心能力：
- `Page.navigate` — 导航到任意 URL
- `Runtime.evaluate` — 执行 JavaScript 提取页面数据
- `Page.captureScreenshot` — 截图
- `DOM.getDocument` + `DOM.querySelector` — DOM 操作

### Safari + osascript（备选，仅 Chrome 不可用时）

⚠️ Safari 自动化有严重可靠性问题：
- Cmd+V + Return 粘贴搜索词经常不触发搜索
- `getConditionItem` 页面结构可能已变化
- `AllowJavaScriptFromAppleEvents` 在 macOS 12.7 上不生效
- 直接 URL (`?key=关键词`) 返回 404

仅当 Chrome 不可用时才用此方案。

## 搜索流程（Chrome + CDP）

### Step 0: 启动 Chrome + 打开猎聘

```bash
# 1. 启动 Chrome（如果还没跑）
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome_hermes_profile &

# 2. 创建新标签页并导航到猎聘
curl -s -X PUT "http://127.0.0.1:9222/json/new?url=https://h.liepin.com" > /dev/null

# 3. 获取 WS URL
WS=$(curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys,json
tabs=[t for t in json.load(sys.stdin) if 'liepin.com' in t.get('url','')]
print(tabs[0]['webSocketDebuggerUrl'] if tabs else '')
")

# 4. 检测登录状态
python3 /tmp/cdp_client.py "$WS" "Runtime.evaluate" \
  '{"expression":"location.href","returnByValue":true}'
```

如果 URL 含 `account/login` → 暂停，让用户手动登录后再继续。

### Step 1: DOM 结构化提取（替代正则）

搜索后用 DOM 选择器提取，不靠正则解析文本：

```javascript
// 姓名
card.querySelector('.new-resume-personal-name em').textContent

// 文本节点成对: [描述, 日期段]
// 工作: "公司 · 职位" + "YYYY.MM-YYYY.MM(时长)"
// 学历: "学校 · 专业 · 学位 · 统招/非统招" + "YYYY.MM-YYYY.MM(时长)"
card.querySelectorAll('*').forEach(el => {
  if (el.children.length === 0 && el.textContent.trim().length > 5) {
    nodes.push(el.textContent.trim());
  }
});
// 成对遍历: 判断 desc 含"统招"则归类教育，否则工作
```

旧版正则提取已废弃。

```bash
WS="ws://127.0.0.1:9222/devtools/page/XXXXX"

# 填入关键词并搜索（用 JS 直接操作 DOM）
python3 /tmp/cdp_client.py "$WS" "Runtime.evaluate" '{
  "expression": "
    (function(){
      const input = document.querySelector(\"input[placeholder*='搜索']\") || 
                    document.querySelector(\"input[type='text']\");
      if(input) {
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, 'value'
        ).set;
        nativeInputValueSetter.call(input, '光学产品经理');
        input.dispatchEvent(new Event('input', {bubbles: true}));
        input.form && input.form.submit();
        return 'submitted';
      }
      return 'no input found';
    })()
  ",
  "returnByValue": true
}'
```

⚠️ **为什么用原生 setter 而不是 `input.value = '...'`**：React/Vue 等框架劫持了 value setter，直接用 `input.value = 'xxx'` 不会触发框架的状态更新。必须用 `Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set` 触发原生行为。

### Step 2: 提取结构化候选人数据（DOM 文本节点成对解析）

猎聘搜索结果卡片 `.tlog-common-resume-card` 的文本节点成对出现：
```
公司 · 职位              ← 描述
2024.01-至今(2年5个月)     ← 日期

学校 · 专业 · 学位 · 类型  ← 描述
2016.09-2020.06(4年)      ← 日期
```

**成对提取 JS**：
```javascript
let nodes = [];
card.querySelectorAll('*').forEach(el => {
    if (el.children.length === 0) {
        let t = el.textContent.trim();
        if (t.length > 5 && t.length < 200 && /[\u4e00-\u9fff]/.test(t)) nodes.push(t);
    }
});
for (let i = 0; i < nodes.length - 1; i++) {
    let desc = nodes[i], ds = nodes[i+1];
    let dm = ds.match(/(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2}|至今)/);
    if (!dm) continue;
    if (desc.includes('统招') || desc.includes('非统招')) {
        // 教育：学校 · 专业 · 学位 · 类型
        let parts = desc.split('·').map(p => p.trim());
        if (parts.length >= 3) edu.push({school: parts[0], major: parts[1], degree: parts[2], type: parts[3]||'', start: dm[1], end: dm[2]});
    } else {
        // 工作：公司 · 职位
        let pos = desc.indexOf('·');
        work.push({company: pos>0 ? desc.substring(0,pos).trim() : desc, title: pos>0 ? desc.substring(pos+1).trim() : '', start: dm[1], end: dm[2]});
    }
}
```

提取字段：姓名(new-resume-personal-name em)、年龄、年限、学历、城市、期望城市、学历详情(学校+专业+学位+时间段)、工作经历(公司+职位+时间段)

**关键选择器**（2026-06-02 验证）:
- 搜索框: `#rc_select_1`
- 搜索按钮: `.search-btn`
- 结果数: `[data-nick="totalcnt"]`
- 候选人卡片: `.tlog-common-resume-card`（在 `.table-box > table.new-resume-card` 内）
- 学历筛选: `.sfilter-edu .tag-item`
- 城市筛选: `.sfilter-city .tag-item`

完整 DOM 参考见 `references/liepin-dom-selectors.md`。

### Step 3: 翻页 & 截图

```bash
# 截图
python3 /tmp/cdp_client.py "$WS" "Page.captureScreenshot" '{"format":"png"}' \
  | python3 -c "import sys,json,base64; d=json.load(sys.stdin); 
     open('/tmp/liepin_screen.png','wb').write(base64.b64decode(d['result']['data']))"

# 翻页（点击"下一页"）
python3 /tmp/cdp_client.py "$WS" "Runtime.evaluate" '{
  "expression": "
    (function(){
      const next = document.querySelector('[class*=next], .pagination .next, [title*=下一页]');
      if(next) { next.click(); return 'clicked'; }
      return 'no next';
    })()
  ",
  "returnByValue": true
}'
```

### Step 4: 简历详情页 — 获取直达链接

**关键发现**（2026-06-02）：猎聘简历详情页的 URL 格式为：

```
https://h.liepin.com/resume/showresumedetail/?res_id_encode={res_id}
```

#### 如何获取 res_id

每个搜索结果卡片中都有一个隐藏的 checkbox，其 `value` 属性即为 `res_id`：

```javascript
// 提取当前页所有候选人的 res_id 和链接
const cards = document.querySelectorAll(".tlog-common-resume-card");
const links = Array.from(cards).map((card, i) => {
  const cb = card.querySelector("input[type=checkbox]");
  const name = card.textContent.trim().replace(/\s+/g, " ").substring(0, 60);
  return {
    name,
    url: "https://h.liepin.com/resume/showresumedetail/?res_id_encode=" + (cb ? cb.value : "??")
  };
});
```

#### 如何发现 URL 格式

URL 是通过拦截 `window.open` 调用发现的。猎聘搜索结果页点击候选人卡片时，React 会调用 `window.open('/resume/showresumedetail/?res_id_encode=...')` 在新标签页打开简历。

```javascript
// 拦截 window.open 获取真实 URL
window.__hermes_new_win = null;
const origOpen = window.open;
window.open = function() {
  window.__hermes_new_win = arguments;
  return origOpen.apply(this, arguments);
};
```

⚠️ **注意**：
- 直接 URL 导航（如 `h.liepin.com/resume/show/?res_id=...`）返回 **404**，必须用 `/resume/showresumedetail/` 路径
- 简历链接必须在**已登录猎聘的浏览器**中打开（Chrome CDP 启动的 profile 已保存登录态）
- 猎聘直接 URL 搜索（`h.liepin.com/search?key=...`）同样返回 404，必须通过搜索框交互触发

#### 打开简历后捕获内容

```bash
WS="ws://127.0.0.1:9222/devtools/page/XXXXX"
RID="eb652d8bdbU1d7abc9c0227"

# 导航到简历页
python3 /tmp/cdp_client.py "$WS" "Page.navigate" \
  "{\"url\":\"https://h.liepin.com/resume/showresumedetail/?res_id_encode=${RID}\"}"

sleep 5

# 提取结构化文本
python3 /tmp/cdp_client.py "$WS" "Runtime.evaluate" '{
  "expression": "document.body.innerText",
  "returnByValue": true
}'
```

#### 卡片摘要 vs 完整简历

猎聘搜索结果页的卡片摘要（`.tlog-common-resume-card` textContent）已包含姓名、年龄、年限、学历、公司、职位、技能——足以完成初步候选评估和打分。完整简历页提供联系方式、详细项目经验等补充信息。

**这是最容易踩的坑。** `computer_use` 在配置中启用不代表当前会话已加载。

检查方法：看你自己的工具列表里有没有 `computer_use`。如果没有 → 立即告诉用户发 `/reset`，不要尝试 AppleScript / Playwright / curl 等绕行方案。7 种绕行方案已在两个会话中全部验证失败，详细记录见 `references/macos-workaround-failures.md`。

```bash
# 确认配置已启用（仅供参考，不代表会话可用）
hermes tools list 2>&1 | grep computer
```

如果配置中启用但你当前工具列表里没有 → **唯一路径：用户 `/reset`**。

### Step 0.5: 登录检测

打开猎聘后**立即验证是否已登录**。未登录时猎聘会 302 重定向到登录页，URL 变为 `https://h.liepin.com/account/login?backurl=...`。

用 Safari URL 检测快速判断：

```bash
osascript -e 'tell application "Safari" to get URL of current tab of window 1'
```

如果 URL 含 `account/login` → 暂停，告知用户需登录。不要尝试自动化登录（认证码/密码不安全）。
如果 URL 回到搜索页 → 继续 Step 1。

### Step 1: 准备策略数据

从策略文档中提取以下结构化数据：
- `search_queries`: 搜索关键词列表（按优先级排列，通常 3-4 组）
- `target_companies`: Tier 1/2/3 公司名单
- `filters`: 学历、工作年限、城市等筛选条件
- `hard_requirements`: 必须满足的硬性条件
- `pass_criteria`: 排除条件

### Step 2: 打开猎聘

**首选：computer_use**
```
computer_use(action="focus_app", app="Safari")
computer_use(action="capture", mode="som", app="Safari")
computer_use(action="click", element=<地址栏索引>)
computer_use(action="type", text="https://h.liepin.com/search/getConditionItem")
computer_use(action="key", keys="return")
computer_use(action="wait", seconds=3)
```

**备选：osascript（无 computer_use 时）**

⚠️ **不要用 Cmd+L + keystroke 导航。** 从 Cmd+L 地址栏输入 URL 时，如果 keystroke 被 Safari 的默认搜索引擎截获，会把 URL 当搜索词发送到百度。始终用 AppleScript 的 `set URL`：

```bash
# ✅ 正确：AppleScript set URL
osascript -e 'tell application "Safari" to set URL of current tab of window 1 to "https://h.liepin.com/search/getConditionItem"'

# ❌ 错误：Cmd+L + keystroke — 可能跳到百度搜索
osascript -e 'tell application "System Events" to keystroke "https://..."'
```

如果未登录，先登录猎聘账号（见 Step 0.5）。

### Step 3: 多轮搜索

每轮搜索的 SOP：

**3a. 定位搜索框并填入关键词**

使用 `computer_use`（首选）：
```
computer_use(action="capture", mode="som", app="Safari")
# 从 AX 索引中找到搜索框（通常是 AXTextField 类型）
computer_use(action="click", element=<搜索框索引>)
computer_use(action="type", text="<关键词>")
computer_use(action="key", keys="return")
computer_use(action="wait", seconds=3)
```

**备选：当 computer_use 不可用时使用 pbcopy + Cmd+V 粘贴**

如果只能用 osascript 键盘模拟：
1. 先用 `echo -n "关键词" | pbcopy` 把中文复制到剪贴板
2. 用 `Cmd+V` 粘贴（避免 IME 输入法把字母转成拼音）
3. 回车搜索

```bash
# 粘贴关键词
echo -n "光学产品经理" | pbcopy
osascript -e 'tell application "System Events" to tell process "Safari"
    keystroke "a" using command down  # 全选
    keystroke "v" using command down  # 粘贴
    keystroke return                   # 搜索
end tell'
```

⚠️ 不要用 `keystroke` 直接打中文 — macOS 中文输入法会把罗马字母当作拼音转成随机汉字。

**备选 2：URL 直接搜索（⚠️ 不可靠 — 已验证返回 404）**

在 macOS 12.7 + Safari 上，`h.liepin.com/search?key=...` 已被验证返回 **404**，猎聘不再支持 URL 参数搜索。不要使用此方法。

**当所有本地方法都失败时：委托给二号机（Worker Agent）**

如果两轮 osascript 粘贴 + URL 方法都失败（即 computer_use 不可用 + 粘贴不生效 + URL 被拒），**不要再继续重试**。立即切换到委托模式：

1. 确认策略文档（.md + .docx）已生成在 `~/Desktop/客户项目/{客户}/`
2. 让用户拉一个包含你和二号机的飞书群
3. 通过 `send_message` 将 .docx 策略 + 完整搜索指令发送到群内
4. 二号机收到后执行搜索

飞书群的 send_message target 格式：`feishu:oc_<chat_id>`（从 `send_message(action='list')` 获取）。
在群消息中 @二号机需用 `<at user_id="open_id">名字</at>` 格式，而非纯文本 `@名字`。

**3b. 查看搜索结果**
```
computer_use(action="capture", mode="som", app="Safari")
# 从截图中提取：
# - 搜索结果总数
# - 前页候选人列表（姓名/公司/职位/年限/活跃度）
```

**3c. 翻页（如需要）**
```
# 找到"下一页"按钮的 element 索引
computer_use(action="click", element=<下一页按钮>)
computer_use(action="wait", seconds=2)
computer_use(action="capture", mode="som", app="Safari")
```

### Step 4: 设置筛选条件

按策略要求设置筛选（按优先级）：

```
# 学历筛选
computer_use(action="click", element=<学历下拉框>)
computer_use(action="click", element=<"硕士及以上">)

# 工作年限
computer_use(action="click", element=<年限筛选>)
computer_use(action="click", element=<"3-5年" or "5-10年">)

# 城市
computer_use(action="click", element=<城市筛选>)
computer_use(action="type", text="上海")
computer_use(action="click", element=<匹配结果>)
```

⚠️ 筛选太严导致结果少时，逐步放宽。

---

## 备选流程（Safari + osascript）

⚠️ 仅当 Chrome 不可用时使用。Safari 自动化有严重可靠性问题，见环境要求章节。

### 搜索轮次策略（先窄后宽）

**核心原则：从最精准的开始，逐轮放宽。** 不要第1轮就用宽泛关键词（会出几百上千条结果无法筛选）。

| 轮次 | 关键词类型 | 词数 | 筛选 | 目标 |
|------|-----------|------|------|------|
| 第1轮 | 精准核心词 | 1-3 词 | 无 | 看精准匹配池大小 |
| 第2轮 | 扩展关键词 | 3-5 词 | 学历+城市 | 在精准池中加筛选 |
| 第3轮 | 公司定向 | 1公司+1-2词 | 全筛选 | T1公司逐个搜 |
| 第4轮 | 技术/能力定向 | 放宽 | 放宽城市 | 扩池，找隐藏人才 |

如果第1轮结果太少（<10人）→ 第2轮就去掉限制词，只保留核心角色词。
如果第1轮结果太多（>200人）→ 第1轮就加学历+城市筛选，不要等到第2轮。

### Step 5: 候选人提取

从每页截图的 AX 树中提取候选人信息：

关键提取字段：
- 姓名（脱敏处理：如"张某某"）
- 当前公司 + 职位
- 工作年限
- 教育背景（学校/学历）
- 活跃度（今天活跃/3天内/一个月内）
- 匹配原因

### 优先级判断

| 维度 | 🔥1级 | 🔥2级 | 普通 |
|------|--------|--------|------|
| 公司 | Tier 1 目标公司 | Tier 1-2 | Tier 2-3 |
| 硬门槛 | 全部满足 | 基本满足 | 部分满足 |
| 活跃度 | 今天/3天内活跃 | 一个月内 | 隐藏 |
| 城市 | 一致性高 | 可接受 | 需 relocate |

Pass 规则：严格应用策略中的排除条件。

### 关键词格式（重要！）

**猎聘不支持 OR/AND/括号等布尔搜索语法。** 策略文档中的复杂布尔表达式仅供人类阅读，实际搜索时必须转换为简单空格分隔的中文关键词。

| 策略文档格式（人类阅读） | 猎聘实际搜索格式 |
|---|---|
| `(光学产品经理 OR "光学" AND "产品经理") AND (半导体 OR 精密)` | `光学 产品经理 半导体 精密` |
| `(ASML OR 蔡司 OR SMEE) AND (光学产品 OR 光学模组)` | `ASML 蔡司 光学` → 或分别搜 `ASML 光学`、`蔡司 光学` |

规则：
- 空格 = AND 语义（猎聘默认行为）
- **第1轮用 1-2 个核心词**（如 `光学产品经理`），先看总量。关键词太多会导致匹配过少
- 第2轮及以后可加 3-4 个词缩小范围
- 公司定向搜索建议逐公司搜，不要一次塞太多公司名
- 每轮一个关键词串，等用户确认结果后决定下一轮方向

### Step 2: 提取候选人 + res_id（通过 window.open 拦截）

res_id **不能**从 checkbox value 读取。必须 hook `window.open` 后点击卡片头像：

```javascript
window.__urls = [];
var orig = window.open;
window.open = function(url) { window.__urls.push(url); return orig.apply(this, arguments); };
document.querySelectorAll('.tlog-common-resume-card')[0].querySelector('img').click();
// URL: /resume/showresumedetail/?res_id_encode=XXXXX
```

⚠️ res_id 末尾几位每次搜索都不同（会话级变化），不能截断、不能存数据库。

每轮搜索后记录：

```
轮次: 第1轮
关键词: 光学 产品经理 半导体 精密
筛选: 硕士以上，上海+苏州
结果数: XX 人
翻页: 前2页（约40人）
入围: X 人（1级=N, 2级=N, 普通=N）
```

### Step 7: 生成 HTML 报告（最终交付物，唯一输出）

一个 HTML 文件集成全部内容：候选人卡片（含分析+橙色简历按钮）、搜索轮次表、优先级。详见 `references/dom-parsing.md`。

保存为 `候选人链接_点击跳转.html`，存放于 `~/Desktop/客户项目/{客户}/`。

```bash
osascript -e 'tell application "Google Chrome" to activate'
osascript -e 'tell application "Google Chrome" to tell front window to set URL of active tab to "file:///PATH.html"'
```

**不再单独生成 .md、.docx、.xlsx 结果报告**。

## 候选人评估打分体系

基于卡片摘要文本（`.tlog-common-resume-card` textContent），按以下维度自动打分：

| 维度 | 条件 | 分值 |
|------|------|------|
| 学历+专业 | 光学/光电/光子/激光 博士 | +30 |
| | 其他 博士 | +10 |
| | 光学工程/光电信息/光信息/光学/激光/精密仪器/光电子 硕士 | +20 |
| | 物理/材料/电子科学/仪器/测控 硕士 | +10 |
| | 非光学专业硕士 | +0 |
| | 光电/光学 本科 | +10 |
| 目标公司 | ASML/KLA/SMEE/蔡司/舜宇/茂莱/御微/天准/海思/Lumileds/禾赛 | +15 |
| | 华为/歌尔光学/海康 | +8~10 |
| PM经验 | 产品经理/产品总监/产品线/产品负责人/产品部长/项目经理 | +10 |
| 光学工具 | Zemax/CodeV/LightTools/Tracepro | +10 |
| 半导体 | 半导体/光刻/晶圆/Stage/量测/精密光学 关键词 | +5 |

**评级阈值**：
- **🔥🔥🔥** ≥40分 — 重点推荐，专业+公司+PM+工具四维匹配
- **🔥🔥** ≥25分 — 值得关注，2-3维匹配
- **🔥** ≥15分 — 光学背景但其他维度弱
- **⚠️** ≥5分 — 勉强相关
- **❌** <5分 — 排除（非光学专业、学历不达标等）

打分逻辑参考 `references/candidate-scoring.md`。

## 搜索策略：4轮推进

**必须按以下顺序执行所有4轮后再做汇总评估**（用户偏好：先跑完所有轮次再抓简历）：

| 轮次 | 关键词 | 筛选 | 目标 |
|------|--------|------|------|
| R1 | 核心词（1-3词）| 无 | 看精准池大小 |
| R2 | 扩展词（3-5词）| 学历+城市 | 缩小范围 |
| R3 | 公司定向（1公司+光学）| 无（每公司单独搜）| 逐T1公司扫描 |
| R4 | 技术定向（Zemax+光学）| 放宽 | 扩池找隐藏人才 |

**筛选器操作（CDP）**：

```javascript
// 学历：点击"硕士"（.sfilter-edu 下 .tag-item 索引 2）
document.querySelector(".sfilter-edu").querySelectorAll(".tag-item")[2].click();

// 城市 — 目前城市：点击"上海"（.sfilter-city[0] 下 .tag-item 索引 0）
// 城市 — 期望城市：点击"苏州"（.sfilter-city[1] 下 .tag-item 索引 2）
var cities = document.querySelectorAll(".sfilter-city");
cities[0].querySelectorAll(".tag-item")[0].click(); // 上海
cities[1].querySelectorAll(".tag-item")[2].click(); // 苏州

// 重置筛选：点击各项的 .tag-item[0]（"不限"）
```

## 搜索结果报告

搜索完成后生成三份输出：

### 1. Markdown 报告（含候选人 + 直达链接）

```bash
~/Desktop/客户项目/{客户}/{客户}_{职位}_寻访结果_{date}.md
```

**每个候选人必须包含**：`res_id` + 猎聘直达链接 `https://h.liepin.com/resume/showresumedetail/?res_id_encode={res_id}`

### 2. HTML 候选人链接页（可点击跳转）

使用模板 `templates/candidate-links.html`，填入候选人卡片后保存到：

```bash
~/Desktop/客户项目/{客户}/候选人链接_点击跳转.html
```

用 osascript + Chrome 打开（不要用 `open -a`，系统可能用 Safari 打开 .html）

### 3. Excel 报告

```bash
~/Desktop/客户项目/{客户}/{客户}_{职位}_寻访结果_{date}.xlsx
```

## 打开候选人链接页

**必须用 osascript 打开 Chrome（非 open -a）**：

```bash
osascript -e 'tell application "Google Chrome" to activate'
osascript -e "tell application \"Google Chrome\" to tell front window to make new tab with properties {URL:\"file://$HOME/Desktop/客户项目/{客户}/候选人链接_点击跳转.html\"}"
```

不要用 `open -a "Google Chrome"` — 系统可能用 Safari 打开 .html 文件。

## 特殊场景

### 极稀缺岗位（如 PDE/PIE）
- 如实报告 0 结果
- 在 Excel 搜索说明中注明"需全国范围+mapping"
- 建议补充：脉脉/LinkedIn 定向触达

### 大结果集（>200人）
- 只取前 2 页（约 40 人）
- 优先取高活跃度 + 关键词精准匹配
- 在搜索说明标注"前2页筛选"

### 搜索无结果
- 第1轮无结果 → 放宽关键词（去掉括号、用OR替代AND）
- 第2轮仍无结果 → 去掉城市限制，全国搜索
- 第3轮仍无结果 → 去掉年限限制，如实报告

### 需要登录
- 打开猎聘后先用 Safari URL 检测是否跳转登录页
- 检测命令：`osascript -e 'tell application "Safari" to get URL of current tab of window 1'`
- 如 URL 含 `account/login?backurl=...` → **暂停，发截图告知用户登录**
- 让用户手动在 Safari 中登录，登录完成后通知你继续
- ⚠️ 不要尝试输入密码或处理短信验证码
- 搜索无结果也可能是登录过期导致，先验证 URL 非登录页再判定

## 搜索结果输出格式

每轮搜索完成后即时汇报：

```
### 第X轮搜索结果
- 关键词: xxx
- 筛选条件: xxx
- 结果总数: N人
- 前页概览:
  | 姓名 | 当前公司 | 职位 | 年限 | 教育 | 活跃度 | 匹配级 |
  |------|----------|------|------|------|--------|--------|
  | 张** | ASML中国 | 高级光学工程师 | 8年 | 浙大硕士 | 今天 | 🔥1级 |
  ...
```

## Common Pitfalls

1. **AX 索引过期**: UI 变化后（弹窗/新页面）必须先重新 `capture`，否则 element 索引指向错误控件
2. **搜索框 ref 值不稳定**: 每次搜索前先 `capture` 确认搜索框位置
3. **猎聘反爬**: 每轮搜索间隔 ≥ 5 秒，配合 `wait` 动作
4. **结果量过大**: >200人只取前2页，优先高活跃度
5. **截图提取信息不全**: 猎聘默认只显示部分字段，需要点进个人主页看详细 → 每个候选人增加 `click` + `capture` + `back`
6. **Safari 弹窗**: 可能有"是否允许通知"等弹窗，先 dismiss 再继续
7. **computer_use 工具未加载**: `hermes tools list` 显示 enabled ≠ 当前会话可用。如果工具列表里没有 `computer_use`，唯一路径是让用户 `/reset`。不要浪费时间尝试 AppleScript（JS 权限问题）、Playwright（未安装）、cua-driver CLI（不存在）等绕行方案。详细失败记录见 `references/macos-workaround-failures.md`。
8. **猎聘登录重定向**: 打开搜索页后 URL 可能自动跳转到 `account/login?backurl=...`。不要假设页面成功加载 — 用 `osascript` 取 URL 确认。未登录时所有搜索操作都无效。
9. **猎聘不支持布尔搜索**: 不要使用 OR/AND/括号等语法。策略文档中的 `(光学产品经理 OR "光学" AND "产品经理")` 仅供人类阅读，实际搜索必须转为空格分隔的简单中文关键词，如 `光学 产品经理 半导体`。一搜一关键词串，不要一次塞所有轮次的关键词。
10. **搜索框位置**: 使用猎聘主页**顶部**的搜索栏，不是页面中间的条件筛选表单。Tab 导航到搜索框时注意区分。每轮搜索独立执行，先输入关键词 → 回车搜索 → 看结果 → 下一轮新关键词。
11. **中文输入法干扰**: 用 `osascript keystroke` 直接打英文时，中文输入法会把它当拼音转成随机汉字（`guangxue` → `啊啊啊啊啊啊`）。解决方案：用 `pbcopy` 先把中文复制到剪贴板，再 `Cmd+V` 粘贴到搜索框。不要用 `keystroke` 逐字打中文。
12. **Cmd+V + Return 搜索可能不触发**: 即使粘贴成功+回车，URL 仍停留在 `getConditionItem`（而非搜索结果页），说明搜索未触发。可能原因：① 用户点击搜索框后鼠标移动导致焦点丢失；② osascript 执行有时间差，焦点已转移。对策：粘贴后检查 URL — 如果仍是 `getConditionItem` 则说明搜索失败，需要用户重新点击搜索框。不要反复重试同样的粘贴命令。
13. **URL 直连搜索返回 404**: `https://h.liepin.com/search?key=...` 直接导航到搜索结果页会被猎聘拦截返回 404。必须通过搜索框交互触发搜索，无法用 URL 参数绕过。
14. **Safari URL set by Cmd+L + keystroke → Baidu hijack**: When using Cmd+L to focus the address bar followed by keystrokes to type a URL, Safari's default search engine (百度) may intercept the text as a search query. Result: you navigate to `https://www.baidu.com/s?wd=...` instead of the intended URL. Solution: always use AppleScript `set URL of current tab` for navigation, never Cmd+L + keystroke.

See also: `references/resume-scraper-plugins.md` — analysis of Chrome extensions that scrape resumes from recruitment sites (useful architectural reference).
15. **React 受控输入框 value 不生效**: 直接用 `input.value = 'xxx'` 在 React/Ant Design 页面上不会触发框架状态更新。必须使用原生 setter：`Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, 'xxx')`，然后 `input.dispatchEvent(new Event('input', {bubbles: true}))`。完整 DOM 选择器参考见 `references/liepin-dom-selectors.md`。
16. **Chrome 登录态持久化**: 用 `--user-data-dir=~/.hermes/chrome_profile/` 启动 Chrome，cookie 自动保存在该 profile 的 SQLite 数据库中，下次启动自动恢复登录态。`scripts/chrome_cdp.sh` 一键启动。
17. **飞书 @mention 机器人**: 在群消息中 @机器人不能用纯文本 `@名字`。飞书正确格式需用 `<at user_id="open_id">名字</at>`。对方 Agent（如 OpenClaw）需开启 `allow_bots` 配置才能响应来自其他机器人的 @。
18. **搜索结果数不稳定**: 同一关键词在不同时间搜索可能返回不同数量（如本次 `光学产品经理` 从 3000+ 变为 70），原因是猎聘结果随机排序/轮换。评估时以当时显示的卡片为准，不依赖跨次搜索的一致性。
20. **搜索不改变 URL 但结果已加载**: 猎聘搜索通过 AJAX 加载结果，URL 停留在 `getConditionItem#session` 不变。不要通过 URL 判断搜索是否成功——直接检查 DOM 中是否有 `.tlog-common-resume-card` 或页面中出现候选人列表文本。搜索后等 4 秒再提取。
21. **res_id 不能截断**: 提取时不要用 `res_id[:20]`，截断末尾几位会导致 URL 跳转"简历编号异常"。必须完整提取完整使用。
22. **res_id 不能存数据库**: 末尾几位每次搜索都不同（会话级变化），存到 DB 下次必然失效。正确做法：每次生成 HTML 报告时当场从搜索结果点击卡片，通过 `window.open` 拦截获取真实 URL。
23. **checkbox value 不是 res_id**: 不能从 `input[type=checkbox]` 的 value 读取 res_id。必须通过 `window.open` 拦截获取。
24. **DOM 文本节点成对解析（结构化提取）**: 卡片详情区域文本节点成对出现——`[公司·职位, 日期]` 为工作经历，`[学校·专业·学位·类型, 日期]` 为学历。用 `card.querySelectorAll('*')` 取 `children.length===0` 的叶子节点，逐对解析可得每条经历的时间段、学校、专业、学位。
25. **Chrome CDP 稳定性（macOS 杀后台）**: macOS 会杀后台 Chrome 进程。解决方案：LaunchAgent 守护（`~/Library/LaunchAgents/ai.hermes.chrome-cdp.plist`），配置 KeepAlive=Crashed+NetworkState，ProcessType=Interactive，Nice=-10。崩溃后 10 秒内自动拉起。端口 9223，profile `~/.hermes/chrome_profile_xhs`。如遇 Chrome 启动提示"正在现有的浏览器会话中打开"→ `rm ~/.hermes/chrome_profile_xhs/SingletonLock` + `pkill -9 -f "Google Chrome"` + `launchctl bootstrap` 重载。
26. **公司名平台差异**: 同一公司在猎聘用简称（"鹏新旭"），X-SaaS 用全称（"深圳市鹏新旭技术有限公司"）。还有变体如"鹏新旭(PST)"、"深圳鹏新旭技术有限公司"、"鵬新旭技術有限公司"。搜索时需覆盖所有变体。映射表见 `references/company-name-aliases.md`。

## Verification Checklist

- [ ] Chrome 已通过 CDP 启动并连接 (`curl http://127.0.0.1:9222/json/version`)
- [ ] 猎聘已登录（URL 不含 `account/login`）
- [ ] 至少完成 3-4 轮不同维度的搜索（核心词/扩展/公司定向/技术定向）
- [ ] 每轮搜索记录：关键词/筛选/结果数/入围数
- [ ] 候选人按优先级分级（🔥🔥🔥/🔥🔥/🔥/⚠️/❌）
- [ ] 使用 `Runtime.evaluate` + `.tlog-common-resume-card` 提取卡片文本
- [ ] Excel 包含搜索说明 Sheet
- [ ] 文件命名符合 `{client}_{position}_寻访结果_{date}.xlsx`
## Verification Checklist

- [ ] Chrome 已通过 CDP 启动并连接 (`curl http://127.0.0.1:9223/json/version`)
- [ ] 猎聘已登录（URL 不含 `account/login`）
- [ ] 至少完成 3-4 轮不同维度的搜索
- [ ] 每轮搜索记录：关键词/筛选/结果数/入围数
- [ ] 候选人按优先级分级（🔥🔥🔥/🔥🔥/🔥/⚠️/❌）
- [ ] 使用 `Runtime.evaluate` + `.tlog-common-resume-card` 提取卡片文本
- [ ] Excel 包含搜索说明 Sheet（openpyxl 已安装：`pip install openpyxl -i mirrors.aliyun.com`）
- [ ] 文件命名符合 `{client}_{position}_寻访结果_{date}.xlsx`
- [ ] 发送前确认发送对象

### Pitfall 20: CDP 端口共享

猎聘可共用小红书 Chrome 实例（端口 9223），用 `Target.createTarget` 或 `json/new?url=` 创建新标签页。无需单独启 9222，但需确认 Chrome profile 已登录猎聘。

### Pitfall 21: 猎聘 SPA 搜索 URL 不变

搜索触发后 URL 仍为 `getConditionItem#session`，结果已通过 React 渲染。不要依赖 URL 变化判断成功，直接提取 `.tlog-common-resume-card`。

## Reference Files

- `references/liepin-dom-selectors.md` — 猎聘搜索页DOM选择器完整参考（2026-06-02 验证）
- `references/eresume-plugin-analysis.md` — E简历Chrome插件架构分析（URL匹配/HTML捕获/后端解析）
- `references/macos-workaround-failures.md` — Safari/computer_use 7种绕行方案全部失败的详细记录
- `references/safari-liepin-failures-12.7.md` — macOS 12.7 Safari 自动化失败记录
- `scripts/cdp_client.py` — Python 标准库 CDP WebSocket 客户端
- `scripts/chrome_cdp.sh` — Chrome CDP 启动脚本（含登录态持久化）
