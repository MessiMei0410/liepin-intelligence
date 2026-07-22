const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  });
  const page = await browser.newPage();
  await page.setContent(`
    <div class="im-ui-contact-list-item" data-tlg-ext="%7B%22to_imid%22%3A%22chat-zhanghang%22%7D">
      <span class="im-ui-contact-title-main">张航</span>
      <span class="im-ui-contact-title-sub">高级机械设计工程师</span>
    </div>
    <div class="im-ui-message-list-wrapper">
      <div class="im-ui-message-item-body im-ui-message-item-send" data-message-id="sent-1" data-message-time="2026-07-14T14:00:00+08:00">
        猎头发出：长越机械岗位 <span class="read-status">已读</span>
      </div>
      <div class="im-ui-message-item-body im-ui-message-item-receive" data-message-id="received-1" data-message-time="2026-07-14T14:05:00+08:00">
        候选人回复：可以了解 <span class="time">14:05</span>
      </div>
    </div>
  `);
  await page.addScriptTag({ path: path.resolve(__dirname, '../liepin-reply-assistant-extension/message-evidence.js') });
  const result = await page.evaluate(() => {
    const api = window.LIEPIN_MESSAGE_EVIDENCE;
    const before = api.conversationIdentity(document, location);
    const received = api.directionalMessage(document, 'received', { capturedAt: '2026-07-14T14:06:00+08:00' });
    const sent = api.directionalMessage(document, 'sent', { capturedAt: '2026-07-14T14:06:00+08:00' });
    const contact = document.querySelector('.im-ui-contact-list-item');
    contact.dataset.tlgExt = '%7B%22to_imid%22%3A%22chat-other%22%7D';
    contact.querySelector('.im-ui-contact-title-main').textContent = '李先生';
    const after = api.conversationIdentity(document, location);
    return {
      ok: received?.evidence === 'explicit_inbound_dom'
        && sent?.evidence === 'explicit_outbound_dom'
        && received?.messageId === 'received-1'
        && sent?.messageId === 'sent-1',
      received: received?.text || '',
      sent: sent?.text || '',
      conversationId: before.conversationId,
      conversationSwitchMatches: api.conversationSnapshotMatches(before, after)
    };
  });
  await browser.close();
  process.stdout.write(`${JSON.stringify(result)}\n`);
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exit(1);
});
