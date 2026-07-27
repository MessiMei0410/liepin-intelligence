# 猎聘搜索页 DOM 选择器参考

> 验证日期: 2026-06-02 | 猎聘版本: 当前生产环境 | Chrome 148

## 搜索流程关键选择器

### 搜索框 (主关键词输入)

猎聘使用 Ant Design AutoComplete 组件，选择器可能因页面版本而变化：

| 优先级 | 选择器 | 备注 |
|--------|--------|------|
| 1 (推荐) | `#rc_select_1` | 最稳定，ID 选择器 |
| 2 | `.ant-select-auto-complete.auto-input-wrap-v3 input` | 类选择器，可能版本变化 |
| 3 | `.search-auto-complete-box .ant-select-selection-search-input` | 层级选择器 |

搜索框 placeholder: `搜职位/公司/行业等（中文用空格隔开，英文用逗号隔开）`

**React 受控组件写入方式**（直接用 `.value = 'xxx'` 不生效）：

```javascript
const input = document.querySelector("#rc_select_1");
const nativeSetter = Object.getOwnPropertyDescriptor(
  window.HTMLInputElement.prototype, 'value'
).set;
nativeSetter.call(input, "关键词");
input.dispatchEvent(new Event('input', {bubbles: true}));
input.dispatchEvent(new Event('change', {bubbles: true}));
```

### 搜索按钮

```javascript
document.querySelector(".search-btn")
// 或: button.ant-btn-primary.search-btn
```

### 搜索结果页

| 元素 | 选择器 | 示例值 |
|------|--------|--------|
| 结果总数 | `i[data-nick="totalcnt"]` | `3000+` |
| 候选人列表容器 | `div.result-list.wrap` (#resultList) | — |
| 卡片表格 | `table.new-resume-card` | 在 `.table-box` 内 |
| 单个候选人卡片 | `div.tlog-common-resume-card` | 每行一个 |
| 简历 ID | 卡片内 `input[type=checkbox]` 的 `value` 属性 | `e2622582d5P157fb7990422` |

### 筛选条件

| 筛选维度 | 容器选择器 | 选项选择器 | 选项示例 |
|----------|-----------|-----------|---------|
| 教育经历 | `.sfilter-edu` | `.tag-item` | 不限/本科/硕士/博士 |
| 目前城市 | `.sfilter-city` (第1个) | `.tag-item` | 上海/北京/深圳... |
| 期望城市 | `.sfilter-city` (第2个) | `.tag-item` | 苏州/上海/杭州... |
| 工作年限 | `.sfilter-work-year` | `.tag-item` | 1-3年/3-5年... |
| 当前行业 | `.sfilter-industry` | — | — |

**设置筛选示例（学历硕士+城市上海/苏州）**：

```javascript
// 学历 → 硕士 (tag-item 索引2)
document.querySelector(".sfilter-edu").querySelectorAll(".tag-item")[2].click();

// 城市 → 上海
document.querySelectorAll(".sfilter-city")[0].querySelectorAll(".tag-item")[0].click();

// 期望城市 → 苏州
const desireCity = document.querySelectorAll(".sfilter-city")[1];
Array.from(desireCity.querySelectorAll(".tag-item"))
  .find(el => el.textContent.trim() === "苏州").click();
```

### 关键词类型切换

搜索框左侧的下拉，控制"包含全部关键词"/"包含任一关键词"/"不包含关键词"：

```javascript
// 选择器: .switch-keyword-type (Ant Design Select)
// 默认: 包含全部关键词
```

### 页面结构

```
.search-area
  └── .search-auto-complete-box
        ├── .switch-keyword-type     (关键词类型)
        └── .auto-input-wrap-v3      (主搜索框)
              └── #rc_select_1
  └── .search-btn                    (搜索按钮)

.search-filter
  ├── .sfilter-city                  (目前城市)
  ├── .sfilter-city                  (期望城市)
  ├── .sfilter-work-year             (工作年限)
  ├── .sfilter-edu                   (教育经历)
  └── ...

.resume-spin-box
  └── .result-list.wrap (#resultList)
        ├── .result-list-bar         (工具栏: 全选/批量/排序)
        └── .table-box
              └── table.new-resume-card
                    └── tr (每行 = 一个候选人卡片)
                          └── .tlog-common-resume-card
```

## 已验证的交互模式

### ✅ 可用: JS 填搜索框 + 点按钮搜索
设置 `#rc_select_1` 的值 → 派发 input/change 事件 → 点 `.search-btn`

### ✅ 可用: 直接 URL 导航（搜索结果页）
URL: `https://h.liepin.com/search/getConditionItem#session`
触发搜索后 URL 不变（SPA），但页面内容通过 AJAX 更新

### ❌ 不可用: GET 参数 URL
`https://h.liepin.com/search?key=xxx` → 返回 **404**

### ❌ 不可用: 直接简历页 URL
`https://h.liepin.com/resume/show/?res_id_encode=xxx` → 返回 **404**

### ⚠️ 未验证: 卡片点击打开详情
点击 `.tlog-common-resume-card` 的简单 click 事件未能打开右侧详情面板。可能需要：
- 更精确的点击目标（姓名/图片元素）
- 双击事件
- 通过 `Input.dispatchMouseEvent` CDP 命令发送原生鼠标事件

## 候选人卡片数据格式

每张卡片的 `textContent` 包含以下模式：

```
[活跃状态] 姓** 年龄 工作N年 学历
[当前城市] 求职期望：[期望城市]
[职位标题]
[技能标签...]
[公司名] · [职位] [起止时间]
[学校] · [专业] · [学历] · [统招/非统招] [起止时间]
立即沟通
```

**提取关键字段的正则可从 `textContent` 直接解析**，无需进入详情页。

## Chrome CDP 快速参考

```bash
# 获取 Tab 列表
curl -s http://127.0.0.1:9222/json/list

# 新建 Tab (PUT 方法)
curl -s -X PUT "http://127.0.0.1:9222/json/new?url=https://h.liepin.com"

# 获取 WebSocket URL
curl -s http://127.0.0.1:9222/json/list | python3 -c "
import sys,json
tabs=[t for t in json.load(sys.stdin) if t['url'].startswith('http')]
print(tabs[-1]['webSocketDebuggerUrl'])"

# 执行 CDP 命令
python3 scripts/cdp_client.py "ws://..." "Runtime.evaluate" '{"expression":"...","returnByValue":true}'
```

### 常用 CDP 方法

| 方法 | 用途 |
|------|------|
| `Page.navigate` | 导航到 URL |
| `Runtime.evaluate` | 执行 JS 并返回结果 |
| `Page.captureScreenshot` | 截图 (返回 base64 PNG) |
| `Network.getAllCookies` | 获取所有 cookies |
| `Network.setCookie` | 设置 cookie |
| `DOM.getDocument` | 获取 DOM 树 |
| `Input.dispatchMouseEvent` | 模拟鼠标点击 |
| `Input.dispatchKeyEvent` | 模拟键盘输入 |
