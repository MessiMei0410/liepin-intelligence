# ASA 界面优化方案与开源项目事实核查

日期：2026-08-05  
范围：ASA Web（React 19 + Vite 8）桌面 1440x900、浮窗 390x700  
结论：先修设计系统、信息层级和可访问性，再补少量状态动效；不把动画组件库直接加入 ASA 运行时。

## 1. 事实核查

数据口径：GitHub REST API、仓库 README/package/registry、npm/PyPI API，采集时间为 2026-08-05（Asia/Shanghai）。Stars 会持续变化，发布文章时应写明采集日期。

| 项目 | 实时 Stars | 维护状态 | 文章描述核查 | 建议改法 |
| --- | ---: | --- | --- | --- |
| [UI-UX-Pro-Max](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill) | 113,530 | 活跃；最后 push 2026-08-03；37 个 GitHub Releases；贡献者 API 返回 74 条 | “AI skill、支持多种编码助手、v2 有设计系统生成”准确；“14 个版本、27 位贡献者”已过期。自动生成结果仍可能误判：本次对 ASA 命中了高密度运营台，也错误推荐了“夸张极简、超大字号” | 写成“设计规则与检索工具，降低基础错误概率；不能替代产品设计判断” |
| [React Bits](https://github.com/DavidHDev/react-bits) | 44,816 | 活跃；最后 push 2026-08-04；贡献者 API 至少 100 条 | README 当前为 165+ 组件、4 种 JS/TS + CSS/Tailwind 变体，文章的 110+ 已过期；David Haz 是 creator & lead maintainer，但“一个人维护”不准确；仓库采用 MIT + Commons Clause，不能把组件本身转售/再分发，不是无附加条件的 MIT | 改为“源码可用的 React 动画组件集合”；删除“Pro 后开源更新变慢”等无仓库证据判断 |
| [roughViz](https://github.com/jwilber/roughViz) | 7,141 | 低维护；最后 push 2024-04-26；npm 最新版 2.0.5 发布于 2023-11 | 手绘图表、7 类图表、D3v5/roughjs、适合表达趋势而非精确值，基本准确；React/Vue/Python wrapper 存在，但 React/Vue wrapper 分别停在 2020/2019，不能写成活跃生态；“Vite/esbuild 需要 polyfill”没有仓库证据 | 明确标注“主库和多数 wrapper 长期未更新”；删除未经复现的 polyfill 断言 |
| [Magic UI](https://github.com/magicuidesign/magicui) | 21,798 | 活跃；最后 push 2026-07-31；贡献者 API 至少 100 条 | shadcn registry、MCP 文档、部分组件依赖 Motion，准确；当前 registry 共 210 项，其中 `registry:ui` 75 项，不能把总 registry 项数直接说成“150+ 动画组件”；“与 shadcn 主题无缝兼容”应降级为“按 shadcn registry 安装并依赖 Tailwind/shadcn 约定” | 写“数十个 UI 组件与大量示例/区块”；不要用主观的“长尾质量参差”冒充事实 |
| [Motion](https://github.com/motiondivision/motion) | 33,084 | 活跃；最后 push 2026-08-05；npm `motion` 最近一周 17,347,686 次下载 | 官方支持 JavaScript、React、Vue，混合 JS/浏览器原生引擎、120fps、tree-shakable，准确；Vue 使用独立的 `motion-v` 包；它是 Framer Motion 的直接延续与更名，不是“精神续作”；“2025 年底独立”未从仓库材料得到支持；“gzip 约 8KB”不适用于完整入口，Bundlephobia 对 12.43.0 完整包估算约 45.3KB gzip | 改为“Motion（原 Framer Motion）”；包体积按具体入口和 tree-shaking 结果实测，不写单一 8KB |

### 文章中应直接修正的句子

1. “这 5 个开源项目”改为“这 5 个开源或源码可用项目”，或单独披露 React Bits 的 Commons Clause 限制。
2. UI-UX-Pro-Max 的版本、贡献者、Stars 改为动态口径，不把短期增长数字写进长期正文。
3. React Bits 改为“165+ 组件、四种技术变体”；删除“单人项目”和“Pro 导致更新变慢”。
4. roughViz 增加醒目的“低维护”提示；wrapper 只写“有第三方封装”，不暗示持续维护。
5. Magic UI 改为“75 个 registry UI 项，另有示例、区块和样式项”；组件数以官网当前分类为准。
6. Motion 改为“原 Framer Motion”；删除未经官方材料支撑的 2025 年底时间线和 8KB 固定体积。

## 2. Skill 安装结果

已安装到 `~/.codex/skills/`，下一次任务可自动匹配：

| Skill | 来源 | ASA 用途 |
| --- | --- | --- |
| `ui-ux-pro-max` | UI-UX-Pro-Max | 设计规则检索、可访问性清单、React 栈建议 |
| `magic-ui` | Magic UI | 仅在 Tailwind + shadcn 项目中选择和接入 Magic UI；当前 ASA 不满足前置条件 |
| `find-animation-opportunities` | React Bits | 找出值得动、也值得保持静止的交互位置 |
| `improve-animations` | React Bits | 只读动效审计与实施计划 |
| `review-animations` | React Bits | 对动效 diff 做高标准审查 |

未安装：roughViz 没有原生 skill；Motion 仓库的 `fix`/`improve` 是维护 Motion 自身 issue/PR 的内部工作流，不是应用接入指南。

## 3. ASA 现状判断

ASA 当前不是“丑”，而是“正确但偏薄”：布局克制、控件统一、没有渐变和装饰性卡片滥用，已经比常见 AI 后台稳定。主要问题在于：

- 字号普遍偏小。大量业务正文在 10-11.5px，1440 桌面可扫描但长时间使用疲劳；浮窗中又承载了同等信息密度。
- 信息层级依赖细边框。白底、浅灰线、相近字号出现得太多，标题、数据、说明的权重差距不足。
- 右侧栏固定 320px，但总览和分析页内容较少，主工作区因此被压窄，页面下半部留下大块空白。
- Agent 首页同时出现左导航、顶部“新任务”和右侧任务栏，入口重复；主区的信息价值反而弱于侧栏。
- 分析页暴露 `days`、ISO 时间和英文列名，属于数据表达问题，会直接拉低界面完成度。
- 交互反馈主要是 hover，缺少全局 `:focus-visible` 规范；移动端多个图标按钮小于 44x44px。
- 动效规则分散，存在 `.2s ease`、`.35s ease`、3s flash 等局部值；`prefers-reduced-motion` 只覆盖少数组件。

## 4. 设计方向

采用“Carbon 式生产力密度 + ASA 现有品牌语义”，不照搬任何品牌：

- 产品气质：安静、专业、可追溯、行动优先；不做营销页式英雄区、光束边框、粒子背景或手绘图表。
- 栅格：8px 主节奏，4px 微调；桌面保持高密度，浮窗采用单列渐进披露。
- 色彩：蓝色只表示主交互和 Agent；绿色表示健康/已完成；琥珀表示待判断；红色表示错误/危险。普通容器用中性灰分层，不再靠更多边框制造结构。
- 圆角：控件 4-6px，面板 6px；不增加大圆角卡片。
- 字体：继续使用系统字体栈，避免引入网络字体；正文提升到 13px，关键业务文本 13-14px，辅助信息最低 11.5px。
- 动效：频繁操作 0-160ms，偶发面板 160-220ms；只动 `transform` 和 `opacity`；键盘高频操作不做入场动画。

## 5. 分阶段实施

### P0：设计 token 与可访问性基线（1-2 天）

目标文件：`src/styles.css`、`src/shared/primitives.tsx`、`src/shared/tabs.tsx`

- 建立语义 token：文字三级、表面三级、边框、焦点、状态色、字号、行高、间距、动效时长与 easing。
- 增加统一 `:focus-visible`；所有图标按钮补足可访问名称，浮窗触控目标不小于 44x44px。
- 把正文、标签、元数据从零散 8.5-12px 收敛到明确的五级字号。
- 将 reduced-motion 扩展到 flash、进度条、面板和 Agent thinking 状态。

验收：键盘可从导航进入主区、打开/关闭对话框并回到触发点；375/390/768/1440 宽度无溢出。

### P1：壳层、总览与 Agent 首页（3-4 天）

目标文件：`src/app/App.tsx`、`src/pages/Overview.tsx`、`src/pages/TodayWorkbench.tsx`、`src/agent/AgentWorkspace.tsx`

- 桌面右栏改为 280px 可折叠上下文栏；小于 1280px 默认收起，释放主工作区。
- 总览把“今日工作台”提升为首要任务队列，KPI 带与任务队列对齐；右栏仅保留需持续查看的连接、固定分析和风险摘要。
- Agent 首页移除重复的新任务入口：保留右栏按钮，顶部改为当前会话/上下文；主区用“待判断、运行中、最近分析”三组可行动列表。
- Composer 始终与当前上下文绑定，状态条简化为一行，避免浮窗首屏被多层 header 占据。

验收：桌面首屏至少显示 6 个可行动项；浮窗首屏无需滚动即可看到当前上下文、最近结论和输入框。

### P2：分析、岗位与工作流详情（4-6 天）

目标文件：`src/pages/AnalysisWorkspace.tsx`、`src/panels/JobPanel.tsx`、`src/workflows/WorkflowPanel.tsx`

- 分析页将 `days`、ISO 日期、`client/title/active_candidates` 等原始字段全部映射为中文业务文案。
- 趋势图保留精确、低装饰风格；增加基线、时间范围和可访问文本，不使用 roughViz。
- 岗位详情把“岗位事实、寻访策略、候选人”做成稳定锚点；右侧候选列表支持状态过滤和当前位置保持。
- 工作流右栏按“待审批、执行动态、产物”分段折叠；顶部摘要只保留结果、风险、下一动作，减少重复状态。

验收：用户能在 5 秒内回答“现在最该处理什么、为什么、下一步是什么”；任何状态都不直接渲染英文原值。

### P3：动效与回归（2 天）

优先使用 CSS，不新增运行时依赖：

- 需要：按钮按压反馈、抽屉/对话框淡入位移、折叠箭头旋转、异步状态的局部更新提示。
- 不需要：KPI 数字滚动、列表整组 stagger、背景动画、卡片光束、磁吸按钮、图表绘线表演。
- 只有在出现可中断手势、复杂 presence 或布局连续性需求时，才评估引入 `motion`；不得因为已安装 skill 就引入库。

验收：运行 `npm run ci:fast`、`npm run ci:e2e-functional`，并重生成桌面/浮窗截图基线；人工检查 1440x900、390x700 和 reduced-motion。

## 6. 组件库取舍

| 候选 | ASA 决策 | 原因 |
| --- | --- | --- |
| UI-UX-Pro-Max | 使用 skill，不进运行时 | 适合生成候选和检查清单，但结果必须人工筛选 |
| React Bits | 暂不接入组件 | 风格偏展示型，依赖因组件而异，许可证带 Commons Clause；ASA 高频操作收益低 |
| roughViz | 不接入 | 低维护，手绘风格不符合严肃、可追溯的数据工作台 |
| Magic UI | 暂不接入 | ASA 当前没有 Tailwind/shadcn；迁移成本远高于微交互收益 |
| Motion | 条件式引入 | 仅在 CSS 无法可靠处理 presence/布局连续性时引入，并以实际 bundle 报告为准 |

## 7. 推荐执行顺序

先完成 P0，再用一个垂直切片验证 P1：`总览桌面 + 总览浮窗 + Agent 首页`。验证通过后再批量扩展到详情页。这样能先统一视觉语言，同时避免在当前大量功能改动尚未收口时重排所有页面。

## 8. 证据链接

- GitHub 仓库元数据：各仓库 `https://api.github.com/repos/{owner}/{repo}`
- [UI-UX-Pro-Max README](https://github.com/nextlevelbuilder/ui-ux-pro-max-skill/blob/main/README.md)
- [React Bits README](https://github.com/DavidHDev/react-bits/blob/main/README.md) 与 [License](https://github.com/DavidHDev/react-bits/blob/main/LICENSE.md)
- [roughViz README](https://github.com/jwilber/roughViz/blob/master/README.md) 与 [package.json](https://github.com/jwilber/roughViz/blob/master/package.json)
- [Magic UI registry](https://github.com/magicuidesign/magicui/blob/main/registry.json)
- [Motion README](https://github.com/motiondivision/motion/blob/main/README.md) 与 [npm 下载 API](https://api.npmjs.org/downloads/point/last-week/motion)
- [Bundlephobia motion@12.43.0](https://bundlephobia.com/package/motion@12.43.0)
