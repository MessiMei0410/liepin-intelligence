---
name: liepin-cdp-search
description: "Automate Liepin (猎聘) candidate search, evaluation, A/B candidate outreach, and resume link extraction via Chrome CDP. Full workflow: launch Chrome → search → filter → extract cards → rank candidates → auto-greet A/B candidates with verified send status → generate direct resume URLs."
version: 1.2.0
author: Hermes Agent
---

# Liepin CDP Search — 猎聘全自动搜索工作流

使用 Chrome DevTools Protocol (CDP) 全自动驱动猎聘搜索候选人、评估匹配度、提取简历详情链接。

**配合使用**: 先加载 `headhunting-search-strategy` 生成寻访策略，再加载本技能执行搜索。搜索结果通过 `talent-pool` 技能存入飞书人才库。三个技能协同完成「策略→搜索→评估→存库」全流程。

## 岗位库优先硬规则

任何搜索、候选评级、带岗位触达、触达统计或复盘前，先读取 `~/.hermes/talent_pool.db` 的岗位库：

```sql
SELECT * FROM positions WHERE client = ? AND status = 'open';
SELECT * FROM position_profiles WHERE client = ?;
```

- 只用 `positions.title` / `position_profiles.position` 作为当前标准岗位名和方向。
- 用 `position_profiles.source_position_ids_json` 校验该画像来自哪个 `positions.id`。
- `candidates.position`、`candidate_clients.position_tag` 可能是旧合并岗位或历史标签，不能作为当前岗位方向的源头。
- 多岗位项目必须逐岗位读取岗位库、逐岗位匹配 hjobId、逐岗位汇总；不得用旧合并名把多个方向混在一起触达或统计。

## 前置条件

- macOS + Google Chrome
- `cdp_client.py` 脚本（见下方 Step 0）
- Chrome 已手动登录猎聘一次（Cookie 持久化在 profile）

## Step 0: 准备 CDP 客户端

将以下脚本保存为 `/tmp/cdp_client.py`（每次会话开始时检查是否存在，不存在则重新创建）：

**⚠️ `/tmp/` 会在系统重启后清空。每次搜索前先检查 `ls /tmp/cdp_client.py`，如缺失则重新写入。**

```bash
CDP_CLIENT="$HOME/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py"
```

每次使用前确认脚本存在：`test -f "$CDP_CLIENT" && echo OK`。

若脚本丢失，用 `skill_view(name='liepin-cdp-search', file_path='scripts/cdp_client.py')` 查看源码重新写入。

```python
#!/usr/bin/env python3
"""Minimal CDP client — send commands to Chrome via raw WebSocket."""
import json, struct, socket, hashlib, base64, os, sys, time
from urllib.parse import urlparse

class CDP:
    def __init__(self, ws_url):
        u = urlparse(ws_url)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect((u.hostname, u.port))
        self._id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {u.path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{u.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp.split(b"\r\n")[0]:
            raise Exception(f"Handshake failed: {resp.split(b'\r\n')[0]}")

    def send(self, method, params=None):
        self._id += 1
        msg = json.dumps({"id": self._id, "method": method, "params": params or {}})
        frame = self._make_frame(msg)
        self.sock.sendall(frame)
        return self._recv()

    def _make_frame(self, text):
        data = text.encode()
        length = len(data)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        masked = bytearray(b ^ mask[i % 4] for i, b in enumerate(data))
        return bytes(header) + mask + masked

    def _recv(self, timeout=5):
        self.sock.settimeout(timeout)
        result = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hdr = self.sock.recv(2)
                if len(hdr) < 2: break
                opcode = hdr[0] & 0x0F
                masked = hdr[1] & 0x80
                length = hdr[1] & 0x7F
                if length == 126: length = struct.unpack(">H", self.sock.recv(2))[0]
                elif length == 127: length = struct.unpack(">Q", self.sock.recv(8))[0]
                if masked: mask_key = self.sock.recv(4)
                data = b""
                while len(data) < length:
                    chunk = self.sock.recv(min(length - len(data), 65536))
                    if not chunk: break
                    data += chunk
                if masked: data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
                msg = json.loads(data.decode())
                if opcode == 0x01 and msg.get("id") == self._id:
                    result = msg; break
            except socket.timeout: break
            except: break
        return result

    def close(self):
        try: self.sock.close()
        except: pass


if __name__ == "__main__":
    ws_url = sys.argv[1]
    method = sys.argv[2] if len(sys.argv) > 2 else "Browser.getVersion"
    params = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    cdp = CDP(ws_url)
    result = cdp.send(method, params)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    cdp.close()
```

## Step 1: 启动 Chrome + 设置环境变量

```bash
# 每次搜索前设置
CDP_CLIENT="$HOME/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py"

# 确认脚本存在
test -f "$CDP_CLIENT" || { echo "CDP client missing"; exit 1; }

# 启动 Chrome
bash "$HOME/.hermes/scripts/chrome_cdp.sh"
```
PROFILE_DIR="$HOME/.hermes/chrome_profile"
mkdir -p "$PROFILE_DIR"

pkill -f "chrome.*remote-debugging-port=9222" 2>/dev/null
sleep 1

"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE_DIR" \
  --no-first-run --no-default-browser-check &

