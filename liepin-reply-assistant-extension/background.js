chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    liepinReplyAssistantInstalledAt: new Date().toISOString()
  });
});
