---
name: a-system-workbench
description: Use when working on the portable A 系统 recruiting workbench, v3 database, Liepin helper, X-SaaS helper, jobs, candidates, outreach, audit, or sync.
---

# A 系统便携工作台

先运行：

```bash
"$HOME/A-System/bin/a_system_startup.py"
```

若安装位置不同，读取当前仓库 `config/a-system.env`。

## 数据边界

- `A_SYSTEM_DB` 是唯一事实源。
- A 系统 HTML 由生成器重建，不手改生成结果。
- 保持四个可见主入口：总览、岗位看板、人选进度、人选列表。
- 手工停止、H5、stop、screen_rejected、rejected 是淘汰/关闭，不得重新计入待跟进。

## 标准命令

```bash
"$HOME/A-System/bin/start.sh"
"$HOME/A-System/bin/sync.sh" --no-open
"$HOME/A-System/bin/doctor.sh"
```

## 插件

- 猎聘扩展：`extensions/liepin-reply-assistant-extension`
- X-SaaS 扩展：`extensions/xsaas-candidate-assistant-extension`
- 两个扩展都只连接 `http://127.0.0.1:8765`。
- 修改扩展后升级 manifest 版本、重载扩展并刷新页面。

## UI 基线

- 人选进度姓名必须能打开精确人选详情。
- 人选详情使用高对比文字和嘉驰式工作/教育/项目时间轴。
- 桌面和移动端不得整页横向溢出或静默裁切。

