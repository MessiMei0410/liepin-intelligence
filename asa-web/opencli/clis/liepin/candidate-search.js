import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  AuthRequiredError,
  CommandExecutionError,
  EmptyResultError,
} from '@jackwener/opencli/errors';

const HOST = 'h.liepin.com';
const SEARCH_URL = `https://${HOST}/search/getConditionItem`;
const MAX_LIMIT = 24;

function parseLimit(value) {
  const limit = Number(value ?? MAX_LIMIT);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) {
    throw new ArgumentError(`limit must be an integer between 1 and ${MAX_LIMIT}`);
  }
  return limit;
}

async function pageState(page) {
  return page.evaluate(() => ({
    href: window.location.href,
    title: document.title,
    ready: Boolean(
      document.querySelector('#rc_select_1')
      || document.querySelector('.ant-select-selection-search-input')
      || document.querySelector('input[placeholder*="\u641c\u7d22"]'),
    ),
  }));
}

async function waitForSearchPage(page, timeoutSeconds = 20) {
  for (let attempt = 0; attempt < timeoutSeconds * 2; attempt += 1) {
    const state = await pageState(page);
    if (String(state.href).includes('login')) {
      throw new AuthRequiredError(HOST, 'Liepin requires a signed-in browser session');
    }
    if (state.ready && String(state.href).includes('/search/getConditionItem')) return;
    await page.wait({ time: 0.5 });
  }
  throw new CommandExecutionError('Liepin search page did not become ready');
}

async function search(page, query) {
  return page.evaluate(async (value) => {
    const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
    const input = document.querySelector('#rc_select_1')
      || document.querySelector('.ant-select-selection-search-input')
      || document.querySelector('input[placeholder*="\u641c\u7d22"]')
      || document.querySelector('input[type="text"]');
    if (!input) return { ok: false, reason: 'search_input_missing' };
    const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (!descriptor?.set) return { ok: false, reason: 'native_input_setter_missing' };
    input.focus();
    descriptor.set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await sleep(250);
    const button = document.querySelector('.search-btn')
      || Array.from(document.querySelectorAll('button,a,div'))
        .find((element) => (element.innerText || '').trim() === '\u641c\u7d22');
    if (button) button.click();
    else input.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true,
    }));
    await sleep(4200);
    const activeInput = document.querySelector('#rc_select_1')
      || document.querySelector('.ant-select-selection-search-input')
      || document.querySelector('input[placeholder*="\u641c\u7d22"]');
    return {
      ok: true,
      actualQuery: String(activeInput?.value || '').trim(),
      totalText: document.querySelector('[data-nick="totalcnt"]')?.textContent?.trim() || '',
      cards: document.querySelectorAll('.tlog-common-resume-card').length,
    };
  }, query);
}