sleep 2
curl -s http://127.0.0.1:9222/json/version | python3 -c "import sys,json;d=json.load(sys.stdin);print(f'CDP: {d[\"Browser\"]}')"
```

首次使用需在 Chrome 中手动登录猎聘，之后 Cookie 持久化在 `~/.hermes/chrome_profile/`。

验证 CDP：`curl -s http://127.0.0.1:9222/json/version`

## Step 2: 获取 Tab WebSocket URL

```bash
WS=$(curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys,json
tabs=json.load(sys.stdin)
print(tabs[0]['webSocketDebuggerUrl'])
")
echo "WS=$WS"
```

后续所有操作通过这个 `WS` 变量发送 CDP 命令。

## Step 3: 搜索候选人

### 3a: 导航到搜索页 + 填入关键词

猎聘搜索框是 Ant Design AutoComplete 组件，需要用 React 原生 setter 来设置值：

```bash
python3 "$CDP_CLIENT" "$WS" "Page.navigate" '{"url":"https://h.liepin.com/search/getConditionItem"}'

sleep 3

# 填入关键词（React native value setter）
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"(()=>{
  var input=document.querySelector(\"#rc_select_1\");
  var setter=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,\"value\").set;
  setter.call(input,\"光学产品经理\");
  input.dispatchEvent(new Event(\"input\",{bubbles:true}));
  input.dispatchEvent(new Event(\"change\",{bubbles:true}));
  return \"set: \"+input.value
})()","returnByValue":true}'

# 点击搜索按钮
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.querySelector(\".search-btn\").click();\"ok\"","returnByValue":true}'

sleep 5
```

### 3b: 设置筛选条件

```bash
# 学历筛选 - tag-item 索引: 0=不限, 1=本科, 2=硕士, 3=博士/博士后, ...
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.querySelector(\".sfilter-edu\").querySelectorAll(\".tag-item\")[2].click();\"硕士\"","returnByValue":true}'

# 城市筛选 - 目前城市
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.querySelectorAll(\".sfilter-city\")[0].querySelectorAll(\".tag-item\")[0].click();\"上海\"","returnByValue":true}'
```

筛选后需重新点击搜索。

### 3c: 获取结果总数

```bash
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.querySelector(\"[data-nick=totalcnt]\").textContent.trim()","returnByValue":true}'
```

## ⚠️ 卡片解析：DOM TextNode 遍历（正确方案）

**❌ 不要用正则解析卡片文本** — 猎聘卡片文本格式不稳定，正则匹配姓名/公司极易出错（匹配到"今天活跃"→"今天活"、匹配到"求职期望"段→公司字段污染）。

**✅ 用 DOM TreeWalker 遍历 TextNode**：

```javascript
var walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
var nodes = [];
while(walker.nextNode()) {
    var t = walker.currentNode.textContent.trim();
    if (t) nodes.push(t);
}

// 姓名：第一个匹配 /^[\u4e00-\u9fa5A-Za-z]{1,4}\*{2}$/ 的 TextNode
// 求职期望城市：TextNode "求职期望：" 后的下一个 TextNode
// 学历：匹配 /^(博士|硕士|本科|大专|MBA)$/ 的 TextNode
// 公司：从 fullText 中找 公司名(含有限公司/科技/集团等后缀) + "·" 的模式
// 职位：第一个 "·" 后的职位关键词(工程师/经理/总监/专家)
```

完整提取函数见 `references/dom-parser.js`。 ⭐ DOM TextNode 遍历（正确方法）

**⚠️ 不要用 `c.textContent.trim().replace(/\s+/g, ' ')` 解析！** 这会把所有字段揉成一团（"今天活跃刘**25岁..."），正则无法可靠拆分——名字字段会误提取"今天活"、公司字段会掺入"求职期望：北京半导体设备工程师"。

**正确方法：DOM TextNode 逐节点遍历**，每个 text node 是独立字段：

```python
r = cdp.send("Runtime.evaluate", {
    "expression": """
    (() => {
        var cards = document.querySelectorAll('.tlog-common-resume-card');
        var data = [];
        cards.forEach(function(c) {
            var walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
            var nodes = [];
            while(walker.nextNode()) {
                var t = walker.currentNode.textContent.trim();
                if (t) nodes.push(t);
            }
            // nodes[0] = "今天活跃" / "隐藏活跃状态" / "7天内活跃"
            // nodes[1] = "刘**"  (姓名 + **)
            // nodes[2] = "25岁"
            // nodes[3] = "工作3年"
            // nodes[4] = "本科"
            // nodes[5] = "北京"  (所在城市)
            // nodes[6] = "求职期望："
            // nodes[7] = "上海"  (期望城市)
            // ...后续nodes是职位关键词和公司名
            data.push({nodes: nodes, name: nodes[1], city: nodes[7]});
        });
        return JSON.stringify(data.slice(0, 15));
    })()
    """,
    "returnByValue": True
})
```

