import { cli, Strategy } from '@jackwener/opencli/registry';
import {
  ArgumentError,
  AuthRequiredError,
  CommandExecutionError,
  EmptyResultError,
  TimeoutError,
} from '@jackwener/opencli/errors';

const HOST = 'headhunt.x-saas.com.cn';
const LIST_URL = `https://${HOST}/#/app/candidate/list`;
const MAX_LIMIT = 100;

function parseLimit(value) {
  const limit = Number(value ?? 30);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) {
    throw new ArgumentError(`limit must be an integer between 1 and ${MAX_LIMIT}`);
  }
  return limit;
}

async function waitForCandidateList(page, timeoutSeconds = 20) {
  const attempts = timeoutSeconds * 2;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const state = await page.evaluate(() => ({
      href: window.location.href,
      login: Boolean(document.querySelector('form[name="loginForm"]')),
      ready: Boolean(document.querySelector('input.search-input[ng-model="ngkeyword"]')),
      dataReady: Boolean(document.querySelector('tr[ng-repeat="candidate in onePagePerson"]'))
        || /[0-9,]+\u6761\u8bb0\u5f55/.test(document.body?.innerText || ''),
    }));
    if (state.login || String(state.href).includes('#/login')) {
      throw new AuthRequiredError(HOST, 'X-SaaS requires a signed-in browser session');
    }
    if (state.ready && state.dataReady && String(state.href).includes('#/app/candidate/list')) return;
    await page.wait({ time: 0.5 });
  }
  throw new TimeoutError('X-SaaS candidate list', timeoutSeconds);
}

async function submitQuery(page, query) {
  const started = await page.evaluate((value) => {
    const input = document.querySelector('input.search-input[ng-model="ngkeyword"]');
    const button = document.querySelector('[ng-click="fnQuerySearch();"]');
    if (!input || !button) return { ok: false, reason: 'search_controls_missing' };
    const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (!descriptor?.set) return { ok: false, reason: 'native_input_setter_missing' };
    descriptor.set.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    button.click();
    return { ok: true };
  }, query);
  if (!started?.ok) {
    throw new CommandExecutionError(`X-SaaS search could not start: ${started?.reason || 'unknown reason'}`);
  }
}

async function readResults(page, query, timeoutSeconds = 20) {
  const attempts = timeoutSeconds * 2;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const result = await page.evaluate((expectedQuery) => {
      const bodyText = document.body?.innerText || '';
      const countMatch = bodyText.match(/([0-9,]+)\u6761\u8bb0\u5f55/);
      const selected = (bodyText.match(/\u5df2\u9009\u6761\u4ef6\s+\u5173\u952e\u5b57\uff1a([^\n]+)/) || [])[1] || '';
      const candidateRows = Array.from(document.querySelectorAll('table.candidate-list tbody tr'));
      const extracted = candidateRows.map((row, index) => {
        const scoped = window.angular ? angular.element(row).scope()?.candidate : null;
        const cells = Array.from(row.querySelectorAll('td'));
        const links = Array.from(row.querySelectorAll('a'));
        const profile = links.find((link) => /candidate\/info\/(\d+)/.test(link.href || ''));
        const idMatch = profile ? (profile.href || '').match(/candidate\/info\/(\d+)/) : null;
        const first = (cells[0]?.innerText || '').trim().split('\n').map((item) => item.trim()).filter(Boolean);
        const second = (cells[1]?.innerText || '').trim().split('\n').map((item) => item.trim()).filter(Boolean);
        const jobs = Array.isArray(scoped?.arrJobDetail) ? scoped.arrJobDetail : [];
        const currentJob = jobs.find((item) => item?.isnow) || jobs[0] || {};
        const personId = String(scoped?.ipersonid || (idMatch ? idMatch[1] : '') || '');
        const displayName = String(
          scoped?.sNameView || scoped?.sName || scoped?.sname
          || profile?.innerText || first.find((item) => !/^\d+$/.test(item)) || ''
        ).trim();
        const company = String(scoped?.scompany || currentJob.scompanyname || second[0] || '').trim();
        const title = String(scoped?.sposition || currentJob.sposition || currentJob.scompanyposition || second[1] || '').trim();
        const workText = jobs.slice(0, 4).map((item) => [
          item?.scompanyname, item?.sposition || item?.scompanyposition, item?.sstart, item?.send,
        ].filter(Boolean).join(' \u00b7 ')).filter(Boolean).join(' | ');
        return {
          rankValue: index + 1,
          candidateIdValue: personId,
          nameValue: displayName,
          companyValue: company,
          titleValue: title,
          workTextValue: workText,
          profileTextValue: workText || second.slice(0, 4).join(' | '),
          urlValue: personId ? `https://headhunt.x-saas.com.cn/#/app/candidate/info/${personId}` : (profile ? profile.href : ''),
        };
      }).filter((item) => item.candidateIdValue && item.nameValue);
      return {
        selectedQuery: selected.trim(),
        queryMatched: selected.includes(expectedQuery),
        loading: bodyText.includes('loading...'),
        resultCount: countMatch ? Number(countMatch[1].replace(/,/g, '')) : 0,
        rows: extracted,
      };
    }, query);

    if (result?.queryMatched && !result.loading) return result;
    await page.wait({ time: 0.5 });
  }
  throw new TimeoutError(`X-SaaS query result for "${query}"`, timeoutSeconds);
}