async function extract(page, query, limit, totalText) {
  return page.evaluate((expectedQuery, maxRows, rawTotal) => {
    const lines = (element) => (element.innerText || element.textContent || '')
      .split(/\n+/).map((item) => item.trim()).filter(Boolean);
    const parseWorkAndEducation = (items) => {
      const workItems = [];
      const educationItems = [];
      for (let index = 0; index < items.length - 1; index += 1) {
        const description = items[index];
        const dates = items[index + 1];
        if (!/(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2}|\u81f3\u4eca)/.test(dates) || !description.includes('\u00b7')) continue;
        const parts = description.split('\u00b7').map((item) => item.trim()).filter(Boolean);
        if (description.includes('\u7edf\u62db') || description.includes('\u975e\u7edf\u62db') || /(\u672c\u79d1|\u7855\u58eb|\u535a\u58eb|\u5927\u4e13|\u4e2d\u4e13\/\u4e2d\u6280)/.test(description)) {
          educationItems.push({ school: parts[0] || '', degree: parts[2] || '', dates });
        } else {
          workItems.push({ company: parts[0] || '', title: parts.slice(1).join('\u00b7') || '', dates });
        }
      }
      return { workItems, educationItems };
    };
    const resumeIdentity = (card) => {
      for (const node of card.querySelectorAll('[data-tlg-ext]')) {
        const encoded = node.getAttribute('data-tlg-ext') || '';
        for (const value of [encoded, (() => {
          try { return decodeURIComponent(encoded); } catch (error) { return ''; }
        })()]) {
          try {
            const payload = JSON.parse(value);
            const resumeId = String(payload.res_id_encode || payload.resIdEncode || '').trim();
            if (resumeId) return resumeId;
          } catch (error) {}
        }
      }
      for (const node of card.querySelectorAll('[data-tlg-scm]')) {
        const match = String(node.getAttribute('data-tlg-scm') || '').match(/(?:^|&)cid=([^&]+)/);
        if (match?.[1]) return decodeURIComponent(match[1]);
      }
      return '';
    };
    const countMatch = String(rawTotal).match(/[\d,]+/);
    const resultCount = countMatch ? Number(countMatch[0].replace(/,/g, '')) : 0;
    return Array.from(document.querySelectorAll('.tlog-common-resume-card')).slice(0, maxRows).map((card, index) => {
      const visible = lines(card);
      const history = parseWorkAndEducation(visible);
      const nameNode = card.querySelector('.new-resume-personal-name em')?.textContent?.trim()
        || visible.find((item) => /^[\u4e00-\u9fa5A-Za-z]{1,4}\*{1,3}$/.test(item))
        || visible.find((item) => item.includes('**')) || '';
      const detail = Array.from(card.querySelectorAll('.new-resume-personal-detail span'))
        .map((item) => (item.innerText || item.textContent || '').trim()).filter(Boolean);
      const firstWork = history.workItems[0] || {};
      const education = detail.find((item) => /^(\u535a\u58eb|\u7855\u58eb|\u672c\u79d1|\u5927\u4e13|MBA|MBA\/EMBA|\u4e2d\u4e13\/\u4e2d\u6280|\u9ad8\u4e2d\u53ca\u4ee5\u4e0b)$/.test(item)) || '';
      const experience = detail.find((item) => /^\u5de5\u4f5c\d+\u5e74/.test(item) || item === '--') || '';
      const city = detail.find((item) => !/^\d+\u5c81$/.test(item) && !/^\u5de5\u4f5c\d+\u5e74/.test(item) && !/^(\u535a\u58eb|\u7855\u58eb|\u672c\u79d1|\u5927\u4e13|MBA|MBA\/EMBA|\u4e2d\u4e13\/\u4e2d\u6280|\u9ad8\u4e2d\u53ca\u4ee5\u4e0b|--)$/.test(item)) || '';
      const resumeId = resumeIdentity(card);
      return {
        rank: index + 1,
        resumeId,
        name: nameNode.replace(/\*+/g, '**'),
        currentCompany: firstWork.company || '',
        currentTitle: firstWork.title || '',
        experience: experience.replace('\u5de5\u4f5c', ''),
        education,
        city,
        workText: history.workItems.map((item) => [item.company, item.title, item.dates].filter(Boolean).join(' \u00b7 ')).join('\n'),
        educationText: history.educationItems.map((item) => [item.school, item.degree, item.dates].filter(Boolean).join(' \u00b7 ')).join('\n'),
        profileText: visible.join(' ').slice(0, 2500),
        url: resumeId ? `https://h.liepin.com/resume/showresumedetail/?showsearchfeedback=1&res_id_encode=${encodeURIComponent(resumeId)}` : '',
        dataStage: 'recall',
        resumeCaptureStatus: 'not_requested',
        query: expectedQuery,
        resultCount,
      };
    }).filter((item) => item.name);
  }, query, limit, totalText);
}

cli({
  site: 'liepin',
  name: 'candidate-search',
  description: 'Read-only Liepin candidate search for ASA sourcing experiments',
  access: 'read',
  example: 'opencli liepin candidate-search "server power" --limit 24 -f json',
  domain: HOST,
  strategy: Strategy.UI,
  browser: true,
  navigateBefore: false,
  args: [
    { name: 'query', type: 'string', positional: true, required: true, help: 'Candidate keyword query' },
    { name: 'limit', type: 'int', default: MAX_LIMIT, help: `Maximum cards to return (1-${MAX_LIMIT})` },
  ],
  columns: [
    'rank', 'resumeId', 'name', 'currentCompany', 'currentTitle', 'experience',
    'education', 'city', 'workText', 'educationText', 'profileText', 'url',
    'dataStage', 'resumeCaptureStatus', 'query', 'resultCount',
  ],
  func: async (page, args) => {
    const query = String(args.query ?? '').replace(/\s+/g, ' ').trim();
    if (!query) throw new ArgumentError('query is required');
    const limit = parseLimit(args.limit);
    const current = await pageState(page);
    if (String(current.href).includes('login')) {
      throw new AuthRequiredError(HOST, 'Liepin requires a signed-in browser session');
    }
    if (!current.ready || !String(current.href).includes('/search/getConditionItem')) {
      await page.goto(SEARCH_URL);
    }
    await waitForSearchPage(page);
    const searched = await search(page, query);
    if (!searched?.ok) {
      throw new CommandExecutionError(`Liepin search could not start: ${searched?.reason || 'unknown reason'}`);
    }
    if (searched.actualQuery && searched.actualQuery !== query) {
      throw new CommandExecutionError(`Liepin query mismatch: expected "${query}", got "${searched.actualQuery}"`);
    }
    const rows = await extract(page, query, limit, searched.totalText || '');
    if (!rows.length) throw new EmptyResultError('liepin candidate-search', `No candidate cards matched "${query}"`);
    return rows;
  },
});
