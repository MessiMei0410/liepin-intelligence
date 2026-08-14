import { describe, expect, it } from 'vitest'
import { tabs } from '../shared/tabs'

// 顶级导航只保留四个工作区，Agent 替代总览（原 overview-status.test.tsx 随死代码 Overview 删除迁入）
describe('顶级导航工作区', () => {
  it('只有 Agent / 岗位看板 / 人选进度 / 人选列表 四个 tab', () => {
    expect(tabs.map(([key, label]) => [key, label])).toEqual([
      ['agent', 'Agent'], ['jobs', '岗位看板'], ['progress', '人选进度'], ['candidates', '人选列表'],
    ])
  })
})
