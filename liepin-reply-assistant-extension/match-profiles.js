(function () {
  'use strict';

  const profiles = {
    longyue_senior_mechanical: {
      client: '长越科技',
      position: '机械高级工程师',
      label: '长越科技 · 机械高级工程师',
      domain: 'precision_mechanical',
      targetLocationName: '上海/长三角',
      targetRegionRe: /上海|苏州|无锡|杭州|嘉兴|南京|宁波|长三角|华东/,
      base: 50,
      benchmarkCompanyRe: /长越科技/,
      targetCompanyRules: [
        { re: /上海微电子|SMEE|华为|新凯来|华卓精科|华海清科|芯源微|中微|北方华创|盛美半导体|微导纳米|拓荆|迈为|精测电子|天准科技|大族激光|雅科贝思/, points: 16, text: '来自半导体设备、光刻/量测或高端精密装备目标公司' },
        { re: /固高|雷赛|汇川|PI|Physik Instrumente|Aerotech|Parker|海德汉|Heidenhain|隐冠|宇量昇/, points: 12, text: '有运动控制、精密定位或关键运动部件公司背景' }
      ],
      skillRules: [
        { re: /微米级|亚微米级|纳米级|亚纳米|精密设备|精密平台|精密定位|运动台|微动平台|宏动平台|气浮|静压|直驱|平面电机|直线电机|光栅尺/, points: 24, text: '命中微米/亚微米级精密设备、运动平台或定位系统经验' },
        { re: /机械设计|结构设计|机械结构|方案设计|详细设计|整机刚性|结构刚性|稳定性|结构优化|材料选型|BOM|装配调试|公差|加工/, points: 18, text: '机械结构设计、刚性/稳定性和工程落地能力明确' },
        { re: /有限元|ANSYS|Ansys|Abaqus|abaqus|COMSOL|仿真|结构强度|振动响应|振动|模态|热变形|热分析|动力学|刚度/, points: 18, text: '有有限元、结构强度、振动响应或热变形分析能力' },
        { re: /紧固件|螺栓|螺钉|连接件|预紧|松动|稳定性|丝杠|导轨|轴承|电机|直线导轨|选型/, points: 14, text: '熟悉紧固件稳定性和电机、丝杠、导轨、轴承、光栅尺等关键部件选型' },
        { re: /光刻|光刻机|EUV|DUV|CHUCK|chuck|晶圆台|工件台|物镜|光机|半导体前道|晶圆|wafer|量测|OCD|OVL/, points: 12, text: '有光刻/前道/晶圆台或光机设备场景' },
        { re: /项目管理|项目负责人|技术负责人|模块负责人|团队|跨部门|从0|0-1|量产|交付|验收/, points: 10, text: '有项目管理、模块负责或从0到1交付经历' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 9, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，贴合高级机械技术岗' },
        { re: /本科/, points: 4, text: '本科及以上基础满足' },
        { re: /机械|力学|机电|自动化|精密仪器|测控/, points: 3, text: '专业方向与精密机械/机电系统较相关' }
      ],
      cityRules: [
        { re: /上海|苏州|无锡|杭州|嘉兴|南京|宁波|长三角|华东/, points: 5, text: '地域在上海/长三角可沟通范围' },
        { re: /北京|深圳|合肥|武汉|成都|西安|广州/, points: 1, text: '异地人选，需要确认上海或长三角接受度' }
      ],
      riskRules: [
        { re: /长越科技/, text: '当前或近期在长越科技：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /期望.*北京|期望.*深圳|期望.*成都|期望.*广州|北京机械|深圳机械|成都机械|广州机械/, text: '期望地不在上海/长三角，需要先确认地点' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年|工作3年|工作4年|工作5年|工作6年/, text: '年限可能低于7年以上要求，需复核项目深度' },
        { re: /销售|质量|工艺\/制程|设备维护|售后|软件|算法|电气|采购/, text: '当前方向可能偏离精密机械结构设计主线' }
      ],
      coreSkillGapText: '未明显看到微米/亚微米级精密设备、精密定位或运动平台主线',
      engineeringGapText: '结构强度、振动响应、热变形、紧固件稳定性或关键运动部件选型证据不足',
      seniorityGapText: '高级工程师层级待核实：未明显看到7年以上、模块负责、项目管理或复杂装备交付证据',
      targetCompanyGapText: '半导体设备、光刻/量测、运动控制或精密装备背景不够明确',
      cityGapText: '上海或长三角接受度待确认'
    },

    longyue_automation_software: {
      client: '长越科技',
      position: '自动化软件高级工程师',
      label: '长越科技 · 自动化软件高级工程师',
      domain: 'automation_software',
      targetLocationName: '上海/长三角',
      targetRegionRe: /上海|苏州|无锡|杭州|嘉兴|南京|宁波|长三角|华东/,
      base: 48,
      benchmarkCompanyRe: /长越科技/,
      targetCompanyRules: [
        { re: /上海微电子|SMEE|新凯来|华卓精科|华海清科|芯源微|中微|北方华创|盛美半导体|微导纳米|拓荆|迈为|精测电子|先导智能|大族激光|博众精工/, points: 16, text: '来自半导体设备、光刻/量测或高端自动化装备目标公司' },
        { re: /汇川|固高|雷赛|倍福|Beckhoff|西门子|Siemens|欧姆龙|Omron|三菱|Mitsubishi|基恩士|Keyence|ACS|Aerotech|PI|Parker/, points: 12, text: '有运动控制、PLC/工控或精密自动化生态公司背景' }
      ],
      skillRules: [
        { re: /EtherCAT|TwinCAT|Codesys|CoDeSys|PLC|HMI|运动控制|Motion Control|伺服|伺服控制|轴控|多轴|插补|同步控制|实时控制/i, points: 26, text: '命中 EtherCAT/TwinCAT/Codesys/PLC/HMI/运动控制等核心控制平台' },
        { re: /C#|\.NET|WPF|WinForm|C\+\+|C语言|Python|LabVIEW|上位机|控制软件|设备软件|软件架构|软件开发/i, points: 20, text: '有 C#/C++/LabVIEW/上位机或设备控制软件开发经验' },
        { re: /半导体设备|晶圆|wafer|光刻|量测|涂胶显影|刻蚀|沉积|清洗|封装设备|自动化设备|非标设备|高端装备/i, points: 16, text: '有半导体设备、晶圆制造装备或非标高端自动化设备场景' },
        { re: /现场调试|设备调试|联调|交付|验收|量产|客户现场|产线导入|问题定位|故障诊断|日志|报警|recipe|配方|SECS|GEM/i, points: 14, text: '有设备现场调试、联调交付、产线导入或问题定位经验' },
        { re: /模块负责人|项目负责人|技术负责人|架构师|团队|代码评审|规范|平台化|复用|从0|0-1|标准化/i, points: 10, text: '有模块负责、软件架构、平台化或团队协同经验' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，贴合高级自动化软件岗' },
        { re: /本科/, points: 4, text: '本科及以上基础满足' },
        { re: /自动化|控制|计算机|软件|电子|电气|测控|机电/, points: 3, text: '专业方向与自动化软件/控制系统相关' }
      ],
      cityRules: [
        { re: /上海|苏州|无锡|杭州|嘉兴|南京|宁波|长三角|华东/, points: 5, text: '地域在上海/长三角可沟通范围' },
        { re: /北京|深圳|合肥|武汉|成都|西安|广州/, points: 1, text: '异地人选，需要确认上海或长三角接受度' }
      ],
      riskRules: [
        { re: /长越科技/, text: '当前或近期在长越科技：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /纯机械|结构设计|采购|质量|工艺\/制程|售后|销售|算法研究|互联网后端|前端开发/, text: '方向可能偏离设备控制软件/自动化软件主线' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年|工作3年|工作4年/, text: '年限可能偏浅，需确认是否达到高级工程师层级' }
      ],
      coreSkillGapText: '未明显看到 EtherCAT/TwinCAT/Codesys/PLC/HMI/运动控制或上位机控制软件主线',
      engineeringGapText: '半导体设备/非标设备现场调试、联调交付、recipe/SECS-GEM 或问题定位证据不足',
      seniorityGapText: '高级工程师层级待核实：未明显看到模块负责、软件架构、平台化或复杂设备交付证据',
      targetCompanyGapText: '半导体设备、工控运动控制或高端自动化装备背景不够明确',
      cityGapText: '上海或长三角接受度待确认'
    },

    weida_procurement: {
      client: '微导纳米',
      position: '双采购岗',
      label: '微导纳米 · 双采购岗',
      domain: 'procurement_semiconductor',
      targetLocationName: '苏州/长三角',
      targetRegionRe: /苏州|上海|无锡|杭州|南京|嘉兴|长三角|华东/,
      base: 42,
      benchmarkCompanyRe: /微导纳米|拓荆|华海清科|盛美半导体|北方华创|中微|芯源微|华卓精科|上海微电子/,
      targetCompanyRules: [
        { re: /半导体设备|晶圆厂|前道|封装|光伏设备|锂电设备|自动化设备|高端装备/, points: 12, text: '来自半导体设备、晶圆厂或高端制造供应链场景' },
        { re: /采购|供应链|供应商|SQE|SCM|PMC|计划|物料|交期|成本|寻源|议价|商务/, points: 14, text: '有采购、供应链、供应商管理或物料协同场景' },
        { re: /机加工|结构件|钣金|标准件|非标件|电子料|备件|BOM|缺料|呆滞|替代料/, points: 10, text: '有机加工件、结构件、电子料或物料替代相关场景' }
      ],
      skillRules: [
        { re: /采购|寻源|议价|比价|定点|开模|询价|供应商开发|供应商管理|交期|缺料|呆滞|降本|成本优化|合同|商务/, points: 22, text: '采购主线明确，覆盖寻源、比价、供应商和交期闭环' },
        { re: /机加工件|结构件|钣金|标准件|非标件|BOM|图纸|规格书|替代料|样品|试产|导入/, points: 16, text: '有工程物料、结构件或导入类采购经验' },
        { re: /半导体设备|晶圆厂|前道|工艺|设备|产线|备件|维修|维保|上线|导入|NPI/, points: 14, text: '与半导体设备或产线物料协同场景接近' },
        { re: /供应链计划|PMC|S&OP|库存|周转|安全库存|交付|排产|物流|仓储|采购订单|PO|合同/, points: 12, text: '有计划、库存、交付或采购订单流程经验' },
        { re: /供应商审核|稽核|质量异常|来料异常|8D|对账|发票|付款|谈判|成本拆解|框架协议/, points: 10, text: '有供应商审核、异常闭环、对账或谈判经验' },
        { re: /主管|经理|负责人|专家|主采|双采购|采购主管|采购经理|供应链经理/, points: 8, text: '有采购管理或双线采购协同层级线索' }
      ],
      educationRules: [
        { re: /本科/, points: 4, text: '本科及以上基础满足' },
        { re: /硕士|MBA/, points: 6, text: '硕士或MBA背景，偏管理协同加分' }
      ],
      cityRules: [
        { re: /苏州|上海|无锡|杭州|南京|嘉兴|长三角|华东/, points: 5, text: '地域在苏州/长三角可沟通范围' }
      ],
      riskRules: [
        { re: /采购员|仓库|质检|检验员|生产操作员/, text: '可能偏执行层，不一定是双采购主线' },
        { re: /纯销售|客服|软件开发|算法工程师|机械设计|结构设计/, text: '方向可能偏离采购主线' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' }
      ],
      coreSkillGapText: '未明显看到采购、寻源、供应商或物料闭环主线，需确认是否负责双采购协同',
      engineeringGapText: '机加工件、结构件、BOM、交期异常或降本证据不足',
      seniorityGapText: '采购管理层级待核实：未明显看到主管、经理或双采购协同证据',
      targetCompanyGapText: '半导体设备或高端制造供应链背景不够明确',
      cityGapText: '苏州或长三角接受度待确认'
    },

    pengxinxu_pqe: {
      client: '鹏新旭',
      position: 'PQE专家',
      label: '鹏新旭 · PQE专家',
      domain: 'quality_pqe',
      targetLocationName: '深圳/苏州',
      targetRegionRe: /深圳|苏州|上海|无锡|南京|杭州|合肥|武汉|西安|长三角|华南|华东/,
      base: 44,
      benchmarkCompanyRe: /鹏新旭|深圳市鹏新旭|鹏芯旭|PST/,
      targetCompanyRules: [
        { re: /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|12吋fab|300mm fab/i, points: 24, text: '明确有12吋/300mm fab产线场景' },
        { re: /长鑫|长江存储|中芯|SMIC|华虹|华力|上海华力|晶合集成|粤芯|士兰|芯联集成|积塔|SK海力士|Hynix|三星|台积电|TSMC|联电|UMC/, points: 18, text: '来自12吋晶圆厂/存储/集成电路制造主线公司' },
        { re: /晶圆厂|Fab|wafer fab|前道|晶圆制造|晶圆产线|wafer line|半导体产线/i, points: 16, text: '有fab/晶圆制造/前道产线场景' },
        { re: /华天科技|通富微电|长电科技|长飞先进|荣芯半导体|源杰|锐石创芯|芯恩|芯粤能|燕东微|安世|闻泰|宜兴中车时代半导体|中车时代半导体|株洲中车时代半导体|中车时代电气/, points: 2, text: '来自封测、化合物半导体、功率器件或相近制造公司，只作弱相关参考，不能等同12吋fab产线' },
        { re: /中微|北方华创|拓荆|微导纳米|盛美|华海清科|芯源微|汇川|东山精密|瑞仪光电/, points: 2, text: '有半导体设备、工艺质量、高端制造或消费电子质量场景，仅作辅助参考' }
      ],
      skillRules: [
        { re: /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|晶圆厂|Fab|wafer fab|前道|晶圆制造|晶圆产线|半导体产线/i, points: 28, text: '12吋fab/300mm晶圆产线主线明确' },
        { re: /SPC|统计过程控制|控制图|过程能力|CPK|Cpk|PPK|Ppk|Minitab|JMP/i, points: 30, text: 'SPC/统计过程控制、控制图或过程能力分析主线明确' },
        { re: /loading|Loading|负载|装载|上料|载片|片盒|wafer\s*loading|loading\s*effect|微负载|负载效应/i, points: 22, text: '有loading问题、装载/载片或负载效应相关分析改善线索' },
        { re: /PQE|CQE|Product Quality|Customer Quality|产品质量|制程质量|过程质量|客户质量|品质工程|QE|QRA|QRE/i, points: 16, text: 'PQE/CQE/产品质量/客户质量主线明确' },
        { re: /line\s*yield|良率|Yield|报废|质量成本|新产品上量|上量管控|制程|工艺异常|量产质量|NPI质量|项目质量|可靠性|可靠性验证|可靠性工程/i, points: 14, text: '有line yield、报废/质量成本、新产品上量、NPI/量产质量或可靠性经验' },
        { re: /MSA|测量系统分析|GRR|Gage\s*R&R|Gauge\s*R&R|量具重复性|再现性|量测系统|测量仪器认证/i, points: 8, text: '有MSA/测量系统分析、GRR或量测系统能力经验，作为加分项' },
        { re: /客诉|客户投诉|客户审核|客户稽核|外审|audit|8D|FA|失效分析|异常处理|质量改善|质量闭环|RMA|MRB|PA改善|PCCB|CCR|CAR|5Why|鱼骨图/i, points: 10, text: '有MRB/PA、客户审核、客诉8D、FA或质量闭环辅助经验' },
        { re: /FMEA|PFMEA|DOE|QMS|ISO9001|IATF16949|APQP|PPAP|QC七大手法/i, points: 8, text: '掌握 FMEA/DOE/QMS 等质量工具或体系方法' },
        { re: /半导体|晶圆|芯片|集成电路|Fab|wafer|DRAM|NAND/i, points: 6, text: '有半导体、晶圆或存储芯片质量场景' },
        { re: /PQE主管|PQE经理|质量主管|质量经理|品质主管|品质经理|主任工程师|资深|高级|专家|负责人|Leader|Lead|Staff/i, points: 10, text: '有主管、专家、主任或质量专项负责层级线索' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，质量专家岗加分' },
        { re: /本科/, points: 4, text: '本科及以上基础满足' },
        { re: /电子|微电子|集成电路|材料|化学|物理|机械|自动化|电气|质量/, points: 3, text: '专业方向与半导体质量/PQE较相关' }
      ],
      cityRules: [
        { re: /深圳|苏州|上海|无锡|南京|杭州|合肥|武汉|西安|长三角|华南|华东/, points: 5, text: '所在地/意向地在深圳、苏州或常见半导体人才城市' }
      ],
      riskRules: [
        { re: /鹏新旭|深圳市鹏新旭|鹏芯旭|PST/, text: '当前或近期在鹏新旭：只作为标杆履历学习，不建议触达' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /IPQC|OQC|IQC|质检员|检验员|生产操作员|仓库|采购|销售|客服/, text: '可能偏检验/生产/非PQE主线，需确认是否承担产品质量闭环' },
        { re: /软件开发|算法工程师|机械设计|纯设备维护/, text: '当前方向可能偏离半导体PQE/产品质量主线' },
        { re: /宜兴中车时代半导体|中车时代半导体|株洲中车时代半导体|中车时代电气|SiC|碳化硅|IGBT|功率器件|功率半导体|车规|封装|封测|消费电子|手机模组|背光模组|设备供应商/, text: '可能不是12吋fab产线场景；功率器件/化合物/封测/设备经验不能直接按鹏新旭12吋loading PQE推进' }
      ],
      coreSkillGapText: '未明显看到12吋fab产线、SPC或loading问题改善主线，需确认最近工作是否在12吋晶圆产线做统计过程控制或loading异常定位',
      engineeringGapText: 'loading问题改善、SPC控制图/过程能力、line yield、制程异常或新产品上量证据不足；MSA可作为加分但非必需',
      seniorityGapText: '专家/主管层级待核实：未明显看到SPC方法主导、质量专项负责或体系改善牵头',
      fabLineGapText: '12吋fab产线背景不明确：只有封测、设备、消费电子或泛半导体质量经验不算核心匹配',
      targetCompanyGapText: '12吋晶圆厂、300mm fab或前道晶圆产线背景不够明确',
      cityGapText: '深圳/苏州接受度待确认'
    },

    silanmicro_technical_marketing: {
      client: '士兰微',
      position: '技术市场经理（三次电源/服务器或PC市场）',
      label: '士兰微 · 技术市场经理（三次电源/服务器/PC方向）',
      domain: 'power_marketing',
      targetLocationName: '杭州/华东',
      targetRegionRe: /杭州|上海|苏州|无锡|南京|嘉兴|宁波|绍兴|长三角|华东/,
      base: 46,
      benchmarkCompanyRe: /杭州士兰微|士兰微电子|士兰微/,
      targetCompanyRules: [
        { re: /英飞凌|Infineon|瑞萨|Renesas|MPS|Monolithic|芯朋微|德州仪器|Texas Instruments|\bTI\b|Intersil|Maxim|美信|Power Integrations|PI半导体|安森美|onsemi|晶丰明源|圣邦微|矽力杰|Silergy|华润微|茂力|茂睿芯/, points: 16, text: '来自电源管理芯片、模拟电源或三次电源相关原厂/竞品公司' },
        { re: /维谛|Vertiv|台达|Delta|华为|新华三|H3C|超聚变|浪潮|Inspur|联想|Lenovo|富士康|Foxconn|记忆科技|ODM|服务器|AI服务器|PC|主板|显卡|GPU/, points: 12, text: '有服务器、PC、ODM 或高算力硬件客户生态背景' },
        { re: /FAE|AE|应用工程|技术市场|产品市场|Product Marketing|Technical Marketing|产品经理|产品定义|方案推广|客户推广|Design[- ]?in|design[- ]?in/i, points: 10, text: '有技术市场、产品定义、FAE/AE 或客户 design-in 推广经验' }
      ],
      skillRules: [
        { re: /三次电源|多相|Multiphase|multi-phase|VRM|CPU\s*VR|VRD|VR14|SVID|SVI3|AMD\s*SVI3|Power Stage|SPS|Smart Power Stage|DrMOS|POL|Point of Load|eFuse|Buck|DC-DC|DCDC/i, points: 24, text: '命中三次电源/多相/VRM/DrMOS/POL/Power Stage 等核心方向' },
        { re: /服务器|AI服务器|PC|笔记本|台式机|主板|显卡|GPU|CPU|ODM|OEM|新华三|H3C|超聚变|浪潮|联想|富士康|记忆科技|数据中心/i, points: 16, text: '有服务器、PC、AI服务器、主板或 ODM 客户应用场景' },
        { re: /技术市场|产品市场|Technical Marketing|Product Marketing|产品定义|产品规划|roadmap|GTM|Go[- ]?to[- ]?Market|竞品分析|市场分析|方案推广|客户推广|Design[- ]?in|design[- ]?in|design win/i, points: 18, text: '有技术市场、产品定义、路线图、竞品分析或 GTM/客户推广经验' },
        { re: /FAE|AE|应用工程师|现场应用|客户支持|原理图|schematic|PCB review|Layout review|板级调试|debug|调试|验证|EVB|demo board|参考设计|datasheet|应用笔记/i, points: 14, text: '有 FAE/AE、原理图/PCB review、调试验证或客户技术支持经验' },
        { re: /电源管理芯片|PMIC|模拟电源|电力电子|电源架构|负载点电源|电源完整性|效率|纹波|瞬态响应|热设计|可靠性/i, points: 10, text: '具备电源管理芯片、模拟电源或电源系统工程理解' },
        { re: /经理|负责人|主管|产品线|产品负责人|Marketing Manager|Product Manager|Technical Marketing Manager|团队|跨部门|客户经理|大客户/i, points: 8, text: '有经理、产品线负责、跨部门或客户推进层级线索' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，贴合技术市场经理画像' },
        { re: /本科/, points: 4, text: '本科及以上基础满足' },
        { re: /电子|微电子|集成电路|自动化|电气|电力电子|通信|计算机/, points: 3, text: '专业方向与电源管理/电子系统较相关' }
      ],
      cityRules: [
        { re: /杭州|上海|苏州|无锡|南京|嘉兴|宁波|绍兴|长三角|华东/, points: 5, text: '地域在杭州/华东可沟通范围' },
        { re: /深圳|东莞|广州|北京|成都|西安|武汉|合肥|厦门/, points: 1, text: '异地人选，需要确认杭州或华东接受度' }
      ],
      riskRules: [
        { re: /杭州士兰微|士兰微电子|士兰微/, text: '当前或近期在士兰微：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /期望.*北京|期望.*深圳|期望.*广州|期望.*成都|期望.*西安|北京技术市场|深圳技术市场|广州技术市场|成都技术市场/, text: '期望地不在杭州/华东，需要先确认地点' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年|工作3年/, text: '年限或客户推广深度可能偏浅，需确认是否达到经理层级' },
        { re: /纯销售|渠道销售|质量|工艺\/制程|封装|测试|热设计|设备维护|售后|软件开发|算法工程师|机械设计|结构设计/, text: '当前方向可能偏离三次电源技术市场/产品定义主线' },
        { re: /ACDC|AC\/DC|一次电源|二次电源|适配器|充电器|逆变器|光伏|储能|BMS/, text: '电源方向可能偏一次/二次电源或泛电力电子，需确认是否做过三次电源/VRM/POL' }
      ],
      coreSkillGapText: '未明显看到三次电源/多相/VRM/DrMOS/POL/Power Stage 等核心证据',
      engineeringGapText: '服务器/PC 客户侧 design-in、调试验证、原理图/PCB review 或产品推广证据不足',
      seniorityGapText: '技术市场/产品定义/客户推广深度待核实：未明显看到产品线、roadmap、GTM、竞品分析或关键客户推进证据',
      targetCompanyGapText: '电源管理原厂、服务器/PC/ODM 或三次电源生态背景不够明确',
      cityGapText: '杭州或华东接受度待确认'
    },

    sukesi_senior_mechanical: {
      client: '苏科思',
      position: '资深机械工程师',
      label: '苏科思 · 资深机械工程师',
      base: 48,
      benchmarkCompanyRe: /江苏集萃苏科思|集萃苏科思|苏科思科技/,
      targetCompanyRules: [
        { re: /上海微电子|SMEE|华为|新凯来|华卓精科|宇量昇|雅科贝思|隐冠|拓荆|屹唐|迈为|玻纳刻|科益虹源|盛美半导体|微导纳米|集萃苏科思/, points: 14, text: '来自半导体设备/精密运动/光机相关目标公司' },
        { re: /ASMPT|库力索法|BESI|先进封装|大族激光|博众精工|芯源微|中微|北方华创/, points: 9, text: '有半导体设备或先进制造相近公司背景' }
      ],
      skillRules: [
        { re: /运动台|气浮|静压|直驱|平面电机|直线电机|微动平台|宏动平台|龙门|光栅|定位平台|纳米级|亚纳米|精密定位/, points: 22, text: '命中精密运动平台/气浮/直驱/纳米级定位经验' },
        { re: /光机|光刻|物镜|光学测试|光学系统|准分子激光|半导体前道|量测|OCD|OVL|膜厚|晶圆|wafer|键合|bonding|封装/, points: 14, text: '有光机、前道量检测或先进封装设备场景' },
        { re: /机械设计|结构设计|非标机械|详细设计|二维出图|BOM|装配调试|公差|加工|选型|方案设计/, points: 12, text: '机械结构设计和工程落地能力明确' },
        { re: /ANSYS|Ansys|abaqus|COMSOL|仿真|有限元|热分析|模态|动力学|刚度|振动|隔振|减振/, points: 10, text: '有仿真、刚度、模态、热或振动分析能力' },
        { re: /项目负责人|技术负责人|架构师|模块负责人|团队|管理|下属人数|产品经理|从0|0-1|量产|交付|验收/, points: 10, text: '有项目负责、团队协同或从0到1交付经历' },
        { re: /SolidWorks|solidworks|Creo|PROE|Pro\/E|UG|NX|CAD|AutoCAD|Catia|catia/, points: 6, text: '机械设计工具链完整' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，满足资深机械技术岗偏好' },
        { re: /本科/, points: 3, text: '本科及以上基础满足' }
      ],
      cityRules: [
        { re: /苏州|上海|无锡|杭州|嘉兴|南京|长三角/, points: 5, text: '地域在苏州/长三角可沟通范围' },
        { re: /成都|北京|深圳|合肥|武汉|广州/, points: 1, text: '异地人选，需要确认苏州接受度' }
      ],
      riskRules: [
        { re: /江苏集萃苏科思|集萃苏科思|苏科思科技/, text: '当前或近期在苏科思：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /期望.*成都|期望.*北京|期望.*深圳|成都机械|北京机械|深圳机械/, text: '期望地不在苏州，需要先确认地点' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年/, text: '年限偏浅，需确认是否达到资深要求' },
        { re: /销售|质量|工艺\/制程|设备工程师|售后|软件|算法|电气/, text: '当前方向可能偏离资深机械结构主线' }
      ]
    },

    sukesi_hardware_manager: {
      client: '苏科思',
      position: '硬件技术主管',
      label: '苏科思 · 硬件技术主管',
      domain: 'hardware_platform',
      base: 44,
      benchmarkCompanyRe: /江苏集萃苏科思|集萃苏科思|苏科思科技/,
      targetCompanyRules: [
        { re: /汇川|禾川|雷赛|固高|埃斯顿|台达|Delta|西门子|Siemens|博世力士乐|Bosch Rexroth|倍福|Beckhoff|安川|松下|三菱电机/, points: 14, text: '来自伺服驱动、运动控制或工业控制硬件平台相关公司' },
        { re: /上海微电子|SMEE|华卓精科|隐冠|华海清科|中科飞测|拓荆|微导纳米|盛美|芯源微|中微|北方华创|KLA|应用材料|AMAT|Lam|泛林|ASML/, points: 10, text: '有半导体设备或高端装备研发场景' },
        { re: /大族激光|博众精工|先导智能|ASMPT|库力索法|BESI|新凯来|华为/, points: 6, text: '有自动化设备、精密装备或复杂电子电气平台背景' }
      ],
      skillRules: [
        { re: /驱控|驱动器|控制器|伺服控制器|伺服驱动|高性能驱动|运动控制|工业控制器|电机控制|功率级/, points: 20, text: '命中驱控系统、控制器或驱动器硬件平台场景' },
        { re: /硬件平台|硬件架构|总体架构|需求分解|平台化规划|标准化|复用|版本基线|接口定义|关键器件选型|方案评审|原理方案|设计取舍/, points: 18, text: '有硬件平台规划、架构设计或关键方案评审经验' },
        { re: /数字电路|模拟电路|电源完整性|电源设计|采样链路|采样电路|编码器接口|隔离保护|保护机制|通信接口|接口电路|EtherCAT|CAN|RS485|LVDS|原理图|PCB|板卡/, points: 16, text: '覆盖数字/模拟/电源/采样/编码器/隔离保护等关键硬件方案' },
        { re: /bring-up|样机调试|板级调试|硬件调试|波形分析|示波器|逻辑分析仪|边界工况|根因|问题定位|整改闭环|系统联调/, points: 14, text: '有样机 bring-up、波形分析、边界问题定位和整改闭环经验' },
        { re: /EMC|EMI|热设计|可靠性|DFM|DFT|安规|认证测试|环境测试|生产导入|量产导入|工程变更|量产问题|可制造性/, points: 14, text: '有 EMC、热设计、可靠性、认证或生产导入问题闭环经验' },
        { re: /硬件主管|硬件经理|硬件负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|设计规范|技术评审|评审机制|规范沉淀|方案复盘|团队带教|带教/, points: 12, text: '有硬件技术负责、评审规范沉淀或团队带教经历' },
        { re: /FPGA|嵌入式|算法|测试团队|系统级|软硬件边界|跨部门协同/, points: 4, text: '有与 FPGA、嵌入式、算法或测试团队协同的系统级线索' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，贴合 JD 学历偏好' },
        { re: /本科/, points: 3, text: '本科及以上基础满足' }
      ],
      cityRules: [
        { re: /苏州|上海|无锡|杭州|嘉兴|南京|长三角/, points: 5, text: '地域在苏州/长三角可沟通范围' },
        { re: /成都|北京|深圳|合肥|武汉|广州|西安/, points: 1, text: '异地人选，需要确认苏州接受度' }
      ],
      riskRules: [
        { re: /江苏集萃苏科思|集萃苏科思|苏科思科技/, text: '当前或近期在苏科思：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /期望.*成都|期望.*北京|期望.*深圳|期望.*广州|成都硬件|北京硬件|深圳硬件|广州硬件/, text: '期望地不在苏州，需要先确认地点' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年|工作3年/, text: '年限或管理经验可能偏浅，需确认是否达到主管层级' },
        { re: /Verilog|VHDL|SystemVerilog|Vivado|Quartus|RTL|时序约束|CDC|时序收敛/, text: '偏 FPGA 逻辑主线，需确认是否实际负责硬件平台、原理设计和量产导入' },
        { re: /消费电子|手机|耳机|家电|电源适配器|充电器|电池包|BMS|ADAS|车载娱乐/, text: '行业可能偏消费类或车载电子，需确认是否有驱控硬件平台/工业控制场景' },
        { re: /销售|质量|工艺\/制程|工艺工程师|设备维护|售后|软件开发|算法工程师|机械设计|结构设计|FAE|技术支持/, text: '当前方向可能偏离驱控硬件平台负责人主线' }
      ],
      coreSkillGapText: '未明显看到驱控硬件平台、控制器/驱动器架构或关键硬件方案',
      engineeringGapText: '样机 bring-up、波形分析、EMC/可靠性或量产导入证据不足',
      seniorityGapText: '主管/资深专家层级待核实：未明显看到技术负责、评审规范、团队带教或模块负责证据',
      targetCompanyGapText: '运动控制、伺服驱动、工业控制或高端装备硬件平台背景不够明确',
      cityGapText: '苏州或长三角接受度待确认'
    },

    sukesi_fpga: {
      client: '苏科思',
      position: 'FPGA技术主管',
      label: '苏科思 · FPGA技术主管',
      domain: 'fpga',
      base: 44,
      benchmarkCompanyRe: /江苏集萃苏科思|集萃苏科思|苏科思科技/,
      targetCompanyRules: [
        { re: /汇川|禾川|雷赛|固高|埃斯顿|台达|Delta|西门子|Siemens|博世力士乐|Bosch Rexroth|倍福|Beckhoff|安川|松下|三菱电机/, points: 14, text: '来自伺服驱动、运动控制或工业控制 FPGA/控制平台相关公司' },
        { re: /上海微电子|SMEE|华卓精科|隐冠|华海清科|中科飞测|拓荆|微导纳米|盛美|芯源微|中微|北方华创|KLA|应用材料|AMAT|Lam|泛林|ASML/, points: 10, text: '有半导体设备或高端装备 FPGA/控制系统场景' },
        { re: /大族激光|博众精工|先导智能|ASMPT|库力索法|BESI|新凯来|华为/, points: 6, text: '有复杂装备、自动化或高可靠电子平台背景' }
      ],
      skillRules: [
        { re: /FPGA|SoC FPGA|RTL|Verilog|SystemVerilog|VHDL|Vivado|Quartus|Xilinx|Intel FPGA|Altera/, points: 20, text: 'FPGA/SoC FPGA 开发主线明确' },
        { re: /逻辑架构|模块划分|资源规划|可复用IP|接口规范|平台化模块|版本演进|逻辑设计/, points: 18, text: '有 FPGA 逻辑架构、模块划分或平台化复用经验' },
        { re: /PWM|采样同步|编码器接口|总线通信|EtherCAT|CAN|高速接口|光纤总线|故障保护|保护逻辑|数据通路|控制时序/, points: 16, text: '命中 PWM/采样同步/编码器/总线/保护逻辑/数据通路等关键模块' },
        { re: /时钟规划|时序约束|CDC|综合实现|时序收敛|资源优化|低延迟|低抖动/, points: 16, text: '有时钟规划、时序约束、CDC 和时序收敛能力' },
        { re: /仿真|仿真验证|板级验证|回归测试|在线调试|逻辑分析仪|示波器|bring-up|边界工况|问题定位/, points: 14, text: '有仿真、板级验证、在线调试和问题定位经验' },
        { re: /高性能驱动|运动控制|伺服驱动|驱动器|控制器|工业控制器|系统联调/, points: 12, text: '有高性能驱动、运动控制或工业控制系统场景' },
        { re: /FPGA主管|FPGA经理|FPGA负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|代码评审|设计规范|仿真规范|规范沉淀|问题复盘|团队带教|带教/, points: 12, text: '有 FPGA 技术负责、代码评审、规范沉淀或团队带教经历' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 8, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 6, text: '硕士背景，贴合 JD 学历偏好' },
        { re: /本科/, points: 3, text: '本科及以上基础满足' }
      ],
      cityRules: [
        { re: /苏州|上海|无锡|杭州|嘉兴|南京|长三角/, points: 5, text: '地域在苏州/长三角可沟通范围' },
        { re: /成都|北京|深圳|合肥|武汉|广州|西安/, points: 1, text: '异地人选，需要确认苏州接受度' }
      ],
      riskRules: [
        { re: /江苏集萃苏科思|集萃苏科思|苏科思科技/, text: '当前或近期在苏科思：不触达，只作为标杆履历学习' },
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /期望.*成都|期望.*北京|期望.*深圳|期望.*广州|成都FPGA|北京FPGA|深圳FPGA|广州FPGA/, text: '期望地不在苏州，需要先确认地点' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年|工作2年|工作3年/, text: '年限或技术负责经验可能偏浅，需确认是否达到主管层级' },
        { re: /销售|质量|工艺\/制程|工艺工程师|设备维护|售后|软件开发|算法工程师|机械设计|结构设计|FAE|技术支持/, text: '当前方向可能偏离 FPGA 逻辑架构和验证调试主线' }
      ],
      coreSkillGapText: '未明显看到 FPGA/SoC FPGA、RTL、逻辑架构或关键模块开发主线',
      engineeringGapText: '时序约束、CDC、仿真验证、板级调试或问题闭环证据不足',
      seniorityGapText: '主管/资深专家层级待核实：未明显看到 FPGA 技术负责、代码评审、规范沉淀或团队带教证据',
      targetCompanyGapText: '运动控制、伺服驱动、工业控制或高端装备 FPGA 场景不够明确',
      cityGapText: '苏州或长三角接受度待确认'
    },

    generic_mechanical: {
      client: '',
      position: '机械工程师',
      label: '通用 · 机械工程师',
      base: 45,
      benchmarkCompanyRe: /a^/,
      targetCompanyRules: [
        { re: /半导体设备|光伏设备|锂电设备|自动化设备|精密设备|非标设备|智能装备|高端装备/, points: 10, text: '有高端装备或自动化设备行业背景' },
        { re: /华为|上海微电子|中微|北方华创|拓荆|迈为|先导智能|博众精工|大族激光|ASMPT/, points: 8, text: '来自相近制造或设备公司' }
      ],
      skillRules: [
        { re: /机械设计|结构设计|非标机械|方案设计|详细设计|二维出图|BOM|装配图|工程图/, points: 16, text: '机械设计主线清晰' },
        { re: /SolidWorks|solidworks|Creo|PROE|Pro\/E|UG|NX|CAD|AutoCAD|Catia|catia/, points: 8, text: '熟悉常用机械设计工具' },
        { re: /公差|加工工艺|材料|钣金|机加工|选型|传动|导轨|丝杆|轴承|气缸|电机/, points: 10, text: '具备结构选型、加工和装配落地经验' },
        { re: /仿真|有限元|ANSYS|Ansys|abaqus|COMSOL|热分析|模态|刚度|振动/, points: 8, text: '有仿真或结构分析能力' },
        { re: /项目负责人|技术负责人|模块负责人|从0|0-1|量产|交付|验收|客户现场/, points: 8, text: '有项目交付或模块负责经验' }
      ],
      educationRules: [
        { re: /博士|博士后/, points: 7, text: '博士背景，技术深度加分' },
        { re: /硕士/, points: 5, text: '硕士背景，技术岗匹配度较好' },
        { re: /本科/, points: 3, text: '本科及以上基础满足' }
      ],
      cityRules: [
        { re: /上海|苏州|无锡|杭州|南京|嘉兴|宁波|长三角|深圳|东莞|广州|北京|天津|武汉|合肥|成都|西安/, points: 3, text: '所在地在常见装备制造人才城市' }
      ],
      riskRules: [
        { re: /暂无跳槽打算|不考虑|不看机会|不感兴趣/, text: '求职意愿偏低，需要低压沟通或暂缓' },
        { re: /销售|质量|工艺\/制程|设备维护|售后|软件|算法|电气|采购/, text: '当前方向可能不是机械设计主线' },
        { re: /大专|非统招/, text: '学历或统招背景可能需复核' },
        { re: /1年以下|工作1年/, text: '年限偏浅，需确认岗位层级是否匹配' }
      ]
    }
  };

  window.LIEPIN_MATCH_PROFILES = Object.assign({}, window.LIEPIN_MATCH_PROFILES || {}, profiles);
})();
