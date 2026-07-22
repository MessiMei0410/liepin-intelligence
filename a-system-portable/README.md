# A 系统便携安装包

这套工具把 A 系统、猎聘专业回复助手、X-SaaS 人选推进助手、本机 `8765` 服务和 Codex skill 组装成可迁移包。

A-System Agent V1.5 随本机服务安装，提供候选人证据判断、上下文追问、未发送草稿、主动注意队列、有限批量评估和人工确认任务提案。模型密钥只从 macOS Keychain 或运行环境读取，不包含在迁移包内。

支持：

- Apple Silicon Mac：`arm64`
- Intel Mac：`x86_64`
- macOS 13 及以上
- Python 3.11（最低 3.10）
- Google Chrome

## 安全边界

导出包固定为空系统，不提供携带业务数据的开关。它不包含客户、岗位、人选、简历、联系方式、浏览器 Cookie、Chrome Profile、Cognee 数据或任何密钥，也不包含原机器上的专属匹配规则。同事安装后使用自己的猎聘和 X-SaaS 账号，并在自己的空库中建立客户、岗位和人选。

## 在你的 Mac 上构建迁移包

```bash
cd a-system-portable
python3 build_bundle.py
```

输出位于：

```text
dist/A-System-Portable-YYYYMMDD/
dist/A-System-Portable-YYYYMMDD.zip
```

## 同事安装

```bash
unzip A-System-Portable-YYYYMMDD.zip
cd A-System-Portable-YYYYMMDD
./install.sh
```

默认安装到：

```text
~/A-System
```

指定安装位置：

```bash
./install.sh --root "$HOME/Applications/A-System"
```

安装完成后：

```bash
~/A-System/bin/start.sh
~/A-System/bin/doctor.sh
```

## 安装 Chrome 扩展

打开 `chrome://extensions`，启用“开发者模式”，分别加载：

```text
~/A-System/extensions/liepin-reply-assistant-extension
~/A-System/extensions/xsaas-candidate-assistant-extension
```

未打包扩展在不同电脑上的扩展 ID 可能不同。A 系统便携版不依赖固定扩展 ID；诊断以扩展目录、版本和本机 API 连通性为准。

## Intel 与 Apple Silicon

安装器通过 `uname -m` 检测 `arm64` 或 `x86_64`，不会写死 `/opt/homebrew` 或 `/usr/local`。虚拟环境和原生依赖必须在目标机器重新安装，不能从另一种芯片的 Mac 复制 `.venv`。

## 卸载

默认保留数据库：

```bash
~/A-System/bin/uninstall.sh
```

明确删除全部数据：

```bash
~/A-System/bin/uninstall.sh --purge-data
```