**字段提取规则**：
- **姓名**: `nodes[1]`，格式 `姓名**`，去 `**` 即可
- **年龄**: `nodes[2]`，格式 `数字岁`
- **工作年限**: `nodes[3]`，格式 `工作数字年`
- **学历**: `nodes[4]`，博士/硕士/本科/大专/MBA
- **所在城市**: `nodes[5]`
- **期望城市**: 紧跟 `"求职期望："` 节点的下一个节点
- **公司名**: 从 `nodes[6]` 之后的节点中，找匹配 `有限公司|科技|半导体|集团|Inc|Corp|Ltd` 后缀的第一个节点；或用正则从全文中提取 `([...]+(?:有限公司|科技|集团|半导体))` 并取最后一个匹配（当前公司通常出现在 `·` 分隔符之前）

完整提取脚本见 `references/dom-extraction.py`。

## Step 5: 获取简历详情链接 ⭐ (推荐方式：window.open 拦截)

**⚠️ 优先使用此方法。** 猎聘 checkbox 的 `value`（res_id）是会话级参数，跨搜索会话或页面刷新后失效，直接拼接 URL 会导致「简历编号异常」。

**正确做法**：拦截 `window.open` 捕获真实链接：

```bash
# 1. 安装拦截器
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"window.__captured=[];window.__o=window.open;window.open=function(u,n,f){window.__captured.push(u);return window.__o(u,n,f)};\"ok\"","returnByValue":true}'

# 2. 逐张点击卡片
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.querySelectorAll(\".tlog-common-resume-card\")[0].click();\"\"","returnByValue":true}'
sleep 2

# 3. 读取捕获的链接（相对路径，需补全域名）
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"JSON.stringify(window.__captured)","returnByValue":true}'
```

捕获到的链接是相对路径（如 `/resume/showresumedetail/?res_id_encode=...`），**需补全域名并完整保留当前会话参数**：
```python
full_url = relative_url if relative_url.startswith("http") else "https://h.liepin.com" + relative_url
```

⚠️ 不要为了美化链接清洗 `ck_id`、`sk_id`、`fk_id`、`sss`、`searchMark`、`pgRef` 等参数。当前会话完整链接是触发「立即沟通」最稳的形态。

**每换一个搜索关键词，需重新安装拦截器**（页面刷新后 `window.open` 恢复原始状态）。

## Step 6: Deliver Results to User

After search is complete, send candidate links as **inline Feishu messages**, NOT as .md attachments:

```
**A级 (3人)**

A1 张**（北京 华卓精科）
https://h.liepin.com/resume/showresumedetail/?res_id_encode=...

A2 李**（上海 SMEE）
https://h.liepin.com/resume/showresumedetail/?res_id_encode=...
```

⚠️ **Do NOT put links inside .md file attachments** — Feishu does not render clickable links in .md attachments. Send the links directly in the chat message body.

Also save a standalone `候选人链接_{职位}_{日期}.md` to the client project folder for record-keeping.

### Fallback: Open resume in CDP Chrome

If user reports links don't open from Feishu, navigate directly via CDP:

```bash
python3 /tmp/cdp_client.py "$WS" "Page.navigate" '{"url":"https://h.liepin.com/resume/showresumedetail/?res_id_encode=..."}'
```

Then screenshot and send: `python3 /tmp/cdp_client.py "$WS" "Page.captureScreenshot"`

```bash
python3 "$CDP_CLIENT" "$WS" "Page.captureScreenshot" '{"format":"png"}' \
  | python3 -c "import sys,json,base64;d=json.load(sys.stdin);open('/tmp/screenshot.png','wb').write(base64.b64decode(d['result']['data']))"
```

## Step 6.5: A/B 候选人自动打招呼（默认执行）

当用户让你跑猎聘寻访、搜索岗位、找人选，且没有明确说「只搜不发」「先别打招呼」「只给名单」时：

- **A级、B级人选：默认自动带岗位开聊**，不再额外询问；首次触达走「立即沟通 → 开聊职位 → 立即开聊」，不要先走「推荐职位」。
- **C级人选：只入库/列入报告**，不要自动打招呼。
- **半导体 TME/FAE/技术市场类岗位评级前必须做三层核验**：产品线、应用市场、客户场景。对标公司或相似 title 只能作为扩池证据，不能单独评 A/B 并触达。若候选人只命中公司/title，但产品线或应用市场错配，降为 C 或 `needs_product_line_verification`，先不自动触达。
- 每个人都要保留结构化日志：姓名/等级/公司/职位/使用的岗位/触达方式/最终状态/失败原因/截图或页面证据路径。
- 自动触达成功不等于已经完整进入工作台人才库。`job_chat_verified` / `job_recommended_verified` 后，必须同步写入/更新：
  - `candidates`：`client`、`position`、`status='contacted'`、`created_at`、`updated_at`、`source='liepin'`
  - `candidate_clients`：同一候选人如已存在旧岗位，要用 `ON CONFLICT ... DO UPDATE SET position_tag=excluded.position_tag` 更新到当前岗位
  - `candidate_profiles`：写入最小画像，供页面助手/工作台识别“已入库”
  - `candidate_intelligence`：写入最小匹配评估和 `recommendation_decision`

### 触达前必须使用当前会话链接

猎聘简历详情链接是强会话上下文相关的。自动打招呼时：

