# E简历 Chrome 插件架构分析

> 来源: 用户提供的 `.crx` 文件分析 | 日期: 2026-06-02

## 插件概况

| 项目 | 内容 |
|------|------|
| 名称 | E简历 — 璞心招聘系统简历管理插件 |
| 版本 | v2.0.4 |
| Manifest | V3 |
| 权限 | `storage`, `<all_urls>` |
| 匹配 | 所有 URL（content script 注入所有页面） |

## 架构

```
招聘网站页面 → content.js (React UI注入 + HTML捕获)
                    ↓
            background.js (Service Worker)
                    ↓
           HOST/handler.aspx (璞心后端ASP.NET)
```

## 核心组件

### 1. URL 匹配 (`background.js::getResumeUrl()`)

从 CDN 动态拉取支持的简历网站 URL 正则列表：
```
http://cdn.fplusats.com/plugin/resume_url.json
```

返回格式：字符串数组，每个元素是一个 URL 正则（如猎聘、BOSS直聘等）。

### 2. 页面检测 (`content.js → background.js`)

Content script 向 Service Worker 发送 `judgeInjection` 消息，携带当前页 URL。
Worker 用正则匹配 `arrResumeUrL` 判断是否是可抓取的简历页。

### 3. HTML 捕获

两种模式：
- **全量 HTML**: `document.body` 内容，strip `<script>` 标签后提交
- **截图模式**: `chrome.tabs.captureVisibleTab` + Canvas 裁剪，或 html2canvas 全页渲染

content.js 依赖:
- `static/js/html2canvas.min.js`
- `static/js/jquery-3.7.1.min.js`

### 4. 服务端提交

所有操作通过 `{HOST}/handler.aspx` 统一端点：

| 用途 | mode | action |
|------|------|--------|
| 登录/Token | 0 | 41 |
| 查重 | 0 | -42 |
| 创建简历 | 35 | -42 |
| 自动抓取 | — | `browseresume` API |

认证：Token 池机制，每次请求消耗一个 token，2小时过期后自动刷新。

### 5. 查重机制

提交前先查重（`judgeRepeat`），如已存在则返回已有候选人信息，避免重复入库。

## 对我们工作流的启示

1. **URL 匹配表有价值**：`resume_url.json` 包含猎聘等网站的简历页正则，可用于定位简历页
2. **HTML 全量抓取 → 服务端解析** 的模式可以在本地复现：用 CDP `Runtime.evaluate` 获取 `document.documentElement.outerHTML`，用本地 Python 解析
3. **截图能力**：html2canvas 全页渲染可作为 `Page.captureScreenshot` 的补充
4. **不能直接使用**：该插件需要璞心后端（HOST 配置），无独立使用价值。但架构模式值得借鉴

## 插件文件清单

```
manifest.json          — Chrome Extension V3 配置
background/
  background.js        — Service Worker: API通信、认证、查重
content/
  content.js           — React UI注入 + HTML捕获（webpack打包，~170KB minified）
  content.css          — 注入UI样式
  html2canvas-helper.js
  screenshot-helper.js
options/               — 配置页面（设置HOST地址等）
static/
  js/html2canvas.min.js
  js/jquery-3.7.1.min.js
  js/jquery-1.9.1.min.js
  img/favicon.png
```
