# 猎聘候选人卡片 — DOM 节点文本提取模式

## 问题

正则表达式从卡片文本提取公司/职位/学历信息不可靠——猎聘卡片的 HTML 结构用 CSS 布局，`textContent` 输出顺序不可预测，正则经常匹配空值。

## 解决方案：DOM 叶子节点遍历

遍历卡片所有子元素，取**无子元素的叶子节点**的 `textContent`，按文档顺序保留。这是猎聘卡片实际渲染的文本流。

```javascript
(function() {
  var cards = document.querySelectorAll(".tlog-common-resume-card");
  return JSON.stringify(Array.from(cards).slice(0, 25).map(function(c, i) {
    var cb = c.querySelector("input[type=checkbox]");
    var nameEl = c.querySelector(".new-resume-personal-name em");
    var name = nameEl ? nameEl.textContent.trim() : "?";
    var nodes = [];
    c.querySelectorAll("*").forEach(function(el) {
      if (el.children.length === 0) {
        var t = el.textContent.trim();
        if (t.length > 2 && t.length < 200) nodes.push(t);
      }
    });
    return {num: i+1, name: name, res_id: cb ? cb.value : "??", nodes: nodes.slice(0, 15)};
  }));
})()
```

## 节点顺序示例

一张典型卡片的叶子节点序列：
```
[0] 赵**
[1] 名片简历
[2] 27岁
[3] AMHS                          ← 当前职位标签
[4] 2021.06-至今(5年)              ← 时间区间
[5] 立即沟通
```

更多上下文时（含公司/学历）：
```
[0] 张**
[1] 中芯国际 · 自动化工程师       ← 公司+职位
[2] 四川农业大学 · 电子科学与技术 · 本科 · 统招  ← 学历
[3] 10年以上经验
```

## 提取启发式

从节点数组中匹配：
- **公司**：含「有限公司/科技/半导体/电子/光电」且长度<50的节点
- **职位**：含「工程师/经理/专家/主任/总监」且长度<40的节点
- **学历**：含「硕士/博士/本科/统招」且长度<35的节点
- **经验**：含「年)/年）/至今」且长度<30的节点

注意：第一张卡片通常是简略模式（仅姓名+标签），完整信息需要点击展开或用 detail API。
