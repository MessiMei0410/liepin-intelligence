---
name: headhunting-workflow
description: 猎头全流程主控——从需求分析到入职跟进的完整生命周期，自动路由到对应子技能。
version: 1.1.0
category: productivity
---

# 猎头全流程工作流

猎头顾问的端到端操作主控。用户说出当前阶段或需求，自动路由到对应子技能执行。

## 岗位库优先硬规则

凡是涉及岗位、岗位方向、岗位统计、候选人归属、触达岗位或复盘口径，必须先读取本地岗位库 `~/.hermes/talent_pool.db`：

```sql
SELECT * FROM positions WHERE client = ? AND status = 'open';
SELECT * FROM position_profiles WHERE client = ?;
```

- `positions.title` / `position_profiles.position` 是当前标准岗位名和方向口径。
- `position_profiles.source_position_ids_json` 绑定 `positions.id`，用于确认标准岗位来源。
- `candidates.position`、`candidate_clients.position_tag` 只能作为历史归属或执行记录；如果与岗位库不一致，先做归一化审计，不得直接用候选表岗位名定义当前岗位方向。
- 多岗位客户必须按岗位库逐岗推进；不得把旧合并岗位名（例如“PC/服务器”）当成当前标准岗位。

## 触发条件

- "猎头工作流"、"猎头流程"、"这个项目到哪了"
- "接了个新单"、"开始寻访"、"候选人反馈了"、"谈薪了"、"发offer"
- 任何涉及猎头全流程的阶段切换

## 八阶段流水线

```
📋 需求分析 → 🔍 寻访搜索 → 📊 推荐报告 → 🎤 面试跟进 → 💰 谈薪 → 🧭 决策辅导 → 📝 Offer → ✅ 入职跟进 → 📚 知识沉淀
    ↓               ↓              ↓             ↓            ↓          ↓            ↓            ↓              ↓
headhunting-    liepin-cdp-    jiashi-       (跟踪)     salary-     candidate-  (确认)     (跟踪)       knowledge-
search-strategy search         recommendation           negotiation decision-                        base-save
                talent-pool    -report                  feedback    coaching                          headhunt-note-
                                                                                                      generator
```

**关键分界**：谈薪（阶段 5）= 双方协商过程；决策辅导（阶段 6）= 候选人有顾虑要放弃时的辅导干预；Offer（阶段 7）= 薪资敲定后走审批/发书面offer。

---

## 阶段路由

### 📋 阶段 1：需求分析

**触发词**：新单、接了个职位、帮我分析这个JD、出个寻访策略

**路由** → `headhunting-search-strategy`

**输出物**：`~/Desktop/客户项目/{客户}/{客户}_{职位}_寻访策略_{日期}.md` + `.docx`

---

### 🔍 阶段 2：寻访搜索

**触发词**：开始搜、找人、继续找、再搜一轮

**路由** → `liepin-cdp-search` + `talent-pool` + `resume-local-save`

**输出物**：候选人卡片 HTML + 人才库入库 + 简历本地存档

---

### 📊 阶段 3：推荐报告

**触发词**：出报告、生成推荐报告、发给客户

**路由** → `jiashi-recommendation-report`

**输出物**：嘉驰国际格式 .doc 推荐报告

---

### 🎤 阶段 4：面试跟进

**触发词**：客户反馈了、面试安排了、面试反馈、终面了、终面反馈

**子状态**：初面 → 技术面 → **终面** → 等结果

**动作**：
1. 候选人进入终面 → 创建 `~/Desktop/人才库/{姓名}_终面跟进.md`
2. 更新 `talent-pool` 状态
3. 记录淘汰原因
4. 输出候选人状态变更摘要

**结束条件**：终面通过 → 进入阶段 5（谈薪）

---

### 💰 阶段 5：谈薪

**触发词**：谈薪了、薪资谈判、人选反馈（关于薪资/待遇/顾虑）、候选人回复

**路由** → `salary-negotiation-feedback`

**输出物**：`~/Desktop/人才库/{姓名}_谈薪反馈.md`

**包含内容**：
- 薪资方案协商过程
- 竞品offer对比
- 候选人核心顾虑（距离/平台/发展）
- 下一步：等答复 or 调整方案

