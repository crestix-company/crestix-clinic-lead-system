// orchestrator.js  v3.1
// ============================================================
// v3.1 変更点（v3.0 からの差分のみ）
//   [FIX-1] content.js 未注入時の "Could not establish connection" エラー対策
//           → navigateTabTo 後に content.js への ping リトライを挟み
//             注入完了を確認してから startScraping を送信する
//   [FIX-2] waitForComboDone に無完了タイムアウト（30分）を追加し無限待機を防止
//   [FIX-3] v3Drive() 重複実行防止フラグ (driveRunning) を追加
//   [FIX-4] SW 再起動後の自動再開で既存ドライブと衝突しないよう制御
//   [SPEED-1] waitForTabComplete: DOM 完了後の固定待ち 1500ms → 800ms
//   [SPEED-2] content.js ping リトライ間隔を 200ms に短縮
//   [SPEED-3] navigateTabTo: waitForTabComplete のタイムアウト 30s → 20s
// ============================================================

// ---- storage key 名前空間 ----------------------------------
const V3K = {
  state: 'v3_state',           // 'idle' | 'running' | 'paused' | 'done' | 'error'
  city: 'v3_city',
  areas: 'v3_areas',
  genres: 'v3_genres',
  totalAreas: 'v3_totalAreas',
  totalGenres: 'v3_totalGenres',
  areaIdx: 'v3_areaIdx',
  genreIdx: 'v3_genreIdx',
  currentArea: 'v3_currentArea',
  currentGenre: 'v3_currentGenre',
  currentUrl: 'v3_currentUrl',
  currentKw: 'v3_currentKeyword',
  logs: 'v3_logs',
  collected: 'v3_collectedData',
  startTime: 'v3_startTime',
  comboDurations: 'v3_comboDurations',
  tabId: 'v3_tabId',
  maxItems: 'v3_maxItems',
  comboStart: 'v3_comboStart',
  scrapeMode: 'v3_scrapeMode',
  rangeMode: 'v3_rangeMode'
};

const V3_LOG_MAX = 500;
const V3_MODE_CONFIG = {
  fast: { label: '高速', cityMax: 30, subAreaMax: 5, maxScrolls: 30, maxEmptyScrolls: 2, timeoutMs: 5 * 60 * 1000, minScore: 50, verificationPasses: 1, scrollDelayMs: 1000 },
  standard: { label: '標準', cityMax: 80, subAreaMax: 10, maxScrolls: 100, maxEmptyScrolls: 6, timeoutMs: 25 * 60 * 1000, minScore: 30, verificationPasses: 2, scrollDelayMs: 1600 },
  exhaustive: { label: '精密・網羅', cityMax: Number.MAX_SAFE_INTEGER, subAreaMax: Number.MAX_SAFE_INTEGER, maxScrolls: 600, maxEmptyScrolls: 15, timeoutMs: 120 * 60 * 1000, minScore: -Infinity, verificationPasses: 7, scrollDelayMs: 2200 }
};

// ---- [FIX-3] 重複ドライブ防止 ------------------------------
let driveRunning = false;

// ---- 共通ユーティリティ ------------------------------------
function v3Get(keys) {
  return new Promise(r => chrome.storage.local.get(keys, r));
}
function v3Set(obj) {
  return new Promise(r => chrome.storage.local.set(obj, r));
}

function safeTabSendMessage(tabId, message) {
  return new Promise(resolve => {
    try {
      chrome.tabs.sendMessage(tabId, message, response => {
        if (chrome.runtime.lastError) {
          resolve(null);
          return;
        }
        resolve(response || null);
      });
    } catch (_) {
      resolve(null);
    }
  });
}

function v3Timestamp() {
  const d = new Date();
  const z = n => String(n).padStart(2, '0');
  return `${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`;
}

async function v3Log(message) {
  const r = await v3Get([V3K.logs]);
  const logs = Array.isArray(r[V3K.logs]) ? r[V3K.logs] : [];
  logs.push({ t: v3Timestamp(), msg: message });
  if (logs.length > V3_LOG_MAX) logs.splice(0, logs.length - V3_LOG_MAX);
  await v3Set({ [V3K.logs]: logs });
  // [FIX] chrome.runtime.sendMessage(...)はコールバックを渡さない場合Promiseを
  // 返すMV3の仕様上、同期的なtry/catchでは非同期に発生するreject（ポップアップが
  // 閉じていて受信側が存在しない場合の「Could not establish connection.
  // Receiving end does not exist.」）を捕捉できない。v3Log()はスクロール1回ごと
  // など非常に高頻度に呼ばれるため、ポップアップを閉じたまま長時間の巡回（寿司・
  // パン屋のように結果件数が多くスクロール回数が増えるジャンルで特に顕著）を
  // させていると、このエラーがコンソールに大量に出続けていた。処理自体への
  // 影響はないが、本来のエラーが埋もれる原因になるため.catch()で確実に握りつぶす。
  chrome.runtime.sendMessage({ action: 'v3_logPush', entry: logs[logs.length - 1] }).catch(() => { });
}

// ---- offscreen document ------------------------------------
const OFFSCREEN_PATH = 'offscreen.html';
let creatingOffscreen = null;

