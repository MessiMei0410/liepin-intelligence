chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    xsaasCandidateAssistantInstalledAt: new Date().toISOString()
  });
});

const WORKBENCH_BASE = 'http://127.0.0.1:8765';
const ALLOWED_METHODS = new Set(['GET', 'POST', 'PATCH']);

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type !== 'asa-workbench-request') return false;
  const method = String(message.method || 'GET').toUpperCase();
  let url;
  try {
    url = new URL(String(message.path || ''), WORKBENCH_BASE);
  } catch (_error) {
    sendResponse({ ok: false, status: 400, error: '本机请求地址无效' });
    return false;
  }
  if (url.origin !== WORKBENCH_BASE || !url.pathname.startsWith('/api/') || !ALLOWED_METHODS.has(method)) {
    sendResponse({ ok: false, status: 403, error: '本机请求不在扩展允许范围内' });
    return false;
  }
  const init = { method, cache: 'no-store', credentials: 'omit' };
  if (method !== 'GET') {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(message.payload || {});
  }
  fetch(url.href, init)
    .then(async response => {
      const body = await response.json().catch(() => null);
      sendResponse({
        ok: response.ok,
        status: response.status,
        body,
        error: response.ok ? '' : (body?.error || body?.stderr || `HTTP ${response.status}`)
      });
    })
    .catch(error => sendResponse({
      ok: false,
      status: 0,
      error: error?.message || '本机 8765 服务未连接',
      transport_error: 'network'
    }));
  return true;
});