- 不要复用历史报告、CSV、飞书消息、数据库里保存过的简历详情链接。
- 不要清洗掉详情 URL 的关键参数，尤其不要移除 `ck_id`、`sk_id`、`fk_id`、`sss`、`searchMark`、`pgRef`、`mscid` 等猎聘上下文参数。
- 创建新标签页时，`/json/new?{url}` 里的目标 URL 必须完整 URL encode：Python 用 `quote(url, safe="")`。不能把 `&` 原样放进 `/json/new?`，否则 Chrome DevTools 会截断参数。
- 优先从当前搜索结果页直接点击候选人卡片进入详情；若必须用 URL，必须是当前会话刚捕获的完整 URL。

### 发送前校验详情页有效

进入详情页后，先验证页面不是异常页，再执行触达：

- 页面正文不得包含「简历编号异常」「简历不存在」「页面异常」「登录」等异常提示。
- 页面上必须能看到候选人关键信息，且有「立即沟通」「推荐职位」等真实操作控件。
- 如果验证失败，记为 `link_invalid` 或 `blocked`，不要尝试硬点按钮，也不要标记为已发送。

### 立即沟通/推荐职位的可靠性规则

- **岗位触达的硬规则**：用户要求“拿发布的岗位触达/推荐岗位/用岗位打招呼”时，必须把目标岗位带给候选人；无岗位开聊不得计入岗位触达成功。
- **首选路径**：搜索结果页安装 `window.open` 拦截器 → 点击卡片捕获当前会话完整详情链接 → 新标签打开详情页 → 校验简历有效 → 点「立即沟通」 → 在“开聊职位”弹窗里选择目标岗位 → 点「立即开聊」→ 等待详情页按钮变为「继续沟通」并记录所选岗位，记为 `job_chat_verified`。不要把首次触达优先做成「推荐职位」，推荐职位入口对求职意向/城市等限制更窄，容易误判失败。
- **兜底路径**：如果详情页已经是「继续沟通」，不能直接算岗位触达成功；此时才点「推荐职位」，选择目标岗位并看到推荐成功/已推荐/岗位名出现在沟通或推荐记录里，才记为 `job_recommended_verified`。
- **禁止路径倒置**：首次触达时，即使页面同时有「推荐职位」按钮，也不能先走「推荐职位」。必须先尝试「立即沟通」里的“开聊职位”下拉；只有页面已是「继续沟通」时，才进入「推荐职位」兜底。
- 「推荐职位」和「立即沟通」是两类结果：推荐职位成功记 `job_recommended_verified`；带岗位开聊成功记 `job_chat_verified`。不要把无岗位开聊、已有继续沟通、普通聊天状态混入岗位触达成功。
- 开聊/推荐弹窗里的岗位列表可能需要滚动才能找到目标岗位；必须滚动/搜索直到目标岗位出现，不能只检查首屏列表。
- 如果岗位下拉框为空或目标岗位不可选，停止该候选人并记为 `job_dropdown_empty` / `target_job_not_found`，不要点击「不选择职位开聊」来冒充岗位触达。
- 「不选择职位开聊」只能在用户明确要求“先无岗位开聊”时使用；状态必须记为 `chat_no_job_verified`，最终汇报必须单列，不能计入岗位触达成功。
- 自动化点击后不能只看 `.click()` 是否执行成功；必要时使用真实鼠标事件序列，并等待页面反馈。
- 批量发送要慢速逐人执行，操作间加随机等待，避免一口气快速循环。
- 每处理完一个候选人的详情页/沟通页，要关闭对应工作标签页，或复用同一个工作标签并在脚本结束时关闭；不要让简历详情标签堆积，避免后续误读旧页面。

### 职聊二次跟进锁页规则（已沟通候选人）

当候选人已经在「职聊」里，二次跟进不要复用临时 Playwright Page 对象直接点发送。必须使用专用脚本：

```bash
python3 /Users/messi/.codex/skills/liepin-cdp-search/scripts/liepin_im_followup.py \
  --port 9223 \
  --candidate '成先生' \
  --check '鸿舸半导体设备(上海)有限公司' \
  --check '战略采购' \
  --check '什么行业的' \
  --message '您好，是半导体前道设备行业...' \
  --send
```

默认不加 `--send` 是 dry-run：只选择会话、复核、填入并清空草稿，不发送。真实发送必须显式加 `--send`。

脚本的硬性保护：

- 只选择 `title == "职聊"` 且 URL 包含 `/im/showmsgnewpage` 的标签页。
- 每一步都重新检查当前标签；若 URL 漂到 `/search/getConditionItem` 或页面出现「找简历/人才管理」，立即退出，状态记为 `PAGE_DRIFT_*`。
- 发送前必须看到候选人姓名和所有 `--check` 字段；字段缺失时退出，状态记为 `VERIFY_FAILED`。
- 发送前先填入并复核 `textarea.im-ui-textarea`，再点击 `button.im-ui-basic-send-btn`。
- 发送后必须在同一职聊页正文看到已发送话术前 50 个字，才记为 `sent_verified`。
- 任何验证码、账号异常、安全验证、登录过期、操作频繁都立即退出，不继续点击。

如果右侧「猎聘专业回复助手」仍显示其他项目（如苏科思），不要使用助手的「填入输入框」按钮；二次跟进脚本直接操作猎聘原生输入框，避免串岗。

### 成功标准：必须二次验证

「按钮点了」不等于「岗位触达成功」。当用户要求用岗位触达时，只有满足下列任一证据，才能记为岗位触达成功：

