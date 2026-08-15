import { beforeEach, describe, expect, it, vi } from 'vitest'
import { hasDirtyForms, resetDirtyFormsForTest, setDirtyForm, subscribeDirtyForms } from '../shared/dirtyForm'

describe('dirtyForm 脏状态注册表', () => {
  beforeEach(() => resetDirtyFormsForTest())

  it('登记与注销正确维护计数，状态未变化不重复通知', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeDirtyForms(listener)
    // 订阅即回调一次当前值（0）
    expect(listener).toHaveBeenLastCalledWith(0)

    setDirtyForm('a', true)
    expect(hasDirtyForms()).toBe(true)
    expect(listener).toHaveBeenLastCalledWith(1)
    // 重复置 true 不变化，不通知
    setDirtyForm('a', true)
    expect(listener).toHaveBeenCalledTimes(2)

    setDirtyForm('b', true)
    expect(listener).toHaveBeenLastCalledWith(2)

    // 注销 a 后仍剩 b
    setDirtyForm('a', false)
    expect(hasDirtyForms()).toBe(true)
    expect(listener).toHaveBeenLastCalledWith(1)

    setDirtyForm('b', false)
    expect(hasDirtyForms()).toBe(false)
    expect(listener).toHaveBeenLastCalledWith(0)

    unsubscribe()
    setDirtyForm('c', true)
    expect(listener).toHaveBeenCalledTimes(5)
  })

  it('重置仅测试用的注册表后为空', () => {
    setDirtyForm('x', true)
    resetDirtyFormsForTest()
    expect(hasDirtyForms()).toBe(false)
  })
})
