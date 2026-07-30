import { describe, it, expect } from 'vitest'
import { parseEducationDetails, parseProjectDetails, parseWorkDetails } from '../panels/resumeDetail'

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

  it('项目经历按项目锚点聚合字段，详情不再拆成时间轴节点', () => {
    const blocks = parseProjectDetails(`储能物流线项目
（2026.01 - 至今）
项目职务：
研发工程师
所在公司：
示例科技
项目描述：
项目目标：实现物料输送
项目概述：完成调度开发
项目职责：
负责整体调度程序调试
项目业绩：
达到客户要求
磁驱输送线项目
（2025.09 - 2026.01）
项目职务：
研发工程师`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ title: '储能物流线项目', period: '2026.01 - 至今', role: '研发工程师', company: '示例科技' })
    expect(blocks[0].description).toEqual(['项目目标：实现物料输送', '项目概述：完成调度开发'])
    expect(blocks[0].duties).toEqual(['负责整体调度程序调试'])
  })

  it('教育经历按学校聚合专业、学历、时间和标签', () => {
    expect(parseEducationDetails(`西安交通大学
·
机械工程
·
硕士
2011.09 - 2014.07
统招
985
211
大连理工大学
·
热能与动力工程
·
本科
2007.09 - 2011.07
统招`)).toEqual([
      { school: '西安交通大学', major: '机械工程', degree: '硕士', period: '2011.09 - 2014.07', details: ['统招', '985', '211'] },
      { school: '大连理工大学', major: '热能与动力工程', degree: '本科', period: '2007.09 - 2011.07', details: ['统招'] },
    ])
  })

  it('兼容 X-SaaS 的时间在前项目和专业/学历合并行', () => {
    const projects = parseProjectDetails(`2024. 06 - 至今
LinuxCommon跨平台框架
责任描述：
完成产品化交付
项目简介：
设计与开发者
2023. 01 - 2024. 05
运行时异常捕获进程
内容:
完成异常定位`)
    expect(projects).toEqual([
      expect.objectContaining({ title: 'LinuxCommon跨平台框架', period: '2024. 06 - 至今', duties: ['完成产品化交付'], description: ['设计与开发者'] }),
      expect.objectContaining({ title: '运行时异常捕获进程', period: '2023. 01 - 2024. 05', description: ['完成异常定位'] }),
    ])
    expect(parseEducationDetails(`2016. 09 - 2019. 06
华中科技大学
专业/学历：
电力电子 / 硕士
2012. 09 - 2016. 06
华中科技大学
专业/学历：
电气工程及其自动化 / 本科`)).toEqual([
      { school: '华中科技大学', major: '电力电子', degree: '硕士', period: '2016. 09 - 2019. 06', details: [] },
      { school: '华中科技大学', major: '电气工程及其自动化', degree: '本科', period: '2012. 09 - 2016. 06', details: [] },
    ])
  })
})
