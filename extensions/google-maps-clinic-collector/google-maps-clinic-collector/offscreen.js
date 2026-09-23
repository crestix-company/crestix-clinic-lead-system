// offscreen.js
// ============================================================
// Offscreen Document
//   Service Worker が idle で停止しないよう、20秒毎に SW へ ping を
//   送る。これにより別タブ移動・Chrome 最小化・他サイト閲覧中でも
//   v3 の取得処理が継続される。
// ============================================================

setInterval(() => {
  try { chrome.runtime.sendMessage({ action: 'v3_ping' }, () => void chrome.runtime.lastError); } catch(_) {}
}, 20000);

// 初回 ping（即時）
try { chrome.runtime.sendMessage({ action: 'v3_ping' }, () => void chrome.runtime.lastError); } catch(_) {}
