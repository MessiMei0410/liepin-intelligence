import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CandidateListCard, type CandidateListCardData } from '../workflows/CandidateListCard'

const data: CandidateListCardData = {
  type: 'candidate_list',
  title: '长越科技｜机械高级工程师（岗位 137）候选名单',
  context: { type: 'job', id: 137 },
  summary: { total: 5, active: 3, stopped: 2, bonder_count: 1 },
  groups: [
    {
      key: 'bonder',
      label: '固晶机/共晶机/键合机背景',
      priority: true,
      candidates: [{ id: 522, name: '张航', company: 'ASM中国集团公司', title: '高级机械设计工程师', stage: '已触达' }],
    },
    {
      key: 'active',
      label: '其余可推进候选',
      priority: false,
      candidates: [
        { id: 519, name: '陈**', company: '先导科技', title: '结构设计工程师', stage: 'S1 新增寻访/待复核' },
        { id: 529, name: '王先生', company: '华为', title: '机械技术专家', stage: 'S1 新增寻访/待复核' },
      ],
    },
    {
      key: 'stopped',
      label: '已停止推进',
      priority: false,
      candidates: [{ id: 511, name: '刘先生', company: '上海泽丰', title: '机械工程师', stage: 'H5 初筛不通过' }],
    },
  ],
}

describe('CandidateListCard', () => {
  it('渲染标题、统计与分组', () => {
    render(<CandidateListCard data={data} compact onOpenCandidate={() => {}} />)
    expect(screen.getByText('长越科技｜机械高级工程师（岗位 137）候选名单')).toBeTruthy()
    expect(screen.getByText(/共 5 人/)).toBeTruthy()
    expect(screen.getByText(/固晶\/共晶\/键合背景 1 人/)).toBeTruthy()
    expect(screen.getByText(text => text.includes('固晶机/共晶机/键合机背景'))).toBeTruthy()
    expect(screen.getByText('其余可推进候选')).toBeTruthy()
    expect(screen.getByText('已停止推进')).toBeTruthy()
  })

  it('点击人选行触发 onOpenCandidate 并带人选 id', () => {
    const onOpenCandidate = vi.fn()
    render(<CandidateListCard data={data} compact onOpenCandidate={onOpenCandidate} />)
    fireEvent.click(screen.getByText('张航'))
    expect(onOpenCandidate).toHaveBeenCalledWith(522)
  })

  it('提供打开岗位入口并触发 onOpenJob', () => {
    const onOpenJob = vi.fn()
    render(<CandidateListCard data={data} compact onOpenCandidate={() => {}} onOpenJob={onOpenJob} />)
    fireEvent.click(screen.getByText('打开岗位查看完整名单'))
    expect(onOpenJob).toHaveBeenCalledWith(137)
  })

  it('无分组时不渲染空组（统计仍取自 summary）', () => {
    render(<CandidateListCard data={{ ...data, groups: [] }} compact onOpenCandidate={() => {}} />)
    expect(screen.queryByText(text => text.includes('固晶机/共晶机/键合机背景'))).toBeNull()
    expect(screen.queryByText('其余可推进候选')).toBeNull()
    expect(screen.getByText(/共 5 人/)).toBeTruthy()
  })
})
