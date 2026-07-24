import { describe, it, expect } from 'vitest'
import { parseWorkDetails } from '../panels/resumeDetail'

const RICH = `在职，看看新机会
男49岁苏州-工业园区本科工作26年保密中共党员
求职意向
硬件产品经理
工作经历
京东方
（2021.07 - 至今, 5年）
计算机硬件
硬件技术经理
下属人数：
0
职责业绩：
搭建硬件研发数据库：CIS 数据库属于硬件设计的核心数据平台，入职后着手搭建。
数字化建设方面: 主导自动化BOM 工具的开发并顺利应用。
北京大豪科技股份有限公司
（2015.03 - 2021.06, 6年4个月）
机械结构设计工程师
职责业绩：
负责服务器结构设计、散热辅助设计。
教育经历
苏州大学
（2009.09 - 2013.06）`

describe('详细工作内容解析（职责业绩块）', () => {
  it('完整履历：两段职责业绩全部解析', () => {
    const blocks = parseWorkDetails(RICH)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '京东方', period: '2021.07 - 至今, 5年', role: '硬件技术经理' })
    expect(blocks[0].description).toHaveLength(2)
    expect(blocks[0].description[0]).toContain('CIS 数据库')
    expect(blocks[1]).toMatchObject({ company: '北京大豪科技股份有限公司', role: '机械结构设计工程师' })
  })

  it('教育经历不被误采', () => {
    const blocks = parseWorkDetails(RICH)
    expect(blocks.flatMap(b => b.description).join('')).not.toContain('苏州大学')
  })

  it('摘要级履历（无职责业绩）返回空数组回退', () => {
    const thin = '在线 邓先生 41岁 工作15年 硕士 杭州 求职期望： 杭州市场总监 矽力杰 · 产品市场经理 2017.01-2023.08(6年7个月)'
    expect(parseWorkDetails(thin)).toEqual([])
    expect(parseWorkDetails('')).toEqual([])
  })
})
