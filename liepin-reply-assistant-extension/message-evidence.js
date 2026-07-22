(function initLiepinMessageEvidence(globalScope, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (globalScope) globalScope.LIEPIN_MESSAGE_EVIDENCE = api;
})(typeof window !== 'undefined' ? window : globalThis, function buildMessageEvidenceApi() {
  'use strict';

  const MESSAGE_SELECTORS = {
    received: [
      '.im-ui-message-list-wrapper .im-ui-message-item-body.im-ui-message-item-receive',
      '.im-ui-chat-list .im-ui-message-item-body.im-ui-message-item-receive',
      '[class*="message-list"] [class*="message-item-receive"]',
      '[class*="chat-list"] [class*="message-item-receive"]'
    ],
    sent: [
      '.im-ui-message-list-wrapper .im-ui-message-item-body.im-ui-message-item-send',
      '.im-ui-chat-list .im-ui-message-item-body.im-ui-message-item-send',
      '[class*="message-list"] [class*="message-item-send"]',
      '[class*="chat-list"] [class*="message-item-send"]'
    ]
  };

  const ACTIVE_CONTACT_SELECTORS = [
    '.im-ui-contact-list-item.active',
    '.im-ui-contact-list-item.is-active',
    '.im-ui-contact-list-item.selected',
    '.im-ui-contact-list-item[class*="active"]',
    '.im-ui-contact-list-item[class*="selected"]',
    '[class*="contact-list-item"][class*="active"]',
    '[class*="contact-list-item"][aria-selected="true"]'
  ];

  function clean(value) {
    return String(value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
  }

  function stableHash(value) {
    let hash = 2166136261;
    for (const char of String(value || '')) {
      hash ^= char.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16).padStart(8, '0');
  }

  function visibleByDefault(element) {
    if (!element) return false;
    if (element.hidden) return false;
    const style = element.ownerDocument?.defaultView?.getComputedStyle?.(element);
    return !style || (style.display !== 'none' && style.visibility !== 'hidden');
  }

  function cleanMessageText(element) {
    if (!element) return '';
    const clone = element.cloneNode(true);
    clone.querySelectorAll?.('[class*="extra-info"], [class*="read-status"], [class*="time"]').forEach(node => node.remove());
    return clean(clone.textContent || element.textContent)
      .replace(/^(?:今天|昨天)?\s*\d{1,2}:\d{2}\s*/, '')
      .replace(/\s*(已读|未读)$/g, '')
      .trim();
  }

  function attributeValue(element, names) {
    let current = element;
    for (let depth = 0; current && depth < 4; depth += 1, current = current.parentElement) {
      for (const name of names) {
        const value = clean(current.getAttribute?.(name));
        if (value) return value;
      }
    }
    return '';
  }

  function directionalMessage(documentRef, direction, options = {}) {
    const selectors = MESSAGE_SELECTORS[direction] || [];
    const isVisible = options.isVisible || visibleByDefault;
    const seen = new Set();
    const items = [];
    selectors.forEach(selector => {
      documentRef.querySelectorAll(selector).forEach(item => {
        if (!seen.has(item) && isVisible(item)) {
          seen.add(item);
          items.push(item);
        }
      });
    });
    for (let index = items.length - 1; index >= 0; index -= 1) {
      const item = items[index];
      const text = cleanMessageText(item);
      if (!text || text.length < 2 || /沟通职位|推荐职位|索要简历|索要手机|索要微信/.test(text)) continue;
      const explicitTime = attributeValue(item, ['data-message-time', 'data-time', 'datetime'])
        || clean(item.querySelector?.('time, [class*="time"]')?.getAttribute?.('datetime'))
        || clean(item.querySelector?.('time, [class*="time"]')?.textContent);
      const capturedAt = options.capturedAt || new Date().toISOString();
      const messageTime = explicitTime || capturedAt;
      const explicitId = attributeValue(item, ['data-message-id', 'data-msg-id', 'data-id', 'id']);
      const messageId = explicitId || `dom-${stableHash([direction, text, messageTime].join('|'))}`;
      return {
        direction,
        text,
        messageId,
        messageTime,
        capturedAt,
        evidence: direction === 'received' ? 'explicit_inbound_dom' : 'explicit_outbound_dom',
        idConfidence: explicitId ? 'dom_id' : 'content_fingerprint',
        timeConfidence: explicitTime ? 'dom_time' : 'captured_at'
      };
    }
    return null;
  }

  function activeContact(documentRef) {
    for (const selector of ACTIVE_CONTACT_SELECTORS) {
      const found = Array.from(documentRef.querySelectorAll(selector)).find(visibleByDefault);
      if (found) return found;
    }
    return Array.from(documentRef.querySelectorAll('.im-ui-contact-list-item, [class*="contact-list-item"]'))
      .find(visibleByDefault) || null;
  }

  function tlgConversationId(contact) {
    const encoded = clean(contact?.getAttribute?.('data-tlg-ext'));
    if (!encoded) return '';
    try {
      const parsed = JSON.parse(decodeURIComponent(encoded));
      return clean(parsed?.to_imid || parsed?.toImid || parsed?.conversation_id);
    } catch (_) {
      return '';
    }
  }

  function conversationIdentity(documentRef, locationLike = {}) {
    const contact = activeContact(documentRef);
    const params = new URLSearchParams(clean(locationLike.search));
    const urlId = ['conversationId', 'conversation_id', 'imId', 'im_id', 'chatId', 'chat_id', 'res_id_encode']
      .map(key => clean(params.get(key)))
      .find(Boolean);
    const domId = attributeValue(contact, [
      'data-conversation-id', 'data-conversationid', 'data-chat-id', 'data-im-id',
      'data-target-uid', 'data-user-id', 'data-res-id', 'data-id'
    ]) || tlgConversationId(contact);
    const name = clean(contact?.querySelector?.('.im-ui-contact-title-main, [class*="title-main"], [class*="name"]')?.textContent);
    const title = clean(contact?.querySelector?.('.im-ui-contact-title-sub, [class*="title-sub"], [class*="position"], [class*="sub"]')?.textContent);
    const fallback = [name, title].filter(Boolean).join('|');
    const conversationId = domId || urlId || (fallback ? `contact-${stableHash(fallback)}` : '');
    return {
      conversationId,
      confidence: domId ? 'dom_id' : urlId ? 'url_id' : fallback ? 'stable_contact_fallback' : 'missing',
      candidateName: name,
      candidateTitle: title,
      snapshotKey: [conversationId, name, title].join('|')
    };
  }

  function conversationSnapshotMatches(snapshot, current) {
    return Boolean(snapshot?.snapshotKey && current?.snapshotKey && snapshot.snapshotKey === current.snapshotKey);
  }

  return {
    clean,
    stableHash,
    cleanMessageText,
    directionalMessage,
    conversationIdentity,
    conversationSnapshotMatches
  };
});
