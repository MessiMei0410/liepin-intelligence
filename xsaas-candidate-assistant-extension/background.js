chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    xsaasCandidateAssistantInstalledAt: new Date().toISOString()
  });
});