- `job_chat_verified`：开聊弹窗中明确选择了目标岗位，点击「立即开聊」后，详情页变为「继续沟通」或职聊页出现该岗位相关会话记录。
- `job_recommended_verified`：点击「推荐职位」后明确选择了目标岗位，页面出现推荐成功/已推荐，或职聊/推荐记录中能看到目标岗位名。

以下状态不得计入岗位触达成功：

- 只有「继续沟通」但没有目标岗位证据。
- 点击了「不选择职位开聊」。
- 点击过按钮但没有成功提示、岗位名、会话记录或按钮状态变化。

日志状态必须区分：

- `job_chat_verified`：已带目标岗位完成首次开聊，且有页面证据。
- `job_recommended_verified`：已把目标岗位推荐给候选人，且有页面证据。
- `needs_product_line_verification`：公司/title/职能看似匹配，但产品线、应用市场或客户场景证据不足；未核验前不要自动触达。
- `contacted_but_misaligned`：历史已触达，但复核后发现产品线或应用市场错配；保留触达事实，但不能计为有效 A/B 匹配。
- `existing_chat_no_job_verified`：页面已是「继续沟通」，但尚未验证目标岗位推荐；不能计入岗位触达成功。
- `chat_no_job_verified`：无岗位开聊成功；不能计入岗位触达成功，除非用户明确只要求开聊。
- `clicked_unverified`：点击过按钮，但没有成功证据；不要对用户说已发送。
- `link_invalid`：详情页链接失效或简历编号异常。
- `job_dropdown_empty`：立即沟通或推荐职位的岗位下拉列表为空。
- `target_job_not_found`：弹窗/列表中没有找到目标岗位。
- `job_location_mismatch`：弹窗明确提示工作地点不符合求职者意愿，且提交按钮不可用；不能计入岗位触达成功。
- `blocked`：登录、风控、页面异常、岗位不可选等导致无法继续。
- `skipped_c_level`：C级候选人按规则跳过触达。

最终汇报里只有 `job_chat_verified` 和 `job_recommended_verified` 可以统计为“岗位触达成功”；`existing_chat_no_job_verified`、`chat_no_job_verified`、`clicked_unverified` 等必须单独列明，避免误报。

### 已验证触达伪代码

```javascript
// 1. 搜索结果页：捕获当前会话详情链接
window.__captured = [];
window.__o = window.__o || window.open;
window.open = function (u, n, f) {
  window.__captured.push(u);
  return window.__o(u, n, f);
};
document.querySelectorAll(".tlog-common-resume-card")[index].click();

// 2. 详情页：岗位触达
const body = document.body.innerText || "";
if (/简历编号异常|简历不存在|页面异常|登录账号/.test(body)) status = "link_invalid";
const buttons = Array.from(document.querySelectorAll("button")).map(b => (b.innerText || "").trim());
if (buttons.some(t => t.includes("继续沟通"))) status = "existing_chat_no_job_verified"; // 还需走推荐职位验证
if (buttons.some(t => t.includes("立即沟通"))) {
  Array.from(document.querySelectorAll("button")).find(b => b.innerText.includes("立即沟通")).click();
  // 弹窗出现后必须选择目标岗位；找不到目标岗位则停止，不能点「不选择职位开聊」
  // 二次验证：只有选择过目标岗位且页面变为继续沟通，才是 job_chat_verified
}
```

## 搜索轮次策略

实际关键词由 `headhunting-search-strategy` 生成的策略文档决定。下表为通用轮次框架：

| 轮次 | 关键词 | 筛选 | 目标 |
|------|--------|------|------|
| R1 | 精准核心词(1-3词) | 无 | 看池大小 |
| R2 | 扩展+行业词 | 学历+城市 | 精准池 |
| R3 | 目标公司+核心词 | 无 | 公司定向 |
| R4 | 技术/工具关键词 | 放宽城市 | 扩池+质量过滤 |

不同岗位的技术过滤词不同：光学用 Zemax/CodeV、运动台用 "精密运动/纳米定位"、半导体质量用 "SPC/FMEA"。参考 `headhunting-search-strategy` 的技能参考范例。

**极窄岗位（全国池 < 200 人）搜索模式**：见 `references/niche-role-company-search.md`。核心策略：跳过通用关键词，直接定向目标公司员工搜索。

## 关键选择器

| 元素 | 选择器 |
|------|--------|
| 搜索输入框 | `#rc_select_1` |
| 搜索按钮 | `.search-btn` |
| 候选人卡片 | `.tlog-common-resume-card` |
| 结果总数 | `[data-nick=totalcnt]` |
| 学历筛选 | `.sfilter-edu .tag-item` |
| 城市筛选 | `.sfilter-city .tag-item` |
| 简历 checkbox (含res_id) | `.tlog-common-resume-card input[type=checkbox]` |
| 详情URL | `window.open` 拦截 → `/resume/showresumedetail/?res_id_encode=...` |

## Pitfalls
## Pitfalls