async function hasOffscreen() {
  if (!chrome.offscreen || !chrome.runtime.getContexts) return false;
  try {
    const contexts = await chrome.runtime.getContexts({
      contextTypes: ['OFFSCREEN_DOCUMENT'],
      documentUrls: [chrome.runtime.getURL(OFFSCREEN_PATH)]
    });
    return contexts.length > 0;
  } catch (e) {
    return false;
  }
}

async function ensureOffscreen() {
  if (!chrome.offscreen) return false;
  if (await hasOffscreen()) return true;
  if (creatingOffscreen) { await creatingOffscreen; return true; }
  try {
    creatingOffscreen = chrome.offscreen.createDocument({
      url: OFFSCREEN_PATH,
      reasons: ['BLOBS'],
      justification: 'v3 バックグラウンド継続実行のためのキープアライブ／タイマー'
    });
    await creatingOffscreen;
    creatingOffscreen = null;
    return true;
  } catch (e) {
    creatingOffscreen = null;
    console.warn('[v3] offscreen create error:', e);
    return false;
  }
}

async function closeOffscreen() {
  if (!chrome.offscreen) return;
  if (await hasOffscreen()) {
    try { await chrome.offscreen.closeDocument(); } catch (_) { }
  }
}

// ---- areas.json / genres.json ロード -----------------------
async function loadJson(path) {
  const url = chrome.runtime.getURL(path);
  const res = await fetch(url);
  return res.json();
}

function normalizeAreaText(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/\s+/g, '')
    .trim();
}

function parseAreaInput(value) {
  const normalized = normalizeAreaText(value).replace(/駅周辺|エリア|付近/g, '');
  if (!normalized) return { prefecture: '', city: '', subArea: '' };

  const match = normalized.match(/^((?:北海道|東京都|大阪府|京都府|.{2,3}県))?((?:.+?郡.+?[町村]|.+?市.+?区|.+?[市区町村]))?(.*)?$/);
  const prefecture = match?.[1] || '';
  // [FIX] 市区町村名が「〜市/〜区/〜町/〜村」で終わっていない入力（例:「銚子」で
  // 「銚子市」ではない場合）だと市区町村グループ(match[2])がマッチせず、
  // 以前はここで normalized（元の文字列全体、都道府県名を含んだまま）を
  // そのままcityにフォールバックしていたため、都道府県名がcityに二重に
  // 混入していた（例:「千葉県銚子」）。この文字列がbuildGoogleMapsSearchQuery()で
  // 「千葉県 千葉県銚子」として検索クエリに使われ、content.js側の同種の
  // フォールバックとも重なって三重混入（「千葉県千葉県千葉県銚子」）にまで
  // 膨れ上がり、該当エリアの店舗が全件「エリア外除外」される深刻な不具合の
  // 原因になっていた（銚子市データで確認）。都道府県名を除いた残り部分だけを
  // cityフォールバックとして使う。
  const remainder = prefecture ? normalized.slice(prefecture.length) : normalized;
  return {
    prefecture,
    city: match?.[2] || remainder,
    subArea: ''
  };
}

function areaDisplayName(targetArea) {
  if (!targetArea || typeof targetArea === 'string') return String(targetArea || '');
  return targetArea.subAreaLabel || targetArea.subArea || targetArea.city || targetArea.prefecture || '';
}

function buildTargetArea(baseArea, selectedArea = '') {
  const normalizedSelected = normalizeAreaText(selectedArea);
  const target = { ...baseArea, subArea: '' };

  if (!normalizedSelected) return target;
  if (!target.city) {
    target.city = selectedArea;
    return target;
  }
  if (normalizedSelected === normalizeAreaText(baseArea.city)) return target;
  if (normalizedSelected === normalizeAreaText(`${baseArea.prefecture}${baseArea.city}`)) return target;

  target.subArea = selectedArea;
  return target;
}

function normalizeRangeMode(value) {
  return ['split', 'whole', 'manual'].includes(value) ? value : 'split';
}

function normalizeScrapeMode(value) {
  return V3_MODE_CONFIG[value] ? value : 'standard';
}

function buildGoogleMapsSearchQuery(targetArea, genre) {
  const parts = [];
  if (targetArea.prefecture) parts.push(targetArea.prefecture);
  if (targetArea.city) parts.push(targetArea.city);
  if (targetArea.subArea) parts.push(targetArea.subArea);
  if (genre) parts.push(genre);
  return parts.join(' ');
}

async function getSmallAreaConfig(rawCity) {
  const data = await loadJson('config/areas.json');
  const parsed = parseAreaInput(rawCity);
  const pref = parsed.prefecture || '';
  const city = parsed.city || normalizeAreaText(rawCity);
  return data?.smallAreas?.[pref]?.[city] || null;
}

async function getAreasForCity(rawCity) {
  const data = await loadJson('config/areas.json');
  if (!data || !data.cities) return [];
  const cities = data.cities;

  const parsed = parseAreaInput(rawCity);
  const city = parsed.city || normalizeAreaText(rawCity);
  if (!city) return [];

  const smallAreaConfig = data?.smallAreas?.[parsed.prefecture || '']?.[city];
  if (smallAreaConfig?.subAreas?.length) {
    return smallAreaConfig.subAreas.map(area => area.label);
  }

  if (cities[city]) return cities[city].slice();
  const noSuffix = city.replace(/[市区]$/, '');
  for (const key of Object.keys(cities)) {
    if (key === city || key.replace(/[市区]$/, '') === noSuffix) return cities[key].slice();
  }
  return [city];
}