cli({
  site: 'xsaas',
  name: 'candidate-search',
  description: 'Read-only X-SaaS candidate search for ASA sourcing experiments',
  access: 'read',
  example: 'opencli xsaas candidate-search "server power" --limit 30 -f json',
  domain: HOST,
  strategy: Strategy.UI,
  browser: true,
  navigateBefore: false,
  args: [
    { name: 'query', type: 'string', positional: true, required: true, help: 'Candidate keyword query' },
    { name: 'limit', type: 'int', default: 30, help: `Maximum rows to return (1-${MAX_LIMIT})` },
  ],
  columns: [
    'rank', 'candidateId', 'name', 'company', 'title', 'workText',
    'educationText', 'profileText', 'url', 'dataStage', 'resumeCaptureStatus',
    'query', 'selectedQuery', 'resultCount',
  ],
  func: async (page, args) => {
    const query = String(args.query ?? '').replace(/\s+/g, ' ').trim();
    if (!query) throw new ArgumentError('query is required');
    const limit = parseLimit(args.limit);

    const current = await page.evaluate(() => ({
      href: window.location.href,
      login: Boolean(document.querySelector('form[name="loginForm"]')),
      ready: Boolean(document.querySelector('input.search-input[ng-model="ngkeyword"]')),
    }));
    if (current.login || String(current.href).includes('#/login')) {
      throw new AuthRequiredError(HOST, 'X-SaaS requires a signed-in browser session');
    }
    if (!current.ready || !String(current.href).includes('#/app/candidate/list')) {
      await page.goto(LIST_URL);
    }
    await waitForCandidateList(page);
    await submitQuery(page, query);
    await page.wait({ time: 3.5 });
    const result = await readResults(page, query);
    if (!result.rows.length) {
      if (result.resultCount > 0) {
        throw new CommandExecutionError(
          `X-SaaS reported ${result.resultCount} matches but no candidate rows could be parsed`,
        );
      }
      throw new EmptyResultError('xsaas candidate-search', `No candidates matched "${query}"`);
    }

    return result.rows.slice(0, limit).map((item) => ({
      rank: item.rankValue,
      candidateId: item.candidateIdValue,
      name: item.nameValue,
      company: item.companyValue,
      title: item.titleValue,
      workText: item.workTextValue,
      educationText: '',
      profileText: item.profileTextValue,
      url: item.urlValue,
      dataStage: 'recall',
      resumeCaptureStatus: 'not_requested',
      query,
      selectedQuery: result.selectedQuery,
      resultCount: result.resultCount,
    }));
  },
});
