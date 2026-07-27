# Liepin Card DOM Parsing (2026-06-04)

## The Problem

Regex-based parsing of Liepin card text was producing garbage:
- "今天活" extracted as name (from "今天活跃刘**")
- "期望：杭州项目经理..." extracted as company (from 求职期望 section)
- Company names mixed with city/position keywords

## The Fix: DOM TextNode Walker

Instead of regex on `textContent`, walk the DOM tree and extract individual text nodes:

```javascript
var walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
var nodes = [];
while(walker.nextNode()) {
    var t = walker.currentNode.textContent.trim();
    if (t) nodes.push(t);
}
```

### Node Structure

Each Liepin card has text nodes in this order:

```
[0] "今天活跃"          ← status badge (strip)
[1] "刘**"              ← NAME (1-4 chars + **)
[2] "25岁"              ← age
[3] "工作3年"           ← experience
[4] "本科"              ← education
[5] "北京"              ← current city
[6] "求职期望："         ← delimiter
[7] "北京"              ← desired city
[8+] ...                ← position keywords + company name
```

### Extraction Rules

```javascript
// Name: first node matching /^[\u4e00-\u9fa5A-Za-z]{1,4}\*{2}$/
var name = '';
for (var i = 0; i < nodes.length; i++) {
    if (/^[\u4e00-\u9fa5A-Za-z]{1,4}\*{2}$/.test(nodes[i])) {
        name = nodes[i].replace('**', '');
        break;
    }
}

// City: node after "求职期望："
var city = '';
for (var i = 0; i < nodes.length; i++) {
    if (nodes[i] === '求职期望：' && i+1 < nodes.length) {
        city = nodes[i+1];
        break;
    }
}

// Education: exact match
var edu = '';
for (var i = 0; i < nodes.length; i++) {
    if (/^(博士|硕士|本科|大专|MBA)$/.test(nodes[i])) {
        edu = nodes[i];
        break;
    }
}

// Company: regex on full text after "求职期望：" section
// Pattern: known company suffixes before the first "·"
var full = card.textContent.trim().replace(/\s+/g, ' ');
var compMatch = full.match(/([\u4e00-\u9fa5A-Za-z（）()·]+(?:有限公司|有限责任公司|科技股份有限公司|半导体有限公司|微电子|光电|技术有限公司|集团公司|集成电路|股份有限公司|半导体科技|半导体技术|半导体设备|科技集团))\s*·/);
var company = compMatch ? compMatch[1].trim().replace(/^.*[)）]\s*/, '') : '';

// Position: text after first "·" matching known suffixes
var dotIdx = full.indexOf('·');
if (dotIdx > 0) {
    var after = full.substring(dotIdx+1).trim();
    var pm = after.match(/^([\u4e00-\u9fa5A-Za-z/]+(?:工程师|经理|总监|专家|主管|主任|顾问))/);
    if (pm) position = pm[1];
}
```

## Pitfalls

1. **Name masking**: Liepin shows "刘**" not full names. Use `name + '**'` for DB storage.
2. **Company suffix patterns**: Chinese company names end with 有限公司/科技/集团 etc. English names end with Inc/Corp/Ltd/Technology.
3. **"名片简历" pattern**: Some cards start with "刁**名片简历" instead of status badge. Handle both.
4. **Single-char names**: English names like "M**" are valid - allow 1-4 chars.
5. **Stripping time prefixes**: Company extraction may capture "(1年)隆基绿能..." — strip `^.*[)）]\s*`.