async function buildTargetAreasForRun(city, selectedAreas, rangeMode) {
  const baseArea = parseAreaInput(city || '');
  const mode = normalizeRangeMode(rangeMode);

  if (mode === 'whole') return [baseArea];

  const smallAreaConfig = await getSmallAreaConfig(city || '');
  if (mode === 'split' && smallAreaConfig?.subAreas?.length) {
    const selected = new Set((selectedAreas || []).filter(Boolean));
    const targetDefs = selected.size
      ? smallAreaConfig.subAreas.filter(area => selected.has(area.label))
      : smallAreaConfig.subAreas;
    return targetDefs.flatMap(area =>
      (area.keywords || [area.label]).map(keyword => ({
        ...baseArea,
        subArea: keyword,
        subAreaLabel: area.label,
        rangeMode: mode
      }))
    );
  }

  const manualAreas = selectedAreas && selectedAreas.length ? selectedAreas : [];
  if (manualAreas.length) {
    return manualAreas.map(area => ({ ...buildTargetArea(baseArea, area), rangeMode: mode }));
  }

  return [{ ...baseArea, rangeMode: mode }];
}

async function getGenres() {
  const data = await loadJson('config/genres.json');
  return (data && Array.isArray(data.genres)) ? data.genres.slice() : [];
}

// ---- タブ管理 ----------------------------------------------
async function ensureMapTab() {
  const r = await v3Get([V3K.tabId]);
  let tabId = r[V3K.tabId];
  if (tabId) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t) {
        await safeSetAutoDiscardable(tabId);
        return tabId;
      }
    } catch (_) { /* タブが閉じられていた */ }
  }
  const tab = await chrome.tabs.create({ url: 'https://www.google.co.jp/maps', active: true });
  await v3Set({ [V3K.tabId]: tab.id });
  await safeSetAutoDiscardable(tab.id);
  return tab.id;
}

// [STABILITY] 長時間の非アクティブタブがChromeのメモリ管理で
// 「破棄（discard）」されると、そのままJSの実行が止まり
// 「途中で止まる」原因になる。自動破棄を明示的に無効化する。
async function safeSetAutoDiscardable(tabId) {
  try {
    await chrome.tabs.update(tabId, { autoDiscardable: false });
  } catch (_) { /* 対応していない環境は無視 */ }
}

