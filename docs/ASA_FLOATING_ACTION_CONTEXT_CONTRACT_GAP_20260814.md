# ASA 当前页面动作身份合同缺口（2026-08-14）

## 结论（已修复）

历史版本的 `POST /api/asa/floating/action` 没有“用户点击时页面身份”的前置条件。Core 会在处理请求时调用 `build_floating_state(state)`，重新选取当下的 `active_context_raw`，再据此派发页面命令或创建推荐报告工作流。

因此，顾问在候选人 A 页面点击“生成推荐报告”后立刻切换到候选人 B，若请求在切换后才被 Core 处理，工作流可能绑定 B。前端无法仅凭丢弃旧回执证明写入目标仍是 A。该缺口现已由服务端原子身份合同补齐。

实现结果：`/api/asa/floating/action` 对页面绑定动作在 `ASA_FLOATING_LOCK` 内比较 expected/actual 身份摘要；冲突在任何命令入队、工作流创建或数据库写入前返回 HTTP 409、`error_code=context_changed` 和最新页面摘要。前端与旧兼容浮窗均携带 context key、instance、候选关系 ID 和 revision，409 只提示重新确认，不自动重放。

## 前端已完成的保护

- 页面身份变化会作废旧动作等待态、成功/失败回执和后续导航。
- 简历评估直接以点击时的 `job_candidate_id` 调用专用接口，回执与详情入口继续绑定该 ID。
- 推荐报告及页面桥动作不再把 A 的回执显示在 B 的页面条上。

这些保护避免界面串人，但不改变 `floating/action` 的服务端选目标时机。

## 建议合同

写动作请求应携带：

- `expected_context_key`
- `expected_instance_id`
- `expected_job_candidate_id`（已定位时）
- `expected_context_revision` 或稳定页面证据指纹

Core 应在产生任何命令、工作流或数据库写入之前原子校验这些字段。身份已变化时返回 `409 context_changed`，响应包含最新页面摘要；前端提示顾问重新确认，不自动重放。

页面桥命令还应继续以 `target_instance_id` 定向，并由扩展回执实际执行时的页面证据指纹。推荐报告创建应直接消费经校验的候选人快照，而不是再次读取全局活动页。

## 验收证据

1. A 点击后、Core 处理前切到 B，接口返回 `409`，不创建工作流、不入队页面命令。
2. 同一浏览器标签从 A 导航到 B，即使 `instance_id` 不变，也能由候选人 ID 或页面指纹识别冲突。
3. 未唯一定位的人选以稳定来源身份校验，不能只比较标题或标签页 ID。
4. 幂等重放保持首次已校验结果；冲突请求不得以新页面身份重新执行。
5. 审计事件记录 expected/actual 身份摘要，但不记录 Cookie、完整简历或 CDP 会话值。

补充实现约束：同一 `instance_id` 导航到另一候选人时，revision 由候选关系、来源候选 ID、候选人公司/职位及安全 URL 路径指纹计算，不依赖标签页 ID；未唯一定位时仍要求稳定来源身份或页面证据指纹。队列入队使用同一 RLock，保证“读取身份、校验、入队/建工作流”不被页面切换插入。