1. **CDP 脚本路径** — 不要依赖 `/tmp/cdp_client.py`（会被系统清理或容器重启清除）。始终用技能内置脚本：`$HOME/.hermes/skills/productivity/liepin-cdp-search/scripts/cdp_client.py`。每次搜索前先 `test -f "$CDP_CLIENT"` 确认存在。
2. **Python 3.11 f-string bug** — `cdp_client.py` 中的 `raise Exception(f"Handshake failed: {resp.split(b'\\r\\n')[0]}")` 在 Python 3.11 报语法错误。修复：先用变量存 `resp.split(b'\r\n')[0]`，再传入 f-string。
3. **React 输入框** — 不能用 `.value=` 直接设值，必须用 `Object.getPropertyDescriptor(window.HTMLInputElement.prototype,"value").set` 然后 dispatch `input` 事件
4. **CDP 连接冲突** — Chrome `--remote-debugging-port` 会为每个页面注册 DevTools 前端，但不影响 CDP 连接。如果 Runtime.evaluate 超时，检查是否有其他调试器占着页面。用 `/json/list` 查看 `devtoolsFrontendUrl` 字段不为空但连接仍可用是正常的。
5. **Chrome 不稳定** — CDP Chrome 进程容易挂。解决方案：创建 LaunchAgent 守护，`KeepAlive` 设为 `true`，`ThrottleInterval` 5 秒。plist 模板见下方 Step 0 附加。
6. **猎聘登录过期** — Cookie 有效期短（几小时~一天）。每次搜索前先导航到 `https://h.liepin.com/` 检查 `window.location.href` 是否含 `login`。如过期，用 `clarify` 请用户手动登录。
7. **res_id 会话级变化** — res_id 每次猎聘会话都不同，不能存入数据库持久化。候选人链接必须实时通过 CDP 拦截 `window.open` 捕获。格式：`https://h.liepin.com/resume/showresumedetail/?res_id_encode=...`
8. **猎聘反爬/防封** ⚠️ — 频繁自动化操作会触发账号异常检测。措施：(a) CDP 操作间加 `random.uniform(1, 4)` 秒延迟；(b) 扫描间隔 ≥ 8 秒；(c) 单次会话搜索关键词不超过 5 组；(d) 避免短时间内多次点击搜索。
9. **JSON 转义（多行 JS）** — 多行 JS 表达式中包含换行符和 `{` `}` 会导致 `cdp_client.py` 的 JSON 解析失败。**推荐方案：将 JS 写入临时文件，用 `python3 -c "import json;print(json.dumps(open('file.js').read()))"` 转义后传入**
4. **React 输入框** — 不能用 `.value=` 直接设值，必须用 `Object.getPropertyDescriptor(window.HTMLInputElement.prototype,"value").set` 然后 dispatch `input` 事件
5. **JSON 转义（多行 JS）** — 多行 JS 表达式中包含换行符和 `{` `}` 会导致 `cdp_client.py` 的 JSON 解析失败。推荐将 JS 写入临时文件后转义传入
6. **搜索结果随机化** — 同一关键词每次搜索结果顺序不同，需通过 res_id 唯一标识候选人
7. **直接URL 404** — `/resume/show/?res_id=...` 等多个变体均无效，唯一有效格式是 `/resume/showresumedetail/?res_id_encode=...`。注意 res_id 会话级变化，跨会话失效。
8. **`.click()` 不触发** — JS `.click()` 和 CDP `Input.dispatchMouseEvent` 对卡片均不触发详情，详情通过 `window.open` 新窗口打开
9. **Cookie 持久化与过期** — Chrome `--user-data-dir=~/.hermes/chrome_profile/` 保存登录态。登录 Session 会过期（通常几小时~一天），需定期手动重新登录
10. **搜索框ID** — `#rc_select_1` 是 Ant Design 动态生成的，页面刷新后可能变化。建议优先尝试 `#rc_select_1`，fallback 试 `.ant-select-selection-search-input`
11. **筛选后需重新搜索** — 设置筛选条件后必须再次点击搜索按钮
12. **中文关键词歧义** — 某些词有多重含义，冷门专业词需要配合行业词过滤
13. **英文公司名歧义** — 猎聘关键词匹配整个简历文本，非仅公司名
14. **CDP JSON 换行符** — `Runtime.evaluate` 的 `expression` 参数通过 shell 的 JSON 字符串传递时，JS 代码中的换行符会变成 JSON 控制字符导致 `json.loads` 报错。必须将 JS 表达式写成单行
15. **Cookie 过期恢复** — 猎聘 Session Token 有效期仅数小时。每次搜索前必须先验证登录态
16. **关键词歧义污染** — 某些中文技术词汇与消费领域重合，导致搜索结果严重稀释
17. **公司名搜索不可靠** — 猎聘关键词匹配整份简历全文而非当前公司
19. **卡片文本解析不可靠** — `c.textContent.trim().replace(/\s+/g, ' ')` 把所有字段揉成一行（如"今天活跃刘**25岁工作3年本科..."），正则无法拆分。会导致：名字误提取"今天活"/"隐藏活"、公司字段混入"求职期望：北京半导体设备工程师"。**唯一正确的提取方式**：`document.createTreeWalker(card, NodeFilter.SHOW_TEXT)` 逐TextNode遍历，每个字段独立。详见 Step 4。
20. **简历详情页搜索关键词** — `shResource?keyword=` 速度慢（2次跳转），直接导航 `showresumedetail?res_id_encode=...` 更快
13. **res_id 会话级失效** — 卡片 checkbox 的 `value`（res_id）是会话级参数，页面刷新或换搜索词后失效。**不要用 checkbox res_id 拼接URL**。用 Step 5 的 `window.open` 拦截方案获取真实链接。
14. **Chrome CDP 启动脚本可能不存在** — `~/.hermes/scripts/chrome_cdp.sh` 不一定存在。手动启动命令：`"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="$HOME/.hermes/chrome_profile_xhs" --no-first-run`（需 `background=true`）。验证：`curl -s http://127.0.0.1:9222/json/version`。
21. **App 读 candidates.client，不读 candidate_clients** — 桌面 App（一站式寻访猎头工作站.app）直连 `talent_pool.db`，SQL 查询 `WHERE client = ?` 从 `candidates.client` 列读取。每次搜索结果入库时，**必须同时写入 `candidates.client`**，不能只写 `candidate_clients` 表。否则 App 的客户下拉框和职位筛选器看不到新数据。
21b. **岗位触达后要完整入库** — 只写 `outreach_events` 或只把 `candidates.status` 改成 `contacted` 不够。猎聘页面助手的“确认入库/已入库”还依赖 `candidate_profiles` / `candidate_intelligence`；同一候选人跨岗位时 `candidate_clients` 的唯一键是 `(candidate_name,candidate_company,client)`，所以必须 upsert 更新 `position_tag`，否则会停留在旧岗位。
21d. **A 系统推进身份必须用本地候选 ID** — `job_candidates.source_candidate_id` 在 A 系统里必须优先写本地 `candidates.id`，不能把猎聘 `res_id_encode` / `resume_id` 当作同一人的第二个推进身份。触达成功回写前，先按 `job_id + person_id + raw_position` 查已有搜索入库行；存在则升级该行到 `job_chat_verified` / `job_recommended_verified` / 消息触达状态，并把猎聘 `res_id_encode` 放进 `candidate_events.raw_json`、`source_id` 或日志证据。不得为同一人同一岗位再插一条 `job_candidates`，否则人岗助手会返回 `未唯一定位`。
21e. **搜索卡片公司字段必须来自工作经历，不得来自学校** — 解析猎聘卡片时先识别 `公司 · 职位 时间` 的工作经历结构，再回退公司后缀匹配。教育经历里的 `江苏科技大学`、`陕西科技大学`、`太原科技大学` 等不能被截成 `江苏科技` / `陕西科技` / `太原科技` 作为现公司；若 `raw_json` 中出现 `候选公司 + 大学/学院/学校`，必须回看工作经历节点并修正 `candidates.company`、`people.current_company`、`candidate_profiles.candidate_company`、`candidate_intelligence.candidate_company` 和相关事件证据。
21c. **批量触达后必须跑一致性审计** — 每批 `job_chat_verified` / `job_recommended_verified` 后，检查成功触达记录是否都有匹配的 `candidates`、`candidate_profiles`、`candidate_intelligence`，且 `candidate_clients.position_tag` 是当前岗位。任何缺失都要先补库，再向用户汇报完成。
22. **A/B 自动打招呼默认开启** — 用户让跑岗位/找人选时，A级和B级默认自动打招呼，除非用户明确说只搜不发。C级只存档不触达。
23. **触达不能用旧链接** — 历史 CSV/报告里的简历链接可能已经失效，甚至打开后显示「简历编号异常」。发送前必须用当前猎聘会话重新打开卡片或重新捕获完整详情 URL。
24. **不要清洗猎聘详情 URL 参数** — `ck_id`、`sk_id`、`fk_id`、`sss`、`searchMark`、`pgRef` 等参数可能是发送动作所需上下文。为美化链接而删参数，会导致页面可打开但无法真实沟通/推荐。苏科思资深机械工程师成功日志使用的是完整搜索上下文链接；PQE 误报日志使用过只剩 `res_id_encode` 的短链接，这是高风险形态。
25. **`/json/new` 必须完整编码 URL** — 如果把带 `&` 的猎聘 URL 直接拼进 `http://127.0.0.1:9222/json/new?...`，DevTools 会把后续参数当成本地接口参数截掉。必须 `quote(url, safe="")`。
26. **成功必须验证** — 自动打招呼/岗位推荐脚本不能以「点击成功」作为最终结果。没有目标岗位证据、页面成功提示、会话记录或按钮状态变化，只能记为 `clicked_unverified`。
27. **岗位下拉框要滚动查找** — 开聊或推荐弹窗首屏看不到目标岗位很常见。必须滚动/搜索列表；若连续出现职位列表为空，停止批量发送并汇报，不要继续制造假阳性。
28. **推荐职位不是首次触达主路径** — 如果候选人详情页有「立即沟通」，必须先走「立即沟通 → 开聊职位 → 立即开聊」。不要因为页面有「推荐职位」就先点推荐职位；推荐职位只用于已是「继续沟通」的候选人兜底。推荐职位成功只能标记为 `job_recommended_verified`，带岗位开聊成功只能标记为 `job_chat_verified`。
29. **「继续沟通」不是岗位触达成功** — 候选人已沟通过时，按钮会变成「继续沟通」，这只能证明有历史会话。必须再验证职聊窗/推荐记录里有目标岗位，或重新用「推荐职位」发送目标岗位；否则只能记为 `existing_chat_no_job_verified`，不要误报岗位触达成功。
30. **处理完要关详情页** — 批量沟通时必须关掉已处理候选人的详情页，或只复用一个工作标签并在脚本结束自动关闭。不要留下大量 `showresumedetail` 标签页，否则旧页面会干扰按钮状态、聊天窗和成功校验。
31. **半导体 TME/FAE 岗不能只看对标公司** — 触达前必须核验产品线、应用市场、客户场景。示例：对标公司市场经理若实际负责 MCU 汽车应用，不能评为 PC 三次电源 TME A级；应记为 C / `contacted_but_misaligned`（若历史已触达）或 `needs_product_line_verification`（若证据不足）。
31. **职聊页漂移到找简历页** — 这是已出现过的真实问题：脚本持有的页面对象可能从 `职聊` 跳到 `找简历/search/getConditionItem`，导致找不到发送按钮或误读其他页。二次跟进必须用 `scripts/liepin_im_followup.py` 的锁页流程；绝不能在当前 URL 不是 `/im/showmsgnewpage` 时寻找或点击 `im-ui-basic-send-btn`。