// [SPEED-1] DOM 完了後の待ち 1500ms → 800ms → 600ms
function waitForTabComplete(tabId, timeoutMs = 20000) {
  return new Promise(resolve => {
    let done = false;
    const finish = ok => {
      if (!done) {
        done = true;
        chrome.tabs.onUpdated.removeListener(listener);
        resolve(ok);
      }
    };
    const timer = setTimeout(() => finish(false), timeoutMs);
    function listener(updatedId, changeInfo) {
      if (updatedId === tabId && changeInfo.status === 'complete') {
        clearTimeout(timer);
        // [SPEED-1] React/Maps 描画待ち: 800ms → 600ms
        setTimeout(() => finish(true), 600);
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function navigateTabTo(tabId, url) {
  await chrome.tabs.update(tabId, { url, active: false });
  return waitForTabComplete(tabId);
}

// ---- [FIX-1] content.js 注入確認（ping リトライ付き） -----
// content.js が DOM に注入されるまで最大 retryMs を待つ。
// 「Receiving end does not exist」エラーはここで吸収する。
async function waitForContentScript(tabId, retryMs = 12000, intervalMs = 150) {
  const deadline = Date.now() + retryMs;
  while (Date.now() < deadline) {
    try {
      const res = await new Promise((resolve, reject) => {
        chrome.tabs.sendMessage(tabId, { action: 'ping' }, resp => {
          if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
          else resolve(resp);
        });
      });
      if (res && res.alive) return true;
    } catch (_) {
      // content.js 未注入 or SW との接続なし → [SPEED-2] 200ms 待って再試行
    }
    await new Promise(r => setTimeout(r, intervalMs));
  }
  return false;
}

// ---- v5: 収集済みデータの重複排除 ---------------------------
// 同じクリニックが別ジャンル・別小エリア・一時停止後の再開で再取得されても、
// Google Maps店舗URLを主キーとして1行にまとめる。URLが取れない場合だけ
// 名前+電話+住所をフォールバックキーにする。既存の非空値を消さず、新しく
// 取得できた非空値で補完/更新する。
function clinicRecordKey(item) {
  const url = String(item?.url || '').trim();
  if (url) return `url:${url.split('?')[0]}`;
  const name = String(item?.name || '').normalize('NFKC').replace(/\s+/g, '');
  const phone = String(item?.phone || '').replace(/\D/g, '').replace(/^0/, '');
  const address = String(item?.address || '').normalize('NFKC').replace(/[\s　-]/g, '');
  return `fallback:${name}|${phone}|${address}`;
}

function mergeUniqueClinicRecords(existing, incoming) {
  const map = new Map();
  const absorb = (row) => {
    if (!row) return;
    const key = clinicRecordKey(row);
    const prev = map.get(key);
    if (!prev) { map.set(key, { ...row }); return; }
    const merged = { ...prev };
    for (const [k, v] of Object.entries(row)) {
      if (v !== '' && v !== null && v !== undefined) merged[k] = v;
    }
    // 公式HPは一度取れた値を、後の「未取得」で消さない。
    if (prev.websiteUrl && !row.websiteUrl) merged.websiteUrl = prev.websiteUrl;
    if (prev.hasWebsiteStatus === 'has_website' && row.hasWebsiteStatus !== 'has_website') {
      merged.hasWebsiteStatus = prev.hasWebsiteStatus;
      merged.hasWebsite = prev.hasWebsite;
    }
    map.set(key, merged);
  };
  (existing || []).forEach(absorb);
  (incoming || []).forEach(absorb);
  return [...map.values()];
}

// ---- [FIX-2] waitForComboDone: 無進捗タイムアウト付き ----------
function waitForComboDone(area, genre, tabId, timeoutMs = 1800000) { // 30分
  return new Promise(resolve => {
    const areaLabel = areaDisplayName(area);
    let lastReportedCount = 0;
    let timedOut = false;
    let timeoutTimer = null;

    const resetTimeout = () => {
      if (timeoutTimer) clearTimeout(timeoutTimer);
      timeoutTimer = setTimeout(async () => {
        timedOut = true;
        chrome.storage.onChanged.removeListener(handler);
        await safeTabSendMessage(tabId, { action: 'stopScraping' });
        await v3Log(`⚠ ${areaLabel} ${genre} タイムアウト（30分間完了通知なし）`);
        resolve([]);
      }, timeoutMs);
    };

    const handler = async (changes, ns) => {
      if (ns !== 'local' || timedOut) return;

      if (changes.scrapingState && changes.scrapingState.newValue === 'active') {
        resetTimeout();
      }

      // 進捗通知
      if (changes.scrapedData) {
        resetTimeout();
        const newVal = Array.isArray(changes.scrapedData.newValue) ? changes.scrapedData.newValue : [];
        if (newVal.length !== lastReportedCount) {
          lastReportedCount = newVal.length;
          await v3Log(`取得件数 ${newVal.length}件`);
          chrome.runtime.sendMessage({ action: 'v3_progress' }).catch(() => { });
        }
      }

      // 完了通知 / ユーザー停止通知
      if (changes.scrapingState && ['done', 'stopped_by_user'].includes(changes.scrapingState.newValue)) {
        clearTimeout(timeoutTimer);
        chrome.storage.onChanged.removeListener(handler);

        const r = await v3Get(['scrapedData', V3K.collected]);
        const fresh = Array.isArray(r.scrapedData) ? r.scrapedData : [];
        const collected = Array.isArray(r[V3K.collected]) ? r[V3K.collected] : [];

        const enriched = fresh.map(it => ({
          ...it,
          searchGenre: it.searchGenre || genre,
          area: areaLabel,
          subArea: it.subArea || area.subAreaLabel || area.subArea || '',
          rangeMode: it.rangeMode || area.rangeMode || ''
        }));
        const merged = mergeUniqueClinicRecords(collected, enriched);
        await v3Set({ [V3K.collected]: merged });
        const statusLabel = changes.scrapingState.newValue === 'stopped_by_user' ? '一時停止時点まで保存' : '完了';
        await v3Log(`${genre} ${statusLabel} (本コンボ ${enriched.length}件 / 累計 ${merged.length}件)`);
        chrome.runtime.sendMessage({ action: 'v3_progress' }).catch(() => { });
        resolve(enriched);
      }
    };

    resetTimeout();
    chrome.storage.onChanged.addListener(handler);
  });
}

// ---- [ZOOM-FIX] エリア中心座標の解決＆キャッシュ ------------
// 座標なしで検索すると Google マップ側が「市区町村全体が収まる縮尺」を
// 自動選択し、広域寄りの結果になってしまう。エリアごとに1回だけ座標を
// 取得してキャッシュし、以降の同エリア×別ジャンルの検索では
// 「その座標＋固定ズーム」で狙った密度の範囲に固定する。
const V3_LOCAL_SEARCH_ZOOM = 14; // 徒歩圏内の店舗が拾える程度のズーム（ご指定のスクショに合わせて調整）
const areaCoordCache = new Map();

function extractLatLngZoomFromUrl(url) {
  const m = String(url || '').match(/@(-?\d+\.\d+),(-?\d+\.\d+),(\d+(?:\.\d+)?)z/);
  if (!m) return null;
  return { lat: m[1], lng: m[2], zoom: m[3] };
}

// /maps/place/<市区町村名> のURLから、Googleが実際に解決した地点名を取り出す
// （URLパスの2番目のセグメントがエンコード済みの地点名になっている）
function extractResolvedPlaceNameFromUrl(url) {
  const m = String(url || '').match(/\/maps\/place\/([^/]+)/);
  if (!m) return '';
  try { return decodeURIComponent(m[1]).replace(/\+/g, ' ').trim(); }
  catch (_) { return m[1]; }
}

// 行政区域名（〜都/道/府/県/市/区/町/村）で終わっているかどうか。
// 「鴨川シーワールド」のように市区町村名を含む観光施設名は市区町村名を
// 前方一致で含んでしまうため、部分一致だけでは見分けられない。
// 行政区域名は必ずこの語尾で終わるという性質を使って判定する。
function looksLikeAdministrativeAreaName(name) {
  return /(都|道|府|県|市|区|町|村)$/.test(String(name || '').trim());
}

async function resolveAreaCoordinates(targetArea, tabId) {
  const cacheKey = JSON.stringify({
    pref: targetArea.prefecture || '',
    city: targetArea.city || '',
    subArea: targetArea.subArea || ''
  });
  if (areaCoordCache.has(cacheKey)) return areaCoordCache.get(cacheKey);

  const searchArea = buildGoogleMapsSearchQuery(targetArea, '');
  if (!searchArea) {
    areaCoordCache.set(cacheKey, null);
    return null;
  }

  try {
    // [FIX] /maps/place/<市区町村名> だけのあいまいな検索は、Googleが
    // 「一番有名なスポット」（例:鴨川市→鴨川シーワールド）に単独ジャンプ
    // してしまうことがある。その場合は市区町村とは無関係な地点の座標になり、
    // 以降の全ジャンル検索がその観光地付近だけに偏る/取得が止まって見える
    // 原因になっていた。/maps/search/ は複数候補を並べる検索エンドポイント
    // で、実際のジャンル検索でも問題なく使われている形式のため、こちらに
    // 統一して単独ジャンプのリスクを下げる。
    const placeUrl = `https://www.google.co.jp/maps/search/${encodeURIComponent(searchArea)}`;
    await chrome.tabs.update(tabId, { url: placeUrl, active: false });
    await waitForTabComplete(tabId);
    const tab = await chrome.tabs.get(tabId);

    // それでも単独の場所ページへジャンプしてしまった場合の保険。
    // 行政区域名（都/道/府/県/市/区/町/村で終わる）ではない地点名に
    // 解決された場合は、無関係な観光施設等に着地したとみなして信用しない。
    // （「鴨川シーワールド」のように市区町村名を前方一致で含む施設名もある
    // ため、部分一致ではなく語尾の行政区域サフィックスで判定する）
    const resolvedName = extractResolvedPlaceNameFromUrl(tab.url);
    const nameLooksUnrelated = resolvedName && !looksLikeAdministrativeAreaName(resolvedName);

    if (nameLooksUnrelated) {
      await v3Log(`⚠ 「${searchArea}」が無関係な地点「${resolvedName}」に解決されたため、座標なしで検索を続行します`);
      areaCoordCache.set(cacheKey, null);
      return null;
    }

    const coords = extractLatLngZoomFromUrl(tab.url);
    if (coords) {
      await v3Log(`📍 「${searchArea}」の中心座標を取得: ${coords.lat}, ${coords.lng}`);
    } else {
      await v3Log(`⚠ 「${searchArea}」の中心座標を取得できませんでした（座標なしで検索を続行します）`);
    }
    areaCoordCache.set(cacheKey, coords);
    return coords;
  } catch (e) {
    await v3Log(`⚠ 中心座標の取得に失敗: ${e.message}`);
    areaCoordCache.set(cacheKey, null);
    return null;
  }
}

// ---- 1コンボ実行 -------------------------------------------
async function runCombo(area, genre, runOptions = {}) {
  const targetArea = typeof area === 'string' ? parseAreaInput(area) : area;
  const areaLabel = areaDisplayName(targetArea);
  const searchArea = buildGoogleMapsSearchQuery(targetArea, '');
  const keyword = buildGoogleMapsSearchQuery(targetArea, genre);
  const scrapeMode = normalizeScrapeMode(runOptions.scrapeMode);
  const rangeMode = normalizeRangeMode(runOptions.rangeMode || targetArea.rangeMode);
  const modeConfig = V3_MODE_CONFIG[scrapeMode];
  if (!targetArea.city || !genre || (targetArea.subArea && !targetArea.prefecture)) {
    await v3Log(`⚠ ${areaLabel || '-'} ${genre || '-'} 検索条件が不完全なためスキップ`);
    return { count: 0, items: [] };
  }

  const stateBeforeCombo = await v3Get([V3K.state]);
  if (stateBeforeCombo[V3K.state] !== 'running') return { count: 0, items: [] };

  const tabId = await ensureMapTab();
  const areaCoords = await resolveAreaCoordinates(targetArea, tabId);
  const stateAfterCoords = await v3Get([V3K.state]);
  if (stateAfterCoords[V3K.state] !== 'running') return { count: 0, items: [] };
  const url = areaCoords
    ? `https://www.google.co.jp/maps/search/${encodeURIComponent(keyword)}/@${areaCoords.lat},${areaCoords.lng},${V3_LOCAL_SEARCH_ZOOM}z`
    : `https://www.google.co.jp/maps/search/${encodeURIComponent(keyword)}`;

  await v3Set({
    [V3K.currentArea]: areaLabel,
    [V3K.currentGenre]: genre,
    [V3K.currentKw]: keyword,
    [V3K.currentUrl]: url,
    [V3K.comboStart]: Date.now(),
    scrapedData: [],
    scrapingState: 'inactive'
  });

  await v3Log(`${keyword} 開始`);

  const ok = await navigateTabTo(tabId, url);
  if (!ok) {
    await v3Log(`⚠ ${keyword} ページ読み込みタイムアウト`);
    return { count: 0, items: [] };
  }

  // ページ遷移直後はcontent.js再注入に時間がかかるため待機
  // [修正8] waitForContentScript が 150ms 間隔リトライ済みのため固定待ちを 800ms → 300ms に短縮
  await new Promise(r => setTimeout(r, 1500));

  const stateAfterNav = await v3Get([V3K.state]);
  if (stateAfterNav[V3K.state] !== 'running') return { count: 0, items: [] };

  const contentReady = await waitForContentScript(tabId, 15000, 200);
  if (!contentReady) {
    await v3Log(`⚠ ${keyword} content.js 未応答（スキップ）`);
    return { count: 0, items: [] };
  }

  const maxItemsR = await v3Get([V3K.maxItems]);
  const storedMax = maxItemsR[V3K.maxItems];
  const modeMax = rangeMode === 'whole' ? modeConfig.cityMax : modeConfig.subAreaMax;
  // 精密・網羅モードは件数上限を設けない。Google Mapsが検索結果として返した全URLを処理する。
  const maxItems = scrapeMode === 'exhaustive' ? 0 : (storedMax === 0 ? 0 : Math.min(Number(storedMax ?? modeMax), modeMax));

  for (let attempt = 1; attempt <= 2; attempt++) {
    const stateBeforeStart = await v3Get([V3K.state]);
    if (stateBeforeStart[V3K.state] !== 'running') return { count: 0, items: [] };
    try {
      await new Promise((resolve, reject) => {
        chrome.tabs.sendMessage(tabId, {
          action: 'startScraping',
          maxItems,
          targetGenres: [],
          filterConfig: { enabled: false },
          searchArea,
          searchGenre: genre,
          scrapeOptions: {
            scrapeMode,
            scrapeModeLabel: modeConfig.label,
            rangeMode,
            maxScrolls: modeConfig.maxScrolls,
            maxEmptyScrolls: modeConfig.maxEmptyScrolls,
            verificationPasses: modeConfig.verificationPasses,
            scrollDelayMs: modeConfig.scrollDelayMs,
            minScore: modeConfig.minScore,
            subArea: targetArea.subArea || '',
            subAreaLabel: targetArea.subAreaLabel || '',
            targetZoom: areaCoords ? V3_LOCAL_SEARCH_ZOOM : null
          }
        }, response => {
          if (chrome.runtime.lastError) reject(chrome.runtime.lastError);
          else resolve(response);
        });
      });
      break; // 成功したらループを抜ける
    } catch (e) {
      await v3Log(`⚠ ${keyword} startScraping 失敗(試行${attempt}): ${e?.message || e}`);
      if (attempt < 2) {
        await new Promise(r => setTimeout(r, 1000));
        const retryReady = await waitForContentScript(tabId, 5000, 150);
        if (!retryReady) {
          await v3Log(`⚠ ${keyword} content.js 再注入タイムアウト → スキップ`);
          return { count: 0, items: [] };
        }
      } else {
        return { count: 0, items: [] };
      }
    }
  }

  const items = await waitForComboDone(targetArea, genre, tabId, modeConfig.timeoutMs);
  return { count: items.length, items };
}

// ---- メインドライバ ----------------------------------------
async function v3Drive() {
  // [FIX-3] 重複起動防止
  if (driveRunning) {
    console.warn('[v3] v3Drive() は既に実行中です。重複起動をスキップします。');
    return;
  }
  driveRunning = true;

  try {
    const r = await v3Get([
      V3K.state, V3K.areas, V3K.genres, V3K.areaIdx, V3K.genreIdx,
      V3K.totalAreas, V3K.totalGenres, V3K.comboDurations, 'v3_runId',
      V3K.city, V3K.scrapeMode, V3K.rangeMode
    ]);
    if (r[V3K.state] !== 'running') return;

    const areas = r[V3K.areas] || [];
    const genres = r[V3K.genres] || [];
    let areaIdx = r[V3K.areaIdx] || 0;
    let genreIdx = r[V3K.genreIdx] || 0;
    const durations = Array.isArray(r[V3K.comboDurations]) ? r[V3K.comboDurations] : [];
    const runId = r.v3_runId;
    const scrapeMode = normalizeScrapeMode(r[V3K.scrapeMode]);
    const rangeMode = normalizeRangeMode(r[V3K.rangeMode]);
    const cityForDownload = r[V3K.city] || '';

    while (true) {
      const cur = await v3Get([V3K.state]);
      if (cur[V3K.state] !== 'running') {
        await v3Log('停止しました');
        return;
      }

      if (genreIdx >= genres.length) break;
      const genre = genres[genreIdx];

      if (areaIdx >= areas.length) {
        const collectedR = await v3Get([V3K.collected]);
        const genreItems = (Array.isArray(collectedR[V3K.collected]) ? collectedR[V3K.collected] : [])
          .filter(item => item.searchGenre === genre);
        if (genreItems.length) {
          await v3Log(`✓ ${genre} 取得完了 (${genreItems.length}件)。結果は「現在までのCSVを保存」から1ファイルで出力できます。`);
        } else {
          await v3Log(`✓ ${genre} 取得対象なし`);
        }
        genreIdx++;
        areaIdx = 0;
        await v3Set({ [V3K.genreIdx]: genreIdx, [V3K.areaIdx]: 0 });
        continue;
      }

      const area = areas[areaIdx];
      const areaLabel = areaDisplayName(area);
      const currentCollectedR = await v3Get([V3K.collected]);
      const currentGenreCount = (Array.isArray(currentCollectedR[V3K.collected]) ? currentCollectedR[V3K.collected] : [])
        .filter(item => item.searchGenre === genre).length;
      if (currentGenreCount >= V3_MODE_CONFIG[scrapeMode].cityMax) {
        await v3Log(`${genre} 市区町村全体上限 ${V3_MODE_CONFIG[scrapeMode].cityMax}件に到達`);
        areaIdx = areas.length;
        await v3Set({ [V3K.areaIdx]: areaIdx });
        continue;
      }

      await v3Set({ [V3K.areaIdx]: areaIdx, [V3K.genreIdx]: genreIdx });
      const t0 = Date.now();
      let comboResult;
      try {
        comboResult = await runCombo(area, genre, { scrapeMode, rangeMode });
      } catch (e) {
        // [STABILITY] 1コンボの失敗で全体を止めない。ログを残して次へ進む。
        console.error('[v3] runCombo error:', e);
        await v3Log(`⚠ ${areaLabel} ${genre} でエラー発生のためスキップ: ${e?.message || e}`);
        comboResult = { count: 0, items: [] };
      }
      const dt = (Date.now() - t0) / 1000;
      durations.push(dt);
      await v3Set({ [V3K.comboDurations]: durations });

      // v5: 一時停止がコンボ途中で押された場合、そのコンボを「完了扱い」にしない。
      // 部分結果はwaitForComboDoneですでに保存済み。再開時に同じコンボをやり直し、
      // Google Maps URLで重複排除して残りを補完する。
      const stateAfterCombo = await v3Get([V3K.state]);
      if (stateAfterCombo[V3K.state] === 'paused') {
        await v3Log(`⏸ ${areaLabel} ${genre} を一時停止しました（途中結果 ${comboResult?.items?.length || 0}件を保存）。再開時はこの検索から続けます。`);
        chrome.runtime.sendMessage({ action: 'v3_paused' }).catch(() => {});
        return;
      }

      await v3Log(`${areaLabel} ${genre} 小エリア取得 ${comboResult?.items?.length || 0}件`);
      areaIdx++;
      await v3Set({ [V3K.areaIdx]: areaIdx });
    }

    await v3Set({ [V3K.state]: 'done' });
    await v3Log(`🎉 全エリア × 全ジャンル 取得完了。CSVを保存できます。`);
    chrome.runtime.sendMessage({ action: 'v3_done' }).catch(() => { });
    await closeOffscreen();

  } catch (e) {
    console.error('[v3] drive error:', e);
    await v3Log(`致命的エラー: ${e?.message || e}`);
    await v3Set({ [V3K.state]: 'error' });
  } finally {
    driveRunning = false;
  }
}

// ---- message handlers --------------------------------------
chrome.runtime.onMessage.addListener((req, sender, sendResponse) => {
  if (!req || !req.action) return false;

  if (req.action === 'v3_contentLog') {
    (async () => {
      await v3Log(req.message || '');
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (req.action === 'v3_getAreas') {
    (async () => {
      const areas = await getAreasForCity(req.city || '');
      sendResponse({ ok: true, areas });
    })();
    return true;
  }

  if (req.action === 'v3_getGenres') {
    (async () => {
      const genres = await getGenres();
      sendResponse({ ok: true, genres });
    })();
    return true;
  }

  if (req.action === 'v3_start') {
    (async () => {
      const { city, areas, genres, maxItems } = req;
      const scrapeMode = normalizeScrapeMode(req.scrapeMode);
      const rangeMode = normalizeRangeMode(req.rangeMode);
      let useAreas = await buildTargetAreasForRun(city || '', areas || [], rangeMode);
      const useGenres = genres && genres.length ? genres : await getGenres();
      const runId = 'v3_' + Date.now() + '_' + Math.random().toString(36).slice(2, 9);

      await v3Set({
        [V3K.state]: 'running',
        [V3K.city]: city || '',
        [V3K.areas]: useAreas,
        [V3K.genres]: useGenres,
        [V3K.totalAreas]: useAreas.length,
        [V3K.totalGenres]: useGenres.length,
        [V3K.areaIdx]: 0,
        [V3K.genreIdx]: 0,
        [V3K.logs]: [],
        [V3K.collected]: [],
        [V3K.startTime]: Date.now(),
        [V3K.comboDurations]: [],
        [V3K.maxItems]: maxItems ?? 100,
        [V3K.scrapeMode]: scrapeMode,
        [V3K.rangeMode]: rangeMode,
        scrapedData: [],
        v3_runId: runId,
        v3_stopReason: ''
      });
      await v3Log(`v3 開始: ${city || '(エリア指定なし)'} | モード ${V3_MODE_CONFIG[scrapeMode].label} | 範囲 ${rangeMode} | 小エリア ${useAreas.length} × ジャンル ${useGenres.length} | runId: ${runId}`);

      try { chrome.power.requestKeepAwake('display'); } catch (_) { }
      try { chrome.alarms.create('v3_tick', { periodInMinutes: 0.5 }); } catch (_) { }
      await ensureOffscreen();

      sendResponse({ ok: true, totalAreas: useAreas.length, totalGenres: useGenres.length });

      // [FIX-3] driveRunning フラグ付きで起動
      v3Drive().catch(async e => {
        console.error('[v3] drive error:', e);
        await v3Log(`致命的エラー: ${e?.message || e}`);
        await v3Set({ [V3K.state]: 'error' });
        driveRunning = false;
      });
    })();
    return true;
  }

  if (req.action === 'v3_pause' || req.action === 'v3_stop') {
    (async () => {
      const cur = await v3Get([V3K.state, V3K.tabId]);
      if (cur[V3K.state] !== 'running') {
        sendResponse({ ok: false, error: '現在実行中ではありません。' });
        return;
      }
      await v3Set({ [V3K.state]: 'paused', v3_stopReason: 'user_paused' });
      await v3Log('⏸ 一時停止を受け付けました。現在処理中の医院までの結果を保存します。');
      if (cur[V3K.tabId]) await safeTabSendMessage(cur[V3K.tabId], { action: 'stopScraping' });
      try { chrome.power.releaseKeepAwake(); } catch (_) { }
      try { chrome.alarms.clear('v3_tick'); } catch (_) { }
      await closeOffscreen();
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (req.action === 'v3_resume') {
    (async () => {
      const cur = await v3Get([V3K.state, V3K.areas, V3K.genres]);
      if (cur[V3K.state] !== 'paused') {
        sendResponse({ ok: false, error: '一時停止中の進捗がありません。' });
        return;
      }
      if (!Array.isArray(cur[V3K.areas]) || !cur[V3K.areas].length || !Array.isArray(cur[V3K.genres]) || !cur[V3K.genres].length) {
        sendResponse({ ok: false, error: '再開に必要な検索条件が保存されていません。' });
        return;
      }
      await v3Set({ [V3K.state]: 'running', v3_stopReason: '' });
      await v3Log('▶ 続きから再開します。停止した検索コンボを再実行し、取得済み医院は重複排除します。');
      try { chrome.power.requestKeepAwake('display'); } catch (_) { }
      try { chrome.alarms.create('v3_tick', { periodInMinutes: 0.5 }); } catch (_) { }
      await ensureOffscreen();
      sendResponse({ ok: true });
      chrome.runtime.sendMessage({ action: 'v3_resumed' }).catch(() => {});
      v3Drive().catch(async e => {
        console.error('[v5 resume] drive error:', e);
        await v3Log(`再開後の致命的エラー: ${e?.message || e}`);
        await v3Set({ [V3K.state]: 'error' });
        driveRunning = false;
      });
    })();
    return true;
  }

  if (req.action === 'v3_reset') {
    (async () => {
      driveRunning = false;
      await v3Set({
        [V3K.state]: 'idle',
        [V3K.areas]: [], [V3K.genres]: [],
        [V3K.areaIdx]: 0, [V3K.genreIdx]: 0,
        [V3K.totalAreas]: 0, [V3K.totalGenres]: 0,
        [V3K.currentArea]: '', [V3K.currentGenre]: '',
        [V3K.currentUrl]: '', [V3K.currentKw]: '',
        [V3K.logs]: [], [V3K.collected]: [],
        [V3K.startTime]: 0, [V3K.comboDurations]: [],
        [V3K.scrapeMode]: 'standard', [V3K.rangeMode]: 'split',
        scrapedData: [], scrapingState: 'inactive', v3_stopReason: ''
      });
      sendResponse({ ok: true });
    })();
    return true;
  }

  if (req.action === 'v3_getStatus') {
    (async () => {
      const r = await v3Get([
        V3K.state, V3K.city, V3K.areas, V3K.genres,
        V3K.areaIdx, V3K.genreIdx, V3K.totalAreas, V3K.totalGenres,
        V3K.currentArea, V3K.currentGenre, V3K.currentUrl, V3K.currentKw,
        V3K.logs, V3K.collected, V3K.startTime, V3K.comboDurations,
        V3K.scrapeMode, V3K.rangeMode
      ]);
      sendResponse({ ok: true, status: r });
    })();
    return true;
  }

  if (req.action === 'v3_ping') {
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

// alarms によるキープアライブ & 状態確認
chrome.alarms.onAlarm.addListener(async alarm => {
  if (alarm.name !== 'v3_tick') return;
  const r = await v3Get([V3K.state, V3K.tabId]);
  if (r[V3K.state] !== 'running') return;
  await ensureOffscreen();
  if (r[V3K.tabId]) {
    await safeTabSendMessage(r[V3K.tabId], { action: 'ping' });
  }
  // [FIX-4] SW 復帰後、v3Drive() が止まっていたら再起動
  if (!driveRunning) {
    v3Drive().catch(e => console.error('[v3 alarm] drive restart error:', e));
  }
});

// SW 起動時に "running" のままだったら自動再開
chrome.runtime.onStartup.addListener(async () => {
  const r = await v3Get([V3K.state]);
  if (r[V3K.state] === 'running') {
    await ensureOffscreen();
    // [FIX-4] driveRunning チェック付き
    if (!driveRunning) {
      v3Drive().catch(() => { });
    }
  }
});

self.addEventListener?.('activate', async () => {
  const r = await v3Get([V3K.state]);
  if (r[V3K.state] === 'running' && !driveRunning) {
    v3Drive().catch(() => { });
  }
});