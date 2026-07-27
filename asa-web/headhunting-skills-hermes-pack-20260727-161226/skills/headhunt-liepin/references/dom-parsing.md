# 猎聘卡片 DOM 结构化解析

## 文本节点成对提取法

猎聘搜索结果卡片的文本节点（`textContent`）按以下规则成对出现：

```
描述节点              日期节点
──────────────────────────────────────────
公司 · 职位            2024.01-至今(2年5个月)
学校 · 专业 · 学位 · 统招  2016.07-2020.06(4年)
```

### 提取代码

```javascript
let nodes = [];
card.querySelectorAll('*').forEach(el => {
    if (el.children.length === 0) {
        let t = el.textContent.trim();
        if (t.length > 5 && t.length < 200) nodes.push(t);
    }
});

let work = [], edu = [];
for (let i = 0; i < nodes.length - 1; i++) {
    let desc = nodes[i], ds = nodes[i+1];
    let dm = ds.match(/(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2}|至今)/);
    if (!dm) continue;
    
    if (desc.includes('统招') || desc.includes('非统招')) {
        // 教育: 学校 · 专业 · 学位 · 类型
        let parts = desc.split('·').map(p => p.trim());
        if (parts.length >= 3) {
            edu.push({ school: parts[0], major: parts[1], degree: parts[2], 
                      type: parts[3]||'', start: dm[1], end: dm[2] });
        }
    } else {
        // 工作: 公司 · 职位
        let idx = desc.indexOf('·');
        let company = idx > 0 ? desc.substring(0, idx).trim() : desc;
        let title = idx > 0 ? desc.substring(idx+1).trim() : '';
        work.push({ company, title, start: dm[1], end: dm[2] });
    }
    i++;  // 跳过日期节点
}
```

### 基本信息提取

从卡片 `textContent` 中用关键词定位：

```javascript
let m = text.match(/活跃(.+?)阅(\d+)岁工作(\d+)年(博士|硕士|本科|大专)(.+?)求职期望：(.+?)(工业|质量|半导|工艺|生产)/);
// → name, age, years, degree, city, want_city
```

## 关键点

- 不依赖正则硬啃全文，用 DOM 文本节点成对解析
- 姓名从 `.new-resume-personal-name em` 选择器准确提取
- res_id 从 `window.open` 拦截获取（不用 checkbox value）
