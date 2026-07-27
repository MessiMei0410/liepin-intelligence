# 猎聘 DOM 文本节点成对提取法

## 核心发现

猎聘搜索结果卡片 DOM 中，文本节点成对出现：`[描述, 日期范围]`。

## 提取方式

```javascript
let textNodes = [];
card.querySelectorAll('*').forEach(el => {
    if (el.children.length === 0 && el.textContent.trim()) {
        let txt = el.textContent.trim();
        if (txt.length > 5 && txt.length < 200) textNodes.push(txt);
    }
});
```

## 示例输出

```
["深圳市鹏新旭技术有限公司 · 生产企划技术专家", "2024.01-至今(2年5个月)",    ← 工作
 "台亚半导体股份有限公司 · 中央生产企划副处长", "2022.10-2023.10(1年)",      ← 工作
 "采钰科技股份有限公司 · 工业工程部经理", "2021.01-2022.08(1年7个月)",        ← 工作
 "台湾国立成功大学 · 工业与资讯管理 · 硕士 · 统招", "2004.09-2006.06(2年)"]  ← 教育
```

## 解析规则

| 条件 | 类型 | 拆分方式 |
|------|------|---------|
| 含 `统招`/`非统招` | 教育 | 学校 · 专业 · 学位 · 类型 |
| 不含 | 工作 | 公司 · 职位 |

每个描述从下一个文本节点取日期范围：`YYYY.MM-YYYY.MM(时长)`

## 提取字段

```
姓名, 年龄, 工作年限, 学历, 目前城市, 期望城市, 期望职位
技能: 从基本信息文本中提取
工作: [{公司, 职位, 开始, 结束, 时长}]
教育: [{学校, 专业, 学位, 类型, 开始, 结束, 时长}]
```

## res_id 提取

不能用 checkbox value（每次变）。必须通过 window.open 拦截：

```javascript
window.__url=null;
var o=window.open;
window.open=function(u){window.__url=u; return o.apply(this,arguments)};
card.querySelector('img').click();
// 从 window.__url 提取 ?res_id_encode=xxx 参数
```

## Pitfalls

- res_id 末尾几位会话级变化，每次搜索都要重新提取
- res_id 不能存入数据库——每次生成 HTML 报告时现场抓取
- 文本节点可能包含期望职位（如 `工业工程师(IE)`），需要通过位置区分