**结束条件**：双方薪资达成一致 → 进入阶段 6

---

### 🧭 阶段 6：候选人决策辅导

**触发词**：候选人犹豫了、想放弃、距离太远、家里不同意、挽留了、怎么说服、该不该去

**参考** → `references/candidate-decision-coaching.md`

**适用场景**：候选人收到 offer 后因非薪资因素（距离/家庭/平台顾虑/原公司挽留）犹豫或要放弃

**核心方法**：三层追问法 + 后悔测试（详见参考文件）

**输出物**（可选）：`~/Desktop/客户项目/{客户}/{姓名}_辅导方案.docx`

**结束条件**：候选人做出决定 → 接受则进入阶段 7，放弃则归档并更新 talent-pool

---

### 📝 阶段 7：Offer

**触发词**：发offer、走审批、薪资敲定了、人选答应了、出offer

**与阶段 5/6 的区别**：谈薪 = 还在协商，决策辅导 = 消除顾虑，Offer = 已谈拢，走正式流程

**动作**：
1. 确认最终薪资包（base + 奖金 + 股票 + 签字费）
2. 确认入职时间
3. 更新 `talent-pool` 状态 → `offered`
4. 生成 `~/Desktop/人才库/{姓名}_Offer确认.md`

**输出物**：`~/Desktop/人才库/{姓名}_Offer确认.md`

---

### ✅ 阶段 8：入职跟进

**触发词**：入职了、确定入职、背调、onboard

**动作**：
1. 更新 `talent-pool` 状态 → `hired`
2. 记录实际入职时间
3. 生成入职确认文档

**输出物**：`~/Desktop/人才库/{姓名}_入职确认.md`

---

### 📚 阶段 9：知识沉淀

**触发词**：整理知识点、行业洞察

**路由** → `knowledge-base-save`（概念问答 → .docx + Obsidian .md）

**触发词**：发小红书、写猎头笔记

**路由** → `headhunt-note-generator`（笔记 + 配图 → 待发/）

---

## 跨阶段工具

| 需求 | 路由 |
|------|------|
| 多渠道搜 {客户} | `multi-channel-search`（自动读 DB 获取岗位列表 → X-SaaS 全量 + 猎聘逐岗搜 → 分类入库） |
| 导出简历 docx | `resume-docx-export` |
| 查看人才库 | `talent-pool`（query 模式） |
| 猎聘搜索 | `liepin-cdp-search` |

### 多渠道搜索触发

| 用户说 | 动作 |
|--------|------|
| "多渠道搜 鹏新旭" | 读 positions 表 → 获取该客户所有在招岗位 → X-SaaS全量搜公司 → 猎聘逐岗位搜 → 分类入库 |
| "多渠道寻访 {客户名}" | 同上 |
| "搜一下 {客户名}" | 同上 |

**注意区分两种搜索模式：**
- **Fab客户**（鹏新旭）：X-SaaS搜"谁在这个公司工作" ✅ 有效
- **设备商客户**（微导纳米）：X-SaaS搜竞品公司无效 ❌ — 应走猎聘/LinkedIn/脉脉定向挖猎

### DB读岗批量建策略

| 用户说 | 动作 |
|--------|------|
| "DB读岗，生成寻访策略" | 读 positions 表 → 并行 sub-agent 逐岗位生成策略.docx → 存入客户/寻访策略/ |
| "批量出策略" | 同上 |

紧急岗位出完整 .docx，次要岗位出简要 .md。策略包含：岗位理解、候选人画像、目标公司Mapping(Tier 1/2/3)、搜索关键词、沟通话术、执行计划。

---

## 阶段识别规则

| 信号 | 阶段 |
|------|:--:|
| 有 JD 没策略 | 1 |
| 有策略没执行 | 2 |
| 搜完了没出报告 | 3 |
| 推了人等反馈（面试中） | 4 |
| 终面环节 | 4 |
| 薪资协商中（还没谈拢） | 5 |
| 候选人有顾虑要放弃 | 6 |
| 薪资谈拢，等offer/走审批 | 7 |
| 确认入职 | 8 |
| 项目结束复盘 | 9 |

