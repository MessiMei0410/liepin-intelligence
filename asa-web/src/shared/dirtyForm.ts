// 表单脏状态注册表。
// 面板内的长表单（如候选人生命周期事件）在用户填写未提交时登记自己；
// App 在切 tab / 进 Agent / 关闭面板等会丢弃表单的导航前弹 React 确认框
// （禁止原生 confirm，见 AGENTS.md），避免静默清空已填内容。
// 注册方负责在卸载时清理，防止已卸载表单永久阻塞导航。
type DirtyFormListener = (dirtyCount: number) => void

const dirtyForms = new Set<string>()
const listeners = new Set<DirtyFormListener>()

const notify = () => {
  for (const listener of listeners) listener(dirtyForms.size)
}

/** 登记/注销一个表单的脏状态；状态未变化时不重复通知。 */
export function setDirtyForm(id: string, dirty: boolean): void {
  const before = dirtyForms.size
  if (dirty) dirtyForms.add(id)
  else dirtyForms.delete(id)
  if (before !== dirtyForms.size) notify()
}

export function hasDirtyForms(): boolean {
  return dirtyForms.size > 0
}

/** 订阅脏表单数量变化；立即回调一次当前值，返回退订函数。 */
export function subscribeDirtyForms(listener: DirtyFormListener): () => void {
  listeners.add(listener)
  listener(dirtyForms.size)
  return () => {
    listeners.delete(listener)
  }
}

/** 仅测试用：清空注册表。 */
export function resetDirtyFormsForTest(): void {
  dirtyForms.clear()
  listeners.clear()
}
