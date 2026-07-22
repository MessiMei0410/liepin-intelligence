# A 系统便携版

- 涉及 A 系统、v3 统一库、岗位、人选进度、猎聘或 X-SaaS 插件时，先运行 `bin/a_system_startup.py`。
- v3 数据库由 `config/a-system.env` 中的 `A_SYSTEM_DB` 指定，是唯一事实源。
- 保持四个可见主入口：总览、岗位看板、人选进度、人选列表。
- 手工停止、H5、stop、screen_rejected、rejected 必须视为淘汰/关闭，不得重新计入待跟进。
- 修改扩展后必须升级对应 `manifest.json` 版本，重载扩展并验证标题版本。
- 修改生成器或数据后，运行 `bin/sync.sh --no-open` 和 `bin/doctor.sh`。
- 不提交数据库、候选人简历、联系方式、Cookie、Chrome Profile、密钥或 Cognee 数据。