不确定时直接问："当前到哪个阶段了？薪资谈拢了吗？"

---

## 桌面浮窗

`~/.hermes/scripts/headhunt_workflow_widget.py` — 桌面常驻浮窗，显示：
- 按客户分组的岗位管线
- 每个岗位的当前阶段徽章（🔍 寻访 / 🎤 终面 / 💰 谈薪 / 📝 Offer / ✅ 入职）
- 候选人数量
- 已实现技能清单

**功能**：
- 点击岗位行 → 打开对应反馈文件或客户项目目录
- ↻ 手动刷新按钮
- 每 30 秒自动轮询 `~/Desktop/人才库/`，检测到文件变化自动更新阶段

**开机自启**：`~/Library/LaunchAgents/ai.hermes.headhunt-workflow.plist`

浮窗从人才库 SQLite 读取岗位列表，从 `~/Desktop/人才库/*.md` 文件推断当前阶段（文件名含「谈薪反馈」「终面」「Offer确认」「入职」等关键词）。

```
~/Desktop/客户项目/{客户}/
├── {客户}_{职位}_寻访策略_{日期}.md
├── {客户}_{职位}_寻访策略_{日期}.docx
├── 候选人链接_点击跳转.html
├── 推荐报告_{职位}_{日期}.doc

~/Desktop/人才库/
├── {姓名}_终面跟进.md      ← 阶段 4（终面环节）
├── {姓名}_谈薪反馈.md      ← 阶段 5（需含"目标岗位"字段供浮窗匹配）
├── {姓名}_辅导方案.docx    ← 阶段 6（候选人有顾虑时生成）
├── {姓名}_Offer确认.md     ← 阶段 7
├── {姓名}_入职确认.md      ← 阶段 8

~/Desktop/talent_pool.db          ← SQLite
~/Desktop/知识库/                   ← 行业知识 .docx
~/Desktop/知识库_Obsidian/          ← 行业知识 .md
```

## 桌面浮窗

`scripts/headhunt_workflow_widget.py` — macOS 桌面常驻浮窗，自动从 talent-pool DB + 人才库反馈文件聚合展示：

**展示内容**：
- 按客户分组的岗位管线（🔍寻访 / 🎤终面 / 💰谈薪 / 📝Offer / ✅入职）
- 每个岗位的候选人数量和当前阶段
- 已实现技能清单（7/7）

**三个交互功能**：
| 功能 | 方式 | 效果 |
|------|------|------|
| 👆 点击跳转 | 点击岗位行 | 有反馈文件→打开.md；寻访阶段→打开Finder目录 |
| ↻ 手动刷新 | 标题栏 ↻ 按钮 | 立即重建UI，读取最新数据 |
| 🕐 自动推进 | 每30秒轮询 | 检测 ~/Desktop/人才库/ 文件变化，阶段自动更新 |

**阶段识别**：通过解析反馈文件中的 `目标岗位` 字段精确匹配，不依赖 DB。

**反馈文件命名规范**（供浮窗自动识别）：
- `{姓名}_终面跟进.md` → 🎤 终面
- `{姓名}_谈薪反馈.md` → 💰 谈薪
- `{姓名}_Offer确认.md` → 📝 Offer
- `{姓名}_入职确认.md` → ✅ 入职

自动注册为 LaunchAgent（`ai.hermes.headhunt-workflow`）开机自启。

### 🔄 App 刷新（"App 没更新" / "刷新 App"）

客户端 App（一站式寻访猎头工作站.app）是 **原生 Swift AppKit 应用**（非 WKWebView），通过 `DataManager` 直读 `talent_pool.db`。

**自动刷新**：`BrowseViewController` 内置 `DispatchSourceFileSystemObject` 文件监听——DB 写入后自动触发 `reloadData()`，无需手动操作。刷新时会保留当前选中的客户、岗位和搜索关键词。

**手动刷新**：`Cmd+R` 或菜单「视图 → 刷新」调用 `browseVC.reloadData()`，同样保留选中状态。

**不再需要 pkill**：文件监听 + Cmd+R 已完全替代旧的 pkill+reopen 模式。详见 `references/app-refresh.md`。