## Cookie 过期恢复流程

```bash
# 1. 检查登录态
python3 "$CDP_CLIENT" "$WS" "Page.navigate" '{"url":"https://h.liepin.com/"}'
sleep 3
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"window.location.href","returnByValue":true}'
# 若输出包含 /account/login → 已过期，进入步骤 2

# 2. 请用户手动登录（用 clarify 工具）
# 用户确认后，验证：
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"window.location.href.indexOf(\"login\")===-1?\"LOGGED_IN\":\"STILL_LOGIN\"\",\"returnByValue\":true}'

# 3. 保存新 Cookie
python3 "$CDP_CLIENT" "$WS" "Network.getCookies" '{"urls":["https://h.liepin.com","https://www.liepin.com"]}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);cookies=d['result']['cookies'];open('$HOME/.hermes/cache/liepin_cookies.json','w').write(json.dumps(cookies,indent=2,ensure_ascii=False));print(f'Saved {len(cookies)} cookies')"
```

## Step 7: 简历导出 .docx

直接从猎聘简历详情页导出排版好的 .docx 文件到 `~/Desktop/客户项目/`。

### 7a: 注入导出按钮（持久化）⭐

推荐用 `Page.addScriptToEvaluateOnNewDocument` 持久注入——切换页面不丢失。**先删旧按钮，再注入新脚本**：

