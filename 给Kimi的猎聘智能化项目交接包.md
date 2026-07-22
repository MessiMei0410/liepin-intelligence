# 给 Kimi 的猎聘智能化项目交接包

以下内容可以直接复制给 Kimi 继续跑。

## 项目目标

把猎聘智能化项目做成一个可日常驱动的闭环系统：

- SQLite 作为执行缓存和兼容层
- 私密 Obsidian Vault 作为结构化主数据库
- 公开 Obsidian Vault 只保留脱敏复盘和方法论
- 一键刷新后自动产出驾驶舱、体检、分流和复盘

## 当前进度

- 私密主库已建好：`/Users/messi/Documents/Obsidian Liepin Private Vault`
- 公开知识库保留：`/Users/messi/Documents/Obsidian Vault`
- 主刷新链路已接好：
  - 重算候选人智能画像
  - 同步私密 Obsidian 主库
  - 体检私密主库
  - 分流待办项目归属
- 最近一次验证通过：
  - 私密主库健康分：`82/100`
  - 候选人：`607`
  - 候选人画像：`607`
  - 候选人人岗匹配：`668`
  - 回复：`84`
  - 打开待办：`84`
  - 触达：`216`
  - 搜索实验：`79`

## 关键目录

- 项目根目录：`/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence`
- 脚本目录：`/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts`
- 输出目录：`/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/outputs`
- 私密主库：`/Users/messi/Documents/Obsidian Liepin Private Vault`
- 公共知识库：`/Users/messi/Documents/Obsidian Vault`
- 主数据库：`/Users/messi/.hermes/talent_pool.db`

## 主要脚本

- `scripts/refresh_liepin_intelligence.py`
- `scripts/generate_candidate_intelligence.py`
- `scripts/sync_obsidian_private_vault.py`
- `scripts/audit_obsidian_private_vault.py`
- `scripts/triage_followup_assignments.py`
- `scripts/confirm_project_assignment.py`
- `scripts/generate_search_experiment_report.py`
- `scripts/generate_reply_dashboard.py`
- `scripts/generate_today_priority_board.py`
- `scripts/generate_position_dashboard.py`
- `scripts/generate_workflow_status_report.py`
- `scripts/generate_next_search_strategy.py`
- `scripts/generate_wakeup_opportunities.py`

## 现在的库边界

- 私密库可以放实名候选人结构化资料
- 不复制聊天全文、简历正文、联系方式全文
- API key、token、Cookie、账号密码、代理订阅 URL 不进入任何 Obsidian 库
- 公共知识库只放脱敏结论、打法、复盘

## 最近新增能力

- 私密 Obsidian 主库同步
- 私密主库数据质量体检
- 待办归属分流
- 候选人基础画像批量补齐
- 一键刷新最后自动串起上面三步

## 重要状态结论

- 人岗匹配缺口已补齐
- 现在主要剩余问题是待办归属需要进一步人工确认
- 待办分流结果：
  - 可安全确认/快速复核：`11`
  - 有候选项目但需人工选择：`15`
  - 必须人工补信息：`58`

## 可直接执行的命令

```bash
cd /Users/messi/Documents/Codex/2026-06-18/liepin-intelligence
python3 scripts/refresh_liepin_intelligence.py --skip-samples --skip-outreach
python3 scripts/audit_obsidian_private_vault.py
python3 scripts/triage_followup_assignments.py
```

## 给 Kimi 的任务说明

请继续推进猎聘智能化项目，优先做这三件事：

1. 处理待办归属分流里的 `11` 个可安全确认/快速复核项，必要时人工复核后再写回 SQLite。
2. 继续提升 15 个“有候选项目但需人工选择”的待办质量，尽量把它们补到明确客户/岗位。
3. 保持一键刷新链路稳定，每次改动后重新跑：
   - `scripts/refresh_liepin_intelligence.py --skip-samples --skip-outreach`
   - `scripts/audit_obsidian_private_vault.py`
   - `scripts/triage_followup_assignments.py`

工作要求：

- 不要把任何敏感信息写进公共知识库
- 私密主库继续作为结构化主数据库
- 新增规则、体检结果、分流结果优先落到私密 Obsidian
- 发现不确定的项目归属时，不要硬改，先保留到人工确认清单

## 最新可参考文件

- [一键刷新记录](/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/outputs/猎聘智能一键刷新记录_20260625_092605.md)
- [私密主库体检](/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/outputs/猎聘私密主库数据质量体检_20260625_092605.md)
- [待办归属分流](/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/outputs/猎聘待办归属分流_20260625_092605.md)
- [私密主库同步](/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/outputs/猎聘Obsidian私密主库同步_20260625_092604.md)

## 备注

- 如果 Kimi 需要更细的项目文件清单，可以继续补一份“按文件作用分类的索引表”
- 如果你要，我也可以下一步把这份交接包压缩成一段更适合直接粘贴给 Kimi 的短提示词
