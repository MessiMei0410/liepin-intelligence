(() => {
  'use strict';

  const ROOT_ID = 'xsaas-candidate-assistant-root';
  const WORKBENCH_BASE = 'http://127.0.0.1:8765';
  const FLOATING_URL = `${WORKBENCH_BASE}/asa-floating`;
  const PROJECT_STORE_KEY = 'xsaasCandidateAssistantProject';
  const EXTENSION_VERSION = chrome.runtime?.getManifest?.().version || 'unknown';
  const STOP_REASON_OPTIONS = [
    { key: 'too_senior', label: '太资深' },
    { key: 'salary_mismatch', label: '薪资太贵' },
    { key: 'direction_mismatch', label: '方向不符' },
    { key: 'experience_mismatch', label: '经验不符' },
    { key: 'location_mismatch', label: '地点不符' },
    { key: 'low_intent', label: '意愿低' },
    { key: 'duplicate_candidate', label: '重复人选' },
    { key: 'other', label: '其他' }
  ];

  if (window.__xsaasCandidateAssistantLoaded) return;
  window.__xsaasCandidateAssistantLoaded = true;

  const state = {
    collapsed: true,
    projectOptions: [],
    selectedProject: null,
    latestCandidate: null,
    latestLookup: null,
    lookupSeq: 0,
    lookupTimer: null,
    lastStatus: '就绪',
    statusKind: '',
    extractionEvidence: {},
    lockedCandidateKey: '',
    lockedCandidate: null,
    floatingUserSelectedUntil: 0,
    bridgeInstanceId: `xsaas_${Math.random().toString(16).slice(2)}_${Date.now()}`,
    bridgeStatus: '未连接 ASA'
  };
  let lastSurfaceSignature = '';

  function clean(value) {
    return String(value ?? '').replace(/\s+/g, ' ').trim();
  }

  function candidatePageKey(candidate = null) {
    const id = clean(candidate?.xsaas_id || extractXsaasId());
    return id ? `xsaas:${id}` : `url:${location.href}`;
  }

  function isUsableCandidateIdentity(candidate) {
    return Boolean(candidate?.xsaas_id && !isBadName(candidate?.candidate));
  }

  function stabilizeCandidateIdentity(candidate) {
    const key = candidatePageKey(candidate);
    if (state.lockedCandidateKey && state.lockedCandidateKey !== key) {
      state.lockedCandidateKey = '';
      state.lockedCandidate = null;
      state.latestLookup = null;
    }
    if (!state.lockedCandidate && isUsableCandidateIdentity(candidate)) {
      state.lockedCandidateKey = key;
      state.lockedCandidate = { ...candidate };
      return candidate;
    }
    if (!state.lockedCandidate || state.lockedCandidateKey !== key) return candidate;
    const locked = state.lockedCandidate;
    const stable = {
      ...candidate,
      candidate: locked.candidate || candidate.candidate,
      company: locked.company || candidate.company,
      title: locked.title || candidate.title,
      xsaas_id: locked.xsaas_id || candidate.xsaas_id,
      source_candidate_id: locked.source_candidate_id || candidate.source_candidate_id,
      source_url: locked.source_url || candidate.source_url
    };
    ['company', 'title', 'city', 'education', 'years'].forEach(keyName => {
      if (!locked[keyName] && stable[keyName]) locked[keyName] = stable[keyName];
    });
    if (!locked.candidate_profile_text && stable.candidate_profile_text) locked.candidate_profile_text = stable.candidate_profile_text;
    return stable;
  }

  function candidateRoot() {
    return document.querySelector('.app-candidate-info') || document.body || document.documentElement;
  }

  function normalizeLabel(value) {
    return clean(value).replace(/[：:\s]/g, '');
  }

  function safeField(value) {
    return clean(value).replace(/^[：:\s|｜-]+/, '').replace(/[：:\s|｜-]+$/, '');
  }

  function isBadName(value) {
    const text = clean(value);
    return !text || ['候选人', '人才', '人选', '姓名'].includes(text) || /^\d+$/.test(text) || text.length > 16;
  }

  function isBadCompany(value) {
    const text = clean(value);
    return !text || ['候选人', '公司', '当前公司'].includes(text) || /^\d+$/.test(text) || text.length < 2;
  }

  function isBadTitle(value) {
    const text = clean(value);
    return !text || ['候选人', '职位', '岗位', '当前职位'].includes(text) || /^\d+$/.test(text) || text.length < 2;
  }

  function evidence(value, source, confidence = 'high', raw = '') {
    return {
      value: clean(value),
      source,
      confidence,
      raw: clean(raw || value)
    };
  }

  function postToWorkbench(path, payload) {
    return new Promise(resolve => {
      chrome.runtime.sendMessage(
        { type: 'asa-workbench-request', method: 'POST', path, payload },
        response => {
          if (chrome.runtime.lastError || !response) {
            resolve({ ok: false, error: chrome.runtime.lastError?.message || '扩展后台未响应', transport_error: 'extension' });
            return;
          }
          resolve(response.ok ? (response.body || { ok: true }) : response);
        }
      );
    });
  }

  function getFromWorkbench(path) {
    return new Promise(resolve => {
      chrome.runtime.sendMessage(
        { type: 'asa-workbench-request', method: 'GET', path },
        response => resolve(chrome.runtime.lastError || !response || !response.ok ? null : response.body)
      );
    });
  }

  function openAsaFloating() {
    window.open(FLOATING_URL, '_blank', 'noopener,noreferrer');
  }

  function renderBridgeStatusDot() {
    const toggle = document.querySelector('#xsa-toggle');
    if (!toggle) return;
    toggle.textContent = state.collapsed ? 'ASA' : '收起';
    toggle.title = `${state.bridgeStatus || 'ASA 桥接'} · v${EXTENSION_VERSION}`;
    toggle.dataset.bridge = /已连接/.test(state.bridgeStatus || '') ? 'connected' : 'disconnected';
  }

  async function reportFloatingContext(userSelected = false) {
    if (!isCandidateInfoPage()) return;
    if (document.visibilityState && document.visibilityState !== 'visible') return;
    const activeUserSelected = Boolean(userSelected || Date.now() < state.floatingUserSelectedUntil);
    const candidate = state.latestCandidate || extractCandidate();
    const payload = readFormPayload('xsaas_bridge_context', {
      surface: 'xsaas',
      instance_id: state.bridgeInstanceId,
      plugin: 'xsaas-candidate-assistant',
      version: EXTENSION_VERSION,
      url: location.href,
      title: document.title,
      page_type: 'candidate_detail',
      page_visible: document.visibilityState === 'visible',
      page_focused: document.hasFocus(),
      user_selected: activeUserSelected,
      candidate: {
        name: candidate.candidate || '',
        company: candidate.company || '',
        title: candidate.title || '',
        xsaas_id: candidate.xsaas_id || '',
        city: candidate.city || '',
        education: candidate.education || '',
        years: candidate.years || '',
        profile_summary: (candidate.candidate_profile_text || '').slice(0, 1200)
      },
      candidate_name: candidate.candidate || '',
      company: candidate.company || '',
      candidate_title: candidate.title || '',
      source_url: location.href,
      status: state.lastStatus || '',
      lookup: state.latestLookup || null,
      actions: ['refresh_bridge', 'dry-intake', 'dry-continue', 'dry-stop', 'copy-current', 'identity-match']
    });
    const result = await postToWorkbench('/api/asa/floating/context', payload);
    state.bridgeStatus = result?.ok ? 'ASA 已连接' : `ASA 未连接：${result?.error || 'unknown'}`;
    renderBridgeStatusDot();
  }

  let lastFloatingUserReportAt = 0;
  function reportFloatingUserActivity() {
    const now = Date.now();
    if (now - lastFloatingUserReportAt < 1500) return;
    lastFloatingUserReportAt = now;
    state.floatingUserSelectedUntil = now + 10000;
    reportFloatingContext(true);
  }

  async function handleFloatingCommand(command) {
    const action = clean(command?.action);
    if (!action) return;
    if (action === 'open_floating') {
      openAsaFloating();
      return;
    }
    if (action === 'refresh_bridge' || action === 'assess_current' || action === 'fill_resume') {
      fillCandidateFields(extractCandidate());
      await refreshCandidateLookup({ silent: false });
      return;
    }
    if (action === 'dry-intake') return intake(false);
    if (action === 'dry-continue') return review('continue', false);
    if (action === 'dry-stop') return review('stop', false);
    if (action === 'copy-current' || action === 'open_source') return copyCurrentPanel();
    if (action === 'identity-match') return discoverSameCandidate();
    status(`ASA 命令已收到：${action}`, '');
  }

  async function pollFloatingCommands() {
    const result = await getFromWorkbench('/api/asa/floating/commands?surface=xsaas');
    const commands = Array.isArray(result?.commands) ? result.commands : [];
    for (const command of commands) {
      await handleFloatingCommand(command);
    }
  }

  function status(text, kind = '') {
    state.lastStatus = text;
    state.statusKind = kind;
    document.querySelectorAll(`#${ROOT_ID} .xsa-status, #xsa-assistant-status`).forEach(el => {
      el.textContent = text;
      el.dataset.kind = kind;
    });
  }

  async function copyText(text) {
    const value = clean(text);
    if (!value) return false;
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
    const textarea = document.createElement('textarea');
    textarea.value = value;
    textarea.setAttribute('readonly', 'readonly');
    textarea.style.position = 'fixed';
    textarea.style.left = '-9999px';
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand('copy');
    textarea.remove();
    return ok;
  }

  function actionConfirmText(actionName, payload) {
    const lookup = state.latestLookup || {};
    const matchedCandidate = lookup.candidate || {};
    const originalProject = [matchedCandidate.client, matchedCandidate.position].filter(Boolean).join(' / ');
    const lines = [
      `${actionName}到 A 系统？`,
      '',
      `人选：${payload.candidate || '未填'}`,
      `公司/职位：${payload.company || '公司待补'} / ${payload.title || '职位待补'}`,
      `客户/岗位：${payload.client || '未填'} / ${payload.job || '未填'}`,
      `X-SaaS ID：${payload.xsaas_id || '未识别'}`
    ];
    if (actionName.includes('入库') && lookup.candidate_matched && !lookup.progress_matched) {
      lines.push('');
      lines.push(`已有库内人才：人才ID ${matchedCandidate.id || '-'}${originalProject ? `，原关联 ${originalProject}` : ''}`);
      lines.push('本次会关联到当前客户/岗位。');
    }
    if (payload.review_result === 'stop') {
      lines.push(`停止原因：${payload.stop_reason_label || payload.stop_reason_code || '未填'}`);
      if (payload.stop_reason_note) lines.push(`备注：${payload.stop_reason_note}`);
    }
    return lines.join('\n');
  }

  function closeActionConfirmLayer(value = null) {
    const layer = document.querySelector(`#${ROOT_ID} .xsa-confirm-layer`);
    if (!layer) return;
    const resolver = layer.__resolveActionConfirm;
    layer.remove();
    if (typeof resolver === 'function') resolver(value);
  }

  function actionWriteLabel(actionName, payload = {}) {
    if (payload.kind === 'xsaas_intake') {
      const lookup = state.latestLookup || {};
      if (lookup.candidate_matched && !lookup.progress_matched) return '复用库内人才，并关联到当前客户/岗位';
      return '新增/复用 X-SaaS 人才入库记录，并生成当前岗位推进关系';
    }
    if (payload.kind === 'xsaas_review' && payload.review_result === 'stop') return '记录复核停止，并更新为 H5 初筛不通过';
    if (payload.kind === 'xsaas_review') return '记录复核通过，进入 X2 待人工联系';
    return actionName;
  }

  function talentSyncSummary(result) {
    return ((result?.sync || {}).result || result?.sync || {}).summary || {};
  }

  function canWriteTalentAction(result) {
    const summary = talentSyncSummary(result);
    return !!result?.ok && Number(summary.would_write || 0) > 0 && Number(summary.pending_review || 0) === 0;
  }

  function preflightText(result) {
    const summary = talentSyncSummary(result);
    if (!result) return '';
    if (Number(summary.would_write || 0) > 0) return `预检通过：将写入 ${summary.would_write} 条`;
    if (Number(summary.pending_review || 0) > 0) return '预检未通过：未唯一定位，请先入库或校正字段';
    if (Number(summary.already_exists || 0) > 0) return '预检结果：已存在相同记录，无需重复写入';
    if (!result.ok) return `预检失败：${clean(result.stderr || result.error || result.body?.stderr || '未知错误')}`;
    return '预检完成';
  }

  function selectedStopReasonFromLayer(layer) {
    const checked = layer.querySelector('input[name="xsa-stop-confirm-reason"]:checked');
    const key = checked?.value || STOP_REASON_OPTIONS[0].key;
    const option = STOP_REASON_OPTIONS.find(item => item.key === key) || STOP_REASON_OPTIONS[0];
    const note = clean(layer.querySelector('#xsa-stop-confirm-note')?.value || '');
    return {
      stop_reason_code: option.key,
      stop_reason_label: option.label,
      stop_reason_note: note
    };
  }

  function confirmAction(actionName, payload = {}) {
    return new Promise(resolve => {
      closeActionConfirmLayer(null);
      const root = document.querySelector(`#${ROOT_ID}`);
      if (!root) {
        resolve(window.confirm(actionConfirmText(actionName, payload)) ? {} : null);
        return;
      }

      const lookup = state.latestLookup || {};
      const matchedCandidate = lookup.candidate || {};
      const originalProject = [matchedCandidate.client, matchedCandidate.position].filter(Boolean).join(' / ');
      const isStop = payload.kind === 'xsaas_review' && payload.review_result === 'stop';
      const isLibraryOnly = actionName.includes('入库') && lookup.candidate_matched && !lookup.progress_matched;
      const preflight = clean(payload._preflight_text || '');
      const reasonOptions = STOP_REASON_OPTIONS.map((item, index) => {
        const checked = payload.stop_reason_code
          ? item.key === payload.stop_reason_code
          : index === 0;
        return `
          <label class="xsa-confirm-reason">
            <input type="radio" name="xsa-stop-confirm-reason" value="${escapeHtml(item.key)}" ${checked ? 'checked' : ''}>
            <span>${escapeHtml(item.label)}</span>
          </label>
        `;
      }).join('');
      const layer = document.createElement('div');
      layer.className = 'xsa-confirm-layer';
      layer.__resolveActionConfirm = resolve;
      layer.innerHTML = `
        <div class="xsa-confirm-panel" role="dialog" aria-modal="true" aria-label="${escapeHtml(actionName)}">
          <div class="xsa-confirm-head">
            <strong>${escapeHtml(actionName)}</strong>
            <button type="button" class="xsa-confirm-close" aria-label="取消">x</button>
          </div>
          <div class="xsa-confirm-summary">
            <div><span>人选</span><b>${escapeHtml(payload.candidate || '未识别')}</b></div>
            <div><span>公司/职位</span><b>${escapeHtml(payload.company || '公司待补')} / ${escapeHtml(payload.title || '职位待补')}</b></div>
            <div><span>客户/岗位</span><b>${escapeHtml(payload.client || '未选客户')} / ${escapeHtml(payload.job || '未选岗位')}</b></div>
            <div><span>X-SaaS ID</span><b>${escapeHtml(payload.xsaas_id || '未识别')}</b></div>
            <div><span>将写入动作</span><b>${escapeHtml(actionWriteLabel(actionName, payload))}</b></div>
          </div>
          ${isLibraryOnly ? `
            <div class="xsa-confirm-guard">
              <strong>人才库已有该人</strong>
              <p>人才ID ${escapeHtml(matchedCandidate.id || '-')} ${originalProject ? `，原关联 ${escapeHtml(originalProject)}` : ''}。本次会关联到当前客户/岗位。</p>
            </div>
          ` : ''}
          ${preflight ? `
            <div class="xsa-confirm-guard">
              <strong>写入预检</strong>
              <p>${escapeHtml(preflight)}</p>
            </div>
          ` : ''}
          ${isStop ? `
            <div class="xsa-confirm-stop">
              <span>停止原因</span>
              <div class="xsa-confirm-reasons">${reasonOptions}</div>
              <input id="xsa-stop-confirm-note" type="text" value="${escapeHtml(payload.stop_reason_note || '')}" placeholder="其他补充，可不填">
            </div>
          ` : ''}
          <div class="xsa-confirm-actions">
            <button type="button" class="xsa-confirm-cancel">取消</button>
            <button type="button" class="xsa-confirm-submit ${isStop ? 'xsa-warn' : 'xsa-primary'}">确认写入</button>
          </div>
        </div>
      `;
      root.appendChild(layer);
      layer.querySelector('.xsa-confirm-close')?.addEventListener('click', () => closeActionConfirmLayer(null));
      layer.querySelector('.xsa-confirm-cancel')?.addEventListener('click', () => closeActionConfirmLayer(null));
      layer.addEventListener('click', event => {
        if (event.target === layer) closeActionConfirmLayer(null);
      });
      layer.querySelector('.xsa-confirm-submit')?.addEventListener('click', () => {
        closeActionConfirmLayer(isStop ? selectedStopReasonFromLayer(layer) : {});
      });
      layer.querySelector('.xsa-confirm-submit')?.focus();
    });
  }

  function extractXsaasId() {
    const href = location.href;
    const patterns = [
      /\/candidate\/info\/([0-9]+)/,
      /candidateId=([0-9]+)/,
      /candidate_id=([0-9]+)/,
      /\bid=([0-9]{4,})\b/
    ];
    for (const pattern of patterns) {
      const m = href.match(pattern);
      if (m) return m[1];
    }
    const header = extractHeaderNameId();
    if (header.xsaas_id) return header.xsaas_id;
    const text = candidateRoot()?.innerText || '';
    const fromText = text.match(/(?:候选人ID|人才ID|ID)[:：\s]*([0-9]{4,})/);
    return fromText ? fromText[1] : '';
  }

  function extractHeaderNameId() {
    const heading = document.querySelector('.app-candidate-info .name-info h3, .name-info h3');
    const text = clean(heading?.textContent || '');
    const titleName = safeField((document.title || '').split('|')[0] || '');
    if (!text) return { candidate: isBadName(titleName) ? '' : titleName, xsaas_id: '' };
    const id = (text.match(/\b([0-9]{4,})\b/) || [])[1] || '';
    const tokens = text
      .replace(id, '')
      .replace(/^ID查看\s*/, '')
      .replace(/[|｜].*$/, '')
      .split(/\s+/)
      .map(safeField)
      .filter(Boolean);
    const tokenName = tokens.find(item => /^[\u4e00-\u9fa5]{2,4}$/.test(item) && !/(先生|女士)$/.test(item)) || '';
    const candidate = tokenName || (isBadName(titleName) ? '' : titleName) || safeField(text.replace(id, '').replace(/[|｜].*$/, ''));
    return {
      candidate: isBadName(candidate) ? '' : candidate,
      xsaas_id: id
    };
  }

  function extractHeaderCompanyTitle() {
    const info = document.querySelector('.app-candidate-info .contact-info, .contact-info');
    const lines = String(info?.innerText || info?.textContent || '')
      .split(/\n+/)
      .map(line => clean(line))
      .filter(Boolean);
    const primary = lines.find(line => /[|｜]/.test(line) && !/@|电话|手机|邮箱|微信/.test(line)) || lines[0] || '';
    const parts = primary.split(/\s*[|｜]\s*/).map(safeField).filter(Boolean);
    return {
      company: isBadCompany(parts[0]) ? '' : (parts[0] || ''),
      title: isBadTitle(parts[1]) ? '' : (parts[1] || ''),
      years: /\d/.test(parts[2] || '') ? parts[2] : ''
    };
  }

  function extractVitaeValue(labels) {
    const scope = document.querySelector('#vitae');
    if (!scope) return '';
    const normalizedLabels = labels.map(normalizeLabel);
    const nodes = Array.from(scope.querySelectorAll('.group-name, td, th, label, span, div'));
    for (const node of nodes) {
      const own = clean(node.textContent || '');
      const normalized = normalizeLabel(own);
      const matched = normalizedLabels.find(label => normalized === label || normalized.startsWith(label));
      if (!matched) continue;
      const inlineValue = safeField(own.replace(new RegExp(`^${matched}[：:\\s]*`), ''));
      if (inlineValue && normalizeLabel(inlineValue) !== matched) return inlineValue;
      const next = node.nextElementSibling || node.parentElement?.querySelector('.group-value, .value, td:nth-child(2)');
      const value = safeField(next?.textContent || '');
      if (value && normalizeLabel(value) !== matched) return value;
    }
    const text = scope.innerText || '';
    for (const label of labels) {
      const pattern = new RegExp(`${label}[:：\\s]*([^\\n]{1,80})`);
      const m = text.match(pattern);
      if (m) return safeField(m[1]);
    }
    return '';
  }

  function extractByLabel(labels) {
    const text = candidateRoot()?.innerText || '';
    const lines = text.split('\n').map(line => clean(line)).filter(Boolean);
    for (const line of lines) {
      for (const label of labels) {
        const pattern = new RegExp(`^${label}[:：\\\\s]+(.+)$`);
        const m = line.match(pattern);
        if (m) return clean(m[1]);
      }
    }
    const nextLabel = '(?:候选人ID|人才ID|当前公司|任职公司|当前职位|职位|学历|城市|所在地|地区|工作年限|年龄)';
    for (const label of labels) {
      const pattern = new RegExp(`${label}[:：\\\\s]+([\\\\s\\\\S]{1,120}?)(?=\\\\s*${nextLabel}[:：]|$)`);
      const m = text.match(pattern);
      if (m) return clean(m[1]);
    }
    return '';
  }

  function textLines() {
    return (candidateRoot()?.innerText || document.body?.innerText || '')
      .split('\n')
      .map(line => clean(line))
      .filter(Boolean)
      .slice(0, 160);
  }

  function guessName(lines) {
    const vitaeName = extractVitaeValue(['中文姓名', '姓名', '人才姓名']);
    if (!isBadName(vitaeName)) return evidence(vitaeName, '简历字段：姓名');
    const header = extractHeaderNameId();
    if (header.candidate) return evidence(header.candidate, '页头姓名', 'high', document.querySelector('.app-candidate-info .name-info h3, .name-info h3')?.textContent || '');
    const labeled = extractByLabel(['中文姓名', '姓名', '人才姓名']);
    if (!isBadName(labeled)) return evidence(labeled.replace(/候选人电话.*$/, '').trim(), '页面标签：姓名', 'medium', labeled);
    for (const line of lines.slice(0, 30)) {
      if (/^[\u4e00-\u9fa5]{2,4}(?:\s*[\u4e00-\u9fa5]*先生|\s*[\u4e00-\u9fa5]*女士)?$/.test(line)) return evidence(line, '页面前30行推断', 'low');
      if (/^[\u4e00-\u9fa5]{1,3}(先生|女士)$/.test(line)) return evidence(line, '页面前30行推断', 'low');
    }
    return evidence('', '未识别', 'missing');
  }

  function splitCompanyTitle(lines) {
    const header = extractHeaderCompanyTitle();
    if (header.company || header.title) {
      return {
        company: evidence(header.company, '页头公司', header.company ? 'high' : 'missing'),
        title: evidence(header.title, '页头职位', header.title ? 'high' : 'missing'),
        years: evidence(header.years, '页头年限', header.years ? 'medium' : 'missing')
      };
    }
    const company = extractByLabel(['当前公司', '任职公司']);
    const title = extractByLabel(['当前职位']);
    if (company || title) {
      return {
        company: evidence(company, '页面标签：当前公司', company ? 'medium' : 'missing'),
        title: evidence(title, '页面标签：当前职位', title ? 'medium' : 'missing')
      };
    }
    for (let i = 0; i < Math.min(lines.length, 80); i += 1) {
      const line = lines[i];
      if (line.includes(' /')) {
        const parts = line.split(/\s*\/\s*/);
        if (parts[0] || parts[1]) {
          const companyValue = clean(parts[0]);
          const titleValue = clean(parts[1]);
          return {
            company: evidence(companyValue, '页面斜杠行推断', isBadCompany(companyValue) ? 'low' : 'medium', line),
            title: evidence(titleValue, '页面斜杠行推断', isBadTitle(titleValue) ? 'low' : 'medium', line)
          };
        }
      }
      if (/公司|有限|半导体|科技|设备|电子|自动化/.test(line)) {
        const next = lines[i + 1] || '';
        if (/工程师|开发|软件|研发|经理|主管|负责人|Leader|C\+\+|C#/.test(next)) {
          return {
            company: evidence(line.replace(/^当前公司[:：\s]+/, ''), '相邻行推断：公司', 'medium', `${line} / ${next}`),
            title: evidence(next.replace(/^当前职位[:：\s]+/, ''), '相邻行推断：职位', 'medium', `${line} / ${next}`)
          };
        }
      }
    }
    return { company: evidence('', '未识别', 'missing'), title: evidence('', '未识别', 'missing') };
  }

  function extractCandidate() {
    const lines = textLines();
    const joined = lines.join('\n');
    const companyTitle = splitCompanyTitle(lines);
    const vitaeYears = extractVitaeValue(['工作年限', '工作经验', '年限']);
    const years = vitaeYears || '';
    const age = (joined.match(/([0-9]{2})\s*岁/) || [])[1] || '';
    const education = extractVitaeValue(['最高学历', '学历']) || (joined.match(/(博士|硕士(?:\(985\/211\))?|本科(?:\(985\/211\))?|大专)/) || [])[1] || '';
    const city = extractVitaeValue(['目前城市', '城市', '所在地', '地区']) || (joined.match(/(杭州|深圳|上海|苏州|南京|北京|广州|浙江|江苏|江西|广东)/) || [])[1] || '';
    const sourceUrl = location.href;
    const xsaasId = extractXsaasId();
    const nameEvidence = guessName(lines);
    const companyEvidence = companyTitle.company || evidence('', '未识别', 'missing');
    const titleEvidence = companyTitle.title || evidence('', '未识别', 'missing');
    const candidate = {
      candidate: isBadName(nameEvidence.value) ? '' : nameEvidence.value,
      company: isBadCompany(companyEvidence.value) ? '' : companyEvidence.value,
      title: isBadTitle(titleEvidence.value) ? '' : titleEvidence.value,
      xsaas_id: xsaasId,
      source_candidate_id: xsaasId,
      source_url: sourceUrl,
      age: age ? `${age}岁` : '',
      years: years ? (/(年|个月|以上|以下)/.test(years) ? years : `工作${years}年`) : (companyTitle.years?.value || ''),
      education,
      city,
      candidate_profile_text: joined.slice(0, 3000)
    };
    state.extractionEvidence = {
      candidate: nameEvidence,
      company: companyEvidence,
      title: titleEvidence,
      xsaas_id: evidence(xsaasId, xsaasId ? 'URL/页头ID' : '未识别', xsaasId ? 'high' : 'missing'),
      city: evidence(city, city ? '简历字段/页面文本' : '未识别', city ? 'medium' : 'missing'),
      education: evidence(education, education ? '简历字段/页面文本' : '未识别', education ? 'medium' : 'missing'),
      years: evidence(candidate.years, candidate.years ? '简历字段/页头年限' : '未识别', candidate.years ? 'medium' : 'missing')
    };
    const stableCandidate = stabilizeCandidateIdentity(candidate);
    state.latestCandidate = stableCandidate;
    return stableCandidate;
  }

  function projectValue(option) {
    return `${option.client}||${option.position || option.job || ''}`;
  }

  function parseProjectValue(value) {
    const [client, job] = String(value || '').split('||');
    return { client: clean(client), job: clean(job) };
  }

  function renderProjectOptions(select, selected = '') {
    if (!select) return;
    const previous = selected || select.value || '';
    select.innerHTML = '';
    const custom = document.createElement('option');
    custom.value = '';
    custom.textContent = '手填客户/岗位';
    select.appendChild(custom);
    state.projectOptions.forEach(option => {
      const el = document.createElement('option');
      el.value = projectValue(option);
      el.textContent = `${option.client} / ${option.position || option.job}`;
      select.appendChild(el);
    });
    select.value = [...select.options].some(opt => opt.value === previous) ? previous : '';
  }

  async function hydrateProjects() {
    const result = await getFromWorkbench('/api/context');
    if (Array.isArray(result?.positions)) {
      state.projectOptions = result.positions
        .filter(item => item?.client && (item.position || item.job))
        .slice(0, 500);
      renderProjectOptions(document.querySelector('#xsa-project-select'));
      status(`岗位库已同步：${state.projectOptions.length} 个岗位`, 'ok');
      try {
        chrome.storage.local.get([PROJECT_STORE_KEY], stored => {
          const value = stored?.[PROJECT_STORE_KEY]?.value || '';
          const select = document.querySelector('#xsa-project-select');
          if (select && value) {
            select.value = value;
            syncProjectInputs();
          }
        });
      } catch (_) {}
    } else {
      status('岗位库未连接，可手填客户/岗位', 'err');
    }
  }

  function syncProjectInputs() {
    const select = document.querySelector('#xsa-project-select');
    const clientInput = document.querySelector('#xsa-client');
    const jobInput = document.querySelector('#xsa-job');
    const project = parseProjectValue(select?.value || '');
    if (project.client || project.job) {
      if (clientInput) clientInput.value = project.client;
      if (jobInput) jobInput.value = project.job;
      try {
        chrome.storage.local.set({ [PROJECT_STORE_KEY]: { value: select.value, at: new Date().toISOString() } });
      } catch (_) {}
    }
    queueCandidateLookup();
  }

  function fillCandidateFields(candidate = extractCandidate()) {
    const currentSourceId = clean(document.querySelector('#xsa-source-id')?.value);
    const sourceChanged = Boolean(candidate.xsaas_id && currentSourceId && currentSourceId !== candidate.xsaas_id);
    const fields = {
      candidate: '#xsa-candidate',
      company: '#xsa-company',
      title: '#xsa-title-input',
      xsaas_id: '#xsa-source-id',
      city: '#xsa-city',
      education: '#xsa-education',
      years: '#xsa-experience',
      candidate_profile_text: '#xsa-profile'
    };
    Object.entries(fields).forEach(([key, selector]) => {
      const el = document.querySelector(selector);
      if (el && (sourceChanged || !el.value || key === 'candidate_profile_text')) el.value = candidate[key] || '';
    });
    renderDetectedPills(candidate);
    renderCandidateMeta(candidate);
    queueCandidateLookup();
  }

  function currentSurfaceSignature() {
    const header = clean(document.querySelector('.app-candidate-info .name-info h3, .name-info h3')?.textContent || '');
    return `${location.href}||${extractXsaasId()}||${header}`;
  }

  function renderCandidateMeta(candidate = state.latestCandidate || {}) {
    const meta = document.querySelector('#xsa-assistant-meta');
    if (!meta) return;
    const name = clean(document.querySelector('#xsa-candidate')?.value) || candidate.candidate || '未识别人选';
    const company = clean(document.querySelector('#xsa-company')?.value) || candidate.company || '公司待补';
    const title = clean(document.querySelector('#xsa-title-input')?.value) || candidate.title || '职位待补';
    const tags = [
      clean(document.querySelector('#xsa-city')?.value) || candidate.city,
      clean(document.querySelector('#xsa-education')?.value) || candidate.education,
      clean(document.querySelector('#xsa-experience')?.value) || candidate.years,
    ].filter(Boolean).join(' · ');
    meta.textContent = `${name}｜${company} · ${title}${tags ? `｜${tags}` : ''}`;
  }

  function readFormPayload(kind, extra = {}) {
    const candidate = clean(document.querySelector('#xsa-candidate')?.value);
    const company = clean(document.querySelector('#xsa-company')?.value);
    const title = clean(document.querySelector('#xsa-title-input')?.value);
    const client = clean(document.querySelector('#xsa-client')?.value);
    const job = clean(document.querySelector('#xsa-job')?.value);
    const sourceId = clean(document.querySelector('#xsa-source-id')?.value) || extractXsaasId();
    const profile = clean(document.querySelector('#xsa-profile')?.value);
    return {
      kind,
      plugin_surface: 'xsaas_candidate_assistant',
      candidate,
      company,
      title,
      client,
      job,
      xsaas_id: sourceId,
      source_candidate_id: sourceId,
      source_url: location.href,
      city: clean(document.querySelector('#xsa-city')?.value),
      education: clean(document.querySelector('#xsa-education')?.value),
      experience: clean(document.querySelector('#xsa-experience')?.value),
      level: clean(document.querySelector('#xsa-level')?.value),
      fit_level: clean(document.querySelector('#xsa-level')?.value),
      candidate_profile_text: profile,
      profile_summary: profile.slice(0, 500),
      refresh_workbench: true,
      ...extra
    };
  }

  function fieldQuality(payload = readFormPayload('quality_check')) {
    const missing = [];
    if (!payload.candidate || isBadName(payload.candidate)) missing.push('姓名');
    if (!payload.company || isBadCompany(payload.company)) missing.push('公司');
    if (!payload.title || isBadTitle(payload.title)) missing.push('职位');
    if (!payload.client) missing.push('客户');
    if (!payload.job) missing.push('岗位');
    if (!payload.xsaas_id) missing.push('X-SaaS ID');
    const projectMismatch = obviousProjectMismatch(payload);
    return {
      ok: missing.length === 0 && !projectMismatch,
      missing,
      projectMismatch,
      message: missing.length
        ? `字段风险：缺 ${missing.join(' / ')}`
        : projectMismatch || '字段完整：可入库'
    };
  }

  function obviousProjectMismatch(payload = {}) {
    const title = clean(payload.title);
    const job = clean(payload.job);
    if (!title || !job) return '';
    const mechanicalTitle = /机械|结构|机械设计|机构设计/.test(title);
    const softwareTitle = /软件|C\+\+|C#|上位机|运动控制|PLC|嵌入式/.test(title);
    const mechanicalJob = /机械|结构|机构/.test(job);
    const softwareJob = /自动化软件|软件|上位机|运动控制/.test(job);
    if (mechanicalTitle && softwareJob) return '岗位方向冲突：机械人选不能写入软件岗位，请先切换推荐岗位';
    if (softwareTitle && mechanicalJob) return '岗位方向冲突：软件人选不能写入机械岗位，请先切换推荐岗位';
    return '';
  }

  function renderFieldQuality(payload = readFormPayload('quality_check')) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-field-quality`);
    if (!el) return fieldQuality(payload);
    const quality = fieldQuality(payload);
    el.dataset.kind = quality.ok ? 'ok' : 'err';
    el.innerHTML = quality.ok
      ? '<span>字段完整</span><span>可入库</span>'
      : quality.projectMismatch && !quality.missing.length
        ? '<span>岗位方向冲突</span><span>请切换岗位</span>'
        : ['字段风险', ...quality.missing.map(item => `缺${item}`)].map(item => `<span>${escapeHtml(item)}</span>`).join('');
    if (!quality.ok) {
      const next = document.querySelector(`#${ROOT_ID} .xsa-next-step`);
      if (next) {
        next.textContent = quality.projectMismatch && !quality.missing.length
          ? `下一步：${quality.projectMismatch}`
          : `下一步：先补全字段（${quality.missing.join(' / ')}）`;
        next.dataset.kind = 'err';
      }
      status(quality.message, 'err');
    }
    return quality;
  }

  function isStoppedProgress(result = state.latestLookup) {
    if (!result?.progress_matched) return false;
    const progress = result.progress || {};
    const values = [
      progress.clean_stage,
      progress.progress_stage,
      progress.latest_review_status,
      progress.latest_event_status,
      progress.latest_review_summary,
      ...(Array.isArray(result.chips) ? result.chips : [])
    ].map(clean).filter(Boolean);
    return values.some(value =>
      value === 'stop'
      || /^H5\b/.test(value)
      || value.includes('初筛不通过')
      || value.includes('复核停止')
      || value.includes('确认停止')
    );
  }

  function isContinuedProgress(result = state.latestLookup) {
    if (!result?.progress_matched || isStoppedProgress(result)) return false;
    const progress = result.progress || {};
    const values = [
      progress.clean_stage,
      progress.progress_stage,
      progress.latest_review_status,
      progress.latest_event_status,
      progress.latest_review_summary,
      ...(Array.isArray(result.chips) ? result.chips : [])
    ].map(clean).filter(Boolean);
    return values.some(value =>
      value === 'continue'
      || value.includes('复核推进')
      || value.includes('确认推进')
      || value.includes('待人工联系')
      || value.includes('已触达')
    );
  }

  function reviewStatusText(result = state.latestLookup) {
    const progress = result?.progress || {};
    const stage = clean(progress.clean_stage || progress.progress_stage || '');
    const latest = clean(progress.latest_review_status || '');
    if (isStoppedProgress(result)) return '已复核停止';
    if (latest === 'continue' || isContinuedProgress(result)) return '已复核推进';
    if (progress.reviewed) return `已复核${latest ? `：${latest}` : ''}`;
    return '未复核';
  }

  function xsaasContactText(result = state.latestLookup) {
    if (!result?.progress_matched) return '未关联';
    if (isStoppedProgress(result)) return '不联系';
    if (isContinuedProgress(result)) return '待人工联系/转猎聘或微信';
    return 'X-SaaS不记录猎聘触达';
  }

  function progressChips(result = state.latestLookup) {
    const progress = result?.progress || {};
    const stage = clean(progress.clean_stage || progress.progress_stage || '未分阶段');
    const jobCandidateId = result?.job_candidate_id || progress.job_candidate_id || '';
    const reviewTime = clean(progress.latest_review_time || '').slice(5, 16);
    return [
      jobCandidateId ? `关系 #${jobCandidateId}` : '已有推进关系',
      `阶段：${stage}`,
      `复核：${reviewStatusText(result)}${reviewTime ? ` ${reviewTime}` : ''}`,
      `触达：${xsaasContactText(result)}`
    ];
  }

  function markLookupStopped(result = state.latestLookup) {
    if (!result) return result;
    const progress = { ...(result.progress || {}) };
    progress.clean_stage = progress.clean_stage || 'H5 最近寻访/初筛不通过';
    progress.progress_stage = progress.progress_stage || progress.clean_stage;
    progress.latest_review_status = 'stop';
    progress.latest_event_status = progress.latest_event_status || 'stop';
    return {
      ...result,
      progress_matched: true,
      progress,
      chips: progressChips({ ...result, progress_matched: true, progress })
    };
  }

  function markLookupContinued(result = state.latestLookup) {
    if (!result) return result;
    const progress = { ...(result.progress || {}) };
    progress.clean_stage = progress.clean_stage || 'X2 X-SaaS复核通过/待人工联系';
    progress.progress_stage = progress.progress_stage || progress.clean_stage;
    progress.latest_review_status = 'continue';
    progress.latest_event_status = progress.latest_event_status || 'continue';
    return {
      ...result,
      progress_matched: true,
      progress,
      chips: progressChips({ ...result, progress_matched: true, progress })
    };
  }

  function queueCandidateLookup(delay = 250) {
    clearTimeout(state.lookupTimer);
    state.lookupTimer = setTimeout(() => {
      refreshCandidateLookup({ silent: true });
    }, delay);
  }

  function lookupPayload() {
    return readFormPayload('xsaas_candidate_lookup', {
      source_candidate_id: clean(document.querySelector('#xsa-source-id')?.value) || extractXsaasId()
    });
  }

  async function refreshCandidateLookup({ silent = false } = {}) {
    const payload = lookupPayload();
    if (!payload.xsaas_id && !payload.candidate) {
      renderLookupProgress(null);
      return null;
    }
    const seq = state.lookupSeq + 1;
    state.lookupSeq = seq;
    if (!silent) status('正在定位人才库...', '');
    const result = await postToWorkbench('/api/xsaas-candidate-lookup', payload);
    if (seq !== state.lookupSeq) return null;
    state.latestLookup = result;
    renderLookupProgress(result);
    if (result?.ok) {
      const quality = renderFieldQuality();
      if (!quality.ok) {
        status(quality.message, 'err');
      } else {
        status(result.message || '人才库定位完成', result.status === 'ambiguous' ? 'err' : 'ok');
      }
    } else {
      status(result?.error || '人才库定位失败', 'err');
    }
    updateActionAvailability();
    return result;
  }

  function renderLookupProgress(result = state.latestLookup) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-talent-progress`);
    if (!el) return;
    if (!result) {
      el.dataset.state = 'idle';
      el.innerHTML = '<span>库状态待检查</span><span>X-SaaS不记录猎聘触达</span>';
      renderLocationLine(null);
      updateActionAvailability();
      return;
    }
    const statusName = result.status || '';
    const chips = result.progress_matched
      ? progressChips(result)
      : (Array.isArray(result.chips) && result.chips.length ? result.chips : ['库状态待检查']);
    const stateName = result.progress_matched ? 'reviewed' : (result.candidate_matched ? 'pending' : (statusName === 'ambiguous' ? 'pending' : 'missing'));
    el.dataset.state = stateName;
    el.innerHTML = chips.map(item => `<span>${escapeHtml(item)}</span>`).join('');
    renderLocationLine(result);
    renderNextStep(result);
    renderCandidateMatches(result);
    renderFieldQuality();
  }

  function renderLocationLine(result = state.latestLookup) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-location-line`);
    if (!el) return;
    if (!result) {
      el.textContent = '定位：等待人才库查询';
      el.dataset.kind = '';
      return;
    }
    if (result.transport_error === 'network' || result.ok === false) {
      el.textContent = '定位：本机 8765 服务未连接';
      el.dataset.kind = 'err';
      return;
    }
    const candidate = result.candidate || {};
    const progress = result.progress || {};
    const reasons = (candidate.reasons || progress.reasons || [])
      .filter(Boolean)
      .slice(0, 3)
      .join(' / ');
    if (result.progress_matched) {
      el.textContent = [
        `定位：人才ID ${candidate.id || progress.source_candidate_id || '-'}`,
        `推进ID ${result.job_candidate_id || progress.job_candidate_id || '-'}`,
        progress.clean_stage ? `阶段 ${progress.clean_stage}` : '',
        reasons ? `依据 ${reasons}` : ''
      ].filter(Boolean).join(' · ');
      el.dataset.kind = 'ok';
      return;
    }
    if (result.candidate_matched) {
      const originalProject = [candidate.client, candidate.position].filter(Boolean).join(' / ');
      el.textContent = [
        `定位：人才ID ${candidate.id || '-'}`,
        originalProject ? `原关联 ${originalProject}` : '当前岗位未关联',
        '当前岗位未关联',
        reasons ? `依据 ${reasons}` : ''
      ].filter(Boolean).join(' · ');
      el.dataset.kind = 'warn';
      return;
    }
    if (result.status === 'ambiguous') {
      const count = Array.isArray(result.candidate_matches) ? result.candidate_matches.length : 0;
      el.textContent = `定位：${count || '多'}条疑似人才，请校正字段后再操作`;
      el.dataset.kind = 'err';
      return;
    }
    el.textContent = '定位：未找到人才库记录';
    el.dataset.kind = 'warn';
  }

  function renderExtractionAudit() {
    const el = document.querySelector(`#${ROOT_ID} .xsa-extraction-audit`);
    if (!el) return;
    const payload = readFormPayload('quality_check');
    const rows = [
      ['姓名', payload.candidate, state.extractionEvidence.candidate],
      ['公司', payload.company, state.extractionEvidence.company],
      ['职位', payload.title, state.extractionEvidence.title],
      ['X-SaaS ID', payload.xsaas_id, state.extractionEvidence.xsaas_id]
    ];
    el.innerHTML = rows.map(([label, value, item]) => {
      const kind = !value || item?.confidence === 'missing' ? 'err' : (item?.confidence === 'low' ? 'warn' : 'ok');
      const source = item?.source || '手动填写/未知来源';
      return `<div data-kind="${kind}"><b>${escapeHtml(label)}</b><span>${escapeHtml(value || '未识别')}</span><em>${escapeHtml(source)}</em></div>`;
    }).join('');
  }

  function renderCandidateMatches(result = state.latestLookup) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-candidate-matches`);
    if (!el) return;
    const matches = Array.isArray(result?.candidate_matches) ? result.candidate_matches.slice(0, 4) : [];
    if (!matches.length) {
      el.innerHTML = '<div class="xsa-empty">库内疑似：无</div>';
      return;
    }
    el.innerHTML = matches.map(item => {
      const project = [item.client, item.position].filter(Boolean).join(' / ');
      const basis = Array.isArray(item.reasons) ? item.reasons.join(' / ') : '';
      return `
        <div class="xsa-match-row">
          <b>${escapeHtml(item.name || '-')} · ID ${escapeHtml(item.id || '-')}</b>
          <span>${escapeHtml([item.company, item.title].filter(Boolean).join(' / ') || '公司职位待补')}</span>
          <em>${escapeHtml(project || '未关联项目')}${basis ? `｜${escapeHtml(basis)}` : ''}</em>
        </div>
      `;
    }).join('');
  }

  function renderNextStep(result = state.latestLookup) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-next-step`);
    if (!el) return;
    if (!result) {
      el.textContent = '下一步：等待人才库定位';
      el.dataset.kind = '';
      return;
    }
    if (result.transport_error === 'network' || result.ok === false) {
      el.textContent = '下一步：先启动或检查本机 8765 服务';
      el.dataset.kind = 'err';
      return;
    }
    const quality = fieldQuality();
    if (!quality.ok) {
      el.textContent = quality.projectMismatch && !quality.missing.length
        ? `下一步：${quality.projectMismatch}`
        : `下一步：先补全字段（${quality.missing.join(' / ')}）`;
      el.dataset.kind = 'err';
      return;
    }
    if (result.status === 'ambiguous') {
      el.textContent = '下一步：补全或校正姓名、公司、职位后再操作';
      el.dataset.kind = 'err';
      return;
    }
    if (isStoppedProgress(result)) {
      el.textContent = '下一步：已确认停止，无需重复复核';
      el.dataset.kind = 'warn';
      return;
    }
    if (isContinuedProgress(result)) {
      el.textContent = '下一步：已复核推进，待人工联系/转猎聘或微信';
      el.dataset.kind = 'ok';
      return;
    }
    if (result.progress_matched) {
      el.textContent = '下一步：可复核推进或确认停止';
      el.dataset.kind = 'ok';
      return;
    }
    if (result.candidate_matched) {
      el.textContent = '下一步：确认入库以关联当前客户/岗位';
      el.dataset.kind = 'warn';
      return;
    }
    el.textContent = '下一步：确认入库，生成当前岗位推进关系';
    el.dataset.kind = 'warn';
  }

  function updateActionAvailability() {
    const lookup = state.latestLookup || {};
    const hasProgress = lookup.progress_matched === true;
    const stopped = isStoppedProgress(lookup);
    const continued = isContinuedProgress(lookup);
    const isAmbiguous = lookup.status === 'ambiguous';
    const disconnected = lookup.transport_error === 'network';
    const quality = renderFieldQuality();
    renderExtractionAudit();
    const continueBtn = document.querySelector(`#${ROOT_ID} [data-action="write-continue"]`);
    const stopBtn = document.querySelector(`#${ROOT_ID} [data-action="write-stop"]`);
    const intakeBtn = document.querySelector(`#${ROOT_ID} [data-action="write-intake"]`);
    if (continueBtn) {
      continueBtn.disabled = stopped || continued || !hasProgress || isAmbiguous || disconnected;
      continueBtn.title = stopped
        ? '已确认停止，无需重复操作'
        : (continued ? '已复核推进，无需重复操作' : (hasProgress ? '已定位当前岗位推进关系' : '请先确认入库，生成当前岗位推进关系'));
    }
    if (stopBtn) {
      stopBtn.disabled = stopped || !hasProgress || isAmbiguous || disconnected;
      stopBtn.title = stopped
        ? '已确认停止，无需重复操作'
        : (continued ? '后续判断不匹配时，可确认停止' : (hasProgress ? '已定位当前岗位推进关系' : '请先确认入库，生成当前岗位推进关系'));
    }
    if (intakeBtn) {
      intakeBtn.disabled = isAmbiguous || hasProgress || disconnected || !quality.ok;
      intakeBtn.title = !quality.ok ? quality.message : (hasProgress ? '当前岗位已有推进关系，无需重复入库' : '写入人才库并关联当前岗位');
    }
    document.querySelectorAll(`#${ROOT_ID} [data-action="dry-intake"], #${ROOT_ID} [data-action="dry-continue"], #${ROOT_ID} [data-action="dry-stop"]`).forEach(btn => {
      if (!btn) return;
      if (btn.dataset.action === 'dry-intake') btn.disabled = isAmbiguous || disconnected || !quality.ok;
      if (btn.dataset.action === 'dry-continue') btn.disabled = stopped || continued || !hasProgress || isAmbiguous || disconnected;
      if (btn.dataset.action === 'dry-stop') btn.disabled = stopped || !hasProgress || isAmbiguous || disconnected;
    });
    renderNextStep(lookup.ok || lookup.status ? lookup : null);
  }

  function talentSummary(result) {
    return talentSyncSummary(result);
  }

  function resultMessage(action, result) {
    const summary = talentSummary(result);
    if (result?.ok) {
      const written = Number(summary.written || 0);
      const would = Number(summary.would_write || 0);
      return written ? `${action}已写入 A 系统` : `${action}预检通过：${would} 条可写入`;
    }
    const raw = clean(result?.stderr || result?.error || result?.body?.stderr || '');
    if (/unique|no_unique_match|pending_review/i.test(raw) || Number(summary.pending_review || 0) > 0) {
      return `${action}未写入：未唯一定位，请先入库或补全姓名/公司/职位/岗位`;
    }
    if (result?.transport_error === 'network') return `${action}失败：本机 8765 服务未连接`;
    return `${action}失败：${raw || '未知错误'}`;
  }

  async function runTalentAction(actionName, payload, write = false) {
    if (!payload.candidate || !payload.client || !payload.job) {
      status('缺少姓名、客户或岗位，先补全再继续', 'err');
      return null;
    }
    if (write) {
      setButtonsDisabled(true);
      status(`${actionName}写入预检中...`);
      const preflight = await postToWorkbench('/api/talent-action', { ...payload, write: false, refresh_workbench: false });
      if (preflight?.lookup) {
        state.latestLookup = preflight.lookup;
        renderLookupProgress(preflight.lookup);
      }
      if (!canWriteTalentAction(preflight)) {
        status(preflightText(preflight), preflight?.ok ? 'warn' : 'err');
        setButtonsDisabled(false);
        updateActionAvailability();
        return preflight;
      }
      setButtonsDisabled(false);
      const confirmResult = await confirmAction(actionName, { ...payload, _preflight_text: preflightText(preflight) });
      if (!confirmResult) {
        status(`${actionName}已取消`, '');
        updateActionAvailability();
        return null;
      }
      payload = { ...payload, ...confirmResult };
    }
    setButtonsDisabled(true);
    status(write ? `${actionName}写入中...` : `${actionName}预检中...`);
    const result = await postToWorkbench('/api/talent-action', { ...payload, write });
    status(resultMessage(actionName, result), result?.ok ? 'ok' : 'err');
    if (result?.lookup) state.latestLookup = result.lookup;
    if (write && result?.ok && actionName.includes('停止')) {
      state.latestLookup = markLookupStopped(state.latestLookup);
    }
    if (write && result?.ok && actionName.includes('推进')) {
      state.latestLookup = markLookupContinued(state.latestLookup);
    }
    renderTalentProgress(actionName, result, write);
    setButtonsDisabled(false);
    updateActionAvailability();
    if (write && result?.ok) await refreshCandidateLookup({ silent: true });
    return result;
  }

  function renderTalentProgress(actionName = '', result = null, write = false) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-talent-progress`);
    if (!el) return;
    const summary = talentSummary(result || {});
    const pending = Number(summary.pending_review || 0);
    const written = Number(summary.written || 0);
    const would = Number(summary.would_write || 0);
    let stateName = 'idle';
    let chips = ['库状态待检查', 'X-SaaS不记录猎聘触达'];
    if (result?.ok && written > 0) {
      stateName = 'reviewed';
      chips = [`${actionName}已写入`, write && actionName.includes('停止') ? 'H5 初筛不通过' : (actionName.includes('入库') ? 'X1 待复核' : 'X2 待人工联系')];
    } else if (result?.ok && would > 0) {
      stateName = 'pending';
      chips = [`${actionName}预检通过`, `${would} 条可写入`];
    } else if (result && pending > 0) {
      stateName = 'pending';
      chips = ['未唯一定位', '先确认入库或补全信息'];
    }
    el.dataset.state = stateName;
    el.innerHTML = chips.map(item => `<span>${escapeHtml(item)}</span>`).join('');
    updateActionAvailability();
  }

  async function intake(write) {
    const payload = readFormPayload('xsaas_intake', {
      summary: `X-SaaS插件确认入库：${clean(document.querySelector('#xsa-candidate')?.value)}｜${clean(document.querySelector('#xsa-client')?.value)}/${clean(document.querySelector('#xsa-job')?.value)}`,
      reason: 'X-SaaS插件确认入库，待人工复核'
    });
    const quality = renderFieldQuality(payload);
    if (!quality.ok) {
      status(`${write ? '确认入库' : '入库预检'}已拦截：${quality.message}`, 'err');
      return;
    }
    await runTalentAction(write ? '确认入库' : '入库预检', payload, write);
  }

  async function review(result, write) {
    if (write && result === 'stop' && isStoppedProgress()) {
      status('已确认停止，无需重复操作', 'warn');
      updateActionAvailability();
      return;
    }
    if (write && result === 'continue' && isContinuedProgress()) {
      status('已复核推进，无需重复操作', 'warn');
      updateActionAvailability();
      return;
    }
    const candidate = clean(document.querySelector('#xsa-candidate')?.value);
    const payload = readFormPayload('xsaas_review', {
      review_result: result,
      summary: result === 'stop'
        ? `X-SaaS插件复核停止：${candidate}`
        : `X-SaaS插件复核推进：${candidate}`,
      reason: result === 'stop'
        ? 'X-SaaS插件确认停止推进'
        : 'X-SaaS插件确认复核推进，待人工联系',
      next_action: result === 'stop' ? '保留记录，不联系' : '人工联系/转猎聘或微信',
      stop_reason_code: result === 'stop' ? clean(document.querySelector('#xsa-stop-reason')?.value) : '',
      stop_reason_label: result === 'stop' ? clean(document.querySelector('#xsa-stop-reason option:checked')?.textContent) : '',
      stop_reason_note: result === 'stop' ? clean(document.querySelector('#xsa-stop-note')?.value) : ''
    });
    await runTalentAction(write ? (result === 'stop' ? '确认停止' : '确认推进') : '推进预检', payload, write);
  }

  async function copyCurrentPanel() {
    const payload = readFormPayload('copy_summary');
    const chips = Array.from(document.querySelectorAll(`#${ROOT_ID} .xsa-talent-progress span`)).map(el => clean(el.textContent)).filter(Boolean);
    const lines = [
      'X-SaaS 人岗匹配助手',
      `人选：${payload.candidate || '未识别'}`,
      `公司/职位：${payload.company || '公司待补'} / ${payload.title || '职位待补'}`,
      `客户/岗位：${payload.client || '未选客户'} / ${payload.job || '未选岗位'}`,
      `X-SaaS ID：${payload.xsaas_id || '未识别'}`,
      chips.length ? `状态：${chips.join('｜')}` : '',
      clean(document.querySelector(`#${ROOT_ID} .xsa-location-line`)?.textContent || ''),
      clean(document.querySelector(`#${ROOT_ID} .xsa-next-step`)?.textContent || '')
    ].filter(Boolean);
    const ok = await copyText(lines.join('\n')).catch(() => false);
    status(ok ? '已复制当前人选状态' : '复制失败，请手动选择文本', ok ? 'ok' : 'err');
  }

  function closeIdentityLayer() {
    document.querySelector(`#${ROOT_ID} .xsa-identity-layer`)?.remove();
  }

  function currentIdentityPersonId() {
    return state.latestLookup?.progress?.person_id || state.latestLookup?.progress_lookup?.match?.person_id || '';
  }

  function identitySourceProfile() {
    const payload = readFormPayload('candidate_identity');
    return {
      source_type: 'xsaas',
      source_candidate_id: payload.source_candidate_id,
      source_url: payload.source_url,
      candidate: payload.candidate,
      company: payload.company,
      title: payload.title,
      client: payload.client,
      position: payload.job,
      candidate_profile_text: payload.candidate_profile_text
    };
  }

  async function discoverSameCandidate() {
    const root = document.getElementById(ROOT_ID);
    if (!root) return;
    const sourceProfile = identitySourceProfile();
    const currentPersonId = currentIdentityPersonId();
    status('正在查找疑似同一人...');
    const result = await postToWorkbench('/api/candidate-identity-matches', {
      ...sourceProfile,
      current_person_id: currentPersonId
    });
    if (!result?.ok) {
      status(`查找失败：${clean(result?.error || '本机服务未响应')}`, 'err');
      return;
    }
    closeIdentityLayer();
    const matches = Array.isArray(result.matches) ? result.matches : [];
    const allowed = matches.filter(item => item.merge_allowed);
    const resolvedCurrentPersonId = result.current_person_id || currentPersonId;
    const rows = matches.length
      ? matches.slice(0, 10).map((item, index) => `
          <label class="xsa-identity-option" data-allowed="${item.merge_allowed ? 'true' : 'false'}">
            <input type="radio" name="xsa-identity-person" value="${escapeHtml(item.person_id)}"
              ${item.merge_allowed && index === matches.findIndex(match => match.merge_allowed) ? 'checked' : ''}
              ${item.merge_allowed ? '' : 'disabled'}>
            <span>
              <b>${escapeHtml(item.candidate || '未命名')} · 人才 #${escapeHtml(item.person_id)}</b>
              <em>${escapeHtml(item.company || '公司未知')} / ${escapeHtml(item.title || '职位未知')}</em>
              <small>${escapeHtml((item.reasons || []).join(' · ') || '证据不足')}｜${item.merge_allowed ? '可对比' : '禁止合并'}</small>
            </span>
          </label>
        `).join('')
      : '<div class="xsa-empty">没有发现疑似同一人的档案</div>';
    const layer = document.createElement('div');
    layer.className = 'xsa-confirm-layer xsa-identity-layer';
    layer.innerHTML = `
      <div class="xsa-confirm-panel" role="dialog" aria-modal="true" aria-label="发现同一人">
        <div class="xsa-confirm-head">
          <strong>发现同一人</strong>
          <button type="button" class="xsa-confirm-close" aria-label="关闭">x</button>
        </div>
        <div class="xsa-identity-current">
          <span>当前 X-SaaS 档案</span>
          <b>${escapeHtml(sourceProfile.candidate || '未识别')} · ${escapeHtml(sourceProfile.company || '公司未知')} / ${escapeHtml(sourceProfile.title || '职位未知')}</b>
        </div>
        ${resolvedCurrentPersonId ? '' : '<div class="xsa-confirm-guard"><strong>当前档案尚未定位</strong><p>请先“确认入库”，再执行档案合并。</p></div>'}
        <div class="xsa-identity-options">${rows}</div>
        <div class="xsa-identity-preflight" hidden></div>
        <div class="xsa-confirm-actions">
          <button type="button" class="xsa-confirm-cancel">取消</button>
          <button type="button" class="xsa-confirm-submit xsa-primary" ${!resolvedCurrentPersonId || !allowed.length ? 'disabled' : ''}>对比档案</button>
        </div>
      </div>
    `;
    root.appendChild(layer);
    const close = () => closeIdentityLayer();
    layer.querySelector('.xsa-confirm-close')?.addEventListener('click', close);
    layer.querySelector('.xsa-confirm-cancel')?.addEventListener('click', close);
    layer.addEventListener('click', event => { if (event.target === layer) close(); });
    const submit = layer.querySelector('.xsa-confirm-submit');
    submit?.addEventListener('click', async () => {
      const selectedPersonId = Number(layer.querySelector('input[name="xsa-identity-person"]:checked')?.value || 0);
      if (!selectedPersonId || !resolvedCurrentPersonId) return;
      submit.disabled = true;
      const request = {
        canonical_person_id: selectedPersonId,
        merged_person_id: Number(resolvedCurrentPersonId),
        source_profile: sourceProfile,
        actor: 'xsaas-candidate-assistant',
        write: false
      };
      if (submit.dataset.step !== 'confirm') {
        status('正在执行合并预检...');
        const preflight = await postToWorkbench('/api/candidate-merge', request);
        if (!preflight?.ok) {
          status(`不能合并：${clean(preflight?.error || '身份或推进状态存在冲突')}`, 'err');
          submit.disabled = false;
          return;
        }
        layer.__mergeRequest = request;
        layer.__confirmationToken = preflight.confirmation_token;
        const plan = preflight.plan || {};
        const compare = layer.querySelector('.xsa-identity-preflight');
        compare.hidden = false;
        compare.innerHTML = `
          <div><span>保留档案</span><b>${escapeHtml(preflight.canonical?.candidate || '')} · 人才 #${escapeHtml(preflight.canonical?.person_id || '')}</b></div>
          <div><span>并入档案</span><b>${escapeHtml(preflight.merged?.candidate || '')} · 人才 #${escapeHtml(preflight.merged?.person_id || '')}</b></div>
          <p>保留双方来源简历；迁移 ${escapeHtml(plan.source_profiles || 0)} 条来源档案、${escapeHtml(plan.job_relations || 0)} 条岗位关系、${escapeHtml(plan.events || 0)} 条事件。</p>
        `;
        submit.dataset.step = 'confirm';
        submit.textContent = '确认合并';
        submit.disabled = false;
        status('对比完成，请人工确认是否合并', 'warn');
        return;
      }
      status('正在合并档案...');
      const merged = await postToWorkbench('/api/candidate-merge', {
        ...layer.__mergeRequest,
        write: true,
        confirmation_token: layer.__confirmationToken
      });
      if (!merged?.ok) {
        status(`合并失败：${clean(merged?.error || '确认已失效')}`, 'err');
        submit.disabled = false;
        return;
      }
      closeIdentityLayer();
      status('档案已合并，双方来源简历和推进历史均已保留', 'ok');
      await refreshCandidateLookup({ silent: true });
    });
    status(allowed.length ? `发现 ${allowed.length} 条可对比档案` : '未发现可安全合并的同一人', allowed.length ? 'warn' : 'ok');
  }

  function setButtonsDisabled(disabled) {
    document.querySelectorAll(`#${ROOT_ID} button[data-action^="write-"]`).forEach(btn => {
      btn.disabled = disabled;
    });
  }

  function renderDetectedPills(candidate = state.latestCandidate || {}) {
    const el = document.querySelector(`#${ROOT_ID} .xsa-pillrow`);
    if (!el) return;
    const items = [
      candidate.xsaas_id ? `ID ${candidate.xsaas_id}` : '',
      candidate.age || '',
      candidate.years || '',
      candidate.education || '',
      candidate.city || ''
    ].filter(Boolean);
    el.innerHTML = items.map(item => `<span class="xsa-pill">${escapeHtml(item)}</span>`).join('');
  }

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function render() {
    let root = document.getElementById(ROOT_ID);
    if (!root) {
      root = document.createElement('div');
      root.id = ROOT_ID;
      document.documentElement.appendChild(root);
    }
    root.dataset.collapsed = String(state.collapsed);
    root.innerHTML = `
      <div class="xsa-card" role="region" aria-label="X-SaaS 人岗匹配助手">
        <div class="xsa-head">
          <div>
            <div class="xsa-title">X-SaaS 人岗匹配助手 v${escapeHtml(EXTENSION_VERSION)}</div>
            <div class="xsa-subtitle">打开人选即看入库状态，复核后同步 A 系统</div>
          </div>
          <div class="xsa-head-actions">
            <button id="xsa-toggle">${state.collapsed ? 'ASA' : '收起'}</button>
          </div>
        </div>
        <div class="xsa-body">
          <div class="xsa-project-picker">
            <label for="xsa-project-select">推荐岗位</label>
            <select id="xsa-project-select"></select>
            <div class="xsa-project-grid">
              <input id="xsa-client" placeholder="客户，可空">
              <input id="xsa-job" placeholder="岗位">
            </div>
          </div>
          <div class="xsa-meta-line">
            <span id="xsa-assistant-score" data-grade="待复核">X-SaaS待复核</span>
            <span id="xsa-assistant-status" class="xsa-status" data-kind="${escapeHtml(state.statusKind)}">${escapeHtml(state.lastStatus)}</span>
          </div>
          <div id="xsa-assistant-meta" class="xsa-meta">正在读取当前人选。</div>
          <div class="xsa-field-quality" data-kind=""><span>字段检查中</span></div>
          <div class="xsa-talent-progress" data-state="idle"><span>库状态待检查</span><span>X-SaaS不记录猎聘触达</span></div>
          <div class="xsa-location-line">定位：等待人才库查询</div>
          <div class="xsa-next-step">下一步：等待人才库定位</div>
          <div class="xsa-pillrow"></div>
          <div class="xsa-actions xsa-resume-actions">
            <button class="xsa-good" data-action="write-continue">确认推进</button>
            <button class="xsa-danger" data-action="write-stop">确认停止</button>
            <button class="xsa-accept" data-action="write-intake">确认入库</button>
            <details class="xsa-more-actions">
              <summary>更多操作</summary>
              <div class="xsa-more-actions-body">
                <button data-action="refresh">刷新匹配</button>
                <button data-action="dry-intake">入库预检</button>
                <button data-action="dry-continue">推进预检</button>
                <button data-action="dry-stop">停止预检</button>
                <button data-action="copy-current">复制当前</button>
                <button data-action="identity-match">发现同一人</button>
              </div>
            </details>
          </div>
          <details class="xsa-detail">
            <summary>人选详情与字段校正</summary>
            <div class="xsa-grid">
              <label>姓名<input id="xsa-candidate" placeholder="候选人姓名"></label>
              <label>X-SaaS ID<input id="xsa-source-id" placeholder="候选人ID"></label>
            <label>公司<input id="xsa-company" placeholder="当前公司"></label>
            <label>职位<input id="xsa-title-input" placeholder="当前职位"></label>
            <label>城市<input id="xsa-city" placeholder="城市"></label>
            <label>学历<input id="xsa-education" placeholder="学历"></label>
            <label>年限<input id="xsa-experience" placeholder="工作年限"></label>
            <label>等级
              <select id="xsa-level">
                <option value="">未评</option>
                <option value="A">A</option>
                <option value="B">B</option>
                <option value="C">C</option>
              </select>
            </label>
            <label class="xsa-wide">停止原因
              <select id="xsa-stop-reason">
                <option value="too_senior">太资深</option>
                <option value="salary_mismatch">薪资太贵</option>
                <option value="direction_mismatch">方向不符</option>
                <option value="experience_mismatch">经验不符</option>
                <option value="location_mismatch">地点不符</option>
                <option value="other">其他</option>
              </select>
            </label>
            <label class="xsa-wide">停止备注<input id="xsa-stop-note" placeholder="例如：薪资期望明显高于区间，或管理岗太资深"></label>
            <label class="xsa-wide">页面摘要<textarea id="xsa-profile"></textarea></label>
            </div>
          </details>
          <details class="xsa-detail xsa-diagnostics">
            <summary>定位诊断</summary>
            <div class="xsa-diagnostics-body">
              <div class="xsa-diagnostics-title">页面抓取依据</div>
              <div class="xsa-extraction-audit"></div>
              <div class="xsa-diagnostics-title">人才库疑似记录</div>
              <div class="xsa-candidate-matches"><div class="xsa-empty">库内疑似：等待查询</div></div>
            </div>
          </details>
        </div>
      </div>
    `;
    renderProjectOptions(root.querySelector('#xsa-project-select'));
    wire(root);
    renderBridgeStatusDot();
    hydrateProjects();
    fillCandidateFields();
    updateActionAvailability();
  }

  function wire(root) {
    root.querySelector('#xsa-toggle')?.addEventListener('click', (event) => {
      if (state.collapsed) {
        if (event.shiftKey) {
          state.collapsed = false;
          render();
          return;
        }
        openAsaFloating();
        renderBridgeStatusDot();
        return;
      }
      state.collapsed = !state.collapsed;
      render();
    });
    root.querySelector('#xsa-project-select')?.addEventListener('change', syncProjectInputs);
    root.querySelectorAll('#xsa-candidate, #xsa-company, #xsa-title-input, #xsa-city, #xsa-education, #xsa-experience, #xsa-source-id, #xsa-client, #xsa-job').forEach(input => {
      input.addEventListener('input', () => {
        renderCandidateMeta();
        renderFieldQuality();
        renderExtractionAudit();
        updateActionAvailability();
        queueCandidateLookup(500);
      });
    });
    root.querySelector('[data-action="refresh"]')?.addEventListener('click', () => {
      fillCandidateFields(extractCandidate());
      status('已重新读取当前页面', 'ok');
      refreshCandidateLookup({ silent: false });
    });
    root.querySelector('[data-action="dry-intake"]')?.addEventListener('click', () => intake(false));
    root.querySelector('[data-action="write-intake"]')?.addEventListener('click', () => intake(true));
    root.querySelector('[data-action="dry-continue"]')?.addEventListener('click', () => review('continue', false));
    root.querySelector('[data-action="dry-stop"]')?.addEventListener('click', () => review('stop', false));
    root.querySelector('[data-action="copy-current"]')?.addEventListener('click', copyCurrentPanel);
    root.querySelector('[data-action="identity-match"]')?.addEventListener('click', discoverSameCandidate);
    root.querySelector('[data-action="write-continue"]')?.addEventListener('click', () => review('continue', true));
    root.querySelector('[data-action="write-stop"]')?.addEventListener('click', () => review('stop', true));
  }

  function isCandidateInfoPage() {
    return /headhunt\.x-saas\.com\.cn/i.test(location.hostname) && /#\/app\/candidate\/info\//i.test(location.href);
  }

  function boot() {
    if (!/headhunt\.x-saas\.com\.cn/i.test(location.hostname)) return;
    const syncSurface = () => {
      const root = document.getElementById(ROOT_ID);
      if (!isCandidateInfoPage()) {
        if (root) root.remove();
        lastSurfaceSignature = '';
        return;
      }
      if (!root) render();
      else fillCandidateFields(extractCandidate());
      lastSurfaceSignature = currentSurfaceSignature();
    };
    syncSurface();
    let lastHref = location.href;
    setInterval(() => {
      const signature = currentSurfaceSignature();
      if (location.href !== lastHref || signature !== lastSurfaceSignature) {
        lastHref = location.href;
        setTimeout(syncSurface, 800);
      }
      reportFloatingContext();
    }, 1000);
    const observer = new MutationObserver(() => {
      const signature = currentSurfaceSignature();
      if (signature && signature !== lastSurfaceSignature) {
        setTimeout(syncSurface, 300);
      }
    });
    observer.observe(document.body || document.documentElement, { childList: true, subtree: true, characterData: true });
    document.addEventListener('pointerdown', reportFloatingUserActivity, { passive: true });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  setInterval(() => {
    pollFloatingCommands();
  }, 1800);
})();