```bash
# 删旧按钮
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"var b=document.getElementById(\"__rs_btn\");if(b)b.remove();\"ok\"","returnByValue":true}'

# 持久注入（所有猎聘页自动出现）
SRC=$(python3 -c "import json;print(json.dumps('''(function(){if(document.getElementById(\"__rs_btn\"))return;var btn=document.createElement(\"div\");btn.id=\"__rs_btn\";btn.textContent=\"📄 导出docx\";btn.style.cssText=\"position:fixed;bottom:80px;right:20px;z-index:999999;background:#1a478a;color:#fff;padding:10px 20px;border-radius:8px;cursor:pointer;font:bold 14px system-ui;box-shadow:0 2px 12px rgba(0,0,0,.3);\";btn.onclick=function(){btn.textContent=\"⏳\";btn.style.background=\"#f59e0b\";window.__rs_data=JSON.stringify({raw_text:(document.body.innerText||\"\").substring(0,15000),url:window.location.href});btn.textContent=\"📤 已采集\";btn.style.background=\"#22c55e\";setTimeout(function(){btn.textContent=\"📄 导出docx\";btn.style.background=\"#1a478a\"},2000)};document.body.appendChild(btn)})()'''))")
python3 "$CDP_CLIENT" "$WS" "Page.addScriptToEvaluateOnNewDocument" "{\"source\": $SRC}"
```

**注意**：`addScriptToEvaluateOnNewDocument` 只在当前标签页持久——新标签页需要重新注入。

### 7b: 采集 + 生成 .docx

```bash
# 点击按钮采集数据
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"document.getElementById(\"__rs_btn\").click()","returnByValue":true}'
sleep 2

# 读取数据并生成 .docx
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"window.__rs_data","returnByValue":true}' > /tmp/rs_data.json
python3 ~/.hermes/scripts/generate_resume_docx.py
```

**前提**：`pip install python-docx`

**脚本**：`~/.hermes/scripts/generate_resume_docx.py` — 解析猎聘简历文本、提取结构化字段、python-docx 排版输出。

**输出**：`~/Desktop/客户项目/resume_{姓名}_{时间}.docx`，含姓名/年龄/城市/学历/工作经历/教育经历。

### 7c: 备选——Blob 直下载（CDP 被 DevTools 阻塞时）

当 DevTools 附着导致 `Runtime.evaluate` 超时，用 Blob 下载兜底：

```bash
python3 "$CDP_CLIENT" "$WS" "Runtime.evaluate" '{"expression":"(function(){var h=document.documentElement.outerHTML;var b=new Blob([h],{type:\"text/html\"});var a=document.createElement(\"a\");a.href=URL.createObjectURL(b);a.download=\"resume_\"+Date.now()+\".html\";a.click()})()","returnByValue":true}'
```

文件下载到 `~/Downloads/`。
