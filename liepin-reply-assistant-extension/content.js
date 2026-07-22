(() => {
  'use strict';

  const ROOT_ID = 'liepin-reply-assistant-root';
  const TOGGLE_ID = 'liepin-reply-assistant-toggle';
  const STORE_KEY = 'liepinReplyAssistantLatestDraft';
  const PROJECT_STORE_KEY = 'liepinReplyAssistantSelectedProject';
  const SAMPLE_STORE_KEY = 'liepinReplyAssistantAcceptedSamples';
  const OUTREACH_EVENT_STORE_KEY = 'liepinReplyAssistantOutreachEvents';
  const WORKBENCH_BASE = 'http://127.0.0.1:8765';
  const FLOATING_URL = `${WORKBENCH_BASE}/asa-floating`;
  const EXTENSION_VERSION = chrome.runtime?.getManifest?.().version || 'unknown';
  const SAMPLE_LIMIT = 300;
  const OUTREACH_EVENT_LIMIT = 500;
  const RECOMMENDATION_MODES = [
    { key: 'outreachGreeting', label: '打招呼语' },
    { key: 'customerRecommendation', label: '推荐文案' }
  ];
  const STOP_REASON_OPTIONS = [
    { key: 'direction_mismatch', label: '方向不符' },
    { key: 'experience_mismatch', label: '经验不符' },
    { key: 'location_mismatch', label: '地点不符' },
    { key: 'salary_mismatch', label: '薪资不符' },
    { key: 'low_intent', label: '意愿低' },
    { key: 'duplicate_candidate', label: '重复人选' },
    { key: 'other', label: '其他' }
  ];
  const PROJECT_MISMATCH_FIELDS = [
    { key: 'recent', label: '最近触达岗位' },
    { key: 'lookup', label: 'A系统定位岗位' }
  ];

  if (window.__liepinProfessionalReplyAssistantLoaded) return;
  window.__liepinProfessionalReplyAssistantLoaded = true;

  const state = {
    collapsed: true,
    currentDraft: '',
    currentRecommendation: null,
    currentRecommendationMode: 'customerRecommendation',
    currentRecommendationSection: 'overview',
    resumeDetailView: 'match',
    currentContext: null,
    lastGenerated: null,
    lastStatus: '就绪',
    lastTalentDryRun: null,
    lastTalentLookupMatch: null,
    lastAutoReviewSignature: '',
    selectedProject: null,
    projectOptions: [],
    lastMatchedUrl: '',
    lastMatchSignature: '',
    projectUserTouched: false,
    projectResolvedFrom: '',
    lastProjectLookupSignature: '',
    lastLookupProjectRefreshSignature: '',
    recentOutreachProject: null,
    lastProjectOptionsSyncAt: 0,
    projectOptionsHydration: null,
    matchRefreshPromise: null,
    lastIntakeBridge: null,
    lockedResumeKey: '',
    lockedResumeIdentity: null,
    floatingUserSelectedUntil: 0,
    bridgeInstanceId: `liepin_${Math.random().toString(16).slice(2)}_${Date.now()}`,
    bridgeStatus: '未连接 ASA'
  };

  function postToWorkbench(path, payload) {
    return fetch(`${WORKBENCH_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      keepalive: true
    })
      .then(async res => {
        const body = await res.json().catch(() => null);
        if (!body) {
          return {
            ok: false,
            error: `本机同步服务返回格式异常（HTTP ${res.status}）`,
            status: res.status,
            transport_error: 'non_json'
          };
        }
        if (res.ok) return body || { ok: true };
        return {
          ok: false,
          error: body?.error || body?.reason || body?.stderr || `HTTP ${res.status}`,
          status: res.status,
          transport_error: 'http'
        };
      })
      .catch(error => ({
        ok: false,
        error: error?.message || '本机同步服务未连接',
        transport_error: 'network'
      }));
  }

  function workbenchSyncFailureMessage(result, fallbackPayload = {}) {
    const raw = clean(result?.stderr || result?.error || '');
    const statusCode = result?.status || result?.http_status || '';
    const summary = talentSyncSummary(result);
    const pending = Number(summary.pending_review || 0) > 0 || /pending_review|no_unique_match|unique/i.test(raw);
    if (pending) {
      return `${lookupIssueMessage(result, fallbackPayload)}；未写入 A 系统。`;
    }
    if (result?.transport_error === 'non_json' || /JSON|Unexpected token|Unexpected end|返回格式异常/i.test(raw)) {
      return '插件同步服务返回格式异常；未写入 A 系统，请重启或检查本机 8765 猎聘工作台服务。';
    }
    if (result?.transport_error === 'http' || /^HTTP\s+\d+/.test(raw) || statusCode) {
      const code = statusCode || (raw.match(/HTTP\s+(\d+)/) || [])[1] || '未知';
      return `插件同步服务暂不可用（HTTP ${code}）；未写入 A 系统，请确认本机 8765 猎聘工作台服务后重试。`;
    }
    if (result?.transport_error === 'network' || /Failed to fetch|NetworkError|Load failed|ECONN|ERR_|timeout/i.test(raw)) {
      return '插件同步服务网络异常；未写入 A 系统，请确认本机 8765 猎聘工作台服务已启动后重试。';
    }
    return raw ? `同步失败：${raw}；未写入 A 系统。` : '同步失败：请确认本机 8765 猎聘工作台服务已启动。';
  }

  function getFromWorkbench(path, params = {}) {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      const text = clean(value);
      if (text) query.set(key, text);
    });
    const suffix = query.toString() ? `?${query.toString()}` : '';
    return fetch(`${WORKBENCH_BASE}${path}${suffix}`, {
      method: 'GET',
      cache: 'no-store'
    })
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`HTTP ${res.status}`)))
      .catch(() => null);
  }

  function openAsaFloating() {
    window.open(FLOATING_URL, '_blank', 'noopener,noreferrer');
  }

  function bridgeCandidateContext() {
    const project = readManualProject();
    if (isLiepinResumeDetailPage()) {
      const resume = state.currentContext?.resume || readResumeContext();
      const identity = extractPrimaryWorkIdentity(resume);
      return {
        name: resume?.name || '',
        company: identity?.company?.value || '',
        title: resume?.titleCompanyLine || '',
        resume_id: resume?.resumeId || resumeIdFromUrl(location.href),
        profile_summary: (resume?.fullText || '').slice(0, 1200)
      };
    }
    const context = state.currentContext || readPageContext();
    return {
      name: normalizeName(context?.contact?.name || state.lastGenerated?.candidateName || ''),
      company: '',
      title: context?.contact?.title || state.lastGenerated?.candidateTitle || '',
      latest_message: context?.latestMessage || '',
      profile_summary: (context?.combinedText || '').slice(0, 1200)
    };
  }

  async function reportFloatingContext(userSelected = false) {
    if (!isLiepinImPage() && !isLiepinResumeDetailPage()) return;
    if (document.visibilityState && document.visibilityState !== 'visible') return;
    const activeUserSelected = Boolean(userSelected || Date.now() < state.floatingUserSelectedUntil);
    const project = readManualProject();
    const candidate = bridgeCandidateContext();
    const payload = {
      surface: 'liepin',
      instance_id: state.bridgeInstanceId,
      plugin: 'liepin-reply-assistant',
      version: EXTENSION_VERSION,
      url: location.href,
      title: document.title,
      page_type: isLiepinResumeDetailPage() ? 'resume_detail' : 'im_chat',
      page_visible: document.visibilityState === 'visible',
      page_focused: document.hasFocus(),
      user_selected: activeUserSelected,
      candidate,
      candidate_name: candidate.name,
      company: candidate.company,
      candidate_title: candidate.title,
      client: project?.client || '',
      job: project?.position || '',
      source_url: location.href,
      status: state.lastStatus || '',
      talent_lookup: state.lastTalentLookupMatch || null,
      actions: ['refresh_bridge', 'assess_current', 'fill_resume', 'draft_outreach', 'generate_report', 'open_source']
    };
    const result = await postToWorkbench('/api/asa/floating/context', payload);
    state.bridgeStatus = result?.ok ? 'ASA 已连接' : `ASA 未连接：${result?.error || 'unknown'}`;
    renderBridgeStatusDot();
  }

  async function reportFloatingCommandResult(command, statusKey, message, result = {}) {
    const payload = {
      surface: 'liepin',
      instance_id: state.bridgeInstanceId,
      plugin: 'liepin-reply-assistant',
      version: EXTENSION_VERSION,
      command_id: command?.id || '',
      action: clean(command?.action || command?.command || ''),
      status: statusKey || 'completed',
      message: clean(message || ''),
      url: location.href,
      page_type: isLiepinResumeDetailPage() ? 'resume_detail' : (isLiepinImPage() ? 'im_chat' : 'unknown'),
      candidate: bridgeCandidateContext(),
      result
    };
    await postToWorkbench('/api/asa/floating/command-result', payload).catch(() => null);
    await reportFloatingContext(true).catch(() => null);
  }

  function showPanelForBridgeAction() {
    if (!document.getElementById(ROOT_ID)) boot();
    const root = document.getElementById(ROOT_ID);
    if (root && state.collapsed) {
      state.collapsed = false;
      root.dataset.collapsed = 'false';
      renderBridgeStatusDot();
    }
    window.focus();
  }

  let lastFloatingUserReportAt = 0;
  function reportFloatingUserActivity() {
    const now = Date.now();
    if (now - lastFloatingUserReportAt < 1500) return;
    lastFloatingUserReportAt = now;
    state.floatingUserSelectedUntil = now + 10000;
    reportFloatingContext(true);
  }

  function renderBridgeStatusDot() {
    const toggle = document.getElementById(TOGGLE_ID);
    if (!toggle) return;
    toggle.textContent = state.collapsed ? 'ASA' : '收起';
    toggle.title = `${state.bridgeStatus || 'ASA 桥接'} · v${EXTENSION_VERSION}`;
    toggle.dataset.bridge = /已连接/.test(state.bridgeStatus || '') ? 'connected' : 'disconnected';
  }

  async function handleFloatingCommand(command) {
    const action = clean(command?.action);
    if (!action) return;
    if (action === 'open_floating') {
      openAsaFloating();
      return;
    }
    if (action === 'open_source') {
      window.focus();
      status('当前页就是猎聘源页面');
      return;
    }
    if (isLiepinResumeDetailPage()) {
      if (action === 'fill_resume') {
        await handleFloatingFillResume(command);
        return;
      }
      if (['refresh_bridge', 'assess_current'].includes(action)) {
        await refreshMatchPanel();
        await reportFloatingCommandResult(command, 'completed', '猎聘页面已重新识别当前简历。');
        return;
      }
      if (action === 'draft_outreach' || action === 'generate_report') {
        switchResumeView('recommendation');
        status(action === 'generate_report' ? '报告生成请在 ASA 工作流中审批执行' : '已切到推荐文案');
        return;
      }
    } else {
      if (['refresh_bridge', 'draft_outreach', 'assess_current'].includes(action)) {
        await generate();
        return;
      }
    }
    status(`ASA 命令已收到：${action}`);
  }

  async function pollFloatingCommands() {
    const result = await getFromWorkbench('/api/asa/floating/commands', { surface: 'liepin', instance_id: state.bridgeInstanceId });
    const commands = Array.isArray(result?.commands) ? result.commands : [];
    for (const command of commands) {
      await handleFloatingCommand(command);
    }
  }

  async function postTalentAction(payload) {
    return postToWorkbench('/api/talent-action', payload);
  }

  async function postCandidateReply(payload) {
    return postToWorkbench('/api/candidate-reply', payload);
  }

  function rememberSyncState(key, ok) {
    try {
      chrome.storage.local.set({
        [key]: {
          ok: !!ok,
          at: new Date().toISOString()
        }
      });
    } catch (_) {
      // best-effort only
    }
  }

  const STRATEGIES = {
    asks_company: {
      label: '询问公司/要求',
      goal: '补足透明度，减少不确定感',
      risk: '如果仍含糊，人选容易流失',
      sampleCount: 2
    },
    job_detail_request: {
      label: '索要岗位详情',
      goal: '先给岗位职责、亮点和关键要求，再引导到一次短沟通',
      risk: '只说“发您看看”会缺少筛选动作',
      sampleCount: 0
    },
    location_check: {
      label: '地点/出差确认',
      goal: '先确认地点、出差或办公模式是否接受',
      risk: '地点不清会影响意愿判断',
      sampleCount: 0
    },
    anchor_first_probe: {
      label: '先补锚点',
      goal: '先把客户、岗位或方向讲清，再决定是否推进',
      risk: '项目没锚点时不要急着约电话',
      sampleCount: 0
    },
    positive_fit: {
      label: '正向匹配',
      goal: '抓住高意向窗口，转入电话或微信深聊',
      risk: '只回复“可以聊”会浪费一次推进机会',
      sampleCount: 4
    },
    contact_exchange: {
      label: '交换联系方式',
      goal: '承接联系方式，并同步确认关键筛选点',
      risk: '只收联系方式，后面还要二次追问',
      sampleCount: 15
    },
    salary: {
      label: '薪资沟通',
      goal: '确认当前薪资结构、期望区间和可谈空间',
      risk: '过早承诺薪资会被动',
      sampleCount: 1
    },
    mismatch_or_reject: {
      label: '不匹配/拒绝',
      goal: '尊重反馈，保留关系并沉淀排除原因',
      risk: '继续强推会损伤关系',
      sampleCount: 4
    },
    broad_semiconductor_wechat: {
      label: '泛半导体触达',
      goal: '低成本建立联系',
      risk: '过于宽泛，容易被判断为群发',
      sampleCount: 24
    },
    general_followup: {
      label: '通用跟进',
      goal: '把沟通拉回岗位、动机、地点、薪资四个关键点',
      risk: '信息不足，需要人工补充判断',
      sampleCount: 0
    }
  };

  const PROJECT_RULES = [
    {
      key: 'silanmicro_technical_marketing',
      re: /士兰微.*(?:技术市场经理|技术市场|产品市场|三次电源|服务器电源|AI服务器电源|VRM|DrMOS|POL|Power Stage|Multiphase|多相)|(?:技术市场经理|技术市场|产品市场|三次电源|服务器电源|AI服务器电源|VRM|DrMOS|POL|Power Stage|Multiphase|多相).*士兰微/i,
      client: '士兰微',
      position: '技术市场经理（三次电源/服务器或PC市场）',
      confidence: '高'
    },
    {
      key: 'longyue_senior_mechanical',
      re: /长越科技.*(?:机械高级工程师|高级机械工程师|精密设备|微米级|亚微米级|精密平台|运动台|光栅尺|直线电机)|(?:机械高级工程师|高级机械工程师|精密设备|微米级|亚微米级|精密平台|运动台|光栅尺|直线电机).*长越科技|(?:上海微电子|SMEE|华为|超精密半导体设备|光刻机|EUV).*(?:机械高级工程师|高级机械工程师|精密设备|精密机械|运动台|微动平台|光栅尺|直线电机|振动|热变形)|(?:机械高级工程师|高级机械工程师|精密设备|精密机械|运动台|微动平台|光栅尺|直线电机|振动|热变形).*(?:上海微电子|SMEE|华为|超精密半导体设备|光刻机|EUV)/i,
      client: '长越科技',
      position: '机械高级工程师',
      confidence: '高'
    },
    {
      key: 'longyue_senior_automation_software',
      re: /长越科技.*(?:自动化软件高级工程师|自动化软件|运动控制|EtherCAT|TwinCAT|Codesys|PLC|HMI|上位机|控制软件)|(?:自动化软件高级工程师|自动化软件|运动控制|EtherCAT|TwinCAT|Codesys|PLC|HMI|上位机|控制软件).*长越科技|(?:EtherCAT|TwinCAT|Codesys|运动控制|PLC|HMI|上位机|控制软件).*(?:自动化软件|半导体设备)|(?:自动化软件|半导体设备).*(?:EtherCAT|TwinCAT|Codesys|运动控制|PLC|HMI|上位机|控制软件)/i,
      client: '长越科技',
      position: '自动化软件高级工程师',
      confidence: '高'
    },
    {
      key: 'longyue_senior_electrical',
      re: /长越科技.*(?:电气高级工程师|化学品供应系统|CDS|特气|PLC|防爆|ATEX|本安栅|SEMI)|(?:电气高级工程师|化学品供应系统|CDS|特气|PLC|防爆|ATEX|本安栅|SEMI).*长越科技/i,
      client: '长越科技',
      position: '电气高级工程师（化学品供应系统）',
      confidence: '高'
    },
    {
      key: 'longyue_failure_analysis',
      re: /长越科技.*(?:高级失效分析工程师|失效分析|FA|FIB|SEM|TEM|EDS|XPS|良率分析)|(?:高级失效分析工程师|失效分析|FA|FIB|SEM|TEM|EDS|XPS|良率分析).*长越科技/i,
      client: '长越科技',
      position: '高级失效分析工程师',
      confidence: '高'
    },
    {
      key: 'sukesi_fpga',
      re: /苏科思.*(?:FPGA技术主管|FPGA主管|FPGA负责人|FPGA经理|SoC\s*FPGA)|(?:FPGA技术主管|FPGA主管|FPGA负责人|FPGA经理|SoC\s*FPGA).*苏科思/i,
      client: '苏科思',
      position: 'FPGA技术主管',
      confidence: '高'
    },
    {
      key: 'sukesi_hardware_manager',
      re: /苏科思.*(?:硬件技术主管|硬件主管|硬件负责人|硬件经理|硬件平台|硬件架构|驱控硬件)|(?:硬件技术主管|硬件主管|硬件负责人|硬件经理|硬件平台|硬件架构|驱控硬件).*苏科思/,
      client: '苏科思',
      position: '硬件技术主管',
      confidence: '高'
    },
    {
      key: 'sukesi_senior_mechanical',
      re: /苏科思.*(?:资深机械|机械工程师|机械设计)|(?:资深机械|机械工程师|机械设计).*苏科思/,
      client: '苏科思',
      position: '资深机械工程师',
      confidence: '高'
    },
    {
      key: 'weida_procurement',
      re: /微导纳米.*(?:双采购岗|采购岗|采购经理|采购工程师|采购专员|采购主管)|(?:双采购岗|采购岗|采购经理|采购工程师|采购专员|采购主管).*微导纳米/i,
      client: '微导纳米',
      position: '双采购岗',
      confidence: '高'
    },
    {
      key: 'device',
      re: /device\s*专家|device专家/i,
      client: '鹏新旭',
      position: 'Device专家',
      confidence: '高'
    },
    {
      key: 'pengxinxu_pqe',
      re: /鹏新旭.*(?:PQE|质量专家|产品质量|制程质量|客户质量|品质专家)|(?:PQE|质量专家|产品质量|制程质量|客户质量|品质专家).*鹏新旭/i,
      client: '鹏新旭',
      position: 'PQE专家',
      confidence: '高'
    },
    {
      key: 'mechanical',
      re: /机械工程师|资深机械|机械设计|机械研发/,
      client: '',
      position: '机械相关岗位',
      confidence: '低'
    },
    {
      key: 'acdc',
      re: /acdc|服务器电源研发总监/i,
      client: '视源电子',
      position: 'ACDC服务器电源研发总监',
      confidence: '高'
    },
    {
      key: 'fpga',
      re: /fpga|fpag/i,
      client: '',
      position: 'FPGA相关岗位',
      confidence: '低'
    },
    {
      key: 'power_hardware',
      re: /电力电子|硬件开发|hardware development|电源硬件/i,
      client: '',
      position: '硬件/电力电子研发相关岗位',
      confidence: '低'
    },
    {
      key: 'power',
      re: /电源|电源研发|拓扑|线路原理|power/i,
      client: '',
      position: '电源研发相关岗位',
      confidence: '低'
    },
    {
      key: 'pqe',
      re: /pqe|质量专家|质量工程师|质量管理/i,
      client: '',
      position: 'PQE质量相关岗位',
      confidence: '低'
    }
  ];

  const PROJECT_OPTIONS = [
    { key: 'auto', label: '自动识别', client: '', position: '', confidence: '自动' },
    { key: 'silanmicro_technical_marketing', label: '士兰微 - 技术市场经理（三次电源/服务器PC）', client: '士兰微', position: '技术市场经理（三次电源/服务器或PC市场）', confidence: '手动选择' },
    { key: 'longyue_senior_mechanical', label: '长越科技 - 机械高级工程师', client: '长越科技', position: '机械高级工程师', confidence: '手动选择' },
    { key: 'longyue_senior_automation_software', label: '长越科技 - 自动化软件高级工程师', client: '长越科技', position: '自动化软件高级工程师', confidence: '手动选择' },
    { key: 'longyue_senior_electrical', label: '长越科技 - 电气高级工程师（化学品供应系统）', client: '长越科技', position: '电气高级工程师（化学品供应系统）', confidence: '手动选择' },
    { key: 'longyue_failure_analysis', label: '长越科技 - 高级失效分析工程师', client: '长越科技', position: '高级失效分析工程师', confidence: '手动选择' },
    { key: 'sukesi_hardware_manager', label: '苏科思 - 硬件技术主管', client: '苏科思', position: '硬件技术主管', confidence: '手动选择' },
    { key: 'sukesi_fpga', label: '苏科思 - FPGA技术主管', client: '苏科思', position: 'FPGA技术主管', confidence: '手动选择' },
    { key: 'sukesi_senior_mechanical', label: '苏科思 - 资深机械工程师', client: '苏科思', position: '资深机械工程师', confidence: '手动选择' },
    { key: 'weida_procurement', label: '微导纳米 - 双采购岗', client: '微导纳米', position: '双采购岗', confidence: '手动选择' },
    { key: 'weida_mechanical', label: '微导纳米 - 机械工程师', client: '微导纳米', position: '机械工程师', confidence: '手动选择' },
    { key: 'pengxinxu_device', label: '鹏新旭 - Device专家', client: '鹏新旭', position: 'Device专家', confidence: '手动选择' },
    { key: 'pengxinxu_pqe', label: '鹏新旭 - PQE专家', client: '鹏新旭', position: 'PQE专家', confidence: '手动选择' },
    { key: 'acdc_director', label: 'ACDC服务器电源研发总监', client: '', position: 'ACDC服务器电源研发总监', confidence: '手动选择' },
    { key: 'fpga', label: 'FPGA相关岗位', client: '', position: 'FPGA相关岗位', confidence: '手动选择' },
    { key: 'power_hardware', label: '硬件/电力电子研发相关岗位', client: '', position: '硬件/电力电子研发相关岗位', confidence: '手动选择' },
    { key: 'power', label: '电源研发相关岗位', client: '', position: '电源研发相关岗位', confidence: '手动选择' },
    { key: 'pqe', label: 'PQE质量相关岗位', client: '', position: 'PQE质量相关岗位', confidence: '手动选择' },
    { key: 'custom', label: '手动输入客户/岗位', client: '', position: '', confidence: '手动输入' }
  ];

  function clean(text) {
    return String(text || '')
      .replace(/\u00a0/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }

  function escapeHtml(text) {
    return clean(text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function compactText(text, maxLength = 220) {
    const value = clean(text);
    return value.length > maxLength ? `${value.slice(0, maxLength)}...` : value;
  }

  function escapeRegExp(text) {
    return clean(text).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }

  function textOf(el) {
    return clean(el ? el.innerText || el.textContent : '');
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' &&
      style.visibility !== 'hidden' &&
      rect.width > 0 &&
      rect.height > 0;
  }

  function stripReadStatus(text) {
    return clean(text).replace(/^\[(?:已读|未读|手机)\]\s*/, '');
  }

  function isLiepinImPage() {
    return /liepin\.com\/im\/showmsgnewpage/.test(location.href);
  }

  function isLiepinResumeDetailPage() {
    return /liepin\.com\/resume\/showresumedetail/.test(location.href);
  }

  const POSITION_MATCH_PROFILES = window.LIEPIN_MATCH_PROFILES || {
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
        { re: /EtherCAT|TwinCAT|Codesys|CoDeSys|PLC|HMI|运动控制|Motion Control|伺服|轴控|多轴|插补|同步控制|实时控制/i, points: 26, text: '命中 EtherCAT/TwinCAT/Codesys/PLC/HMI/运动控制等核心控制平台' },
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
    }
  };

  function findActiveContactElement() {
    const preferredSelectors = [
      '.im-ui-contact-list-item.active',
      '.im-ui-contact-list-item.is-active',
      '.im-ui-contact-list-item.selected',
      '.im-ui-contact-list-item[class*="active"]',
      '.im-ui-contact-list-item[class*="selected"]',
      '[class*="contact-list-item"][class*="active"]',
      '[class*="contact-list-item"][aria-selected="true"]'
    ];

    for (const selector of preferredSelectors) {
      const found = Array.from(document.querySelectorAll(selector)).find(isVisible);
      if (found) return found;
    }

    return Array.from(document.querySelectorAll('.im-ui-contact-list-item, [class*="contact-list-item"]'))
      .find(el => {
        const rect = el.getBoundingClientRect();
        return isVisible(el) && rect.left < Math.max(420, window.innerWidth * 0.45);
      }) || null;
  }

  function readContactFromElement(el) {
    if (!el) return null;
    const name = textOf(el.querySelector('.im-ui-contact-title-main, [class*="title-main"], [class*="name"]'));
    const title = textOf(el.querySelector('.im-ui-contact-title-sub, [class*="title-sub"], [class*="position"], [class*="sub"]'));
    const preview = stripReadStatus(textOf(el.querySelector('.im-ui-last-message, .im-ui-contact-item-message, [class*="last-message"], [class*="message"]')));
    const rawText = textOf(el);
    return {
      name: name || inferNameFromText(rawText),
      title,
      preview,
      rawText
    };
  }

  function inferNameFromText(text) {
    const value = clean(text);
    const match = value.match(/([\u4e00-\u9fa5]{2,4}(?:先生|女士|老师)?)/);
    return match ? match[1] : '';
  }

  function readChatAreaText() {
    const selectors = [
      '.im-ui-chat-content-wrapper',
      '[class*="chat-content"]',
      '[class*="message-list"]',
      '[class*="conversation"]'
    ];

    for (const selector of selectors) {
      const el = Array.from(document.querySelectorAll(selector)).find(isVisible);
      const text = textOf(el);
      if (text && !/未选中会话|请选择|暂无/.test(text)) return text;
    }
    return '';
  }

  function cleanChatMessageText(value) {
    return stripReadStatus(clean(value))
      .replace(/^(?:今天|昨天)?\s*\d{1,2}:\d{2}\s*/, '')
      .replace(/^(沟通职位|推荐职位)[：:].*$/g, '')
      .replace(/\s*(已读|未读)$/g, '')
      .trim();
  }

  function directionalMessageEvidence(direction) {
    return window.LIEPIN_MESSAGE_EVIDENCE?.directionalMessage?.(document, direction, { isVisible }) || null;
  }

  function currentConversationSnapshot() {
    return window.LIEPIN_MESSAGE_EVIDENCE?.conversationIdentity?.(document, location) || {
      conversationId: '',
      confidence: 'missing',
      candidateName: '',
      candidateTitle: '',
      snapshotKey: ''
    };
  }

  function conversationSnapshotMatches(snapshot, current = currentConversationSnapshot()) {
    return Boolean(window.LIEPIN_MESSAGE_EVIDENCE?.conversationSnapshotMatches?.(snapshot, current));
  }

  function readLatestReceivedMessageText() {
    const evidence = directionalMessageEvidence('received');
    if (evidence?.text) return evidence.text;
    const selectors = [
      '.im-ui-message-list-wrapper .im-ui-message-item-body.im-ui-message-item-receive',
      '.im-ui-chat-list .im-ui-message-item-body.im-ui-message-item-receive',
      '[class*="message-list"] [class*="message-item-receive"]',
      '[class*="chat-list"] [class*="message-item-receive"]'
    ];
    const items = selectors
      .flatMap(selector => Array.from(document.querySelectorAll(selector)))
      .filter(isVisible);
    for (const item of items.reverse()) {
      const clone = item.cloneNode(true);
      clone.querySelectorAll?.('[class*="extra-info"], [class*="read-status"], [class*="time"]').forEach(el => el.remove());
      const text = cleanChatMessageText(textOf(clone) || textOf(item));
      if (text && text.length >= 2 && !/沟通职位|推荐职位|索要简历|索要手机|索要微信|发送/.test(text)) {
        return text;
      }
    }
    return '';
  }

  function pickLatestHumanMessage(contact, chatText) {
    const candidates = [];
    if (contact?.preview) candidates.push(contact.preview);
    if (chatText) {
      const parts = chatText
        .split(/\n|(?<=。)|(?<=！)|(?<=？)/)
        .map(stripReadStatus)
        .filter(Boolean)
        .filter(line => line.length >= 2 && line.length <= 500)
        .filter(line => !/发送|常用语|表情|图片|未选中会话|请输入/.test(line));
      candidates.push(...parts.slice(-8).reverse());
    }
    return candidates.find(line => /[\u4e00-\u9fa5a-zA-Z]/.test(line)) || '';
  }

  function readPageContext() {
    const contactEl = findActiveContactElement();
    const contact = readContactFromElement(contactEl) || {
      name: '',
      title: '',
      preview: '',
      rawText: ''
    };
    const conversationText = readChatAreaText();
    const latestReceivedEvidence = directionalMessageEvidence('received');
    const latestSentEvidence = directionalMessageEvidence('sent');
    const latestReceivedMessage = clean(latestReceivedEvidence?.text || '');
    const latestMessage = latestReceivedMessage || pickLatestHumanMessage(contact, conversationText);
    const conversation = currentConversationSnapshot();
    const combined = [
      contact.name,
      contact.title,
      contact.preview,
      contact.rawText,
      conversationText,
      document.title
    ].filter(Boolean).join(' ');

    return {
      contact,
      conversationText,
      latestMessage,
      latestReceivedMessage,
      latestReceivedEvidence,
      latestSentEvidence,
      conversation,
      combinedText: clean(combined)
    };
  }

  function detectStrategy(text) {
    const value = clean(text);
    if (/已找到|已经找到|入职了|不找了|暂时稳定|暂不考虑|不考虑|没兴趣|不合适|不匹配|不对口|不是技术|不是.*岗|不是.*专业|专业不|区域不|地点不|暂不/.test(value)) return 'mismatch_or_reject';
    if (/微信|手机号|电话|联系方式|联系我|简历|附件|邮箱|加我/.test(value)) return 'contact_exchange';
    if (/薪资|待遇|年包|总包|月薪|多少钱|预算|可谈|期望|职级|title|股票|奖金|公积金/.test(value)) return 'salary';
    if (/地点|在哪|哪里|base|办公|通勤|出差|外派|远程|城市|上海|苏州|杭州|深圳|北京|成都/.test(value)) return 'location_check';
    if (/岗位详情|岗位介绍|岗位信息|JD|jd|职责|工作内容|要求发我|发.*(?:岗位|资料|介绍|详情|JD|jd)|(?:岗位|资料|介绍|详情|JD|jd).*发|先.*看看|看一下/.test(value)) return 'job_detail_request';
    if (/哪家公司|公司是哪|哪个公司|什么公司|客户|行业|规模|融资|工作年限|几年经验|要求|岗位要求/.test(value)) return 'asks_company';
    if (/匹配|感兴趣|详聊|还在招|可以聊|进一步沟通|应聘|期待|方便沟通|符合|可以了解|想了解|发来看看/.test(value)) return 'positive_fit';
    if (/半导体|机会|看看机会|随时沟通|加个微信/.test(value)) return 'broad_semiconductor_wechat';
    return 'general_followup';
  }

  // 岗位状态黑名单（2026-07-22 产品裁决）：status 命中任一关键词即不可入库/不可自动采用。
  // 与 a_system_agent/job_status.py、talent_system_sync.py 中的同名单保持同步。
  const JOB_INTAKE_BLOCKED_KEYWORDS = ['待启动', '暂停', '关闭', 'closed', '只读快照', '已拆分', '误归属', '归档'];
  function jobStatusIntakeBlocked(status) {
    const text = clean(status).toLowerCase();
    if (!text) return false;
    return JOB_INTAKE_BLOCKED_KEYWORDS.some(keyword => text.includes(keyword.toLowerCase()));
  }

  function dynamicProjectOptions() {
    return (state.projectOptions || [])
      .filter(option => !['auto', 'custom'].includes(option.key))
      .filter(option => option.source === 'talent_system_v3' || option.confidence === 'A系统v3岗位库' || String(option.key || '').startsWith('v3_'))
      .filter(option => clean(option.client) && clean(option.position))
      .filter(option => !jobStatusIntakeBlocked(option.status));
  }

  function projectKeywordTokens(position) {
    const value = clean(position);
    const tokens = new Set();
    const add = token => {
      const cleaned = clean(token);
      if (cleaned && cleaned.length >= 2) tokens.add(cleaned);
    };
    add(value);
    value.split(/[\\/、,，｜|（）()\s\-_:：]+/).forEach(add);
    const patterns = [
      /AMHS/i, /MES/i, /CIM/i, /PQE/i, /Device/i, /Etch/i, /Litho/i, /PVD/i, /CVD/i, /CMP/i,
      /PIE/i, /YE/i, /EES/i, /DBA/i, /APC/i, /FUR/i, /PDE/i, /FPGA/i, /ACDC/i, /TCB/i,
      /IE\s*Cost/i, /MFG\s*CIM/i, /Power\s*Stage/i, /DrMOS/i, /VRM/i, /POL/i,
      /机械高级工程师|机械工程师|资深机械|机械设计/g,
      /自动化软件|上位机软件|软件专家|软件工程师/g,
      /电气高级工程师|电气工程师/g,
      /失效分析|可靠性/g,
      /技术市场|产品市场|三次电源|服务器电源|PC电源|电源专家|电源研发/g,
      /硬件技术主管|硬件主管|硬件平台|硬件架构|驱控硬件|电力电子/g,
      /采购|非标采购|紧急采购|半导体CIP/g,
      /质量|品质|客户质量|制程质量/g,
      /数据分析|基础设施|网络|运维|设备专家|工艺专家|主任工程师/g
    ];
    patterns.forEach(pattern => {
      if (pattern.global) {
        Array.from(value.matchAll(pattern)).forEach(match => add(match[0]));
      } else {
        const match = value.match(pattern);
        if (match) add(match[0]);
      }
    });
    return Array.from(tokens).filter(token => {
      const normalized = normalizeProjectText(token);
      return normalized.length >= 2 && !['工程师', '专家', '主管', '经理', '主任', '资深', '高级', '技术'].includes(normalized);
    });
  }

  function scoreDynamicProject(option, text) {
    const value = clean(text);
    const normalizedText = normalizeProjectText(value);
    const client = clean(option.client);
    const position = clean(option.position);
    const normalizedClient = normalizeProjectText(client);
    const normalizedPosition = normalizeProjectText(position);
    let score = 0;
    const reasons = [];
    if (normalizedClient && normalizedText.includes(normalizedClient)) {
      score += 55;
      reasons.push('客户命中');
    }
    if (normalizedPosition && normalizedText.includes(normalizedPosition)) {
      score += 80;
      reasons.push('岗位全称命中');
    }
    const tokenHits = projectKeywordTokens(position).filter(token => {
      const normalized = normalizeProjectText(token);
      return normalized && normalizedText.includes(normalized);
    });
    if (tokenHits.length) {
      score += Math.min(45, tokenHits.length * 18);
      reasons.push(`岗位关键词：${tokenHits.slice(0, 4).join('/')}`);
    }
    if (/P0[-\s]*最急|用户指定最高优先级|谈薪中/.test(`${option.priority || ''} ${option.status || ''}`)) {
      score += 4;
    }
    return { option, score, reasons, tokenHits };
  }

  function isGenericNoClientPosition(position) {
    const normalized = normalizeProjectText(position);
    return [
      '机械工程师',
      '机械相关岗位',
      '资深机械工程师',
      '硬件主管',
      '硬件技术主管',
      '电源专家',
      '电源研发相关岗位',
      '硬件电力电子研发相关岗位',
      'PQE专家',
      'PQE质量相关岗位',
      '质量专家',
      '质量工程师'
    ].some(item => normalized === normalizeProjectText(item) || normalized.includes(normalizeProjectText(item)));
  }

  function detectDynamicProject(text) {
    const options = dynamicProjectOptions();
    if (!options.length) return null;
    const scored = options
      .map(option => scoreDynamicProject(option, text))
      .filter(item => item.score >= 50)
      .sort((a, b) => b.score - a.score || projectPriorityRank(a.option) - projectPriorityRank(b.option));
    if (!scored.length) return null;
    const [best, second] = scored;
    const hasClientHit = best.reasons.includes('客户命中');
    const hasFullPositionHit = best.reasons.includes('岗位全称命中');
    const hasSpecificPositionEvidence = hasFullPositionHit || (
      Array.isArray(best.tokenHits) &&
      best.tokenHits.some(token => !isGenericNoClientPosition(token))
    );
    const isUniqueEnough = !second || best.score - second.score >= 18;
    const canUseNoClientPosition = hasFullPositionHit && !isGenericNoClientPosition(best.option.position);
    const canAutoUse = (
      hasClientHit &&
      hasSpecificPositionEvidence &&
      best.score >= 70 &&
      isUniqueEnough
    ) || (canUseNoClientPosition && best.score >= 78 && isUniqueEnough);
    if (!canAutoUse) {
      return {
        client: '',
        position: '',
        confidence: '待确认',
        rule: 'dynamic_ambiguous',
        candidates: scored.slice(0, 5).map(item => ({
          client: item.option.client,
          position: item.option.position,
          score: item.score,
          reasons: item.reasons
        }))
      };
    }
    return {
      client: best.option.client,
      position: best.option.position,
      confidence: hasClientHit || hasFullPositionHit ? 'A系统岗位库-高' : 'A系统岗位库-中',
      rule: best.option.key,
      source: best.option.source || 'talent_system_v3',
      reasons: best.reasons
    };
  }

  function detectProject(text) {
    const value = clean(text);
    const dynamic = detectDynamicProject(value);
    if (dynamic) return dynamic;
    for (const rule of PROJECT_RULES) {
      if (rule.re.test(value)) {
        return {
          client: rule.client,
          position: rule.position,
          confidence: rule.confidence,
          rule: rule.key
        };
      }
    }
    return {
      client: '',
      position: '',
      confidence: '待确认',
      rule: 'none'
    };
  }

  function getProjectOption(key) {
    const options = state.projectOptions?.length ? state.projectOptions : PROJECT_OPTIONS;
    return options.find(item => item.key === key) || PROJECT_OPTIONS[0];
  }

  function projectOptionKey(client, position, prefix = 'v3') {
    const normalized = normalizeProjectText(`${client}_${position}`) || 'project';
    return `${prefix}_${normalized.slice(0, 80)}`;
  }

  function baseProjectOptions() {
    return PROJECT_OPTIONS.filter(option => option.key !== 'custom');
  }

  function projectPriorityRank(option) {
    const priority = clean(option?.priority);
    const status = clean(option?.status);
    const text = `${priority} ${status}`;
    if (jobStatusIntakeBlocked(status)) return 99;
    if (/P0[-\s]*最急|用户指定最高优先级|谈薪中/.test(text)) return 0;
    if (/^P0|P0紧急|P0-/.test(text)) return 1;
    if (/^P1|P1-/.test(text)) return 2;
    if (/已发布|推进中|已搜索|可筛人|已触达/.test(status)) return 3;
    if (/搜索|反馈|待启动|计划/.test(status)) return 4;
    return 9;
  }

  function projectPriorityLabel(option) {
    const priority = clean(option?.priority);
    const status = clean(option?.status);
    const text = `${priority} ${status}`;
    if (/P0[-\s]*最急|用户指定最高优先级|谈薪中/.test(text)) return 'P0最急';
    if (/^P0|P0紧急|P0-/.test(text)) return 'P0';
    if (/^P1|P1-/.test(text)) return 'P1';
    return '';
  }

  function compareProjectOptions(a, b) {
    const rankDelta = projectPriorityRank(a) - projectPriorityRank(b);
    if (rankDelta) return rankDelta;
    const orderA = Number(a?.source_order);
    const orderB = Number(b?.source_order);
    if (Number.isFinite(orderA) && Number.isFinite(orderB) && orderA !== orderB) return orderA - orderB;
    const dateDelta = Date.parse(clean(b?.updated_at) || 0) - Date.parse(clean(a?.updated_at) || 0);
    if (Number.isFinite(dateDelta) && dateDelta) return dateDelta;
    return `${clean(a?.client)} ${clean(a?.position)}`.localeCompare(`${clean(b?.client)} ${clean(b?.position)}`, 'zh-Hans-CN');
  }

  function mergeProjectOptions(dynamicPositions = []) {
    const merged = [];
    const seen = new Set();
    const add = option => {
      const key = option.key || projectOptionKey(option.client, option.position);
      const client = clean(option.client);
      const position = clean(option.position);
      const identity = `${normalizeProjectText(client)}|${normalizeProjectText(position)}`;
      if (!key || seen.has(key) || (client || position) && seen.has(identity)) return;
      seen.add(key);
      if (client || position) seen.add(identity);
      merged.push({
        ...option,
        key,
        client,
        position,
        label: option.label || `${client} - ${position}`,
        confidence: option.confidence || '岗位库'
      });
    };

    add(PROJECT_OPTIONS.find(option => option.key === 'auto') || { key: 'auto', label: '自动识别', client: '', position: '', confidence: '自动' });
    dynamicPositions
      .map((position, index) => ({ ...position, source_order: index }))
      .sort(compareProjectOptions)
      .forEach(position => {
      const client = clean(position.client);
      const title = clean(position.position || position.title);
      if (!client || !title) return;
      const priority = clean(position.priority);
      const priorityLabel = projectPriorityLabel(position);
      const status = clean(position.status);
      const blockedSuffix = jobStatusIntakeBlocked(status) ? `（${status}·不可入库）` : '';
      add({
        key: projectOptionKey(client, title, position.source === 'talent_system_v3' ? 'v3' : 'pool'),
        label: `${priorityLabel ? `${priorityLabel}｜` : ''}${client} - ${title}${blockedSuffix}`,
        client,
        position: title,
        confidence: position.source === 'talent_system_v3' ? 'A系统v3岗位库' : '岗位库',
        source: position.source || 'context',
        status,
        priority,
        updated_at: clean(position.updated_at),
        source_order: position.source_order
      });
    });
    baseProjectOptions().filter(option => option.key !== 'auto').forEach(add);
    add(PROJECT_OPTIONS.find(option => option.key === 'custom') || { key: 'custom', label: '手动输入客户/岗位', client: '', position: '', confidence: '手动输入' });
    return merged;
  }

  function renderProjectOptions(select, selectedValue = '') {
    if (!select) return;
    const options = state.projectOptions?.length ? state.projectOptions : PROJECT_OPTIONS;
    const previous = selectedValue || select.value || 'auto';
    select.innerHTML = '';
    options.forEach(option => {
      const el = document.createElement('option');
      el.value = option.key;
      el.textContent = option.label;
      if (option.status || option.priority) el.title = `${option.client} / ${option.position} / ${option.priority || '普通优先级'} / ${option.status || ''}`;
      select.appendChild(el);
    });
    select.value = options.some(option => option.key === previous) ? previous : 'auto';
    updateProjectPickerState(select);
  }

  function updateProjectPickerState(select = null) {
    const projectSelect = select || document.querySelector('#liepin-reply-project-select');
    const picker = projectSelect?.closest('.lpra-project-picker');
    if (!projectSelect || !picker) return;
    picker.dataset.mode = projectSelect.value === 'custom' ? 'custom' : 'preset';
  }

  function hasDynamicProjectOptions() {
    const options = state.projectOptions || [];
    return options.length >= 20 && options.some(option =>
      option.source === 'talent_system_v3' ||
      option.confidence === 'A系统v3岗位库' ||
      String(option.key || '').startsWith('v3_')
    );
  }

  async function hydrateProjectOptionsFromWorkbench(root = document, options = {}) {
    const select = root?.querySelector('#liepin-reply-project-select');
    if (!select) return;
    if (state.projectOptionsHydration) return state.projectOptionsHydration;
    const silent = options.silent === true;
    state.projectOptionsHydration = (async () => {
      state.projectOptions = mergeProjectOptions();
      renderProjectOptions(select, select.value);
      const result = await getFromWorkbench('/api/context');
      if (!Array.isArray(result?.positions)) {
        if (!silent) status('岗位库未连接，使用内置岗位列表');
        return;
      }
      const previous = select.value;
      state.projectOptions = mergeProjectOptions(result.positions);
      state.lastProjectOptionsSyncAt = Date.now();
      renderProjectOptions(select, previous);
      if (isLiepinResumeDetailPage() && !state.projectUserTouched) {
        const clientInput = root.querySelector('#liepin-reply-project-client');
        const positionInput = root.querySelector('#liepin-reply-project-position');
        select.value = 'auto';
        if (clientInput) clientInput.value = '';
        if (positionInput) positionInput.value = '';
        updateProjectPickerState(select);
        state.selectedProject = null;
        state.projectResolvedFrom = '';
      }
      if (select.value !== 'auto') {
        const option = getProjectOption(select.value);
        const clientInput = root.querySelector('#liepin-reply-project-client');
        const positionInput = root.querySelector('#liepin-reply-project-position');
        if (select.value !== 'custom' && clientInput && positionInput) {
          clientInput.value = option.client;
          positionInput.value = option.position;
          updateProjectPickerState(select);
          state.selectedProject = readManualProject();
        }
      }
      if (!silent || !hasDynamicProjectOptions()) {
        status(`岗位库已同步：${Math.max(0, state.projectOptions.length - 2)} 个岗位`);
      }
    })();
    try {
      await state.projectOptionsHydration;
    } finally {
      state.projectOptionsHydration = null;
    }
  }

  function ensureDynamicProjectOptions(root = document) {
    const select = root?.querySelector('#liepin-reply-project-select');
    if (!select || hasDynamicProjectOptions()) return;
    const lastSyncAgeMs = Date.now() - (state.lastProjectOptionsSyncAt || 0);
    if (state.projectOptionsHydration || lastSyncAgeMs < 5000) return;
    hydrateProjectOptionsFromWorkbench(root, { silent: true });
  }

  function readManualProject() {
    const select = document.querySelector('#liepin-reply-project-select');
    const clientInput = document.querySelector('#liepin-reply-project-client');
    const positionInput = document.querySelector('#liepin-reply-project-position');
    const key = select?.value || 'auto';
    if (key === 'auto') return null;

    const option = getProjectOption(key);
    const client = clean(clientInput?.value) || option.client;
    const position = clean(positionInput?.value) || option.position;
    if (!client && !position) return null;

    return {
      client,
      position,
      confidence: key === 'custom' ? '手动输入' : '手动选择',
      rule: key
    };
  }

  function persistProjectChoice(project) {
    try {
      chrome.storage.local.set({
        [PROJECT_STORE_KEY]: project
      });
    } catch (_) {
      // storage is best-effort only
    }
  }

  function syncProjectInputsFromSelect() {
    const select = document.querySelector('#liepin-reply-project-select');
    const clientInput = document.querySelector('#liepin-reply-project-client');
    const positionInput = document.querySelector('#liepin-reply-project-position');
    if (!select || !clientInput || !positionInput) return;

    const option = getProjectOption(select.value);
    if (select.value === 'auto') {
      clientInput.value = '';
      positionInput.value = '';
    } else if (select.value !== 'custom') {
      clientInput.value = option.client;
      positionInput.value = option.position;
    }
    updateProjectPickerState(select);

    const choice = {
      key: select.value,
      client: clean(clientInput.value),
      position: clean(positionInput.value)
    };
    state.selectedProject = readManualProject();
    persistProjectChoice(choice);
    status(select.value === 'auto' ? '已切回自动识别岗位' : '已使用手动岗位，生成时优先采用');
  }

  function hydrateProjectChoice() {
    try {
      chrome.storage.local.get(PROJECT_STORE_KEY, stored => {
        const value = stored?.[PROJECT_STORE_KEY];
        const select = document.querySelector('#liepin-reply-project-select');
        const clientInput = document.querySelector('#liepin-reply-project-client');
        const positionInput = document.querySelector('#liepin-reply-project-position');
        if (!select || !clientInput || !positionInput || !value) return;
        const options = state.projectOptions?.length ? state.projectOptions : PROJECT_OPTIONS;
        select.value = options.some(item => item.key === value.key) ? value.key : 'custom';
        clientInput.value = clean(value.client);
        positionInput.value = clean(value.position);
        updateProjectPickerState(select);
        state.selectedProject = readManualProject();
      });
    } catch (_) {
      // storage is best-effort only
    }
  }

  function normalizeName(name) {
    const value = clean(name);
    if (!value) return '';
    const match = value.match(/[\u4e00-\u9fa5]{2,4}(?:先生|女士|老师)?/);
    return match ? match[0] : value.slice(0, 12);
  }

  function surnameOf(name) {
    const value = normalizeName(name).replace(/先生$|女士$|老师$/g, '');
    if (!value) return '';
    const compoundSurnames = [
      '欧阳', '司马', '诸葛', '上官', '司徒', '东方', '南宫', '夏侯',
      '皇甫', '尉迟', '公孙', '赫连', '澹台', '轩辕', '令狐', '钟离',
      '宇文', '长孙', '慕容', '端木', '独孤', '拓跋', '百里', '公冶'
    ];
    const compound = compoundSurnames.find(item => value.startsWith(item));
    return compound || value.slice(0, 1);
  }

  function seniorityFromTitle(title) {
    const value = clean(title);
    if (!value) return 'unknown';
    if (/总经理|副总|总监|经理|负责人|部长|总裁|合伙人|创始人|董事|vp|vice president|director|manager|head|leader/i.test(value)) {
      return 'senior';
    }
    if (/工程师|研发|技术|算法|硬件|软件|机械|电气|电子|工艺|设备|质量|测试|设计|专家|架构师|scientist|researcher|architect|engineer|developer/i.test(value)) {
      return 'engineer';
    }
    return 'unknown';
  }

  function addressName(name, title) {
    const normalized = normalizeName(name);
    const surname = surnameOf(normalized);
    const seniority = seniorityFromTitle(title);
    if (surname && seniority === 'senior') return `${surname}总`;
    if (surname && seniority === 'engineer') return `${surname}工`;
    if (normalized) return normalized;
    return '';
  }

  function salutation(name, title) {
    const value = addressName(name, title);
    if (!value) return '您好';
    return `${value}，您好`;
  }

  function projectLabel(project) {
    if (project.client && project.position) return `${project.client}的${project.position}`;
    if (project.position) return project.position;
    if (project.client) return `${project.client}的岗位`;
    return '这个机会';
  }

  function normalizeProjectText(value) {
    return clean(value).replace(/[\\/\s（）()·,，、\-_:：]/g, '').toLowerCase();
  }

  function findProjectOptionForProject(project) {
    const client = normalizeProjectText(project?.client || '');
    const position = normalizeProjectText(project?.position || '');
    const options = (state.projectOptions?.length ? state.projectOptions : PROJECT_OPTIONS);
    return options.find(option => {
      if (['auto', 'custom'].includes(option.key)) return false;
      const optionClient = normalizeProjectText(option.client || '');
      const optionPosition = normalizeProjectText(option.position || '');
      return (!client || !optionClient || client === optionClient) &&
        position &&
        (
          optionPosition === position ||
          optionPosition.includes(position) ||
          position.includes(optionPosition)
        );
    }) || null;
  }

  function applyProjectSelection(project, sourceLabel, persist = true) {
    const select = document.querySelector('#liepin-reply-project-select');
    const clientInput = document.querySelector('#liepin-reply-project-client');
    const positionInput = document.querySelector('#liepin-reply-project-position');
    const client = clean(project?.client || '');
    const position = clean(project?.position || '');
    if (!select || !clientInput || !positionInput || (!client && !position)) return false;

    const option = findProjectOptionForProject(project);
    if (option) {
      select.value = option.key;
      clientInput.value = option.client;
      positionInput.value = option.position;
    } else {
      select.value = 'custom';
      clientInput.value = client;
      positionInput.value = position;
    }
    updateProjectPickerState(select);
    state.selectedProject = readManualProject();
    state.projectResolvedFrom = sourceLabel || '';
    if (persist) {
      persistProjectChoice({
        key: select.value,
        client: clean(clientInput.value),
        position: clean(positionInput.value),
        source: sourceLabel || ''
      });
    }
    return true;
  }

  function resumeIdFromUrl(value) {
    try {
      return new URL(value || location.href).searchParams.get('res_id_encode') || '';
    } catch (_) {
      return String(value || location.href).split('res_id_encode=')[1]?.split('&')[0] || '';
    }
  }

  function resumePageKey(resume = null) {
    const resumeId = clean(resume?.resumeId || resumeIdFromUrl(location.href));
    return resumeId ? `liepin:${resumeId}` : `url:${location.href}`;
  }

  function isUsableResumeIdentity(resume) {
    return Boolean(clean(resume?.resumeId || resumeIdFromUrl(location.href)) && clean(resume?.name));
  }

  function stabilizeResumeIdentity(resume) {
    if (!isLiepinResumeDetailPage() || !resume) return resume;
    const key = resumePageKey(resume);
    if (state.lockedResumeKey && state.lockedResumeKey !== key) {
      state.lockedResumeKey = '';
      state.lockedResumeIdentity = null;
    }
    if (!state.lockedResumeIdentity && isUsableResumeIdentity(resume)) {
      state.lockedResumeKey = key;
      state.lockedResumeIdentity = {
        resumeId: clean(resume.resumeId || resumeIdFromUrl(location.href)),
        name: clean(resume.name),
        titleCompanyLine: clean(resume.titleCompanyLine),
        statusText: clean(resume.statusText),
        fullText: clean(resume.fullText)
      };
      return resume;
    }
    if (!state.lockedResumeIdentity || state.lockedResumeKey !== key) return resume;
    const locked = state.lockedResumeIdentity;
    const stable = {
      ...resume,
      resumeId: locked.resumeId || resume.resumeId,
      name: locked.name || resume.name,
      titleCompanyLine: locked.titleCompanyLine || resume.titleCompanyLine,
      statusText: locked.statusText || resume.statusText
    };
    if (!locked.titleCompanyLine && clean(resume.titleCompanyLine)) locked.titleCompanyLine = clean(resume.titleCompanyLine);
    if (!locked.statusText && clean(resume.statusText)) locked.statusText = clean(resume.statusText);
    const currentFullText = clean(resume.fullText);
    if (currentFullText && locked.name && currentFullText.includes(locked.name) && currentFullText.length > clean(locked.fullText).length) {
      locked.fullText = currentFullText;
    }
    stable.fullText = clean(locked.fullText) || resume.fullText;
    return stable;
  }

  function extractPrimaryWorkIdentity(resume) {
    const lines = String(resume?.workRawText || '')
      .split('\n')
      .map(clean)
      .filter(Boolean);
    const companyRe = /公司|集团|科技|半导体|设备|电子|微电子|精科|华为|研究所|研究院|中心|厂|Ltd|Inc|Corp/i;
    const titleRe = /工程师|经理|专家|主管|负责人|架构师|设计师|研发|总监|部长|主任|产品经理|leader|manager|engineer/i;
    const timeRe = /\d{4}\.\d{2}\s*-\s*(?:至今|\d{4}\.\d{2})/;

    for (let i = 0; i < lines.length; i += 1) {
      if (!timeRe.test(lines[i])) continue;
      const company = lines.slice(Math.max(0, i - 3), i).reverse().find(line =>
        companyRe.test(line) && !titleRe.test(line) && line.length <= 80
      ) || '';
      const title = lines.slice(i + 1, i + 8).find(line =>
        titleRe.test(line) && !companyRe.test(line) && line.length <= 80
      ) || '';
      if (company || title) {
        return {
          company,
          title,
          workSummary: lines.slice(Math.max(0, i - 3), Math.min(lines.length, i + 10)).join(' ')
        };
      }
    }

    return {
      company: '',
      title: '',
      workSummary: lines.slice(0, 30).join(' ')
    };
  }

  async function resolveProjectFromWorkbench(context = {}, options = {}) {
    const force = options.force === true;
    const shouldRefresh = options.refresh !== false;
    if (state.projectUserTouched && !force) return null;
    const resume = context.resume || null;
    const contact = context.contact || null;
    const workIdentity = resume ? extractPrimaryWorkIdentity(resume) : {};
    const params = {
      source_url: isLiepinImPage() ? '' : location.href,
      resume_id: resumeIdFromUrl(location.href),
      candidate_name: resume?.name || contact?.name || state.lastGenerated?.candidateName || '',
      candidate_company: workIdentity.company || '',
      candidate_title: workIdentity.title || resume?.titleCompanyLine || contact?.title || state.lastGenerated?.candidateTitle || '',
      candidate_profile_text: [
        resume?.titleCompanyLine || contact?.title || '',
        state.lastGenerated?.candidateTitle || '',
        state.currentContext?.combinedText || '',
        workIdentity.workSummary || '',
        resume?.workRawText || '',
        resume?.projectRawText || ''
      ].map(clean).filter(Boolean).join('\n').slice(0, 3000)
    };
    const signature = JSON.stringify(params);
    if (!force && signature === state.lastProjectLookupSignature) return null;
    state.lastProjectLookupSignature = signature;

    const result = await getFromWorkbench('/api/recent-outreach-project', params);
    if (!result?.matched || !result.project?.position || (state.projectUserTouched && !force)) return result;
    const exactMatchReasons = Array.isArray(result.match?.reasons)
      ? result.match.reasons
      : [];
    const hasExactWorkbenchLink = exactMatchReasons.some(reason =>
      reason === '简历链接精确匹配' || reason === '来源链接完全一致'
    );
    const canAutoApplyProject = result.auto_apply === true ||
      result.match?.auto_apply === true ||
      hasExactWorkbenchLink;
    if (!canAutoApplyProject) {
      const matchText = result.match?.confidence ? `触达记录${result.match.confidence}置信` : '触达记录';
      status(`找到${matchText}疑似岗位，未自动切换：${projectLabel(result.project)}`);
      return result;
    }
    const applied = applyProjectSelection(result.project, '触达记录', !isLiepinResumeDetailPage());
    if (!applied) return result;

    const matchText = result.match?.confidence ? `触达记录${result.match.confidence}置信` : '触达记录';
    status(`已按${matchText}识别岗位：${projectLabel(result.project)}`);
    if (isLiepinResumeDetailPage()) {
      refreshMatchPanel();
    } else if (isLiepinImPage() && shouldRefresh) {
      generate();
    }
    return result;
  }

  async function resolveResumeProjectForMatch(resume) {
    if (state.projectUserTouched) return readManualProject();
    if (state.projectResolvedFrom === 'A系统定位') return readManualProject();
    const select = document.querySelector('#liepin-reply-project-select');
    const clientInput = document.querySelector('#liepin-reply-project-client');
    const positionInput = document.querySelector('#liepin-reply-project-position');
    if (select) select.value = 'auto';
    if (clientInput) clientInput.value = '';
    if (positionInput) positionInput.value = '';
    updateProjectPickerState(select);
    state.selectedProject = null;
    if (!hasDynamicProjectOptions()) {
      await hydrateProjectOptionsFromWorkbench(document, { silent: true }).catch(() => null);
    }
    await resolveProjectFromWorkbench({ resume });
    const linkedProject = readManualProject();
    if (linkedProject && (state.projectUserTouched || state.projectResolvedFrom)) return linkedProject;
    const detected = detectDynamicProject([
      resume?.titleCompanyLine,
      resume?.intentionText,
      resume?.workRawText,
      resume?.projectRawText,
      resume?.educationRawText
    ].map(clean).filter(Boolean).join('\n'));
    if (detected?.client && detected?.position && !/待确认|低/.test(detected.confidence || '')) {
      applyProjectSelection(detected, '简历自动识别', false);
      status(`已按简历自动识别岗位：${projectLabel(detected)}`);
      return readManualProject();
    }
    if (detected?.rule === 'dynamic_ambiguous') {
      status('岗位自动识别不唯一，请手动选择推荐岗位');
    }
    return null;
  }

  function restoreProjectChoiceInto(root) {
    if (isLiepinResumeDetailPage()) return;
    try {
      chrome.storage.local.get(PROJECT_STORE_KEY, stored => {
        const value = stored?.[PROJECT_STORE_KEY];
        const select = root?.querySelector('#liepin-reply-project-select');
        const clientInput = root?.querySelector('#liepin-reply-project-client');
        const positionInput = root?.querySelector('#liepin-reply-project-position');
        if (!select || !clientInput || !positionInput || !value) return;
        const options = state.projectOptions?.length ? state.projectOptions : PROJECT_OPTIONS;
        select.value = options.some(item => item.key === value.key) ? value.key : 'custom';
        clientInput.value = clean(value.client);
        positionInput.value = clean(value.position);
        updateProjectPickerState(select);
        state.selectedProject = readManualProject();
        if (isLiepinResumeDetailPage()) refreshMatchPanel();
      });
    } catch (_) {
      // storage is best-effort only
    }
  }

  function fallbackMatchProfileForResume(resume, profiles) {
    const titleText = clean(resume?.titleCompanyLine || '');
    const text = [
      resume?.titleCompanyLine,
      resume?.intentionText,
      resume?.workRawText,
      resume?.projectRawText,
      resume?.educationRawText
    ].map(clean).filter(Boolean).join('\n');
    if (
      /PQE|CQE|\bQE\b|QRA|QRE|客户质量|产品质量|制程质量|供应商质量|质量(?:经理|专家|工程师|主管|负责人)|品质(?:经理|专家|工程师|主管|负责人)|SPC|8D|FMEA/i.test(text) ||
      (/良率|Yield|缺陷|defect/i.test(text) && /质量|品质|PQE|CQE|\bQE\b/i.test(text)) ||
      (/(12吋|12寸|300mm)/i.test(text) && /PQE|CQE|\bQE\b|质量(?:经理|专家|工程师|主管|负责人)|品质(?:经理|专家|工程师|主管|负责人)|SPC|FMEA|8D/i.test(text))
    ) {
      return profiles.pengxinxu_pqe || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (/FPGA|RTL|Verilog|VHDL|时序|CDC|Vivado|Quartus/i.test(text)) {
      return profiles.sukesi_fpga || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (
      /机械|机械设计|结构设计|资深机械|高级机械|精密设备/i.test(titleText) &&
      !/FAE|AE|现场应用|电源|硬件|电气|FPGA|PQE|CQE|采购/i.test(titleText)
    ) {
      if (/精密定位|运动台|气浮|静压|直驱|光刻|光机|亚微米|纳米级|长越科技/i.test(text)) {
        return profiles.longyue_senior_mechanical || profiles.generic_mechanical || Object.values(profiles)[0];
      }
      return profiles.generic_mechanical || profiles.sukesi_senior_mechanical || Object.values(profiles)[0];
    }
    if (/MPS|Monolithic|英飞凌|Infineon|瑞萨|Renesas|德州仪器|Texas Instruments|\bTI\b|芯朋微|安森美|onsemi|FAE|AE|现场应用|应用工程|三次电源|VRM|DrMOS|POL|Power Stage|技术市场|产品市场|Design[- ]?in|服务器电源|PC电源|电源管理/i.test(text)) {
      return profiles.silanmicro_technical_marketing || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (/自动化软件|设备软件|控制软件|上位机|EtherCAT|TwinCAT|Codesys|PLC|HMI|运动控制|C#|C\+\+|LabVIEW/i.test(text)) {
      return profiles.longyue_automation_software || profiles.generic_hardware || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (/硬件|驱控|控制器|驱动器|原理图|PCB|EMC|电控|电路|bring-up/i.test(text)) {
      return profiles.sukesi_hardware_manager || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (/采购|供应链|供应商|寻源|议价|交期|BOM|非标件|机加工件/i.test(text)) {
      return profiles.weida_procurement || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    if (/精密定位|运动台|气浮|静压|直驱|光刻|光机|亚微米|纳米级|长越科技/i.test(text)) {
      return profiles.longyue_senior_mechanical || profiles.generic_mechanical || Object.values(profiles)[0];
    }
    return profiles.generic_mechanical || profiles.sukesi_senior_mechanical || Object.values(profiles)[0];
  }

  function getActiveMatchProfile(resume = null) {
    const profiles = window.LIEPIN_MATCH_PROFILES || POSITION_MATCH_PROFILES;
    const manual = readManualProject();
    if (
      manual?.rule === 'longyue_senior_mechanical' ||
      (
        /长越科技/.test(manual?.client || '') &&
        /机械高级工程师|高级机械工程师|机械工程师|精密设备|精密机械/i.test(manual.position || '')
      )
    ) {
      return profiles.longyue_senior_mechanical ||
        profiles.generic_mechanical ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.longyue_senior_mechanical;
    }
    if (
      manual?.rule === 'longyue_senior_automation_software' ||
      (
        /长越科技/.test(manual?.client || '') &&
        /自动化软件高级工程师|自动化软件|运动控制|EtherCAT|TwinCAT|Codesys|PLC|HMI|上位机|控制软件/i.test(manual.position || '')
      )
    ) {
      const baseProfile = profiles.longyue_automation_software ||
        POSITION_MATCH_PROFILES.longyue_automation_software ||
        profiles.generic_hardware ||
        profiles.sukesi_hardware_manager ||
        profiles.longyue_senior_mechanical ||
        profiles.generic_mechanical ||
        POSITION_MATCH_PROFILES.longyue_senior_mechanical;
      return {
        ...baseProfile,
        client: '长越科技',
        position: '自动化软件高级工程师',
        label: '长越科技 · 自动化软件高级工程师',
        domain: 'automation_software'
      };
    }
    if (
      manual?.rule === 'weida_procurement' ||
      (
        /微导纳米/.test(manual?.client || '') &&
        /双采购岗|采购岗|采购经理|采购工程师|采购专员|采购主管/i.test(manual.position || '')
      )
    ) {
      return profiles.weida_procurement ||
        profiles.generic_procurement ||
        profiles.generic_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'weida_mechanical' ||
      (
        /微导纳米/.test(manual?.client || '') &&
        /机械工程师|机械设计|资深机械/i.test(manual.position || '')
      )
    ) {
      return profiles.generic_mechanical ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'pengxinxu_pqe' ||
      (
        /鹏新旭/.test(manual?.client || '') &&
        /PQE|质量专家|产品质量|制程质量|客户质量|品质专家/i.test(manual.position || '')
      )
    ) {
      return profiles.pengxinxu_pqe ||
        profiles.generic_quality ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'pqe' ||
      /PQE|质量专家|质量工程师|质量管理|品质工程|QE|QRA|QRE/i.test(manual?.position || '')
    ) {
      return profiles.generic_quality ||
        profiles.pengxinxu_pqe ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'silanmicro_technical_marketing' ||
      (
        /士兰微/.test(manual?.client || '') &&
        /技术市场经理|技术市场|产品市场|三次电源|服务器电源|AI服务器电源|VRM|DrMOS|POL|Power Stage|Multiphase|多相/i.test(manual.position || '')
      )
    ) {
      return profiles.silanmicro_technical_marketing ||
        profiles.generic_power_marketing ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'sukesi_fpga' ||
      (
        manual?.client === '苏科思' &&
        /FPGA技术主管|FPGA主管|FPGA负责人|FPGA经理|SoC\s*FPGA/i.test(manual.position || '')
      )
    ) {
      return profiles.sukesi_fpga ||
        profiles.generic_fpga ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'sukesi_hardware_manager' ||
      (
        manual?.client === '苏科思' &&
        /硬件技术主管|硬件主管|硬件负责人|硬件经理|硬件平台|硬件架构|驱控硬件/.test(manual.position || '')
      )
    ) {
      return profiles.sukesi_hardware_manager ||
        profiles.generic_hardware ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'power_hardware' ||
      /硬件\/电力电子研发相关岗位|硬件开发|电力电子|硬件平台|硬件架构|驱控硬件/i.test(manual?.position || '')
    ) {
      return profiles.generic_hardware ||
        profiles.sukesi_hardware_manager ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'acdc_director' ||
      /ACDC服务器电源研发总监|服务器电源研发总监|ACDC/i.test(manual?.position || '')
    ) {
      return profiles.silanmicro_technical_marketing ||
        profiles.generic_power_marketing ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'power' ||
      /电源研发相关岗位|电源研发|拓扑|线路原理|power/i.test(manual?.position || '')
    ) {
      return profiles.generic_power_marketing ||
        profiles.silanmicro_technical_marketing ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (/FPGA/i.test(manual?.position || '')) {
      return profiles.sukesi_fpga ||
        profiles.generic_fpga ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (
      manual?.rule === 'sukesi_senior_mechanical' ||
      (manual?.client === '苏科思' && /资深机械/.test(manual.position || '')) ||
      manual?.rule === 'weida_mechanical' ||
      /机械工程师|机械设计|机械研发/i.test(manual?.position || '')
    ) {
      return profiles.sukesi_senior_mechanical || profiles.generic_mechanical || POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (/机械/.test(manual?.position || '')) {
      return profiles.generic_mechanical || profiles.sukesi_senior_mechanical || POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    if (/硬件技术|硬件主管|硬件负责人|硬件经理|硬件平台|硬件架构|驱控硬件|电控|电路/.test(manual?.position || '')) {
      return profiles.sukesi_hardware_manager ||
        profiles.generic_hardware ||
        profiles.sukesi_senior_mechanical ||
        POSITION_MATCH_PROFILES.sukesi_senior_mechanical;
    }
    return fallbackMatchProfileForResume(resume, profiles);
  }

  function getRecommendationCopy() {
    const api = window.LIEPIN_RECOMMENDATION_COPY;
    if (!api?.buildRecommendationCopy || !state.currentContext || !state.lastGenerated) return null;
    const resume = state.currentContext.resume || readResumeContext();
    const profile = getActiveMatchProfile(resume);
    const result = state.currentContext.result || state.lastGenerated;
    return api.buildRecommendationCopy(result, resume, profile);
  }

  function matchProfileMetaLabel(profile) {
    const project = readManualProject();
    if (isConcreteProject(project)) return `岗位：${profile.label}`;
    return `评分画像：${profile.label}（未绑定岗位）`;
  }

  function renderRecommendationPanel(copy, resume, profile) {
    const detail = document.querySelector('#liepin-reply-assistant-detail');
    const meta = document.querySelector('#liepin-reply-assistant-meta');
    const score = document.querySelector('#liepin-reply-assistant-score');
    const statusEl = document.querySelector('#liepin-reply-assistant-status');
    if (!detail || !copy) return;

    const currentMode = state.currentRecommendationMode;
    const text = copy[currentMode] || '';
    const splitRecommendation = value => {
      const lines = String(value || '').split('\n').map(clean).filter(Boolean);
      const sections = {
        overview: [],
        evidence: [],
        risks: [],
        next: []
      };
      let active = 'overview';
      lines.forEach(line => {
        if (/^\d+\.\s*核心匹配点/.test(line) || /^核心匹配点/.test(line)) {
          active = 'evidence';
          return;
        }
        if (/^\d+\.\s*风险点/.test(line) || /^风险点/.test(line)) {
          active = 'risks';
          return;
        }
        if (/^\d+\.\s*推进建议|^\d+\.\s*观察条件|^推进建议|^观察条件/.test(line)) {
          active = 'next';
          sections.next.push(line.replace(/^\d+\.\s*/, ''));
          return;
        }
        sections[active].push(line.replace(/^\d+\.\s*/, ''));
      });
      return sections;
    };
    const sections = { overview: [text], evidence: [], risks: [], next: [] };
    const sectionTabs = [
      { key: 'overview', label: currentMode === 'outreachGreeting' ? '打招呼语' : '推荐文案', count: 0 }
    ];
    const renderModuleItems = (wrap, sectionKey, items) => {
      const list = document.createElement('div');
      list.className = `lpra-module-list lpra-module-${sectionKey}`;
      const parseEvidence = item => {
        const [head, raw = ''] = item.split('；原文：');
        const parts = head.split('：');
        const meta = parts.slice(1).join('：');
        const cleanedRaw = clean(raw).replace(/；；/g, '；');
        return {
          title: parts[0] || '匹配证据',
          meta,
          raw: cleanedRaw && cleanedRaw !== meta ? cleanedRaw : ''
        };
      };
      const parseRisk = item => {
        const [label, rest = item] = item.includes('：') ? item.split(/：(.+)/) : ['风险核实', item];
        const [basis, action = ''] = rest.split(/，需|，建议/);
        return {
          label,
          basis,
          action: action ? `需${action}` : ''
        };
      };
      items.forEach((item, index) => {
        const card = document.createElement('div');
        card.className = 'lpra-module-item';
        const no = document.createElement('i');
        no.textContent = String(index + 1);
        card.appendChild(no);

        const body = document.createElement('div');
        body.className = 'lpra-module-item-body';
        if (sectionKey === 'evidence') {
          const parsed = parseEvidence(item);
          body.innerHTML = '<strong></strong><em></em><p></p>';
          body.querySelector('strong').textContent = parsed.title;
          body.querySelector('em').textContent = parsed.meta;
          const rawEl = body.querySelector('p');
          rawEl.textContent = parsed.raw;
          if (!parsed.raw) rawEl.remove();
        } else if (sectionKey === 'risks') {
          const parsed = parseRisk(item);
          body.innerHTML = '<strong></strong><em></em><p></p>';
          body.querySelector('strong').textContent = parsed.label;
          body.querySelector('em').textContent = parsed.basis;
          body.querySelector('p').textContent = parsed.action || '建议沟通时单独确认';
        } else {
          body.innerHTML = '<p></p>';
          body.querySelector('p').textContent = item;
        }
        card.appendChild(body);
        list.appendChild(card);
      });
      wrap.appendChild(list);
    };
    if (!sectionTabs.some(item => item.key === state.currentRecommendationSection)) {
      state.currentRecommendationSection = 'overview';
    }

    if (score) {
      score.textContent = `${resume?.score || state.lastGenerated?.score || ''}${resume?.score || state.lastGenerated?.score ? '分' : ''} · ${state.lastGenerated?.grade || ''}`.trim();
    }
    if (statusEl) {
      statusEl.textContent = currentMode === 'outreachGreeting' ? '可直接复制打招呼语' : '可直接复制推荐文案';
    }
    if (meta) {
      meta.textContent = [
        `人选：${resume.name || '未识别'}`,
        resume.titleCompanyLine ? `当前：${compactText(resume.titleCompanyLine, 80)}` : '',
        matchProfileMetaLabel(profile),
        `当前视图：${RECOMMENDATION_MODES.find(item => item.key === currentMode)?.label || '推荐文案'}`
      ].filter(Boolean).join(' ｜ ');
    }

    detail.innerHTML = '';
    delete detail.dataset.view;
    const modeWrap = document.createElement('div');
    modeWrap.className = 'lpra-segmented lpra-mode-segmented';
    RECOMMENDATION_MODES.forEach(mode => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = mode.label;
      btn.className = mode.key === currentMode ? 'lpra-segment-active' : '';
      btn.addEventListener('click', () => {
        state.currentRecommendationMode = mode.key;
        state.currentRecommendationSection = 'overview';
        renderRecommendationPanel(copy, resume, profile);
      });
      modeWrap.appendChild(btn);
    });
    detail.appendChild(modeWrap);

    const activeSection = sections[state.currentRecommendationSection] || sections.overview || [];
    const summaryText = activeSection.length ? activeSection.join('\n') : '暂无内容';
    const row = document.createElement('div');
    row.className = 'lpra-detail-row lpra-detail-row-active lpra-module-row';
    row.innerHTML = `<span></span><b></b>`;
    row.querySelector('span').textContent = sectionTabs.find(item => item.key === state.currentRecommendationSection)?.label || '内容';
    const content = row.querySelector('b');
    if (['evidence', 'risks', 'next'].includes(state.currentRecommendationSection) && activeSection.length) {
      renderModuleItems(content, state.currentRecommendationSection, activeSection);
    } else {
      content.textContent = summaryText;
    }
    detail.appendChild(row);

    if (currentMode === 'customerRecommendation') {
      const fullRow = document.createElement('div');
      fullRow.className = 'lpra-detail-row lpra-copy-row';
      fullRow.innerHTML = '<span>复制全文</span><b></b>';
      fullRow.querySelector('b').textContent = '点“复制当前”会复制完整推荐理由';
      detail.appendChild(fullRow);
    }
  }

  function extractResumeSection(lines, startRe, endRe) {
    const start = lines.findIndex(line => startRe.test(line));
    if (start < 0) return '';
    const rest = lines.slice(start + 1);
    const end = rest.findIndex(line => endRe.test(line));
    return (end >= 0 ? rest.slice(0, end) : rest).join('\n');
  }

  function readResumeContext() {
    const helperRoot = document.getElementById(ROOT_ID);
    const previousDisplay = helperRoot?.style.display || '';
    if (helperRoot) helperRoot.style.display = 'none';
    const rawBodyText = document.body?.innerText || '';
    if (helperRoot) helperRoot.style.display = previousDisplay;
    const lines = rawBodyText
      .split('\n')
      .map(clean)
      .filter(Boolean)
      .filter(line => !/每日任务|你好，|我的主页|个人中心|安全中心|账户资源|用户规则|通话管理|安全退出|金领券|ICP备|查看大图/.test(line));
    const bodyText = clean(lines.join('\n'));
    const resumeId = location.href.match(/res_id_encode=([^&]+)/)?.[1] || '';
    let name = textOf(document.querySelector('.new-resume-personal-name em'));
    const isCandidateNameLine = line =>
      /^[\u4e00-\u9fa5]{1,4}(?:\*{1,2}|先生|女士|老师)?$/.test(line) &&
      !/简历|洞察|中文|英文|查看|收藏|转发|在线|活跃/.test(line);
    if (!isCandidateNameLine(name)) name = '';
    if (!name) {
      const nameLine = lines.find((line, index) =>
        isCandidateNameLine(line) &&
        /在职|离职|暂无跳槽|看看新机会|急寻新工作/.test(lines[index + 1] || '')
      );
      name = nameLine || '';
    }
    const nameIndex = lines.findIndex(line => line === name);
    const nearNameLines = nameIndex >= 0 ? lines.slice(nameIndex + 1, nameIndex + 8) : lines;
    const statusText = nearNameLines.find(line =>
      /在职|离职|已离职|暂无跳槽|看看新机会|急寻新工作|暂不考虑|不看机会/.test(line)
    ) || lines.find(line =>
      /在职|离职|已离职|暂无跳槽|看看新机会|急寻新工作|暂不考虑|不看机会/.test(line) &&
      line.length <= 40
    ) || '';
    const titleCompanyLine = nearNameLines.find(line =>
      /工程师|经理|专家|主管|负责人|架构师|产品经理|技术市场|产品市场|FAE|AE/i.test(line) &&
      /公司|华为|苏科思|士兰微|微电子|半导体|科技|设备|精科|迈为|隐冠|拓荆|屹唐|雅科贝思|科益虹源|英飞凌|瑞萨|芯朋微|德州仪器|MPS|Monolithic|Power Integrations|安森美|维谛|新华三|超聚变|浪潮|联想|富士康|记忆科技/i.test(line)
    ) || lines.find(line =>
      /工程师|经理|专家|主管|负责人|架构师|产品经理|技术市场|产品市场|FAE|AE/i.test(line) &&
      /公司|华为|苏科思|士兰微|微电子|半导体|科技|设备|精科|迈为|隐冠|拓荆|屹唐|雅科贝思|科益虹源|英飞凌|瑞萨|芯朋微|德州仪器|MPS|Monolithic|Power Integrations|安森美|维谛|新华三|超聚变|浪潮|联想|富士康|记忆科技/i.test(line)
    ) || '';
    const intentionMatch = bodyText.match(/求职意向\s+([\s\S]{0,120})工作经历/);
    const workRawText = extractResumeSection(lines, /工作经历/, /项目经历|教育经历|语言能力|我的技能|自我评价|附加信息|简历备注/);
    const projectRawText = extractResumeSection(lines, /项目经历/, /教育经历|语言能力|我的技能|自我评价|附加信息|简历备注|资格证书|培训经历|作品|附件/);
    const educationRawText = extractResumeSection(lines, /教育经历/, /语言能力|我的技能|自我评价|附加信息|简历备注|资格证书|培训经历|作品|附件/);

    const resume = {
      resumeId,
      name: clean(name),
      statusText: clean(statusText),
      titleCompanyLine: clean(titleCompanyLine),
      intentionText: clean(intentionMatch?.[1] || ''),
      workText: clean(workRawText),
      projectText: clean(projectRawText),
      educationText: clean(educationRawText),
      workRawText,
      projectRawText,
      educationRawText,
      fullText: bodyText
    };
    return stabilizeResumeIdentity(resume);
  }

  function hasReadableResumeContext(resume) {
    if (!resume) return false;
    if (clean(resume.name)) return true;
    return clean(resume.titleCompanyLine || resume.workRawText || resume.projectRawText).length > 80;
  }

  function resumeEvidenceQuality(resume, profile) {
    const evidence = window.LIEPIN_RECOMMENDATION_COPY?.extractResumeEvidence
      ? window.LIEPIN_RECOMMENDATION_COPY.extractResumeEvidence(resume, profile)
      : [];
    const concreteEvidence = evidence.filter(item => /工作经历显示|项目经历显示/.test(item));
    const projectEvidence = evidence.filter(item => /项目经历显示/.test(item));
    const topOnly = evidence.length > 0 && concreteEvidence.length === 0;
    const text = [
      resume.workRawText,
      resume.projectRawText,
      resume.educationRawText,
      resume.titleCompanyLine
    ].map(clean).filter(Boolean).join('\n');
    const domain = clean(profile?.domain);
    const strictFabEvidence = /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|12吋fab|300mm\s*fab|300mm线|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix/i.test(text);
    const weakFabRisk = /宜兴中车时代半导体|中车时代半导体|株洲中车时代半导体|中车时代电气|SiC|碳化硅|IGBT|功率器件|功率半导体|车规|封装|封测|化合物半导体|设备供应商/i.test(text);
    const coreEvidence =
      domain === 'quality_pqe'
        ? strictFabEvidence && /loading|负载|SPC|统计过程控制|控制图|过程能力|CPK|PPK|Minitab|JMP|line\s*yield|良率|Yield/i.test(text)
        : domain === 'procurement_semiconductor'
          ? /采购|寻源|议价|供应商|交期|缺料|降本|机加工件|结构件|非标件|BOM|供应链|采购订单|PO/i.test(text)
          : domain === 'hardware_platform'
            ? /驱控|驱动器|控制器|硬件平台|硬件架构|原理图|PCB|采样|编码器|隔离|EMC|bring-up|板级调试|量产导入/i.test(text)
            : domain === 'fpga'
            ? /FPGA|RTL|Verilog|VHDL|时序|CDC|PWM|编码器|总线|仿真|板级验证/i.test(text)
            : domain === 'power_marketing'
              ? /三次电源|多相|VRM|DrMOS|POL|Power Stage|技术市场|产品市场|FAE|AE|Design[- ]?in|design[- ]?in/i.test(text)
              : domain === 'automation_software'
                ? /EtherCAT|TwinCAT|Codesys|PLC|HMI|运动控制|上位机|控制软件|设备软件|C#|C\+\+|LabVIEW|现场调试|联调|SECS|GEM/i.test(text)
                : /运动台|气浮|静压|直驱|精密定位|机械设计|结构设计|仿真|BOM|装配调试/i.test(text);
    return {
      evidence,
      concreteCount: concreteEvidence.length,
      projectCount: projectEvidence.length,
      topOnly,
      coreEvidence,
      strictFabEvidence,
      weakFabRisk
    };
  }

  function scoreResumeAgainstProfile(resume, profile) {
    const text = resume.fullText;
    let score = profile.base || 50;
    const matched = [];
    const risks = [];
    const categories = {
      company: false,
      fabLine: false,
      coreSkill: false,
      engineering: false,
      seniority: false,
      education: false,
      city: false
    };

    for (const rule of profile.targetCompanyRules || []) {
      if (rule.re.test(text)) {
        score += rule.points;
        matched.push(rule.text);
        categories.company = true;
        break;
      }
    }

    let skillHits = 0;
    for (const rule of profile.skillRules || []) {
      if (rule.re.test(text)) {
        score += rule.points;
        matched.push(rule.text);
        skillHits += 1;
        if (profile.domain === 'fpga') {
          if (/FPGA|逻辑架构|RTL|关键模块|时序|CDC|PWM|采样同步|编码器|数据通路|保护逻辑/.test(rule.text)) categories.coreSkill = true;
          if (/仿真|验证|调试|时序|CDC|收敛|问题定位|bring-up|板级/.test(rule.text)) categories.engineering = true;
        } else if (profile.domain === 'hardware_platform') {
          if (/驱控|控制器|驱动器|硬件平台|硬件架构|数字|模拟|电源|采样|编码器|隔离保护|关键硬件方案/.test(rule.text)) categories.coreSkill = true;
          if (/bring-up|波形|边界|EMC|热设计|可靠性|DFM|DFT|认证|生产导入|量产|调试/.test(rule.text)) categories.engineering = true;
        } else if (profile.domain === 'power_marketing') {
          if (/三次电源|多相|VRM|DrMOS|POL|Power Stage|服务器|PC|AI服务器|主板|ODM/.test(rule.text)) categories.coreSkill = true;
          if (/design-in|Design-in|原理图|PCB|调试|验证|客户|FAE|AE|产品定义|路线图|GTM|竞品|推广/i.test(rule.text)) categories.engineering = true;
        } else if (profile.domain === 'quality_pqe') {
          if (/12吋|12寸|12英寸|300mm|300 mm|晶圆厂|Fab|fab|晶圆制造|晶圆产线|半导体产线|前道/.test(rule.text)) categories.fabLine = true;
          if (/loading|负载|装载|上料|载片|SPC|统计过程控制|控制图|过程能力|Minitab|JMP/i.test(rule.text)) categories.coreSkill = true;
          if (/loading|负载|装载|上料|载片|SPC|MSA|GRR|控制图|过程能力|line yield|良率|制程|可靠性|量产质量|NPI|新产品上量|客户审核|报废|质量成本|FMEA|CPK|DOE|质量工具|体系|半导体|晶圆|封装|12吋|12寸|12英寸|300mm|300 mm|Fab|fab|晶圆产线|前道/i.test(rule.text)) categories.engineering = true;
        } else if (profile.domain === 'automation_software') {
          if (/EtherCAT|TwinCAT|Codesys|PLC|HMI|运动控制|控制平台|上位机|设备控制软件/.test(rule.text)) categories.coreSkill = true;
          if (/半导体设备|现场调试|联调|交付|产线导入|问题定位|recipe|SECS|GEM|设备软件|软件架构/i.test(rule.text)) categories.engineering = true;
        } else {
          if (/运动台|气浮|直驱|纳米|定位|硬件|电控|电路|FPGA|高速信号|运动控制|伺服|电机驱动/.test(rule.text)) categories.coreSkill = true;
          if (/机械|工程|仿真|工具|硬件|测试|仪器|调试|量产|EMC|可靠性/.test(rule.text)) categories.engineering = true;
        }
        if (/技术负责|团队带教|评审规范|规范沉淀|代码评审|模块负责|质量专项|主管|专家|主任|负责人|经理|产品线|产品负责人|跨部门|客户推进/.test(rule.text)) categories.seniority = true;
      }
    }

    for (const rule of profile.educationRules || []) {
      if (rule.re.test(text)) {
        score += rule.points;
        matched.push(rule.text);
        categories.education = true;
        break;
      }
    }

    for (const rule of profile.cityRules || []) {
      if (rule.re.test(text)) {
        score += rule.points;
        matched.push(rule.text);
        categories.city = true;
        break;
      }
    }

    for (const rule of profile.riskRules || []) {
      if (rule.re.test(text)) risks.push(rule.text);
    }

    const seniorityEvidenceRe = profile.domain === 'fpga'
      ? /FPGA主管|FPGA经理|FPGA负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|代码评审|设计规范|仿真规范|规范沉淀|问题复盘|团队带教|带教/
      : profile.domain === 'hardware_platform'
        ? /硬件主管|硬件经理|硬件负责人|技术负责人|项目负责人|模块负责人|团队负责人|下属人数|设计规范|技术评审|评审机制|规范沉淀|方案复盘|团队带教|带教/
        : profile.domain === 'automation_software'
          ? /自动化软件主管|软件主管|软件经理|软件负责人|技术负责人|项目负责人|模块负责人|架构师|团队负责人|代码评审|平台化|标准化|规范沉淀|问题复盘|团队带教|带教/i
        : profile.domain === 'power_marketing'
          ? /技术市场经理|产品市场经理|Technical Marketing Manager|Product Marketing Manager|产品经理|产品负责人|产品线|roadmap|GTM|客户推广|Design[- ]?in|design[- ]?in|竞品分析|市场分析|团队负责人|负责人|经理/i
        : profile.domain === 'quality_pqe'
            ? /PQE主管|PQE经理|CQE主管|CQE经理|质量主管|质量经理|品质主管|品质经理|主任工程师|资深|高级|专家|质量负责人|客户质量负责人|MRB主导|MRB会议|质量专项|8D负责人|FA负责人|QRA负责人|Leader|Lead|Staff/i
            : null;
    if (seniorityEvidenceRe && seniorityEvidenceRe.test(text)) {
      categories.seniority = true;
    }

    if (profile.domain === 'quality_pqe') {
      const fabLineEvidenceRe = /12吋|12寸|12英寸|12\s*inch|12-inch|300\s*mm|300mm|12吋线|12寸线|12英寸线|300mm线|12吋fab|300mm\s*fab|300mm线|上海华力|华力集成|长鑫|长江存储|中芯|SMIC|华虹|晶合集成|粤芯|台积电|TSMC|联电|UMC|SK海力士|Hynix/i;
      if (fabLineEvidenceRe.test(text)) categories.fabLine = true;
    }

    if (profile.domain === 'quality_pqe' && !categories.fabLine) {
      score = Math.min(score - 16, 72);
      risks.push(profile.fabLineGapText || '12吋fab产线背景不明确');
    }
    if (!categories.coreSkill) {
      score -= 10;
      risks.push(profile.coreSkillGapText || '未明显看到运动台/气浮/精密定位主线');
    }
    if (!categories.company) {
      score -= 6;
      risks.push(profile.targetCompanyGapText || '目标公司相似度不够明确');
    }
    if (!categories.engineering) {
      score -= 5;
      risks.push(profile.engineeringGapText || '机械设计/仿真/工程落地信息不足');
    }
    if (!categories.seniority && (profile.domain === 'hardware_platform' || profile.domain === 'fpga' || profile.domain === 'power_marketing' || profile.domain === 'quality_pqe' || profile.domain === 'automation_software')) {
      score = Math.min(score, 78);
      risks.push(profile.seniorityGapText || '主管/资深专家层级待核实');
    }
    if (!categories.city) {
      score -= 3;
      risks.push(profile.cityGapText || '苏州或长三角接受度待确认');
    }
    if (skillHits >= 4) score += 5;

    const quality = resumeEvidenceQuality(resume, profile);
    if (profile.domain === 'quality_pqe' && quality.weakFabRisk && !quality.strictFabEvidence) {
      score = Math.min(score - 18, 58);
      risks.push('场景不符：疑似功率器件/化合物半导体/封测/设备或泛半导体质量经历，不能等同鹏新旭12吋fab loading PQE主线');
    }
    if (!quality.coreEvidence) {
      score = Math.min(score - 8, 66);
      risks.push('核心证据不足：未在工作/项目经历中看到目标岗位关键证据，需人工复核后再推进');
    }
    if (quality.concreteCount === 0) {
      score = Math.min(score - 12, 58);
      risks.push('证据来源偏弱：当前只识别到标题/教育或零散关键词，缺少可引用的工作/项目原文');
    } else if (quality.concreteCount === 1 && score >= 82) {
      score = Math.min(score, 78);
      risks.push('证据厚度不足：仅识别到一条具体工作/项目证据，建议补看完整经历再评 A');
    } else if (quality.topOnly && score >= 68) {
      score = Math.min(score, 62);
      risks.push('证据来源偏弱：顶部职位命中不能单独支撑推荐结论');
    }

    const benchmark = profile.benchmarkCompanyRe.test(text);
    if (benchmark) score = Math.min(score, 76);
    const bounded = Math.max(20, Math.min(96, score));
    const grade = benchmark ? '标杆样本' : bounded >= 82 ? 'A 级优先' : bounded >= 68 ? 'B 级可沟通' : bounded >= 55 ? 'C 级复核' : '暂缓';
    const targetLocation = profile.targetLocationName || '苏州/长三角';
    const action = benchmark
      ? '学习履历，不触达'
      : bounded >= 82
        ? `优先沟通，重点确认${targetLocation}/薪资/意愿`
        : bounded >= 68
          ? '可以沟通，先确认地点和看机会意愿'
          : bounded >= 55
            ? '先复核完整经历，再决定是否沟通'
            : '暂缓，除非客户放宽画像';

    return {
      score: bounded,
      grade,
      action,
      matched: [...new Set(matched)].slice(0, 8),
      risks: [...new Set(risks)].slice(0, 6),
      benchmark
    };
  }

  function renderMatchPanel(result, resume, profile) {
    const signature = JSON.stringify({
      url: location.href,
      score: result.score,
      grade: result.grade,
      action: result.action,
      matched: result.matched,
      risks: result.risks,
      name: resume.name,
      titleCompanyLine: resume.titleCompanyLine,
      workRawText: resume.workRawText,
      projectRawText: resume.projectRawText,
      educationRawText: resume.educationRawText,
      detailView: state.resumeDetailView,
      recommendationMode: state.currentRecommendationMode
    });
    if (state.lastMatchSignature === signature) return;
    state.lastMatchSignature = signature;

    const score = document.querySelector('#liepin-reply-assistant-score');
    const statusEl = document.querySelector('#liepin-reply-assistant-status');
    const meta = document.querySelector('#liepin-reply-assistant-meta');
    const detail = document.querySelector('#liepin-reply-assistant-detail');
    const draft = document.querySelector('#liepin-reply-assistant-draft');
    const numbered = items => items.map((item, index) => `${index + 1}、${item}`).join('\n');
    const parseTimeline = (rawText, type) => {
      const lines = String(rawText || '')
        .split('\n')
        .map(clean)
        .filter(Boolean)
        .filter(line => !/^·$/.test(line))
        .filter(line => !/^\d+\/\d+$/.test(line))
        .filter(line => !/^(展开|收起|查看|更多|职位|职责|业绩|汇报对象|下属人数)$/.test(line));
      const rows = [];

      const timeRe = /[（(]?(\d{4}\.\d{2}\s*-\s*(?:至今|\d{4}\.\d{2})(?:,\s*[^）)]*)?)[）)]?/;
      const companyRe = /公司|集团|科技|半导体|设备|电子|微电子|精科|苏科思|华为|研究所|研究院|中心|厂|Ltd|Inc|Corp/i;
      const titleRe = /工程师|经理|专家|主管|负责人|架构师|设计师|研发|总监|部长|主任|事业部|产品经理|leader|manager|engineer/i;
      const schoolRe = /大学|学院|学校|研究所|院校|University|Institute|College/i;
      const degreeRe = /博士后|博士|硕士|本科|大专|MBA|统招|非统招|985|211/i;
      const majorRe = /机械|自动化|工程|光学|仪器|电子|物理|材料|设计|制造|控制|计算机|软件|专业/i;
      const badWorkLineRe = /项目经历|项目名称|项目描述|项目职责|职责描述|工作职责|工作业绩|职位名称|职位\s*\d+|教育经历|语言能力/;
      const degreeLevels = ['博士后', '博士', '硕士', '本科', '大专', 'MBA'];

      const normalizeTime = value => clean(value).replace(/[（）()]/g, '').replace(/,\s*.*$/, '');
      const stripEducationTags = value => clean(value)
        .replace(/博士后|博士|硕士|本科|大专|MBA|统招|非统招|985|211/g, '')
        .replace(/[｜|·,，、/]/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();

      for (let i = 0; i < lines.length; i += 1) {
        const timeMatch = lines[i].match(timeRe);
        if (!timeMatch) continue;

        if (type === 'work') {
          const company = lines.slice(Math.max(0, i - 3), i).reverse().find(line =>
            companyRe.test(line) &&
            !titleRe.test(line) &&
            !badWorkLineRe.test(line) &&
            line.length <= 60
          );
          if (!company) continue;
          const after = lines.slice(i + 1, i + 8);
          const titleLine = after.find(line =>
            titleRe.test(line) &&
            !companyRe.test(line) &&
            !badWorkLineRe.test(line) &&
            line.length <= 48
          ) || '';
          rows.push(`${normalizeTime(timeMatch[1])}｜${compactText(company, 44)}${titleLine ? `｜${compactText(titleLine, 38)}` : ''}`);
        } else {
          let schoolIndex = -1;
          for (let j = i - 1; j >= Math.max(0, i - 8); j -= 1) {
            if (schoolRe.test(lines[j]) && lines[j].length <= 42) {
              schoolIndex = j;
              break;
            }
          }
          if (schoolIndex < 0) continue;
          const nextSchoolOffset = lines.slice(i + 1).findIndex(line => schoolRe.test(line) && line.length <= 42);
          const blockEnd = nextSchoolOffset >= 0 ? i + 1 + nextSchoolOffset : Math.min(lines.length, i + 8);
          const block = lines.slice(schoolIndex + 1, blockEnd);
          const major = block
            .map(stripEducationTags)
            .find(line => majorRe.test(line) && !schoolRe.test(line) && !timeRe.test(line) && line.length <= 42) || '';
          const degrees = degreeLevels.filter(level => block.some(line => line.includes(level)));
          const tags = block
            .filter(line => /统招|非统招|985|211/.test(line))
            .map(line => line.match(/统招|非统招|985|211/)?.[0])
            .filter(Boolean);
          rows.push([
            normalizeTime(timeMatch[1]),
            compactText(lines[schoolIndex], 42),
            major ? compactText(major, 42) : '',
            ...degrees,
            ...[...new Set(tags)]
          ].filter(Boolean).join('｜'));
        }
        if (rows.length >= 6) break;
      }
      return numbered(rows);
    };
    const workTimeline = parseTimeline(resume.workRawText, 'work') || '1、未识别到工作经历';
    const eduTimeline = parseTimeline(resume.educationRawText, 'edu') || '1、未识别到学习经历';
    const evidencePoints = window.LIEPIN_RECOMMENDATION_COPY?.extractResumeEvidence
      ? window.LIEPIN_RECOMMENDATION_COPY.extractResumeEvidence(resume, profile)
      : result.matched;
    const detailedRisks = window.LIEPIN_RECOMMENDATION_COPY?.extractResumeRisks
      ? window.LIEPIN_RECOMMENDATION_COPY.extractResumeRisks(resume, profile)
      : result.risks;
    const quality = resumeEvidenceQuality(resume, profile);
    const qualityText = [
      `具体证据 ${quality.concreteCount} 条`,
      quality.projectCount ? `项目证据 ${quality.projectCount} 条` : '',
      profile.domain === 'quality_pqe'
        ? (quality.strictFabEvidence ? '12吋fab证据已命中' : '12吋fab证据未命中')
        : '',
      quality.coreEvidence ? '核心证据已命中' : '核心证据未充分命中',
      quality.weakFabRisk ? '存在非12吋/弱相关场景风险' : ''
    ].filter(Boolean).join('｜');
    const matchSummary = [
      `匹配判断：${result.score}分，${result.grade}`,
      `建议动作：${result.action}`,
      `证据质量：${qualityText}`,
      '匹配点：',
      evidencePoints.length ? numbered(evidencePoints) : '1、暂未识别到明显命中项',
      '风险点：',
      detailedRisks.length ? numbered(detailedRisks) : '1、暂无明显风险',
      '工作经历：',
      workTimeline,
      '学习经历：',
      eduTimeline
    ].join('\n');
    state.currentDraft = matchSummary;
    state.currentContext = {
      resume,
      profile,
      result
    };
    const activeProject = readManualProject();
    state.lastGenerated = {
      candidateName: resume.name,
      candidateTitle: resume.titleCompanyLine,
      latestMessage: resume.intentionText || '',
      strategyKey: 'resume_match',
      strategyLabel: '简历匹配',
      score: result.score,
      grade: result.grade,
      project: isConcreteProject(activeProject)
        ? activeProject
        : { client: '', position: '', confidence: '待确认', rule: 'auto_unresolved' },
      matchProfile: {
        client: profile.client || '',
        position: profile.position || '',
        label: profile.label || '',
        domain: profile.domain || ''
      }
    };
    const recommendation = window.LIEPIN_RECOMMENDATION_COPY?.buildRecommendationCopy
      ? window.LIEPIN_RECOMMENDATION_COPY.buildRecommendationCopy(result, resume, profile)
      : null;
    state.currentRecommendation = recommendation;
    state.currentRecommendationMode = state.currentRecommendationMode || 'customerRecommendation';

    if (score) {
      score.textContent = `${result.score}分 · ${result.grade}`;
      score.dataset.grade = result.grade;
    }
    if (statusEl) statusEl.textContent = result.action;
    if (meta) {
      meta.textContent = [
        `人选：${resume.name || '未识别'}`,
        resume.titleCompanyLine ? `当前：${compactText(resume.titleCompanyLine, 80)}` : '',
        matchProfileMetaLabel(profile)
      ].filter(Boolean).join(' ｜ ');
    }
    if (draft) {
      draft.value = matchSummary;
    }
    if (detail) {
      if (state.resumeDetailView === 'recommendation' && recommendation) {
        renderRecommendationPanel(recommendation, resume, profile);
      } else {
        detail.innerHTML = '';
        detail.dataset.view = 'resume-evidence';
        const rows = [
          ['匹配点', evidencePoints.length ? numbered(evidencePoints) : '1、暂未识别到明显命中项'],
          ['证据质量', qualityText],
          ['风险点', detailedRisks.length ? numbered(detailedRisks) : '1、暂无明显风险'],
          ['求职意向', compactText(resume.intentionText || '未识别', 180)],
          ['工作经历', workTimeline],
          ['学习经历', eduTimeline]
        ];
        const evidenceWrap = document.createElement('details');
        evidenceWrap.className = 'lpra-resume-evidence';
        const summary = document.createElement('summary');
        summary.innerHTML = '<span>匹配依据</span><em></em>';
        summary.querySelector('em').textContent = `匹配 ${evidencePoints.length || 0} · 风险 ${detailedRisks.length || 0}`;
        evidenceWrap.appendChild(summary);
        const body = document.createElement('div');
        body.className = 'lpra-resume-evidence-body';
        rows.forEach(([label, value]) => {
          const row = document.createElement('div');
          row.className = 'lpra-detail-row';
          row.innerHTML = `<span>${label}</span><b></b>`;
          row.querySelector('b').textContent = value;
          body.appendChild(row);
        });
        evidenceWrap.appendChild(body);
        detail.appendChild(evidenceWrap);
      }
    }
    setTimeout(() => {
      autoPreviewResumeReview({ force: true }).catch(() => {
        status('统一人才库预检失败，请检查本机 8765 工作台服务');
      });
    }, 300);
  }

  async function refreshMatchPanel() {
    if (state.matchRefreshPromise) return state.matchRefreshPromise;
    state.matchRefreshPromise = (async () => {
      ensureDynamicProjectOptions();
      if (state.lastMatchedUrl && state.lastMatchedUrl !== location.href) clearTalentLookupMatch();
      state.lastTalentDryRun = null;
      state.lastAutoReviewSignature = '';
      updateCandidateIntakeButtonFromLookup(null);
      if (!shouldKeepTalentProgressDuringRefresh()) renderTalentProgress(null);
      const resume = readResumeContext();
      await resolveResumeProjectForMatch(resume);
      const profile = getActiveMatchProfile(resume);
      const result = scoreResumeAgainstProfile(resume, profile);
      renderMatchPanel(result, resume, profile);
      state.lastMatchedUrl = location.href;
      setTimeout(() => {
        autoPreviewResumeReview({ force: true }).catch(() => {
          status('统一人才库预检失败，请检查本机 8765 工作台服务');
        });
      }, 500);
    })();
    try {
      await state.matchRefreshPromise;
    } finally {
      state.matchRefreshPromise = null;
    }
  }

  function shouldKeepTalentProgressDuringRefresh() {
    if (!isLiepinResumeDetailPage()) return false;
    if (state.lastMatchedUrl !== location.href) return false;
    const progress = clean(document.querySelector('#liepin-reply-talent-progress')?.textContent || '');
    return /已定位 #/.test(progress);
  }

  function shouldSkipInitialResumeRefresh() {
    if (!isLiepinResumeDetailPage()) return false;
    if (state.matchRefreshPromise) return true;
    if (state.lastMatchedUrl !== location.href) return false;
    const meta = clean(document.querySelector('#liepin-reply-assistant-meta')?.textContent || '');
    const progress = clean(document.querySelector('#liepin-reply-talent-progress')?.textContent || '');
    return /人选：/.test(meta) && (/已定位|待确认|未唯一定位|未通过/.test(progress) || /岗位：|评分画像：/.test(meta));
  }

  async function switchResumeView(view) {
    state.resumeDetailView = view;
    const resume = readResumeContext();
    await resolveResumeProjectForMatch(resume);
    const profile = getActiveMatchProfile(resume);
    const result = scoreResumeAgainstProfile(resume, profile);
    renderMatchPanel(result, resume, profile);
    if (view === 'recommendation') {
      status('已切到推荐文案');
    } else {
      status('已切到判断');
    }
  }

  function titleLabel(title) {
    return clean(title) ? `看您目前是${clean(title)}` : '结合您目前的经历';
  }

  function sentenceJoin(parts) {
    return parts.map(clean).filter(Boolean).join('');
  }

  function roleLead(title) {
    const value = clean(title);
    return value ? `看您这边是${value}，` : '';
  }

  function softProjectLabel(project) {
    if (project.client && project.position) return `${project.client}${project.position}`;
    if (project.position) return project.position;
    if (project.client) return `${project.client}这边岗位`;
    return '这个机会';
  }

  function hasProjectAnchor(project) {
    return !!clean(project?.client || project?.position);
  }

  function opportunityAnchor(project) {
    if (project.client && project.position) return `${project.client}的${project.position}`;
    if (project.position) return project.position;
    if (project.client) return `${project.client}这边岗位`;
    return '半导体/高端制造方向机会';
  }

  function conciseProjectIntro(project) {
    if (project.client && project.position) return `我这边是${project.client}在招${project.position}`;
    if (project.position) return `这边岗位方向是${project.position}`;
    if (project.client) return `我这边是${project.client}有岗位在招`;
    return '我这边有半导体/高端制造方向机会';
  }

  function hiringProjectLine(project) {
    if (project.client && project.position) return `我这边是${project.client}在招${project.position}`;
    if (project.position) return `我这边在招${project.position}`;
    if (project.client) return `我这边是${project.client}有岗位在招`;
    return '我这边有半导体/高端制造方向机会在招';
  }

  function detailScope(project) {
    const position = clean(project?.position || '');
    if (/自动化软件|控制软件|上位机|PLC|HMI|运动控制/i.test(position)) return '职责、控制平台和经验要求';
    if (/机械/.test(position)) return '设备方向、精度要求和机械模块职责';
    if (/电气|化学品|CDS|特气/.test(position)) return '系统方向、电气/PLC要求和现场条件';
    if (/失效分析|FA|FIB|SEM|TEM/.test(position)) return '分析方向、设备平台和样品类型';
    if (/FPGA/i.test(position)) return '产品方向、技术栈和团队职责';
    return '职责、要求和客户侧关注点';
  }

  function projectConfidenceRank(project) {
    const value = clean(project?.confidence || '');
    if (/高|confirmed|手动选择/.test(value)) return 3;
    if (/中/.test(value)) return 2;
    if (/低/.test(value)) return 1;
    return 0;
  }

  function hasStrongProjectAnchor(project) {
    return !!clean(project?.client || project?.position);
  }

  function canFastLaneProject(project) {
    const rank = projectConfidenceRank(project);
    if (!hasStrongProjectAnchor(project)) return false;
    if (rank >= 3) return true;
    if (rank === 2 && clean(project?.client)) return true;
    return false;
  }

  function needsAnchorFirst(project) {
    const rank = projectConfidenceRank(project);
    if (!hasStrongProjectAnchor(project)) return true;
    if (rank <= 1) return true;
    if (rank === 2 && !clean(project?.client)) return true;
    return false;
  }

  function oneKeyQuestion(strategyKey, project, title) {
    const titleText = clean(title);
    if (strategyKey === 'asks_company') {
      if (needsAnchorFirst(project)) return '您更关注岗位方向、客户背景，还是地点？';
      return /资深|专家|主管|经理|总监/.test(clean(project?.position))
        ? '您这块大概有几年相关经验？'
        : '您方便先说下这块大概几年经验吗？';
    }
    if (strategyKey === 'job_detail_request') return '您先看岗位要点，我再按您关注的点补充，可以吗？';
    if (strategyKey === 'location_check') return '这个地点或出差节奏您能接受吗？';
    if (strategyKey === 'salary') return '您方便先说下目前大概总包区间吗？';
    if (strategyKey === 'positive_fit') return '您看方便的话，后面再约 10 分钟快速对一下，可以吗？';
    if (strategyKey === 'anchor_first_probe') return '您更关注岗位方向、客户背景，还是地点？';
    if (strategyKey === 'general_followup') return '您近期有在看新的机会吗？';
    if (titleText) return '我微信里先把岗位要点发您，可以吗？';
    return '方便先加个微信沟通吗？';
  }

  function buildDraft(context) {
    const rawText = context.combinedText || context.latestMessage || '';
    let strategyKey = detectStrategy(`${context.latestMessage} ${rawText}`);
    const manualProject = readManualProject();
    const project = manualProject || detectProject(rawText);
    const hello = salutation(context.contact.name, context.contact.title);
    const projectText = projectLabel(project);
    const roleText = roleLead(context.contact.title);
    const anchorFirst = needsAnchorFirst(project);
    const directPush = canFastLaneProject(project);

    if (strategyKey === 'positive_fit' && !directPush) {
      strategyKey = 'anchor_first_probe';
    } else if ((strategyKey === 'asks_company' || strategyKey === 'general_followup' || strategyKey === 'broad_semiconductor_wechat') && anchorFirst) {
      strategyKey = 'anchor_first_probe';
    }
    const strategy = STRATEGIES[strategyKey] || STRATEGIES.general_followup;

    let draft = '';
    const missing = [];
    const risk = [];

    if (strategyKey === 'asks_company') {
      draft = sentenceJoin([
        `${hello}，`,
        project.client && !anchorFirst
          ? `是${softProjectLabel(project)}。`
          : project.position
            ? `岗位方向是${project.position}，客户名称我这边先确认下可透露范围。`
            : '客户名称我这边先确认下可透露范围。',
        project.position ? '这边可能要相对资深些的。' : roleText,
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      if (!project.client) missing.push('客户名称可透露范围');
      if (!project.position) missing.push('岗位方向');
    } else if (strategyKey === 'contact_exchange') {
      draft = sentenceJoin([
        `${addressName(context.contact.name, context.contact.title) || '您好'}，`,
        `${hiringProjectLine(project)}。`
      ]);
      if (!project.position) missing.push('明确岗位');
    } else if (strategyKey === 'job_detail_request') {
      draft = sentenceJoin([
        `${hello}，可以，`,
        `${conciseProjectIntro(project)}。`,
        `我先把${detailScope(project)}发您，`,
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      if (!project.position) missing.push('具体岗位');
      if (!project.client) missing.push('客户名称或可透露范围');
    } else if (strategyKey === 'location_check') {
      draft = sentenceJoin([
        `${hello}，`,
        hasProjectAnchor(project) ? `我先按${projectText}确认下。` : '我先把地点和出差节奏确认清楚。',
        '地点这块我会以客户最新要求为准同步，',
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      missing.push('客户最新地点/出差要求');
      risk.push('地点或出差是强筛选点，未确认前不要强推进');
    } else if (strategyKey === 'salary') {
      draft = sentenceJoin([
        `${hello}，收到，薪资这块可以先对齐。`,
        hasProjectAnchor(project) ? `我先按${opportunityAnchor(project)}判断预算匹配度。` : '',
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      missing.push('当前总包区间');
      if (!project.position) missing.push('岗位预算范围');
      risk.push('不要直接承诺客户薪资');
    } else if (strategyKey === 'mismatch_or_reject') {
      draft = sentenceJoin([
        '明白，有合适机会随时沟通。'
      ]);
    } else if (strategyKey === 'positive_fit') {
      draft = sentenceJoin([
        `${hello}，我看您和${projectText}方向比较贴。`,
        `我先把${detailScope(project)}同步给您，`,
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      if (!project.position) missing.push('具体岗位');
    } else if (strategyKey === 'anchor_first_probe') {
      draft = sentenceJoin([
        `${hello}，`,
        hasProjectAnchor(project)
          ? `我先按${projectText}和您同步一下。`
          : '我先把这个机会的核心方向和您说清。',
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      if (!project.position) missing.push('具体岗位');
      if (anchorFirst && !project.client) missing.push('客户名称');
      risk.push('项目没锚点时先补信息，不直接推进');
    } else if (strategyKey === 'broad_semiconductor_wechat') {
      draft = sentenceJoin([
        `${hello}，我这边主要看半导体和高端制造方向机会。`,
        '方便的话咱们先加个微信，后面有贴近您背景的岗位我及时同步。'
      ]);
      risk.push('泛触达口径需要尽量补岗位关键词');
      if (!project.position) missing.push('补一个岗位或方向关键词');
    } else {
      draft = sentenceJoin([
        `${hello}，收到。`,
        hasProjectAnchor(project) ? `我这边先按${projectText}给您同步。` : '',
        oneKeyQuestion(strategyKey, project, context.contact.title)
      ]);
      if (!project.position) missing.push('具体岗位');
    }

    const quality = scoreDraft({
      draft,
      strategyKey,
      project,
      contact: context.contact,
      missing,
      risk,
      anchorFirst,
      directPush
    });

    return {
      draft,
      strategyKey,
      strategy,
      project,
      quality,
      missing,
      risk,
      latestMessage: context.latestMessage
    };
  }

  function scoreDraft(result) {
    let score = 70;
    const reasons = [];
    if (result.project.position) {
      score += 10;
      reasons.push('带岗位方向');
    } else {
      score -= 12;
      reasons.push('缺具体岗位');
    }

    if (result.project.client) {
      score += 8;
      reasons.push('客户明确');
    } else if (result.strategyKey === 'asks_company') {
      score -= 8;
      reasons.push('候选人问公司但客户未确认');
    }

    const confidence = clean(result.project.confidence || '');
    if (/低|待确认|unmatched/.test(confidence)) {
      score -= 14;
      reasons.push('项目置信不足');
    } else if (/中/.test(confidence) && !result.project.client) {
      score -= 8;
      reasons.push('中置信但缺客户名');
    }

    if (/手动/.test(result.project.confidence || '')) {
      score += 6;
      reasons.push('已手动指定岗位');
    }

    if (result.contact.title) {
      score += 5;
      reasons.push('结合人选头衔');
    }

    if (/电话|微信|沟通|确认/.test(result.draft)) {
      score += 5;
      reasons.push('有下一步动作');
    }

    const questionCount = (result.draft.match(/[？?]/g) || []).length;
    if (questionCount <= 1) {
      score += 4;
      reasons.push('问题节奏克制');
    } else if (questionCount > 2) {
      score -= 10;
      reasons.push('问题偏多，建议拆开问');
    }

    if (result.strategyKey === 'salary') {
      score -= 6;
      reasons.push('薪资需人工核预算');
    }

    if (result.strategyKey === 'job_detail_request') {
      score += 4;
      reasons.push('先发岗位详情再推进');
    }

    if (result.strategyKey === 'location_check') {
      score -= 3;
      reasons.push('地点/出差需人工确认');
    }

    if (result.strategyKey === 'contact_exchange') {
      score += 4;
      reasons.push('一句话明确客户和在招岗位');
    }

    if (result.strategyKey === 'anchor_first_probe') {
      score -= 4;
      reasons.push('先补锚点，不直接推进');
    }

    if (result.strategyKey === 'mismatch_or_reject') {
      score += 8;
      reasons.push('拒绝场景口径克制');
    }

    if (result.draft.length > 220) {
      score -= 4;
      reasons.push('略长，发送前可删减');
    }

    const bounded = Math.max(35, Math.min(98, score));
    let grade = '先补信息';
    if (bounded >= 85 && result.strategyKey !== 'anchor_first_probe') grade = '可直接复制';
    else if (bounded >= 65) grade = '复制前看一眼';

    return {
      score: bounded,
      grade,
      reasons
    };
  }

  function findEditor() {
    const selectors = [
      'textarea',
      '[contenteditable="true"]',
      'input[type="text"]',
      '[class*="input"] [contenteditable="true"]',
      '[class*="editor"] [contenteditable="true"]'
    ];
    const candidates = selectors.flatMap(selector => Array.from(document.querySelectorAll(selector)));
    return candidates
      .filter(isVisible)
      .filter(el => {
        const rect = el.getBoundingClientRect();
        const text = clean(el.getAttribute('placeholder') || el.getAttribute('aria-label') || '');
        const looksLikeSearch = /搜索|筛选|查找/.test(text) || rect.left < 380;
        return !looksLikeSearch && rect.width > 120 && rect.top > window.innerHeight * 0.35;
      })
      .sort((a, b) => {
        const ar = a.getBoundingClientRect();
        const br = b.getBoundingClientRect();
        return br.top - ar.top;
      })[0] || null;
  }

  function setEditorValue(editor, text) {
    editor.focus();
    if (editor.isContentEditable) {
      editor.textContent = text;
      editor.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        inputType: 'insertText',
        data: text
      }));
      return true;
    }

    const proto = editor instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
    if (descriptor?.set) descriptor.set.call(editor, text);
    else editor.value = text;
    editor.dispatchEvent(new Event('input', { bubbles: true }));
    editor.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }

    const temp = document.createElement('textarea');
    temp.value = text;
    temp.style.position = 'fixed';
    temp.style.left = '-9999px';
    document.body.appendChild(temp);
    temp.select();
    const ok = document.execCommand('copy');
    temp.remove();
    return ok;
  }

  function status(text) {
    state.lastStatus = text;
    const el = document.querySelector('#liepin-reply-assistant-status');
    if (el) el.textContent = text;
  }

  function saveLatestDraft(payload) {
    try {
      chrome.storage.local.set({
        [STORE_KEY]: {
          ...payload,
          savedAt: new Date().toISOString(),
          url: location.href
        }
      });
    } catch (_) {
      // storage is best-effort only
    }
  }

  function currentDraftPayload(editedDraft) {
    const generated = state.lastGenerated || {};
    return {
      candidateName: generated.candidateName || normalizeName(state.currentContext?.contact?.name),
      candidateTitle: generated.candidateTitle || state.currentContext?.contact?.title || '',
      latestMessage: generated.latestMessage || state.currentContext?.latestMessage || '',
      strategyKey: generated.strategyKey || '',
      strategyLabel: generated.strategyLabel || '',
      score: generated.score || 0,
      grade: generated.grade || '',
      project: generated.project || readManualProject() || detectProject(state.currentContext?.combinedText || ''),
      draft: clean(editedDraft || state.currentDraft),
      url: location.href
    };
  }

  function currentProjectForAction() {
    const manualProject = readManualProject();
    const generatedProject = state.lastGenerated?.project || null;
    const lookupProject = state.lastTalentDryRun?.result?.lookup?.matched
      ? state.lastTalentDryRun.result.lookup.normalized
      : null;
    const recentOutreachProject = state.recentOutreachProject;
    const detectedProject = detectProject(state.currentContext?.combinedText || '');
    return [recentOutreachProject, manualProject, generatedProject, lookupProject, detectedProject].find(isConcreteProject) || {};
  }

  async function ensureProjectFromRecentOutreachForCurrentContext() {
    const context = state.currentContext || readPageContext();
    if (isLiepinImPage()) state.currentContext = context;
    const result = await resolveProjectFromWorkbench({
      contact: context.contact,
      resume: context.resume
    }, { force: true, refresh: false });
    if (result?.matched && result?.project?.client && result?.project?.position && (result.auto_apply || result.match?.auto_apply)) {
      const project = normalizeTalentProject(result.project);
      state.recentOutreachProject = project;
      applyProjectSelection(project, '触达记录', false);
      return project;
    }
    return readManualProject();
  }

  async function recentOutreachProjectForCurrentContact(extraParams = {}) {
    const context = state.currentContext || readPageContext();
    if (isLiepinImPage()) state.currentContext = context;
    const safeExtraParams = { ...extraParams };
    if (isLiepinImPage()) delete safeExtraParams.source_url;
    const identityParams = {
      candidate_title: context.contact?.title || state.lastGenerated?.candidateTitle || '',
      candidate_profile_text: context.combinedText || ''
    };
    const candidateNames = [
      normalizeName(context.contact?.name),
      context.contact?.name,
      safeExtraParams.candidate_name,
      state.lastGenerated?.candidateName
    ].map(clean).filter(Boolean);
    const seen = new Set();
    for (const candidateName of candidateNames) {
      const key = normalizeProjectText(candidateName);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      const attempts = [
        { ...identityParams, ...safeExtraParams, candidate_name: candidateName },
        { candidate_name: candidateName }
      ];
      for (const params of attempts) {
        const result = await getFromWorkbench('/api/recent-outreach-project', params);
        if (result?.matched && result?.project?.client && result?.project?.position && (result.auto_apply || result.match?.auto_apply)) {
          const project = normalizeTalentProject(result.project);
          state.recentOutreachProject = project;
          applyProjectSelection(project, '触达记录', false);
          return project;
        }
      }
    }
    return null;
  }

  function isConcreteProject(project = {}) {
    project = project || {};
    return !!(clean(project.client || '') && clean(project.position || project.job || ''));
  }

  function normalizeTalentProject(project = {}) {
    const client = clean(project.client || '');
    const position = clean(project.position || project.job || '');
    if (/鹏新旭/.test(client) && /PQE|质量/.test(position)) {
      return { ...project, client: '鹏新旭', position: 'PQE专家', job: 'PQE专家' };
    }
    return { ...project, client, position, job: position };
  }

  function currentTalentLocator(projectOverride = null) {
    const project = normalizeTalentProject(isConcreteProject(projectOverride) ? projectOverride : currentProjectForAction());
    const resume = state.currentContext?.resume || null;
    const contact = state.currentContext?.contact || null;
    const imCandidate = isLiepinImPage() ? normalizeName(contact?.name) : '';
    const imTitle = isLiepinImPage() ? clean(contact?.title) : '';
    const workIdentity = resume ? extractPrimaryWorkIdentity(resume) : {};
    const profileText = [
      resume?.titleCompanyLine || contact?.title || '',
      workIdentity.workSummary || '',
      resume?.workRawText || '',
      resume?.projectRawText || ''
    ].map(clean).filter(Boolean).join('\n').slice(0, 3000);
    return {
      candidate: resume?.name || imCandidate || state.lastGenerated?.candidateName || normalizeName(contact?.name),
      company: workIdentity.company || '',
      title: workIdentity.title || resume?.titleCompanyLine || imTitle || state.lastGenerated?.candidateTitle || contact?.title || '',
      client: project.client || '',
      job: project.position || project.job || '',
      source_candidate_id: resume?.resumeId || '',
      candidate_profile_text: profileText,
      source_url: location.href
    };
  }

  function closeIdentityLayer() {
    document.querySelector(`#${ROOT_ID} .lpra-identity-layer`)?.remove();
  }

  function currentIdentityPersonId() {
    return state.lastTalentLookupMatch?.match?.person_id || state.lastTalentDryRun?.result?.lookup?.match?.person_id || '';
  }

  function identitySourceProfile() {
    const locator = currentTalentLocator();
    return {
      source_type: 'liepin',
      source_candidate_id: locator.source_candidate_id,
      source_url: locator.source_url,
      candidate: locator.candidate,
      company: locator.company,
      title: locator.title,
      client: locator.client,
      position: locator.job,
      candidate_profile_text: locator.candidate_profile_text
    };
  }

  async function discoverSameCandidate() {
    const root = document.getElementById(ROOT_ID);
    if (!root || !isLiepinResumeDetailPage()) return;
    const sourceProfile = identitySourceProfile();
    const currentPersonId = currentIdentityPersonId();
    status('正在查找疑似同一人...');
    const result = await postToWorkbench('/api/candidate-identity-matches', {
      ...sourceProfile,
      current_person_id: currentPersonId
    });
    if (!result?.ok) {
      status(`查找失败：${clean(result?.error || '本机服务未响应')}`);
      return;
    }
    closeIdentityLayer();
    const matches = Array.isArray(result.matches) ? result.matches : [];
    const allowed = matches.filter(item => item.merge_allowed);
    const resolvedCurrentPersonId = result.current_person_id || currentPersonId;
    const firstAllowedIndex = matches.findIndex(item => item.merge_allowed);
    const rows = matches.length
      ? matches.slice(0, 10).map((item, index) => `
          <label class="lpra-identity-option" data-allowed="${item.merge_allowed ? 'true' : 'false'}">
            <input type="radio" name="lpra-identity-person" value="${escapeHtml(item.person_id)}"
              ${item.merge_allowed && index === firstAllowedIndex ? 'checked' : ''}
              ${item.merge_allowed ? '' : 'disabled'}>
            <span>
              <b>${escapeHtml(item.candidate || '未命名')} · 人才 #${escapeHtml(item.person_id)}</b>
              <em>${escapeHtml(item.company || '公司未知')} / ${escapeHtml(item.title || '职位未知')}</em>
              <small>${escapeHtml((item.reasons || []).join(' · ') || '证据不足')}｜${item.merge_allowed ? '可对比' : '禁止合并'}</small>
            </span>
          </label>
        `).join('')
      : '<div class="lpra-identity-empty">没有发现疑似同一人的档案</div>';
    const layer = document.createElement('div');
    layer.className = 'lpra-confirm-layer lpra-identity-layer';
    layer.innerHTML = `
      <div class="lpra-confirm-panel" role="dialog" aria-modal="true" aria-label="发现同一人">
        <div class="lpra-confirm-head">
          <strong>发现同一人</strong>
          <button type="button" class="lpra-confirm-close" aria-label="关闭">x</button>
        </div>
        <div class="lpra-identity-current">
          <span>当前猎聘档案</span>
          <b>${escapeHtml(sourceProfile.candidate || '未识别')} · ${escapeHtml(sourceProfile.company || '公司未知')} / ${escapeHtml(sourceProfile.title || '职位未知')}</b>
        </div>
        ${resolvedCurrentPersonId ? '' : '<div class="lpra-confirm-guard"><strong>当前档案尚未定位</strong><p>请先“确认入库”，再执行档案合并。</p></div>'}
        <div class="lpra-identity-options">${rows}</div>
        <div class="lpra-identity-preflight" hidden></div>
        <div class="lpra-confirm-actions">
          <button type="button" class="lpra-confirm-cancel">取消</button>
          <button type="button" class="lpra-confirm-submit lpra-primary" ${!resolvedCurrentPersonId || !allowed.length ? 'disabled' : ''}>对比档案</button>
        </div>
      </div>
    `;
    root.appendChild(layer);
    const close = () => closeIdentityLayer();
    layer.querySelector('.lpra-confirm-close')?.addEventListener('click', close);
    layer.querySelector('.lpra-confirm-cancel')?.addEventListener('click', close);
    layer.addEventListener('click', event => { if (event.target === layer) close(); });
    const submit = layer.querySelector('.lpra-confirm-submit');
    submit?.addEventListener('click', async () => {
      const selectedPersonId = Number(layer.querySelector('input[name="lpra-identity-person"]:checked')?.value || 0);
      if (!selectedPersonId || !resolvedCurrentPersonId) return;
      submit.disabled = true;
      const request = {
        canonical_person_id: selectedPersonId,
        merged_person_id: Number(resolvedCurrentPersonId),
        source_profile: sourceProfile,
        actor: 'liepin-candidate-assistant',
        write: false
      };
      if (submit.dataset.step !== 'confirm') {
        status('正在执行合并预检...');
        const preflight = await postToWorkbench('/api/candidate-merge', request);
        if (!preflight?.ok) {
          status(`不能合并：${clean(preflight?.error || '身份或推进状态存在冲突')}`);
          submit.disabled = false;
          return;
        }
        layer.__mergeRequest = request;
        layer.__confirmationToken = preflight.confirmation_token;
        const plan = preflight.plan || {};
        const compare = layer.querySelector('.lpra-identity-preflight');
        compare.hidden = false;
        compare.innerHTML = `
          <div><span>保留档案</span><b>${escapeHtml(preflight.canonical?.candidate || '')} · 人才 #${escapeHtml(preflight.canonical?.person_id || '')}</b></div>
          <div><span>并入档案</span><b>${escapeHtml(preflight.merged?.candidate || '')} · 人才 #${escapeHtml(preflight.merged?.person_id || '')}</b></div>
          <p>保留双方来源简历；迁移 ${escapeHtml(plan.source_profiles || 0)} 条来源档案、${escapeHtml(plan.job_relations || 0)} 条岗位关系、${escapeHtml(plan.events || 0)} 条事件。</p>
        `;
        submit.dataset.step = 'confirm';
        submit.textContent = '确认合并';
        submit.disabled = false;
        status('对比完成，请人工确认是否合并');
        return;
      }
      status('正在合并档案...');
      const merged = await postToWorkbench('/api/candidate-merge', {
        ...layer.__mergeRequest,
        write: true,
        confirmation_token: layer.__confirmationToken
      });
      if (!merged?.ok) {
        status(`合并失败：${clean(merged?.error || '确认已失效')}`);
        submit.disabled = false;
        return;
      }
      closeIdentityLayer();
      clearTalentLookupMatch();
      status('档案已合并，双方来源简历和推进历史均已保留');
      await autoPreviewResumeReview({ force: true }).catch(() => null);
    });
    status(allowed.length ? `发现 ${allowed.length} 条可对比档案` : '未发现可安全合并的同一人');
  }

  function latestCandidateReplyText(context = null) {
    const source = context || readPageContext();
    return clean(source.latestReceivedMessage || '');
  }

  function normalizeMessageDirectionText(value) {
    return clean(value).replace(/[\s，。！？、；：,.!?;:'"“”‘’（）()\[\]【】<>《》-]+/g, '').toLowerCase();
  }

  function isOutboundDraftCollision(replyText, outboundDraft) {
    const reply = normalizeMessageDirectionText(replyText);
    const draft = normalizeMessageDirectionText(outboundDraft);
    return Boolean(reply && draft && reply === draft);
  }

  function candidateReplyPayload(rawText, context = null, projectOverride = null) {
    const source = context || readPageContext();
    const project = normalizeTalentProject(isConcreteProject(projectOverride) ? projectOverride : currentProjectForAction());
    const imCandidate = isLiepinImPage() ? normalizeName(source.contact?.name) : '';
    const imTitle = isLiepinImPage() ? clean(source.contact?.title) : '';
    const evidence = source.latestReceivedEvidence || null;
    const conversation = source.conversation || currentConversationSnapshot();
    const capturedAt = new Date().toISOString();
    const normalizedText = clean(rawText);
    const isExplicitEvidence = evidence && normalizeMessageDirectionText(evidence.text) === normalizeMessageDirectionText(normalizedText);
    return {
      candidate_name: imCandidate || state.lastGenerated?.candidateName || normalizeName(source.contact?.name),
      candidate_company: '',
      candidate_title: imTitle || state.lastGenerated?.candidateTitle || source.contact?.title || '',
      client: project.client || '',
      position: project.position || project.job || '',
      channel: 'liepin',
      conversation_id: conversation.conversationId,
      conversation_identity_confidence: conversation.confidence,
      message_id: isExplicitEvidence
        ? evidence.messageId
        : `manual-${window.LIEPIN_MESSAGE_EVIDENCE?.stableHash?.([conversation.conversationId, normalizedText, capturedAt].join('|')) || Date.now()}`,
      message_time: isExplicitEvidence ? evidence.messageTime : capturedAt,
      message_evidence: isExplicitEvidence ? 'explicit_inbound_dom' : 'manual_transcription',
      raw_text: normalizedText,
      source_url: location.href
    };
  }

  function talentLocatorLabel(payload = {}) {
    return [
      payload.candidate ? `人选:${payload.candidate}` : '人选:未识别',
      payload.client ? `客户:${payload.client}` : '客户:未选',
      payload.job ? `岗位:${payload.job}` : '岗位:未选'
    ].join(' ');
  }

  function lookupIssueMessage(result = null, fallbackPayload = {}) {
    const lookup = result?.lookup || {};
    const normalized = lookup.normalized || fallbackPayload || {};
    const summary = talentSyncSummary(result);
    const rawReason = clean(
      lookup.reason ||
      result?.reason ||
      result?.sync?.result?.items?.[0]?.reason ||
      result?.sync?.items?.[0]?.reason ||
      ''
    );
    const matches = Array.isArray(lookup.matches)
      ? lookup.matches
      : Array.isArray(result?.matches)
        ? result.matches
        : [];
    if (lookup.matched && lookup.match) {
      return `已唯一定位：${lookup.match.candidate || normalized.candidate || '人选'}｜${lookup.match.client || normalized.client}/${lookup.match.job || normalized.job}`;
    }
    if (!clean(normalized.candidate || fallbackPayload.candidate)) return '未唯一定位：缺人选姓名';
    if (!clean(normalized.client || fallbackPayload.client)) {
      return rawReason ? `未唯一定位：${rawReason}` : '未唯一定位：未选择客户';
    }
    if (!clean(normalized.job || fallbackPayload.job)) {
      return rawReason ? `未唯一定位：${rawReason}` : '未唯一定位：未选择岗位';
    }
    if (!clean(normalized.company || fallbackPayload.company) && !clean(normalized.title || fallbackPayload.title)) {
      return '未唯一定位：缺当前公司/职位证据，建议先确认入库';
    }
    if (/ambiguous|multiple|not_unique|重复|多条/i.test(rawReason) || matches.length > 1) {
      return `未唯一定位：疑似 ${matches.length || '多'} 条同名/同岗记录，请先检查岗位或入库`;
    }
    if (/identity|conflict|company|title|身份|公司|职位/i.test(rawReason)) {
      return '未唯一定位：姓名相近但公司/职位证据不一致';
    }
    if (Number(summary.pending_review || 0) > 0) return '未唯一定位：A 系统暂无唯一人选关系，可先确认入库';
    return `未唯一定位：${talentLocatorLabel(normalized || fallbackPayload)}`;
  }

  function normalizeProjectPair(value = {}) {
    const client = clean(value.client || value.raw_client || '');
    const job = clean(value.job || value.position || value.raw_position || '');
    return { client, job };
  }

  function sameProjectPair(a = {}, b = {}) {
    const left = normalizeProjectPair(a);
    const right = normalizeProjectPair(b);
    if (!left.client || !left.job || !right.client || !right.job) return true;
    return normalizeProjectText(`${left.client}|${left.job}`) === normalizeProjectText(`${right.client}|${right.job}`);
  }

  function projectPairLabel(value = {}) {
    const pair = normalizeProjectPair(value);
    return `${pair.client || '未选客户'} / ${pair.job || '未选岗位'}`;
  }

  function projectMismatchWarnings(payload = {}, dryRun = null) {
    const warnings = [];
    const lookupMatch = dryRun?.lookup?.match || state.lastTalentLookupMatch?.match || null;
    const sources = [
      { key: 'recent', value: state.recentOutreachProject },
      { key: 'lookup', value: lookupMatch }
    ];
    sources.forEach(source => {
      if (!source.value || sameProjectPair(payload, source.value)) return;
      const field = PROJECT_MISMATCH_FIELDS.find(item => item.key === source.key);
      warnings.push(`${field?.label || '关联岗位'}：${projectPairLabel(source.value)}`);
    });
    return [...new Set(warnings)];
  }

  function attachProjectGuardWarnings(payload = {}, dryRun = null) {
    const warnings = projectMismatchWarnings(payload, dryRun);
    if (warnings.length) {
      payload.project_guard_warnings = warnings;
      payload.project_guard_text = `当前将写入：${projectPairLabel(payload)}；${warnings.join('；')}`;
    }
    return payload;
  }

  function talentActionConfirmMessage(actionLabel, payload = {}) {
    return [
      `确认执行：${actionLabel}`,
      `人选：${payload.candidate || '未识别'}`,
      `客户/岗位：${payload.client || '未选客户'} / ${payload.job || '未选岗位'}`
    ].join('\n');
  }

  function closeTalentConfirmLayer(value) {
    const layer = document.querySelector('#liepin-reply-action-confirm');
    if (!layer) return;
    const resolver = layer.__resolveTalentConfirm;
    layer.remove();
    if (typeof resolver === 'function') resolver(value);
  }

  function talentWriteLabel(actionLabel, payload = {}) {
    if (payload.kind === 'candidate_intake') return '新增/复用人才入库记录';
    if (payload.kind === 'resume_review' && payload.review_result === 'stop') return '记录复核停止，默认确认触达，并更新为 H5 初筛不通过';
    if (payload.kind === 'resume_review') return '记录复核通过，默认确认触达并继续推进';
    if (payload.kind === 'outreach_verification') return '记录猎聘触达已核验，并更新为已触达';
    if (payload.kind === 'candidate_message' && payload.direction === 'received') return '记录候选人已回复，并更新到 S3 已回复';
    if (payload.kind === 'candidate_message') return '记录已人工发送/填入沟通动作';
    return actionLabel;
  }

  function selectedStopReasonFromLayer(layer) {
    const checked = layer.querySelector('input[name="lpra-stop-reason"]:checked');
    const key = checked?.value || STOP_REASON_OPTIONS[0].key;
    const option = STOP_REASON_OPTIONS.find(item => item.key === key) || STOP_REASON_OPTIONS[0];
    const note = clean(layer.querySelector('#lpra-stop-reason-note')?.value || '');
    return {
      stop_reason_code: option.key,
      stop_reason_label: option.label,
      stop_reason_note: note
    };
  }

  function confirmTalentAction(actionLabel, payload = {}) {
    return new Promise(resolve => {
      closeTalentConfirmLayer(false);
      const root = document.querySelector(`#${ROOT_ID}`);
      if (!root) {
        const confirmed = window.confirm(talentActionConfirmMessage(actionLabel, payload));
        if (!confirmed) {
          resolve(null);
          return;
        }
        const poolOnlyFallback = payload.kind === 'candidate_intake' && (!clean(payload.client) || !clean(payload.job));
        resolve(poolOnlyFallback ? { pool_only: true } : {});
        return;
      }

      const needsStopReason = payload.kind === 'resume_review' && payload.review_result === 'stop';
      const poolOnlyIntake = payload.kind === 'candidate_intake' && (!clean(payload.client) || !clean(payload.job));
      const layer = document.createElement('div');
      layer.id = 'liepin-reply-action-confirm';
      layer.className = 'lpra-confirm-layer';
      layer.__resolveTalentConfirm = resolve;
      const reasonOptions = STOP_REASON_OPTIONS.map((item, index) => `
        <label class="lpra-confirm-reason">
          <input type="radio" name="lpra-stop-reason" value="${item.key}" ${index === 0 ? 'checked' : ''}>
          <span>${item.label}</span>
        </label>
      `).join('');
      const guardWarnings = Array.isArray(payload.project_guard_warnings)
        ? payload.project_guard_warnings
        : [];
      const guardBlock = guardWarnings.length ? `
        <div class="lpra-confirm-guard">
          <strong>岗位一致性提醒</strong>
          <p>${escapeHtml(payload.project_guard_text || '当前岗位与关联记录不一致，请确认后再写入。')}</p>
        </div>
      ` : '';
      const poolOnlyBlock = poolOnlyIntake ? `
        <div class="lpra-confirm-guard">
          <strong>人才库储备</strong>
          <p>未选客户/岗位，确认后将先入库为人才库储备（不挂岗位），之后可在 A 系统中补选客户和岗位。</p>
        </div>
      ` : '';

      layer.innerHTML = `
        <div class="lpra-confirm-panel" role="dialog" aria-modal="true" aria-label="${escapeHtml(actionLabel)}">
          <div class="lpra-confirm-head">
            <strong>${escapeHtml(actionLabel)}</strong>
            <button type="button" class="lpra-confirm-close" aria-label="取消">x</button>
          </div>
          <div class="lpra-confirm-summary">
            <div><span>人选</span><b>${escapeHtml(payload.candidate || '未识别')}</b></div>
            <div><span>客户/岗位</span><b>${escapeHtml(payload.client || '未选客户')} / ${escapeHtml(payload.job || '未选岗位')}</b></div>
            <div><span>将写入动作</span><b>${escapeHtml(talentWriteLabel(actionLabel, payload))}</b></div>
          </div>
          ${guardBlock}
          ${poolOnlyBlock}
          ${needsStopReason ? `
            <div class="lpra-confirm-stop">
              <span>停止原因</span>
              <div class="lpra-confirm-reasons">${reasonOptions}</div>
              <input id="lpra-stop-reason-note" type="text" placeholder="其他补充，可不填">
            </div>
          ` : ''}
          <div class="lpra-confirm-actions">
            <button type="button" class="lpra-confirm-cancel">取消</button>
            <button type="button" class="lpra-confirm-submit ${needsStopReason ? 'lpra-warn' : 'lpra-primary'}">${poolOnlyIntake ? '入人才库储备（不挂岗位）' : '确认写入'}</button>
          </div>
        </div>
      `;
      root.appendChild(layer);
      layer.querySelector('.lpra-confirm-close')?.addEventListener('click', () => closeTalentConfirmLayer(null));
      layer.querySelector('.lpra-confirm-cancel')?.addEventListener('click', () => closeTalentConfirmLayer(null));
      layer.addEventListener('click', event => {
        if (event.target === layer) closeTalentConfirmLayer(null);
      });
      layer.querySelector('.lpra-confirm-submit')?.addEventListener('click', () => {
        const result = needsStopReason ? selectedStopReasonFromLayer(layer) : {};
        if (poolOnlyIntake) result.pool_only = true;
        closeTalentConfirmLayer(result);
      });
      layer.querySelector('.lpra-confirm-submit')?.focus();
    });
  }

  function updateCandidateIntakeButtonFromLookup(result = null) {
    const btn = document.querySelector('#liepin-reply-candidate-intake');
    const matched = !!result?.lookup?.matched;
    if (btn) {
      btn.hidden = matched;
      btn.disabled = matched;
      btn.setAttribute('aria-hidden', matched ? 'true' : 'false');
      btn.title = matched
        ? '当前人选已唯一定位，无需重复确认入库'
        : '当前人选还未唯一定位时，先加入统一人才库';
    }
    updateResumeReviewButtonsFromLookup(result);
  }

  function setButtonWritableState(button, writable, title) {
    if (!button) return;
    button.disabled = !writable;
    button.dataset.writeReady = writable ? 'true' : 'false';
    button.title = title;
    button.setAttribute('aria-disabled', writable ? 'false' : 'true');
  }

  function updateResumeReviewButtonsFromLookup(result = null) {
    const continueButton = document.querySelector('#liepin-reply-review-continue');
    const stopButton = document.querySelector('#liepin-reply-review-stop');
    if (!continueButton && !stopButton) return;
    const writable = canWriteTalentAction(result);
    const lookup = result?.lookup || {};
    const summary = talentSyncSummary(result);
    const pending = Number(summary.pending_review || 0) > 0;
    const unmatched = !lookup.matched || pending;
    const blockedTitle = unmatched
      ? '需先唯一定位到 A 系统人选，或先确认入库后再推进/停止'
      : '当前动作预检未通过，暂不允许写库';
    setButtonWritableState(
      continueButton,
      writable,
      writable ? '触达后复核通过：默认确认触达已核验，并继续推进' : blockedTitle
    );
    setButtonWritableState(
      stopButton,
      writable,
      writable ? '触达后复核不通过：默认确认触达已核验，并停止推进' : blockedTitle
    );
    const shouldFoldReview = !!result && !writable && unmatched;
    [continueButton, stopButton].forEach(button => {
      if (!button) return;
      button.hidden = shouldFoldReview;
      button.setAttribute('aria-hidden', shouldFoldReview ? 'true' : 'false');
    });
    const statusEl = document.querySelector('#liepin-reply-assistant-status');
    if (!writable && statusEl?.textContent?.includes('可确认推进或停止')) {
      if (!result) {
        statusEl.textContent = '正在预检统一人才库...';
      } else if (lookup.matched) {
        statusEl.textContent = `${talentLookupMessage(result)}，当前动作不可写`;
      } else {
        statusEl.textContent = `${talentLookupMessage(result)}，可先确认入库`;
      }
    }
  }

  function formatTalentReviewStatus(match = {}) {
    const stage = clean(match.clean_stage || match.progress_stage || '');
    const latest = clean(match.latest_review_status || '');
    if (latest === 'stop' || /^H5\s/.test(stage)) return '已复核停止';
    if (latest === 'continue') return '已复核推进';
    if (!match?.reviewed) return '未复核';
    return `已复核${latest ? `：${latest}` : ''}`;
  }

  function formatTalentOutreachStatus(match = {}) {
    const value = clean(match.latest_outreach_status || '');
    const eventType = clean(match.latest_outreach_type || match.latest_event_type || '');
    const stage = clean(match.clean_stage || match.progress_stage || '');
    const text = `${eventType} ${value} ${stage}`;
    if (/candidate_message_received|replied|已回复/.test(text)) return '已回复';
    if (/clicked_unverified|pending_contact|recommend_clicked_unverified|message_clicked_unverified|existing_chat_no_job_verified|needs_page_review|待核验/.test(text)) return '触达待核验';
    if (/job_chat_verified|message_outreach_verified|im_followup_verified|job_recommended_verified|contacted_backfilled|已触达|contacted/.test(text)) return '已核验触达';
    if (/candidate_message_sent/.test(eventType)) return '消息已发';
    if (!value) return '触达未记录';
    return value;
  }

  function syncProjectSelectionFromLookup(result = null) {
    if (state.projectUserTouched) return false;
    const lookup = result?.lookup || {};
    const match = lookup.match || {};
    const client = clean(match.client || lookup.normalized?.client || '');
    const position = clean(match.job || lookup.normalized?.job || '');
    if (!lookup.matched || !client || !position) return false;
    const applied = applyProjectSelection({ client, position }, 'A系统定位', false);
    const meta = document.querySelector('#liepin-reply-assistant-meta');
    if (applied && meta) {
      meta.textContent = meta.textContent.replace(/(?:岗位|评分画像)：[^｜]+(?:（未绑定岗位）)?/, `岗位：${client} · ${position}`);
    }
    return applied;
  }

  function rememberTalentLookupMatch(result = null) {
    const lookup = result?.lookup || {};
    const match = lookup.match || {};
    const jobCandidateId = match.job_candidate_id || lookup.job_candidate_id || '';
    if (!lookup.matched || !jobCandidateId) return null;
    state.lastTalentLookupMatch = {
      at: Date.now(),
      sourceUrl: location.href,
      job_candidate_id: jobCandidateId,
      match: { ...match },
      normalized: { ...(lookup.normalized || {}) }
    };
    return state.lastTalentLookupMatch;
  }

  function clearTalentLookupMatch() {
    state.lastTalentLookupMatch = null;
  }

  function locatorWithRememberedTalentMatch(locator = {}) {
    const cached = state.lastTalentLookupMatch;
    if (!cached || cached.sourceUrl !== location.href) return locator;
    const match = cached.match || {};
    const normalized = cached.normalized || {};
    const jobCandidateId = match.job_candidate_id || cached.job_candidate_id || locator.job_candidate_id || '';
    if (!jobCandidateId) return locator;
    return {
      ...locator,
      job_candidate_id: jobCandidateId,
      candidate: locator.candidate || normalized.candidate || match.candidate || '',
      company: locator.company || normalized.company || match.company || '',
      title: locator.title || normalized.title || match.title || '',
      client: match.client || normalized.client || locator.client || '',
      job: match.job || normalized.job || locator.job || '',
      source_candidate_id: locator.source_candidate_id || normalized.source_candidate_id || match.source_candidate_id || ''
    };
  }

  function renderTalentProgress(result = null) {
    const el = document.querySelector('#liepin-reply-talent-progress');
    if (!el) return;
    const lookup = result?.lookup || {};
    const match = lookup.match || {};
    if (lookup.matched && match.job_candidate_id) {
      rememberTalentLookupMatch(result);
      const synced = syncProjectSelectionFromLookup(result);
      const projectSignature = `${clean(match.client)}|${clean(match.job)}|${clean(match.job_candidate_id)}`;
      if (synced && projectSignature !== state.lastLookupProjectRefreshSignature) {
        state.lastLookupProjectRefreshSignature = projectSignature;
        setTimeout(() => refreshMatchPanel(), 50);
      }
      const stage = clean(match.progress_stage || match.clean_stage || '未分阶段');
      const review = formatTalentReviewStatus(match);
      const outreach = formatTalentOutreachStatus(match);
      const reviewTime = clean(match.latest_review_time || '').slice(5, 16);
      el.dataset.state = match.reviewed ? 'reviewed' : 'active';
      updateCandidateIntakeButtonFromLookup(result);
      el.innerHTML = `
        <span>关系 #${escapeHtml(String(match.job_candidate_id))}</span>
        <span>阶段：${escapeHtml(stage)}</span>
        <span>复核：${escapeHtml(review)}${reviewTime ? ` ${escapeHtml(reviewTime)}` : ''}</span>
        <span>触达：${escapeHtml(outreach)}</span>
      `;
      return;
    }
    const summary = talentSyncSummary(result);
    if (Number(summary.pending_review || 0) > 0 || result?.ok) {
      el.dataset.state = 'pending';
      updateCandidateIntakeButtonFromLookup(result);
      const issue = lookupIssueMessage(result, state.lastTalentDryRun?.payload || {});
      el.innerHTML = `<span>${escapeHtml(issue)}</span><span>可先确认入库/检查岗位</span>`;
      return;
    }
    el.dataset.state = 'idle';
    updateCandidateIntakeButtonFromLookup(null);
    el.innerHTML = '<span>库状态待检查</span>';
  }

  function summarizeTalentSync(result) {
    const summary = result?.sync?.result?.summary || result?.sync?.summary || {};
    if (!result) return '同步服务未响应';
    if (summary.pending_review) return `${lookupIssueMessage(result, state.lastTalentDryRun?.payload || {})}；未写入 A 系统`;
    if (!result.ok) return workbenchSyncFailureMessage(result, state.lastTalentDryRun?.payload || {});
    const firstItem = result?.sync?.result?.items?.[0] || result?.sync?.items?.[0] || {};
    const poolIntake = firstItem.resolve_status === 'pool_intake' || /pool_intake/.test(clean(firstItem.reason || ''));
    const poolReason = clean(firstItem.reason || '').replace(/^pool_intake:\s*/, '');
    if (summary.would_write) {
      return poolIntake
        ? `预检通过：${poolReason || '将入人才库储备（不挂岗位）'}`
        : `预检通过，将写入 ${summary.would_write} 条`;
    }
    if (summary.written) {
      return poolIntake
        ? `${poolReason || '已入人才库储备（不挂岗位），之后可补选客户/岗位'}`
        : `已同步 ${summary.written} 条`;
    }
    if (summary.already_exists) return '已同步过，未重复写入';
    return result.dry_run ? '预检完成' : '同步完成';
  }

  async function dryRunTalentAction(payload) {
    status('正在预检同步...');
    const result = await postTalentAction({
      ...payload,
      write: false,
      refresh_workbench: false
    });
    state.lastTalentDryRun = {
      at: Date.now(),
      payload,
      result
    };
    rememberTalentLookupMatch(result);
    if (payload?.plugin_surface === 'resume_match') {
      updateCandidateIntakeButtonFromLookup(result);
      renderTalentProgress(result);
    }
    status(summarizeTalentSync(result));
    return result;
  }

  async function writeTalentAction(payload) {
    status('正在写入统一人才库...');
    const result = await postTalentAction({
      ...payload,
      write: true,
      refresh_workbench: true
    });
    status(summarizeTalentSync(result));
    if (payload?.plugin_surface === 'resume_match') {
      setTimeout(() => autoPreviewResumeReview({ force: true }), 500);
    }
    return result;
  }

  function resumeReviewPayload(decision = 'continue', options = {}) {
    if (options.refresh !== false) refreshMatchPanel();
    const locator = locatorWithRememberedTalentMatch(currentTalentLocator());
    const result = state.currentContext?.result || state.lastGenerated || {};
    const scoreText = `${result.score || state.lastGenerated?.score || ''}分 ${result.grade || state.lastGenerated?.grade || ''}`.trim();
    const shouldStop = decision === 'stop';
    return {
      kind: 'resume_review',
      plugin_surface: 'resume_match',
      ...locator,
      review_result: shouldStop ? 'stop' : 'continue',
      summary: shouldStop
        ? `人岗匹配插件复核：停止推进 ${scoreText}`.trim()
        : `人岗匹配插件复核：继续推进 ${scoreText}`.trim(),
      next_action: shouldStop ? '停止推进，默认确认猎聘触达已核验并保留为不匹配/暂缓记录' : (result.action || '继续推进，默认确认猎聘触达已核验'),
      reason: shouldStop ? '插件内人工复核后选择停止推进，并默认确认猎聘触达已核验' : '插件内人工复核后选择继续推进，并默认确认猎聘触达已核验',
      stage_after: shouldStop ? 'H5 最近寻访/初筛不通过' : '已触达',
      flow_bucket: shouldStop ? '最近寻访' : '猎聘触达',
      outreach_status: 'job_chat_verified',
      verification_evidence: shouldStop ? '确认停止默认确认：猎聘触达已核验' : '确认推进默认确认：猎聘触达已核验',
      score: result.score || state.lastGenerated?.score || '',
      grade: result.grade || state.lastGenerated?.grade || '',
      source_url: location.href
    };
  }

  function candidateIntakePayload(options = {}) {
    if (options.refresh !== false) refreshMatchPanel();
    const locator = currentTalentLocator();
    const result = state.currentContext?.result || state.lastGenerated || {};
    const scoreText = `${result.score || state.lastGenerated?.score || ''}分 ${result.grade || state.lastGenerated?.grade || ''}`.trim();
    return {
      kind: 'candidate_intake',
      plugin_surface: 'resume_match',
      ...locator,
      summary: `人岗匹配插件确认入库：${locator.candidate || '未识别人选'}｜${locator.client || '未选客户'}/${locator.job || '未选岗位'} ${scoreText}`.trim(),
      reason: '插件内人工确认加入统一人才库',
      clean_stage: 'H1 最近寻访/待筛',
      flow_bucket: '最近寻访',
      score: result.score || state.lastGenerated?.score || '',
      grade: result.grade || state.lastGenerated?.grade || '',
      source_url: location.href
    };
  }

  function outreachVerificationPayload(options = {}) {
    if (options.refresh !== false) refreshMatchPanel();
    const locator = locatorWithRememberedTalentMatch(currentTalentLocator());
    return {
      kind: 'outreach_verification',
      plugin_surface: 'resume_match',
      ...locator,
      outreach_status: 'job_chat_verified',
      clean_stage: '已触达',
      flow_bucket: '猎聘触达',
      summary: `人岗匹配插件确认触达已核验：${locator.candidate || '未识别人选'}｜${locator.client || '未选客户'}/${locator.job || '未选岗位'}`,
      reason: '插件内人工确认猎聘页面已显示继续沟通/目标岗位触达成功',
      verification_evidence: '猎聘页面人工核验：继续沟通/目标岗位推荐成功/岗位消息已发送',
      source_url: location.href
    };
  }

  async function previewResumeReview(decision = 'continue') {
    if (!isLiepinResumeDetailPage()) return;
    await dryRunTalentAction(resumeReviewPayload(decision));
  }

  function talentSyncSummary(result) {
    return result?.sync?.result?.summary || result?.sync?.summary || {};
  }

  function canWriteTalentAction(result) {
    const summary = talentSyncSummary(result);
    return !!result?.ok && Number(summary.would_write || 0) > 0 && Number(summary.pending_review || 0) === 0;
  }

  function talentLookupMessage(result, fallbackPayload = {}) {
    const lookup = result?.lookup || {};
    if (lookup.matched && lookup.match) {
      return `已定位：${lookup.match.candidate}｜${lookup.match.client}/${lookup.match.job}`;
    }
    return lookupIssueMessage(result, fallbackPayload);
  }

  async function confirmResumeReview(decision = 'continue') {
    if (!isLiepinResumeDetailPage()) return;
    let payload = resumeReviewPayload(decision, { refresh: false });
    const actionLabel = decision === 'stop' ? '确认停止' : '确认推进';
    if (!canWriteTalentAction(state.lastTalentDryRun?.result)) {
      let dryRun = await dryRunTalentAction(payload);
      if (!canWriteTalentAction(dryRun)) {
        const summary = talentSyncSummary(dryRun);
        const canBridgeByIntake = Number(summary.pending_review || 0) > 0 || !dryRun?.lookup?.matched;
        if (canBridgeByIntake) {
          const intakePayload = candidateIntakePayload({ refresh: false });
          const intakeDryRun = await dryRunTalentAction(intakePayload);
          if (canWriteTalentAction(intakeDryRun)) {
            attachProjectGuardWarnings(intakePayload, intakeDryRun);
            const intakeConfirm = await confirmTalentAction(`先入库后${actionLabel}`, intakePayload);
            if (!intakeConfirm) {
              status(`已取消先入库后${actionLabel}`);
              return;
            }
            const intakeResult = await writeTalentAction(intakePayload);
            if (!intakeResult?.ok) return;
            state.lastIntakeBridge = {
              at: Date.now(),
              actionLabel,
              payload: intakePayload
            };
            payload = resumeReviewPayload(decision, { refresh: false });
            dryRun = await dryRunTalentAction(payload);
          }
        }
        if (!canWriteTalentAction(dryRun)) {
          status(summarizeTalentSync(dryRun));
          return;
        }
      }
    }
    Object.assign(payload, locatorWithRememberedTalentMatch(payload));
    attachProjectGuardWarnings(payload, state.lastTalentDryRun?.result);
    const confirmResult = await confirmTalentAction(actionLabel, payload);
    if (!confirmResult) {
      status(`已取消${actionLabel}`);
      return;
    }
    if (decision === 'stop') {
      Object.assign(payload, confirmResult);
      const reasonTail = payload.stop_reason_label
        ? `｜原因：${payload.stop_reason_label}${payload.stop_reason_note ? ` - ${payload.stop_reason_note}` : ''}`
        : '';
      payload.summary = `${payload.summary}${reasonTail}`;
      payload.reason = `插件内人工复核后选择停止推进${reasonTail}`;
      payload.next_action = `停止推进，保留为不匹配/暂缓记录${reasonTail}`;
    }
    Object.assign(payload, locatorWithRememberedTalentMatch(payload));
    const dryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(dryRun)) {
      status(summarizeTalentSync(dryRun));
      return;
    }
    await writeTalentAction({
      ...payload,
      summary: decision === 'stop'
        ? payload.summary.replace('人岗匹配插件复核', '人岗匹配插件确认复核')
        : payload.summary.replace('人岗匹配插件复核', '人岗匹配插件确认复核')
    });
  }

  async function confirmOutreachVerification() {
    if (!isLiepinResumeDetailPage()) return;
    let payload = outreachVerificationPayload({ refresh: false });
    let dryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(dryRun)) {
      status(summarizeTalentSync(dryRun));
      return;
    }
    Object.assign(payload, locatorWithRememberedTalentMatch(payload));
    attachProjectGuardWarnings(payload, dryRun);
    const confirmResult = await confirmTalentAction('核验触达', payload);
    if (!confirmResult) {
      status('已取消核验触达');
      return;
    }
    payload = { ...payload, ...confirmResult };
    dryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(dryRun)) {
      status(summarizeTalentSync(dryRun));
      return;
    }
    await writeTalentAction(payload);
  }

  function replyAssistantStopPayload(projectOverride = null) {
    const context = readPageContext();
    state.currentContext = context;
    const locator = currentTalentLocator(projectOverride);
    const latestMessage = latestCandidateReplyText(context);
    return {
      kind: 'resume_review',
      plugin_surface: 'reply_assistant',
      ...locator,
      review_result: 'stop',
      summary: `回复助手确认：停止推进${latestMessage ? ` - ${compactText(latestMessage, 80)}` : ''}`,
      next_action: '停止推进，保留为不匹配/暂缓记录',
      reason: '回复助手内人工选择停止推进',
      stage_after: 'H5 最近寻访/初筛不通过',
      flow_bucket: '最近寻访',
      message_preview: latestMessage,
      source_url: location.href
    };
  }

  async function confirmReplyAssistantStop() {
    if (!isLiepinImPage()) return;
    let payload;
    let dryRun;
    let intakeBeforeStopPayload = null;
    let projectOverride = null;
    try {
      const context = readPageContext();
      state.currentContext = context;
      projectOverride = await recentOutreachProjectForCurrentContact({
        candidate_title: context.contact?.title || '',
        candidate_profile_text: context.combinedText || ''
      });
      if (!projectOverride) {
        projectOverride = normalizeTalentProject(await ensureProjectFromRecentOutreachForCurrentContext());
      }
      payload = replyAssistantStopPayload(projectOverride);
      const recent = await getFromWorkbench('/api/recent-outreach-project', {
        candidate_name: payload.candidate || state.lastGenerated?.candidateName || normalizeName(state.currentContext?.contact?.name)
      });
      if (recent?.matched && recent?.project?.client && recent?.project?.position && (recent.auto_apply || recent.match?.auto_apply)) {
        projectOverride = normalizeTalentProject(recent.project);
        state.recentOutreachProject = projectOverride;
        applyProjectSelection(projectOverride, '触达记录', false);
        payload = replyAssistantStopPayload(projectOverride);
      }
      dryRun = await dryRunTalentAction(payload);
      if (!canWriteTalentAction(dryRun) && !isConcreteProject(projectOverride)) {
        status('正在按最近触达岗位重新定位...');
        const retryRecent = await getFromWorkbench('/api/recent-outreach-project', {
          candidate_name: payload.candidate,
          candidate_title: payload.title,
          candidate_profile_text: [
            payload.candidate,
            payload.title,
            state.currentContext?.combinedText || ''
          ].map(clean).filter(Boolean).join('\n').slice(0, 3000),
          source_url: isLiepinImPage() ? '' : location.href
        });
        if (retryRecent?.matched && retryRecent?.project?.client && retryRecent?.project?.position && (retryRecent.auto_apply || retryRecent.match?.auto_apply)) {
          projectOverride = normalizeTalentProject(retryRecent.project);
          state.recentOutreachProject = projectOverride;
          applyProjectSelection(projectOverride, '触达记录', false);
          status(`已按最近触达岗位定位：${projectLabel(projectOverride)}`);
          payload = replyAssistantStopPayload(projectOverride);
          dryRun = await dryRunTalentAction(payload);
        }
      }
      if (!canWriteTalentAction(dryRun)) {
        intakeBeforeStopPayload = {
          kind: 'candidate_intake',
          plugin_surface: 'reply_assistant',
          candidate: payload.candidate,
          company: payload.company,
          title: payload.title,
          client: payload.client,
          job: payload.job,
          candidate_profile_text: payload.candidate_profile_text,
          source_url: payload.source_url,
          summary: `回复助手停止前补充入库：${payload.candidate || '未识别人选'}｜${payload.client || '未选客户'}/${payload.job || '未选岗位'}`,
          reason: '停止推进前补齐触达岗位绑定',
          clean_stage: 'H1 最近寻访/待筛',
          flow_bucket: '最近寻访'
        };
        const intakeDryRun = await dryRunTalentAction(intakeBeforeStopPayload);
        if (!canWriteTalentAction(intakeDryRun)) {
          dryRun = intakeDryRun;
          intakeBeforeStopPayload = null;
        }
      }
    } catch (error) {
      status(`停止推进定位失败：${error?.message || error}`);
      return;
    }
    if (!canWriteTalentAction(dryRun) && !intakeBeforeStopPayload) {
      status(summarizeTalentSync(dryRun));
      return;
    }
    const confirmResult = await confirmTalentAction('停止推进', payload);
    if (!confirmResult) {
      status('已取消停止推进');
      return;
    }
    Object.assign(payload, confirmResult);
    const reasonTail = payload.stop_reason_label
      ? `｜原因：${payload.stop_reason_label}${payload.stop_reason_note ? ` - ${payload.stop_reason_note}` : ''}`
      : '';
    payload.summary = `${payload.summary}${reasonTail}`;
    payload.reason = `回复助手内人工选择停止推进${reasonTail}`;
    payload.next_action = `停止推进，保留为不匹配/暂缓记录${reasonTail}`;
    if (intakeBeforeStopPayload) {
      const intakeResult = await writeTalentAction(intakeBeforeStopPayload);
      if (!intakeResult?.ok) return;
      dryRun = await dryRunTalentAction(payload);
      if (!canWriteTalentAction(dryRun)) {
        status(summarizeTalentSync(dryRun));
        return;
      }
    }
    const confirmedDryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(confirmedDryRun)) {
      status(summarizeTalentSync(confirmedDryRun));
      return;
    }
    await writeTalentAction(payload);
  }

  async function handleFloatingFillResume(command = {}) {
    if (!isLiepinResumeDetailPage()) {
      await reportFloatingCommandResult(command, 'blocked', '当前不是猎聘简历详情页，无法采集简历入库。');
      return;
    }
    showPanelForBridgeAction();
    status('ASA 正在采集简历并做入库预检...');
    await reportFloatingCommandResult(command, 'running', '猎聘页面正在采集简历并做入库预检。');
    try {
      await refreshMatchPanel();
      const payload = candidateIntakePayload({ refresh: false });
      let dryRun = await dryRunTalentAction(payload);
      const summary = talentSyncSummary(dryRun);
      if (dryRun?.lookup?.matched || Number(summary.already_exists || 0) > 0) {
        const message = `${talentLookupMessage(dryRun, payload)}；当前人选已在 A 系统中定位，无需重复入库。`;
        status(message);
        await reportFloatingCommandResult(command, 'completed', message, {
          lookup: dryRun?.lookup || null,
          summary
        });
        return;
      }
      if (!canWriteTalentAction(dryRun)) {
        const message = summarizeTalentSync(dryRun);
        status(message);
        await reportFloatingCommandResult(command, 'blocked', message, {
          lookup: dryRun?.lookup || null,
          summary
        });
        return;
      }

      attachProjectGuardWarnings(payload, dryRun);
      const poolOnlyIntake = !clean(payload.client) || !clean(payload.job);
      const confirmMessage = poolOnlyIntake
        ? `入库预检通过：${payload.candidate || '当前人选'}｜未选客户/岗位，可入人才库储备（不挂岗位）。请在猎聘页确认。`
        : `入库预检通过：${payload.candidate || '当前人选'}｜${payload.client || '未选客户'} / ${payload.job || '未选岗位'}。请在猎聘页确认写入。`;
      status(confirmMessage);
      await reportFloatingCommandResult(command, 'requires_confirmation', confirmMessage, {
        lookup: dryRun?.lookup || null,
        summary
      });
      const confirmResult = await confirmTalentAction('确认入库', payload);
      if (!confirmResult) {
        status('已取消确认入库');
        await reportFloatingCommandResult(command, 'cancelled', '已取消确认入库，未写入 A 系统。');
        return;
      }
      if (confirmResult.pool_only) payload.pool_only = true;

      dryRun = await dryRunTalentAction(payload);
      if (!canWriteTalentAction(dryRun)) {
        const message = summarizeTalentSync(dryRun);
        status(message);
        await reportFloatingCommandResult(command, 'blocked', message, {
          lookup: dryRun?.lookup || null,
          summary: talentSyncSummary(dryRun)
        });
        return;
      }
      const result = await writeTalentAction(payload);
      const writeSummary = talentSyncSummary(result);
      if (!result?.ok) {
        const message = summarizeTalentSync(result);
        status(message);
        await reportFloatingCommandResult(command, 'failed', message, {
          summary: writeSummary,
          error: result?.error || result?.stderr || ''
        });
        return;
      }
      state.lastAutoReviewSignature = '';
      setTimeout(async () => {
        await autoPreviewResumeReview({ force: true }).catch(() => null);
        if (canWriteTalentAction(state.lastTalentDryRun?.result)) {
          status('已入库并重新定位；触达后可确认推进/停止');
        }
      }, 500);
      await reportFloatingCommandResult(command, 'completed', summarizeTalentSync(result), {
        summary: writeSummary,
        lookup: result?.lookup || null
      });
    } catch (error) {
      const message = `入库流程执行失败：${error?.message || error}`;
      status(message);
      await reportFloatingCommandResult(command, 'failed', message);
    }
  }

  async function confirmCandidateIntake() {
    if (!isLiepinResumeDetailPage()) return;
    const payload = candidateIntakePayload();
    const dryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(dryRun)) {
      status(summarizeTalentSync(dryRun));
      return;
    }
    attachProjectGuardWarnings(payload, dryRun);
    const confirmResult = await confirmTalentAction('确认入库', payload);
    if (!confirmResult) {
      status('已取消确认入库');
      return;
    }
    if (confirmResult.pool_only) payload.pool_only = true;
    const finalDryRun = await dryRunTalentAction(payload);
    if (!canWriteTalentAction(finalDryRun)) {
      status(summarizeTalentSync(finalDryRun));
      return;
    }
    const result = await writeTalentAction(payload);
    if (result?.ok) {
      state.lastAutoReviewSignature = '';
      setTimeout(async () => {
        await autoPreviewResumeReview({ force: true }).catch(() => null);
        if (canWriteTalentAction(state.lastTalentDryRun?.result)) {
          status('已入库并重新定位；触达后可确认推进/停止');
        }
      }, 500);
    }
  }

  async function autoPreviewResumeReview(options = {}) {
    if (!isLiepinResumeDetailPage()) return;
    const payload = resumeReviewPayload('continue', { refresh: false });
    const signature = JSON.stringify({
      url: location.href,
      candidate: payload.candidate,
      client: payload.client,
      job: payload.job,
      score: payload.score,
      grade: payload.grade
    });
    if (!options.force && state.lastAutoReviewSignature === signature) return;
    state.lastAutoReviewSignature = signature;
    const previousStatus = state.lastStatus;
    const result = await dryRunTalentAction(payload);
    const summary = talentSyncSummary(result);
    if (canWriteTalentAction(result)) {
      status(`${talentLookupMessage(result, payload)}，触达后可确认推进或停止`);
    } else if (Number(summary.pending_review || 0) > 0) {
      status(`${talentLookupMessage(result, payload)}，可先确认入库`);
    } else {
      status(previousStatus || summarizeTalentSync(result));
    }
  }

  function candidateMessagePayload(direction, context, projectOverride, messageText = '') {
    const locator = currentTalentLocator(projectOverride);
    const conversation = context.conversation || currentConversationSnapshot();
    const evidence = direction === 'received' ? context.latestReceivedEvidence : context.latestSentEvidence;
    const text = clean(messageText || evidence?.text || '');
    const capturedAt = new Date().toISOString();
    const explicit = Boolean(evidence && normalizeMessageDirectionText(evidence.text) === normalizeMessageDirectionText(text));
    const candidate = candidateReplyPayload(text, context, projectOverride);
    const textarea = document.querySelector('#liepin-reply-assistant-draft');
    const outboundDraft = clean(textarea?.value || state.currentDraft);
    return {
      kind: 'candidate_message',
      plugin_surface: 'reply_assistant',
      ...candidate,
      ...locator,
      direction,
      channel: 'liepin',
      message_status: 'done',
      message_intent: state.lastGenerated?.strategyLabel || state.lastGenerated?.strategyKey || '',
      summary: direction === 'received'
        ? `回复助手核验候选人回复：${compactText(text, 80)}`
        : `回复助手核验我方已发送：${compactText(text, 80)}`,
      reason: direction === 'received' ? '候选人回复经原文核对和入站证据预检' : '我方消息经猎聘出站节点核验',
      raw_text: text,
      message_preview: text,
      message_evidence: explicit
        ? evidence.evidence
        : direction === 'received' ? 'manual_transcription' : '',
      message_id: explicit
        ? evidence.messageId
        : `manual-${window.LIEPIN_MESSAGE_EVIDENCE?.stableHash?.([conversation.conversationId, text, capturedAt].join('|')) || Date.now()}`,
      message_time: explicit ? evidence.messageTime : capturedAt,
      conversation_id: conversation.conversationId,
      conversation_identity_confidence: conversation.confidence,
      outbound_draft_preview: outboundDraft,
      stage_after: direction === 'received' ? 'S3 已回复' : '',
      flow_bucket: direction === 'received' ? '正式流程' : '',
      source_url: location.href
    };
  }

  function confirmCandidateMessageAtomic(actionLabel, initialPayload, conversationSnapshot, editable = false) {
    return new Promise(resolve => {
      closeTalentConfirmLayer(null);
      const root = document.querySelector(`#${ROOT_ID}`);
      if (!root) {
        resolve(null);
        return;
      }
      const layer = document.createElement('div');
      layer.id = 'liepin-reply-action-confirm';
      layer.className = 'lpra-confirm-layer';
      layer.__resolveTalentConfirm = resolve;
      layer.innerHTML = `
        <div class="lpra-confirm-panel" role="dialog" aria-modal="true" aria-label="${escapeHtml(actionLabel)}">
          <div class="lpra-confirm-head">
            <strong>${escapeHtml(actionLabel)}</strong>
            <button type="button" class="lpra-confirm-close" aria-label="取消">x</button>
          </div>
          <div class="lpra-confirm-summary">
            <div><span>人选</span><b>${escapeHtml(initialPayload.candidate || initialPayload.candidate_name || '未识别')}</b></div>
            <div><span>客户/岗位</span><b>${escapeHtml(initialPayload.client || '未选客户')} / ${escapeHtml(initialPayload.job || initialPayload.position || '未选岗位')}</b></div>
            <div><span>会话证据</span><b>${escapeHtml(initialPayload.conversation_id || '未识别')} · ${escapeHtml(initialPayload.message_evidence || '待人工录入')}</b></div>
          </div>
          <label class="lpra-confirm-message-label" for="lpra-candidate-message-text">将记录的消息原文</label>
          <textarea id="lpra-candidate-message-text" class="lpra-confirm-textarea" spellcheck="false" ${editable ? '' : 'readonly'} placeholder="${editable ? '粘贴或输入候选人的实际回复' : ''}">${escapeHtml(initialPayload.message_preview || '')}</textarea>
          <div id="lpra-candidate-message-preflight" class="lpra-confirm-guard" hidden></div>
          <div class="lpra-confirm-actions">
            <button type="button" class="lpra-confirm-cancel">取消</button>
            <button type="button" class="lpra-confirm-submit lpra-primary">运行预检</button>
          </div>
        </div>
      `;
      root.appendChild(layer);
      const textarea = layer.querySelector('#lpra-candidate-message-text');
      const preflightEl = layer.querySelector('#lpra-candidate-message-preflight');
      const submit = layer.querySelector('.lpra-confirm-submit');
      let preflight = null;
      let preparedPayload = null;
      const close = value => closeTalentConfirmLayer(value);
      const resetPreflight = () => {
        preflight = null;
        preparedPayload = null;
        submit.textContent = '运行预检';
        preflightEl.hidden = true;
      };
      textarea?.addEventListener('input', resetPreflight);
      layer.querySelector('.lpra-confirm-close')?.addEventListener('click', () => close(null));
      layer.querySelector('.lpra-confirm-cancel')?.addEventListener('click', () => close(null));
      layer.addEventListener('click', event => {
        if (event.target === layer) close(null);
      });
      submit?.addEventListener('click', async () => {
        if (preflight?.confirmation_token && preparedPayload) {
          close({ payload: preparedPayload, preflight });
          return;
        }
        const text = clean(textarea?.value || '');
        if (!text) {
          preflightEl.hidden = false;
          preflightEl.innerHTML = '<strong>预检未通过</strong><p>消息原文不能为空。</p>';
          return;
        }
        const payload = { ...initialPayload, raw_text: text, message_preview: text };
        if (editable && normalizeMessageDirectionText(text) !== normalizeMessageDirectionText(initialPayload.message_preview)) {
          const capturedAt = new Date().toISOString();
          payload.message_evidence = 'manual_transcription';
          payload.message_time = capturedAt;
          payload.message_id = `manual-${window.LIEPIN_MESSAGE_EVIDENCE?.stableHash?.([payload.conversation_id, text, capturedAt].join('|')) || Date.now()}`;
          payload.summary = `回复助手核验候选人回复：${compactText(text, 80)}`;
        }
        if (payload.direction === 'received' && isOutboundDraftCollision(text, payload.outbound_draft_preview)) {
          preflightEl.hidden = false;
          preflightEl.innerHTML = '<strong>预检未通过</strong><p>这段文字与我方草稿相同，请核对候选人的实际回复。</p>';
          return;
        }
        submit.disabled = true;
        submit.textContent = '预检中...';
        preflight = await postToWorkbench('/api/candidate-message-preflight', payload);
        submit.disabled = false;
        preflightEl.hidden = false;
        if (!preflight?.ok) {
          preflightEl.innerHTML = `<strong>预检未通过</strong><p>${escapeHtml(preflight?.reason || preflight?.error || '无法安全写入')}</p>`;
          submit.textContent = '重新预检';
          return;
        }
        preparedPayload = payload;
        const classification = preflight.classification || {};
        preflightEl.innerHTML = `
          <strong>预检通过</strong>
          <p>意图：${escapeHtml(classification.intent || 'outbound')} · 优先级：P${escapeHtml(classification.priority || 3)}</p>
          <p>${escapeHtml(classification.suggested_next_action || '等待候选人回复。')}</p>
        `;
        submit.textContent = '确认原子写入';
      });
      textarea?.focus();
      if (editable) textarea?.select();
    });
  }

  async function commitCandidateMessageAtomic(actionLabel, payload, conversationSnapshot, editable = false) {
    const confirmed = await confirmCandidateMessageAtomic(actionLabel, payload, conversationSnapshot, editable);
    if (!confirmed) {
      status(`已取消${actionLabel}`);
      return null;
    }
    if (!conversationSnapshotMatches(conversationSnapshot)) {
      status('会话已切换，本次写入已取消，请在当前人选会话重新操作');
      return null;
    }
    const result = await postToWorkbench('/api/candidate-message-commit', {
      ...confirmed.payload,
      confirmation_token: confirmed.preflight.confirmation_token
    });
    if (!result?.ok) {
      status(`写入失败：${clean(result?.reason || result?.error || '预检确认已失效')}`);
      return null;
    }
    status(result.message || '消息与 A 系统阶段已同步');
    return result;
  }

  async function previewCandidateMessage(direction = 'sent') {
    const context = readPageContext();
    state.currentContext = context;
    const projectOverride = await recentOutreachProjectForCurrentContact() || await ensureProjectFromRecentOutreachForCurrentContext();
    const evidence = direction === 'received' ? context.latestReceivedEvidence : context.latestSentEvidence;
    if (!evidence?.text) {
      status(direction === 'sent' ? '未找到猎聘明确的我方已发送消息节点' : '未找到候选人明确的入站消息节点');
      return;
    }
    const payload = candidateMessagePayload(direction, context, projectOverride, evidence.text);
    const result = await postToWorkbench('/api/candidate-message-preflight', payload);
    status(result?.ok ? `预检通过：${result.candidate}｜${result.client}/${result.job}` : `预检未通过：${clean(result?.reason || result?.error)}`);
  }

  async function confirmCandidateMessage(direction = 'sent') {
    if (direction === 'received') {
      await recordCandidateReplyFromChat();
      return;
    }
    const context = readPageContext();
    state.currentContext = context;
    if (!context.latestSentEvidence?.text) {
      status('未找到猎聘明确的我方出站消息，不能仅凭“已手发”按钮写入');
      return;
    }
    const snapshot = context.conversation || currentConversationSnapshot();
    const projectOverride = await recentOutreachProjectForCurrentContact() || await ensureProjectFromRecentOutreachForCurrentContext();
    if (!conversationSnapshotMatches(snapshot)) {
      status('岗位识别期间会话已切换，请重新操作');
      return;
    }
    const payload = candidateMessagePayload('sent', context, projectOverride, context.latestSentEvidence.text);
    await commitCandidateMessageAtomic('核验我方已发', payload, snapshot, false);
  }

  async function recordCandidateReplyFromChat() {
    const context = readPageContext();
    state.currentContext = context;
    const snapshot = context.conversation || currentConversationSnapshot();
    const projectOverride = await recentOutreachProjectForCurrentContact({
      candidate_title: context.contact?.title || '',
      candidate_profile_text: context.combinedText || ''
    }) || await ensureProjectFromRecentOutreachForCurrentContext();
    if (!conversationSnapshotMatches(snapshot)) {
      status('岗位识别期间会话已切换，请重新操作');
      return;
    }
    const payload = candidateMessagePayload('received', context, projectOverride, context.latestReceivedEvidence?.text || '');
    if (!payload.candidate_name && !payload.candidate) {
      status('未识别人选，请先选中具体会话后再记录回复');
      return;
    }
    await commitCandidateMessageAtomic('确认候选人回复', payload, snapshot, true);
  }

  async function saveOutreachEvent(eventType, editedDraft, extra = {}) {
    const payload = currentDraftPayload(editedDraft);
    if (!payload.draft && !payload.candidateName) return;

    const event = {
      id: `${Date.now()}_${Math.random().toString(16).slice(2)}`,
      eventAt: new Date().toISOString(),
      eventType,
      eventStatus: extra.eventStatus || 'done',
      channel: 'liepin',
      candidateName: payload.candidateName,
      candidateTitle: payload.candidateTitle,
      latestMessage: payload.latestMessage,
      strategyKey: payload.strategyKey,
      strategyLabel: payload.strategyLabel,
      score: payload.score,
      grade: payload.grade,
      project: payload.project,
      draft: payload.draft,
      messageSummary: compactText(payload.draft, 220),
      sourceUrl: payload.url,
      note: extra.note || ''
    };

    try {
      chrome.storage.local.get(OUTREACH_EVENT_STORE_KEY, stored => {
        const events = Array.isArray(stored?.[OUTREACH_EVENT_STORE_KEY])
          ? stored[OUTREACH_EVENT_STORE_KEY]
          : [];
        const next = [event, ...events].slice(0, OUTREACH_EVENT_LIMIT);
        chrome.storage.local.set({ [OUTREACH_EVENT_STORE_KEY]: next });
      });
    } catch (_) {
      // outreach event logging is best-effort only
    }
    const synced = await postToWorkbench('/api/reply-assistant-outreach', {
      source: 'extension-direct',
      event
    });
    rememberSyncState('liepinReplyAssistantLastOutreachSync', !!synced?.ok);
  }

  function saveAcceptedSample() {
    const textarea = document.querySelector('#liepin-reply-assistant-draft');
    const editedDraft = clean(textarea?.value || '');
    if (!editedDraft) {
      status('没有可采纳的话术');
      return;
    }
    if (!state.lastGenerated) {
      status('请先生成一条话术');
      return;
    }

    const originalDraft = clean(state.lastGenerated.originalDraft);
    const sample = {
      id: `${Date.now()}_${Math.random().toString(16).slice(2)}`,
      acceptedAt: new Date().toISOString(),
      url: location.href,
      changed: editedDraft !== originalDraft,
      originalDraft,
      editedDraft,
      lengthDelta: editedDraft.length - originalDraft.length,
      candidateName: state.lastGenerated.candidateName,
      candidateTitle: state.lastGenerated.candidateTitle,
      latestMessage: state.lastGenerated.latestMessage,
      strategyKey: state.lastGenerated.strategyKey,
      strategyLabel: state.lastGenerated.strategyLabel,
      score: state.lastGenerated.score,
      grade: state.lastGenerated.grade,
      project: state.lastGenerated.project,
      reasons: state.lastGenerated.reasons,
      missing: state.lastGenerated.missing,
      risk: state.lastGenerated.risk
    };
    saveOutreachEvent('reply_assistant_accept', editedDraft, {
      note: sample.changed ? '采纳了人工修改后的话术' : '采纳了原始生成话术'
    });
    postToWorkbench('/api/reply-assistant-sample', {
      source: 'extension-direct',
      sample
    }).then(result => {
      rememberSyncState('liepinReplyAssistantLastSampleSync', !!result?.ok);
    });

    try {
      chrome.storage.local.get(SAMPLE_STORE_KEY, stored => {
        const samples = Array.isArray(stored?.[SAMPLE_STORE_KEY])
          ? stored[SAMPLE_STORE_KEY]
          : [];
        const next = [sample, ...samples].slice(0, SAMPLE_LIMIT);
        chrome.storage.local.set({ [SAMPLE_STORE_KEY]: next }, () => {
          status(`已采纳修改，累计 ${next.length} 条样本；工作台开着会自动同步`);
        });
      });
    } catch (_) {
      status('采纳失败，请刷新页面后重试');
    }
  }

  function copyAcceptedSamples() {
    try {
      chrome.storage.local.get(SAMPLE_STORE_KEY, async stored => {
        const samples = Array.isArray(stored?.[SAMPLE_STORE_KEY])
          ? stored[SAMPLE_STORE_KEY]
          : [];
        if (!samples.length) {
          status('还没有采纳样本');
          return;
        }
        const payload = {
          type: 'liepin_reply_assistant_samples',
          exportedAt: new Date().toISOString(),
          count: samples.length,
          samples
        };
        await copyText(JSON.stringify(payload, null, 2));
        status(`已复制 ${samples.length} 条样本`);
      });
    } catch (_) {
      status('复制样本失败，请刷新页面后重试');
    }
  }

  function renderResult(result, context) {
    state.currentDraft = result.draft;
    state.currentContext = context;

    const textarea = document.querySelector('#liepin-reply-assistant-draft');
    const meta = document.querySelector('#liepin-reply-assistant-meta');
    const detail = document.querySelector('#liepin-reply-assistant-detail');
    const score = document.querySelector('#liepin-reply-assistant-score');

    if (textarea) textarea.value = result.draft;
    state.lastGenerated = {
      originalDraft: result.draft,
      candidateName: normalizeName(context.contact.name),
      candidateTitle: context.contact.title,
      latestMessage: result.latestMessage,
      strategyKey: result.strategyKey,
      strategyLabel: result.strategy.label,
      score: result.quality.score,
      grade: result.quality.grade,
      project: result.project,
      reasons: result.quality.reasons,
      missing: result.missing,
      risk: result.risk
    };
    if (score) {
      score.textContent = `${result.quality.score}分 · ${result.quality.grade}`;
      score.dataset.grade = result.quality.grade;
    }
    if (meta) {
      meta.textContent = [
        `人选：${normalizeName(context.contact.name) || '未识别'}`,
        context.contact.title ? `头衔：${context.contact.title}` : '',
        `策略：${result.strategy.label}`,
        `项目：${projectLabel(result.project)}（${result.project.confidence}）`
      ].filter(Boolean).join(' ｜ ');
    }
    if (detail) {
      detail.innerHTML = '';
      detail.dataset.view = 'reply-reasoning';
      const rows = [
        ['人选最新回复', compactText(result.latestMessage || context.contact.preview || '未识别')],
        ['生成依据', result.quality.reasons.join('、') || '按通用跟进口径生成'],
        ['需补信息', result.missing.length ? result.missing.join('、') : '暂无明显缺口'],
        ['风险提醒', result.risk.length ? result.risk.join('、') : result.strategy.risk]
      ];
      const reasoning = document.createElement('details');
      reasoning.className = 'lpra-reasoning';
      const summary = document.createElement('summary');
      summary.innerHTML = '<span>判断依据</span><em></em>';
      summary.querySelector('em').textContent = `${result.strategy.label} · ${result.quality.grade}`;
      reasoning.appendChild(summary);
      const body = document.createElement('div');
      body.className = 'lpra-reasoning-body';
      rows.forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'lpra-detail-row';
        row.innerHTML = `<span>${label}</span><b></b>`;
        row.querySelector('b').textContent = value;
        body.appendChild(row);
      });
      reasoning.appendChild(body);
      detail.appendChild(reasoning);
    }

    saveLatestDraft({
      draft: result.draft,
      strategyKey: result.strategyKey,
      score: result.quality.score,
      grade: result.quality.grade,
      candidateName: normalizeName(context.contact.name),
      candidateTitle: context.contact.title,
      latestMessage: result.latestMessage,
      project: result.project
    });
    status('已生成，发送前请快速看一眼');
  }

  async function generate() {
    const context = readPageContext();
    if (!hasDynamicProjectOptions()) {
      await hydrateProjectOptionsFromWorkbench(document, { silent: true }).catch(() => null);
    }
    await resolveProjectFromWorkbench({
      contact: context.contact
    }, { refresh: false });
    const result = buildDraft(context);
    renderResult(result, context);
  }

  function togglePanel(event) {
    if (state.collapsed) {
      if (event?.shiftKey) {
        state.collapsed = false;
        const root = document.getElementById(ROOT_ID);
        if (root) root.dataset.collapsed = 'false';
        renderBridgeStatusDot();
        return;
      }
      openAsaFloating();
      renderBridgeStatusDot();
      return;
    }
    state.collapsed = !state.collapsed;
    const root = document.getElementById(ROOT_ID);
    const toggle = document.getElementById(TOGGLE_ID);
    if (!root || !toggle) return;
    root.dataset.collapsed = String(state.collapsed);
    toggle.textContent = state.collapsed ? (isLiepinResumeDetailPage() ? '匹配' : '话术') : '收起';
    renderBridgeStatusDot();
  }

  function createButton(text, className, onClick, title) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || '';
    btn.textContent = text;
    if (title) btn.title = title;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function createActionRow(container, className) {
    const row = document.createElement('div');
    row.className = `lpra-action-row ${className || ''}`.trim();
    container.appendChild(row);
    return row;
  }

  function createPanel() {
    const resumeMode = isLiepinResumeDetailPage();
    if ((!isLiepinImPage() && !resumeMode) || document.getElementById(ROOT_ID)) return;

    const root = document.createElement('div');
    root.id = ROOT_ID;
    root.dataset.collapsed = String(state.collapsed);
    root.dataset.surface = resumeMode ? 'resume' : 'reply';
    root.innerHTML = `
      <div class="lpra-card" role="region" aria-label="${resumeMode ? '猎聘人岗匹配助手' : '猎聘专业回复助手'}">
        <div class="lpra-head">
          <div>
            <div class="lpra-title">${resumeMode ? '猎聘人岗匹配助手' : '猎聘专业回复助手'} v${EXTENSION_VERSION}</div>
            <div class="lpra-subtitle">${resumeMode ? '打开简历即看匹配度、匹配点和风险点' : '按历史话术生成草稿，不自动发送'}</div>
          </div>
          <div class="lpra-head-actions"></div>
        </div>
        <div class="lpra-body">
          <div class="lpra-project-picker">
            <label for="liepin-reply-project-select">${resumeMode ? '推荐岗位' : '岗位'}</label>
            <select id="liepin-reply-project-select"></select>
            <div class="lpra-project-grid">
              <input id="liepin-reply-project-client" type="text" placeholder="客户，可空">
              <input id="liepin-reply-project-position" type="text" placeholder="岗位">
            </div>
          </div>
          <div class="lpra-toolbar"></div>
          <div class="lpra-meta-line">
            <span id="liepin-reply-assistant-score" data-grade="先补信息">${resumeMode ? '待识别' : '待生成'}</span>
            <span id="liepin-reply-assistant-status">就绪</span>
          </div>
          <div id="liepin-reply-assistant-meta" class="lpra-meta">${resumeMode ? '正在读取当前简历。' : '选择一位人选后点击“生成回复”。'}</div>
          ${resumeMode ? '<div id="liepin-reply-talent-progress" class="lpra-talent-progress" data-state="idle"><span>库状态待检查</span></div>' : ''}
          ${resumeMode ? '' : '<textarea id="liepin-reply-assistant-draft" spellcheck="false" placeholder="这里会出现可复制或填入的回复草稿"></textarea>'}
          <div class="lpra-actions"></div>
          <div id="liepin-reply-assistant-detail" class="lpra-detail"></div>
        </div>
      </div>
    `;

    const toggle = document.createElement('button');
    toggle.id = TOGGLE_ID;
    toggle.type = 'button';
    toggle.textContent = state.collapsed ? 'ASA' : '收起';
    toggle.title = '打开 ASA 浮窗';
    toggle.addEventListener('click', togglePanel);

    root.querySelector('.lpra-head-actions').appendChild(toggle);
    renderBridgeStatusDot();
    const projectSelect = root.querySelector('#liepin-reply-project-select');
    state.projectOptions = mergeProjectOptions();
    renderProjectOptions(projectSelect);
    if (resumeMode) {
      projectSelect.value = 'auto';
      root.querySelector('#liepin-reply-project-client').value = '';
      root.querySelector('#liepin-reply-project-position').value = '';
      state.selectedProject = null;
      state.projectUserTouched = false;
      state.projectResolvedFrom = '';
    }
    updateProjectPickerState(projectSelect);
    projectSelect.addEventListener('change', () => {
      state.projectUserTouched = true;
      state.projectResolvedFrom = '手动选择';
      state.lastTalentDryRun = null;
      clearTalentLookupMatch();
      state.lastAutoReviewSignature = '';
      updateCandidateIntakeButtonFromLookup(null);
      syncProjectInputsFromSelect();
      if (resumeMode) refreshMatchPanel();
      else generate();
    });
    root.querySelector('#liepin-reply-project-client').addEventListener('input', () => {
      state.projectUserTouched = true;
      state.projectResolvedFrom = '手动输入';
      state.lastTalentDryRun = null;
      clearTalentLookupMatch();
      state.lastAutoReviewSignature = '';
      updateCandidateIntakeButtonFromLookup(null);
      if (projectSelect.value !== 'custom' && clean(root.querySelector('#liepin-reply-project-client').value)) {
        projectSelect.value = 'custom';
      }
      updateProjectPickerState(projectSelect);
      state.selectedProject = readManualProject();
      if (!resumeMode) {
        persistProjectChoice({
          key: projectSelect.value,
          client: clean(root.querySelector('#liepin-reply-project-client').value),
          position: clean(root.querySelector('#liepin-reply-project-position').value)
        });
      }
    });
    root.querySelector('#liepin-reply-project-position').addEventListener('input', () => {
      state.projectUserTouched = true;
      state.projectResolvedFrom = '手动输入';
      state.lastTalentDryRun = null;
      clearTalentLookupMatch();
      state.lastAutoReviewSignature = '';
      updateCandidateIntakeButtonFromLookup(null);
      if (projectSelect.value !== 'custom' && clean(root.querySelector('#liepin-reply-project-position').value)) {
        projectSelect.value = 'custom';
      }
      updateProjectPickerState(projectSelect);
      state.selectedProject = readManualProject();
      if (!resumeMode) {
        persistProjectChoice({
          key: projectSelect.value,
          client: clean(root.querySelector('#liepin-reply-project-client').value),
          position: clean(root.querySelector('#liepin-reply-project-position').value)
        });
      }
    });
    if (resumeMode) {
      root.querySelector('.lpra-toolbar')?.remove();
      const actions = root.querySelector('.lpra-actions');
      actions.classList.add('lpra-resume-actions');
      const reviewContinueButton = createButton('确认推进', 'lpra-good', () => confirmResumeReview('continue'), '触达后复核通过：默认确认触达已核验，并继续推进');
      reviewContinueButton.id = 'liepin-reply-review-continue';
      const reviewStopButton = createButton('确认停止', 'lpra-warn', () => confirmResumeReview('stop'), '触达后复核不通过：默认确认触达已核验，并停止推进');
      reviewStopButton.id = 'liepin-reply-review-stop';
      const intakeButton = createButton('确认入库', 'lpra-accept', confirmCandidateIntake, '当前人选还未唯一定位时，先加入统一人才库');
      intakeButton.id = 'liepin-reply-candidate-intake';
      actions.appendChild(reviewContinueButton);
      actions.appendChild(reviewStopButton);
      actions.appendChild(intakeButton);
      actions.appendChild(createButton('推荐文案', 'lpra-primary lpra-recommendation-action', () => switchResumeView('recommendation'), '显示可复制给客户或候选人的文案'));
      const moreActions = document.createElement('details');
      moreActions.className = 'lpra-more-actions lpra-resume-more-actions';
      moreActions.innerHTML = '<summary>更多操作</summary>';
      const moreActionsBody = document.createElement('div');
      moreActionsBody.className = 'lpra-more-actions-body lpra-resume-more-actions-body';
      moreActions.appendChild(moreActionsBody);
      actions.appendChild(moreActions);
      moreActionsBody.appendChild(createButton('刷新匹配', 'lpra-primary', refreshMatchPanel, '重新读取当前简历并计算匹配度'));
      moreActionsBody.appendChild(createButton('发现同一人', 'lpra-primary', discoverSameCandidate, '查找其他来源的疑似同一人，人工对比后合并档案'));
      moreActionsBody.appendChild(createButton('复制当前', 'lpra-good', async () => {
        const copy = state.currentRecommendation || getRecommendationCopy();
        const text = state.resumeDetailView === 'recommendation'
          ? clean(copy?.[state.currentRecommendationMode] || '')
          : clean(state.currentDraft || '');
        if (!text) {
          status('还没有可复制内容');
          return;
        }
        await copyText(text);
        status(state.resumeDetailView === 'recommendation' ? '已复制当前推荐文案' : '已复制当前匹配摘要');
      }, '复制当前视图里的内容'));
      updateResumeReviewButtonsFromLookup(null);
    } else {
      root.querySelector('.lpra-toolbar').appendChild(createButton('生成回复', 'lpra-primary', generate, '根据当前会话生成专业回复'));
      const actions = root.querySelector('.lpra-actions');
      actions.classList.add('lpra-message-actions');
      const draftActions = createActionRow(actions, 'lpra-draft-actions');
      draftActions.appendChild(createButton('复制', 'lpra-good', async () => {
        const textarea = document.querySelector('#liepin-reply-assistant-draft');
        const text = clean(textarea?.value || state.currentDraft);
        if (!text) {
          status('还没有草稿');
          return;
        }
        await copyText(text);
        status('已复制到剪贴板');
      }, '复制草稿'));
      draftActions.appendChild(createButton('填入输入框', 'lpra-warn', () => {
        const textarea = document.querySelector('#liepin-reply-assistant-draft');
        const text = clean(textarea?.value || state.currentDraft);
        if (!text) {
          status('还没有草稿');
          return;
        }
        const editor = findEditor();
        if (!editor) {
          status('没有找到输入框，请先点开具体会话');
          return;
        }
        setEditorValue(editor, text);
        saveOutreachEvent('reply_assistant_fill', text, {
          note: '填入猎聘输入框，仍需人工手动发送'
        });
        status('已填入输入框，需你手动发送');
      }, '填入猎聘输入框，但不自动发送'));
      const moreActions = document.createElement('details');
      moreActions.className = 'lpra-more-actions';
      moreActions.innerHTML = '<summary>同步/学习</summary>';
      const moreActionsBody = document.createElement('div');
      moreActionsBody.className = 'lpra-more-actions-body';
      moreActions.appendChild(moreActionsBody);
      actions.appendChild(moreActions);
      const syncLabel = document.createElement('div');
      syncLabel.className = 'lpra-more-section-title';
      syncLabel.textContent = 'A 系统同步';
      moreActionsBody.appendChild(syncLabel);
      const syncActions = createActionRow(moreActionsBody, 'lpra-sync-actions');
      syncActions.appendChild(createButton('刷新识别', '', generate, '重新读取当前人选和消息'));
      syncActions.appendChild(createButton('预检已发', '', () => previewCandidateMessage('sent'), '读取猎聘明确的我方出站消息节点并预检，不写库'));
      syncActions.appendChild(createButton('核验已发', 'lpra-good', () => confirmCandidateMessage('sent'), '只有检测到猎聘明确的我方出站消息后才可同步'));
      syncActions.appendChild(createButton('确认已回复', 'lpra-accept', recordCandidateReplyFromChat, '在同一弹层核对原文、预检意图并原子同步回复、任务和阶段'));
      syncActions.appendChild(createButton('停止推进', 'lpra-warn', confirmReplyAssistantStop, '确认当前会话人选不再推进，并同步到 H5 初筛不通过'));
      const learningLabel = document.createElement('div');
      learningLabel.className = 'lpra-more-section-title';
      learningLabel.textContent = '话术学习';
      moreActionsBody.appendChild(learningLabel);
      const learningActions = createActionRow(moreActionsBody, 'lpra-learning-actions');
      learningActions.appendChild(createButton('采纳修改', 'lpra-accept', saveAcceptedSample, '把你修改后的话术记为学习样本'));
      learningActions.appendChild(createButton('复制样本', '', copyAcceptedSamples, '复制已采纳的话术样本，便于后续升级规则'));
      learningActions.appendChild(createButton('清空', '', () => {
        const textarea = document.querySelector('#liepin-reply-assistant-draft');
        if (textarea) textarea.value = '';
        state.currentDraft = '';
        status('已清空');
      }, '清空草稿'));
    }

    document.body.appendChild(root);
    hydrateProjectOptionsFromWorkbench(root).then(() => {
      if (resumeMode) restoreProjectChoiceInto(root);
      else hydrateProjectChoice();
    });

    if (resumeMode) {
      let resumeRefreshAttempts = 0;
      const scheduleResumeRefresh = delay => setTimeout(async () => {
        try {
          if (shouldSkipInitialResumeRefresh()) return;
          resumeRefreshAttempts += 1;
          await refreshMatchPanel();
          if (!hasReadableResumeContext(readResumeContext()) && resumeRefreshAttempts < 2) {
            scheduleResumeRefresh(1800);
          }
        } catch (_) {
          if (resumeRefreshAttempts < 2) scheduleResumeRefresh(1800);
          else status('等待简历加载');
        }
      }, delay);
      scheduleResumeRefresh(1000);
    } else {
      setTimeout(() => {
        try {
          generate();
        } catch (_) {
          status('等待选择会话');
        }
      }, 800);
    }
  }

  function boot() {
    if (!isLiepinImPage() && !isLiepinResumeDetailPage()) return;
    const hadPanel = !!document.getElementById(ROOT_ID);
    createPanel();
    if (isLiepinResumeDetailPage() && hadPanel && document.getElementById(ROOT_ID) && state.lastMatchedUrl !== location.href) {
      setTimeout(refreshMatchPanel, 300);
    }
  }

  let lastUrl = location.href;
  const observer = new MutationObserver(() => {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      state.lockedResumeKey = '';
      state.lockedResumeIdentity = null;
      state.projectUserTouched = false;
      state.projectResolvedFrom = '';
      state.lastProjectLookupSignature = '';
      state.lastTalentDryRun = null;
      clearTalentLookupMatch();
      state.recentOutreachProject = null;
      updateCandidateIntakeButtonFromLookup(null);
      setTimeout(boot, 500);
    }
  });

  if (document.body) {
    boot();
    observer.observe(document.body, {
      childList: true,
      subtree: true
    });
    document.addEventListener('pointerdown', reportFloatingUserActivity, { passive: true });
    window.addEventListener('scroll', reportFloatingUserActivity, { passive: true });
    window.addEventListener('focus', reportFloatingUserActivity);
  } else {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
    document.addEventListener('DOMContentLoaded', () => {
      document.addEventListener('pointerdown', reportFloatingUserActivity, { passive: true });
      window.addEventListener('scroll', reportFloatingUserActivity, { passive: true });
      window.addEventListener('focus', reportFloatingUserActivity);
    }, { once: true });
  }

  setInterval(() => {
    if (!document.getElementById(ROOT_ID)) boot();
    else if (isLiepinResumeDetailPage()) ensureDynamicProjectOptions();
    reportFloatingContext();
  }, 3000);

  setInterval(() => {
    pollFloatingCommands();
  }, 1800);
})();
