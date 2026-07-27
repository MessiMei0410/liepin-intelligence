# 猎头 Skills Hermes 安装包

这个包用于把本机常用的猎头相关 Codex/Hermes skills 转给同事安装。

## 包内容

- `skills/`：可直接放进同事机器 `~/.codex/skills/` 的 skill 目录。
- `install-for-hermes.sh`：一键安装脚本。默认会把同名旧目录备份到 `~/.codex/skills_backup_时间戳/`，再安装新版本。
- `HERMES_INSTALL.md`：给同事或 Hermes 阅读的安装说明。
- `hermes-skill-pack.manifest.json`：机器可读清单。

## 快速安装

解压后进入包目录：

```bash
cd headhunting-skills-hermes-pack-20260727-161226
./install-for-hermes.sh
```

安装后可让 Hermes 自检：

```bash
/Users/messi/.local/bin/hermes chat -q "请检查 ~/.codex/skills 下是否已安装猎头 skills，并列出可用的 skill 名称。"
```

如果同事的 Hermes 路径不是 `/Users/messi/.local/bin/hermes`，把命令里的路径换成他们本机的 Hermes CLI 路径即可，例如：

```bash
hermes chat -q "请检查 ~/.codex/skills 下是否已安装猎头 skills，并列出可用的 skill 名称。"
```

## 已包含 Skill

- `headhunting-workflow`：猎头全流程主 skill。
- `headhunting-copilot`：猎头 Copilot 辅助。
- `headhunting-search-strategy`：寻访/搜索策略。
- `recruitment-analysis`：招聘分析。
- `candidate-matching-report`：候选人与岗位匹配报告。
- `candidate-salary-report`：候选人薪酬报告。
- `salary-negotiation-feedback`：谈薪反馈。
- `talent-pool`：人才库保存与复用。
- `headhunt-liepin`：猎聘专项操作。
- `liepin-cdp-search`：猎聘 CDP 搜索。
- `liepin-job-publish`：猎聘职位发布。
- `resume-export`：猎聘简历导出。
- `resume-local-save`：简历本地保存。
- `resume-docx-export`：简历导出为 docx。
- `multi-channel-search`：多渠道候选人搜索。
- `headhunt-note-generator`：猎头小红书/外发笔记生成。

## 给同事的建议

安装完成后，可以直接对 Hermes 说：

> 使用 `headhunting-workflow` 帮我规划这个岗位的猎头寻访流程。

或：

> 使用 `candidate-matching-report`，根据这份候选人简历和岗位 JD 生成匹配报告。

Hermes/Codex 会根据 `~/.codex/skills/<skill-name>/SKILL.md` 中的说明加载对应能力。
