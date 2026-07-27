# DOM TextNode 提取 — 猎聘候选人卡片

## 原理

猎聘候选人卡片 (`.tlog-common-resume-card`) 的内部结构是用独立的 `<span>` 包裹每个信息字段：

```html
<div class="tlog-common-resume-card">
  <span>今天活跃</span>
  <span>刘**</span>
  <span>25岁</span>
  <span>工作3年</span>
  <span>本科</span>
  <span>北京</span>
  <span>求职期望：</span>
  <span>上海</span>
  <span>半导体设备工程师</span>
  <span>应用材料(中国)有限公司</span>
  <span> · </span>
  <span>半导体设备工程师</span>
  <span>2023.06-至今</span>
  ...
</div>
```

`document.createTreeWalker(card, NodeFilter.SHOW_TEXT)` 按 DOM 顺序逐节点返回文本，每个 `<span>` 的文本是独立节点，天然分隔。

## Python 提取脚本

```python
def extract_liepin_cards(cdp, max_cards=30):
    """从当前猎聘搜索结果页提取候选人结构化数据"""
    result = cdp.send("Runtime.evaluate", {
        "expression": f"""
        (() => {{
            var cards = document.querySelectorAll('.tlog-common-resume-card');
            var data = [];
            cards.forEach(function(c) {{
                var walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
                var nodes = [];
                while(walker.nextNode()) {{
                    var t = walker.currentNode.textContent.trim();
                    if (t) nodes.push(t);
                }}
                
                // 提取字段
                var name = '', city = '', edu = '', company = '', position = '';
                
                if (nodes.length >= 2 && /^[\u4e00-\u9fa5A-Za-z]{{1,4}}\\*{{2}}$/.test(nodes[1]))
                    name = nodes[1].replace('**', '');
                if (nodes.length >= 8) city = nodes[7];  // 期望城市
                if (nodes.length >= 5) edu = nodes[4];
                
                // 公司名：在全部节点中找 有限公司/科技/半导体 后缀的
                for (var i = 6; i < nodes.length; i++) {{
                    if (/有限公司|科技股份有限公司|半导体|集成电路|集团|Inc|Corp|Ltd/.test(nodes[i])) {{
                        company = nodes[i];
                        // 找紧随其后的职位
                        if (i+1 < nodes.length && nodes[i+1] !== ' · ')
                            position = nodes[i+1];
                        break;
                    }}
                }}
                
                data.push({{name, city, edu, company, position, nodeCount: nodes.length}});
            }});
            return JSON.stringify(data.slice(0, {max_cards}));
        }})()
        """,
        "returnByValue": True
    })
    return json.loads(result['result']['result']['value'])
```

## 反例：文本拼接解析（不可用）

```python
# ❌ 错误：文本拼接后无法可靠拆分
text = card.textContent.trim().replace(/\s+/g, ' ')
# "今天活跃刘**25岁工作3年本科北京求职期望：上海半导体设备工程师应用材料..."
name = re.match(r'^[\u4e00-\u9fa5]{2,3}', text)  # "今天活" ← 错！
```

## 为什么 `textContent` 不可靠

`textContent` 把所有 `<span>` 文本拼成一行，丢失字段边界。状态标签（"今天活跃"/"隐藏活跃状态"/"7天内活跃"）与姓名字段紧邻，正则无可依赖的锚点区分。TextNode 逐节点遍历天然避开了这个陷阱——状态和姓名在不同的 DOM 节点中，不需要正则识别。
