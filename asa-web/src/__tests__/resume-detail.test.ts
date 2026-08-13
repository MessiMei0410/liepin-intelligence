/* eslint-disable no-irregular-whitespace -- 测试夹具保留真实简历中的全角空格（如“薪　　资：”） */
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

  it('X-SaaS 工作经历：裸期间行 + 字段标题解析为结构化块', () => {
    const blocks = parseWorkDetails(`张雯 5128177
上海微电子装备(集团)股份有限公司 | 机械设计工程师 | 10年
工作经历
离职所需时间：
2017. 01 - 至今
上海微电子装备(集团)股份有限公司
部门/职位：
专项精密运动部 / 机械设计工程师
汇报对象/下属：
/ 0
工作内容/业绩：
14薪
职位类别：
机械结构工程师
工作背景：承担国家 02 专项光刻机半导体制造装备结构开发工作。
设计难点：精度要求达 1 微米、小批量、非标类。
2014. 11 - 2016. 06
研究生实习
部门/职位：
研发部门 / 助理
汇报对象/下属：
/ 0
工作内容/业绩：
产品经理助理 江森自控(中国)投资有限公司 (2015.12-2016.06 实习)
协助工程师完成零件校核。
教育经历
2014. 09 - 2017. 04
东华大学
专业/学历：
机械工程 / 硕士`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({
      company: '上海微电子装备(集团)股份有限公司',
      period: '2017. 01 - 至今',
      role: '专项精密运动部 / 机械设计工程师',
    })
    expect(blocks[0].description.join('')).toContain('工作背景：承担国家 02 专项')
    expect(blocks[1]).toMatchObject({ company: '研究生实习', period: '2014. 11 - 2016. 06', role: '研发部门 / 助理' })
    expect(blocks.flatMap(block => block.description).join('')).not.toContain('东华大学')
  })

  it('X-SaaS 0.00 重复工作经历只保留有效期间那条', () => {
    const work = `2017. 01 - 至今
上海微电子装备(集团)股份有限公司
部门/职位：
专项精密运动部 / 机械设计工程师
汇报对象/下属：
/ 0
工作内容/业绩：
14薪
职位类别：
机械结构工程师
工作背景：承担国家 02 专项光刻机结构开发。
2017. 01 - 0. 00
上海微电子装备(集团)股份有限公司
部门/职位：
专项精密运动部 / 机械设计工程师
汇报对象/下属：
/ 0
工作内容/业绩：
14薪
职位类别：
机械结构工程师
工作背景：承担国家 02 专项光刻机结构开发。`
    const blocks = parseWorkDetails(`工作经历
${work}
教育经历
2014. 09 - 2017. 04
东华大学`)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].period).toBe('2017. 01 - 至今')
    expect(blocks[0].description.join('')).not.toContain('0. 00')
  })

  it('X-SaaS 孤立 0.00 坏期间按时间不详展示', () => {
    const blocks = parseWorkDetails(`工作经历
2018. 03 - 0. 00
北京示例科技有限公司
部门/职位：
研发部 / 结构工程师
汇报对象/下属：
/ 0
工作内容/业绩：
负责整机结构设计。
教育经历
2014. 09 - 2018. 06
示例大学`)
    expect(blocks).toHaveLength(1)
    expect(blocks[0].company).toBe('北京示例科技有限公司')
    expect(blocks[0].period).toBe('时间不详')
  })

  it('猎聘履历含工作地点/薪资/职位类别字段：职位不被字段值覆盖', () => {
    const blocks = parseWorkDetails(`工作经历
通用电气医疗贸易发展有限公司
（2019.08 - 2024.05, 4年9个月）
医疗器械
商务精益经理
工作地点：
北京-大兴区
下属人数：
0
薪　　资：
46k
职位类别：
销售经理/主管
职责业绩：
就职于通用电气医疗中国区精益团队，从事Commercial Lean Leader岗位。
恩智浦(NXP)半导体(北京)有限公司
（2007.09 - 2011.06, 3年9个月）
电子/半导体/集成电路
分析和改进经理
下属人数：
5
职位类别：
其他高级管理
职责业绩：
从事六西格玛改善。
教育经历
苏州大学
（2009.09 - 2013.06）`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '通用电气医疗贸易发展有限公司', role: '商务精益经理', period: '2019.08 - 2024.05, 4年9个月' })
    expect(blocks[0].description.join('')).toContain('Commercial Lean Leader')
    expect(blocks[0].description.join('')).not.toContain('销售经理/主管')
    expect(blocks[1]).toMatchObject({ company: '恩智浦(NXP)半导体(北京)有限公司', role: '分析和改进经理' })
    expect(blocks[1].description.join('')).toContain('六西格玛改善')
  })

  it('职责正文里的“项目经验：”不截断工作经历', () => {
    const blocks = parseWorkDetails(`工作经历
汇川联合动力
（2025.04 - 2025.10, 6个月）
新能源汽车
硬件工程师
下属人数：
0
职责业绩：
项目经验：新能源汽车底盘电机控制器开发
项目背景：汽车底盘电机控制器开发（双电控）
华为技术
（2022.07 - 2023.06, 11个月）
电子/半导体/集成电路
硬件工程师
下属人数：
0
职责业绩：
项目经验：智能光伏控制器开发
教育经历
苏州大学
（2015.09 - 2019.06）`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '汇川联合动力', role: '硬件工程师' })
    expect(blocks[0].description.join('')).toContain('项目经验：新能源汽车底盘电机控制器开发')
    expect(blocks[1]).toMatchObject({ company: '华为技术', role: '硬件工程师' })
    expect(blocks[1].description.join('')).toContain('项目经验：智能光伏控制器开发')
  })

  it('X-SaaS 0.00 起始坏期间（离职所需时间残留）保留为时间不详', () => {
    const blocks = parseWorkDetails(`工作经历
离职所需时间：
0. 00 - 至今
上海正泰电源系统有限公司
部门/职位：
工程师 / 软件设计
2024. 05 - 2024. 12
德州仪器
部门/职位：
/ 软件设计
工作内容/业绩：
使用TMS320F28P65x芯片实现三电平SVPWM调制。
教育经历
2014. 09 - 2017. 04
示例大学`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '上海正泰电源系统有限公司', role: '工程师 / 软件设计', period: '时间不详' })
    expect(blocks[1]).toMatchObject({ company: '德州仪器', role: '软件设计', period: '2024. 05 - 2024. 12' })
  })

  it('X-SaaS 缺职位重复块与 0.00 坏期间一起按时间不详去重', () => {
    const blocks = parseWorkDetails(`工作经历
离职所需时间：
2019. 06 - 至今
睿励科学仪器(上海) 有限公司
部门/职位：
产品应用部 / 应用总监
汇报对象/下属：
/ 13
工作内容/业绩：
1.带领应用团队，完成现场工作。
2018. 05 - 0. 00
Intel on site
工作内容/业绩：
1.Provides pre-sales support。
2018. 05 - 0. 00
Intel on site
工作内容/业绩：
1.Provides pre-sales support。
教育经历
2005. 09 - 2009. 06
示例大学`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '睿励科学仪器(上海) 有限公司', period: '2019. 06 - 至今' })
    expect(blocks[1]).toMatchObject({ company: 'Intel on site', period: '时间不详' })
  })

  it('“工作经验”章节标题也能解析', () => {
    const blocks = parseWorkDetails(`工作经验
某公司
（2020.01 - 至今, 5年）
硬件工程师
职责业绩：
负责硬件设计。
教育经历
某大学
（2010.09 - 2014.06）`)
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toMatchObject({ company: '某公司', role: '硬件工程师', period: '2020.01 - 至今, 5年' })
  })

  it('X-SaaS 缺公司名的工作经历保留职位与期间', () => {
    const blocks = parseWorkDetails(`工作经历
离职所需时间：
0. 00 - 至今
部门/职位：
/ Senior Director, Head of Strategy
2017. 10 - 2019. 10
部门/职位：
/ Vice President, Head of E-commerce Vertical
工作内容/业绩：
全面负责电子商务行业领域的业务战略。
教育经历
2014. 09 - 2017. 04
示例大学`)
    expect(blocks).toHaveLength(2)
    expect(blocks[0]).toMatchObject({ company: '', period: '时间不详', role: 'Senior Director, Head of Strategy' })
    expect(blocks[1]).toMatchObject({ company: '', period: '2017. 10 - 2019. 10', role: 'Vice President, Head of E-commerce Vertical' })
    expect(blocks[1].description.join('')).toContain('电子商务')
  })

  it('项目经历 0.00 坏期间按时间不详聚合', () => {
    const blocks = parseProjectDetails(`0. 00 - 0. 00
PCB-AOI 缺陷检测
项目简介：
通过解压行业标准协议获得标准图，与实物图进行缺陷检测。
项目业绩：
新一代软件适用于标机，出货后设备稳定生产。`)
    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toMatchObject({ title: 'PCB-AOI 缺陷检测', period: '时间不详' })
    expect(blocks[0].description).toEqual(['通过解压行业标准协议获得标准图，与实物图进行缺陷检测。'])
    expect(blocks[0].achievements).toEqual(['新一代软件适用于标机，出货后设备稳定生产。'])
  })

  it('教育经历：英文校名与 0.00 坏期间结构化解析', () => {
    expect(parseEducationDetails(`0. 00 - 0. 00
University of London
专业/学历：
English / 其它
2021. 00 - 2025. 00
University of London
专业/学历：
Law / 硕士`)).toEqual([
      { school: 'University of London', major: 'English', degree: '其它', period: '时间不详', details: [] },
      { school: 'University of London', major: 'Law', degree: '硕士', period: '2021. 00 - 2025. 00', details: [] },
    ])
  })

  it('教育经历：点分格式英文校名与带括号中文校名', () => {
    expect(parseEducationDetails(`吉林化工学院(现吉林化工大学)
·
电气工程及其自动化
·
本科
2013.09 - 2017.06
统招`)).toEqual([
      { school: '吉林化工学院(现吉林化工大学)', major: '电气工程及其自动化', degree: '本科', period: '2013.09 - 2017.06', details: ['统招'] },
    ])
    expect(parseEducationDetails(`University of London
·
English
·
其它
2021. 00 - 2025. 00`)).toEqual([
      { school: 'University of London', major: 'English', degree: '其它', period: '2021. 00 - 2025. 00', details: [] },
    ])
  })

})
