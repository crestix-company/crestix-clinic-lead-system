// service-worker-v3.js - Clinic Collector v5.5
// 取得専用。コムデスクとの照合・統合は別アプリ側で行う。

self.addEventListener('error', event => {
  console.error('[service-worker error]', {
    message: event.message,
    filename: event.filename,
    lineno: event.lineno,
    colno: event.colno,
    error: event.error && event.error.stack ? event.error.stack : event.error
  });
});

self.addEventListener('unhandledrejection', event => {
  console.error('[service-worker unhandledrejection]', {
    reason: event.reason && event.reason.stack ? event.reason.stack : event.reason
  });
});

if (typeof chrome !== 'undefined' && chrome.runtime?.onMessage?.addListener) {
  const originalAddOnMessageListener = chrome.runtime.onMessage.addListener.bind(chrome.runtime.onMessage);
  chrome.runtime.onMessage.addListener = listener => {
    originalAddOnMessageListener((message, sender, sendResponse) => {
      try {
        return listener(message, sender, sendResponse);
      } catch (error) {
        console.error('[service-worker onMessage error]', {
          message,
          sender,
          error: error && error.stack ? error.stack : error
        });
        try { sendResponse({ ok: false, error: String(error?.message || error) }); } catch (_) {}
        return true;
      }
    });
  };
}

try {
  importScripts('background.js');
} catch (e) {
  console.error('[v5 SW] background.js の読み込みに失敗:', e);
}

try {
  importScripts('orchestrator.js');
} catch (e) {
  console.error('[v5 SW] orchestrator.js の読み込みに失敗:', e);
}

try { importScripts('queue-logic.js'); importScripts('queue-mode.js'); } catch(e) { console.error('[v5.7 queue] load failed', e); }
