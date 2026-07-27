# E简历 Chrome 扩展逆向分析

本 session 分析了「E简历」(bffcfnaphpneefffmnfmmhpaefoboabh_main) Chrome 扩展的工作机制。

## 扩展概述
- 名称：E简历
- 描述：璞心招聘系统简历管理插件，支持招聘和社交网站简历一键导入
- 权限：`storage`
- 公司目标服务器：`http://headhunt.x-saas.com.cn`（通过 `chrome.storage.sync` 的 `pxip` key 配置）

## 数据流
```
猎聘简历页
  → content/content.js 抓取页面 HTML + 截图（html2canvas）
  → chrome.runtime.connect({name: "contentToBg"}) 发送到 background.js
  → background.js fetch POST → ${HOST}/handler.aspx
  → 公司数据库系统
```

## 关键源码位置
- `background/background.js`: 核心逻辑，包含 HOST 配置、数据上传、重名检测
- `content/content.js`: 页面注入脚本，负责抓取简历 HTML、生成截图、显示浮窗 UI
- `content/html2canvas-helper.js`: 页面截图辅助
- `static/js/html2canvas.min.js`: 截图库

## 核心 API 端点
- `POST ${HOST}/handler.aspx` — 上传简历 HTML 文件
- `GET ${HOST}/handler.aspx?action=getsystem` — 获取系统配置
- `POST ${oSystem.resumeapi}/api/resume/browseresume` — 更新候选人
- `GET http://cdn.fplusats.com/plugin/resume_url.json` — 获取支持的简历网站列表

## 拦截方案（可行）
可以通过 CDP `Fetch.enable` 拦截发往 `/handler.aspx` 的请求，复制一份简历 HTML 数据解析存入本地 SQLite 人才库。这样点击"一键导入"时，简历**同时进公司系统和我们自己的本地库**。

实现步骤：
1. `Fetch.enable` 开启请求拦截
2. 过滤 `*handler.aspx*` 的 POST 请求
3. 读取请求 body（简历 HTML）
4. 解析 HTML 提取结构化字段（姓名/公司/职位/学历/经历）
5. 写入 `~/.hermes/talent_pool.db`

待实现。当前仅完成源码分析，拦截逻辑未编码。

## 存储配置
扩展通过 `chrome.storage.sync` 存储：
- `pxip`: 公司服务器地址
- `eresumemin`: 是否最小化浮窗
