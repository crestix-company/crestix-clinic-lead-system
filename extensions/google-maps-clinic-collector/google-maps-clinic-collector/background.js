// background.js  v5.6.0 (クリニック29列固定出力版)

const downloadedRunIds = new Set();
const COMDESK_ONLY_EXPORT = true; // v5.5: 自動CSV出力を停止。CSVはユーザー操作で1ファイルのみ保存

// =====================================================================
// CSVヘッダー定義 (共通スキーマ + デバッグ項目)
// =====================================================================
const CSV_HEADERS = [
  '店名', 'ジャンル', '検索ジャンル', '取得元ジャンル', '都道府県', '市区町村', '住所', '電話番号',
  '定休日', '営業日', '営業開始A', '営業終了A', '営業開始B', '営業終了B',
  '営業時間原文', 'URL', 'HP有無', '媒体', '取得元URL', '取得日時'
];

function normalizeExportRecord(item) {
  return {
    name: item.name || '',
    genre: item.genre || '',
    sourceGenre: item.sourceGenre || '',
    prefecture: item.prefecture || '',
    city: item.city || '',
    subArea: item.subArea || '',
    address: item.address || '',
    phone: item.phone || '',
    regularHoliday: item.regularHoliday || '',
    businessDays: item.businessDays || '',
    openTimeA: item.openTimeA || '',
    closeTimeA: item.closeTimeA || '',
    openTimeB: item.openTimeB || '',
    closeTimeB: item.closeTimeB || '',
    rawHours: item.rawHours || '',
    url: item.url || '',
    websiteUrl: item.websiteUrl || '',
    hasWebsite: item.hasWebsite || '無',
    source: item.source || 'GoogleMap',
    sourceUrl: item.sourceUrl || '',
    scrapedAt: item.scrapedAt || '',
    searchGenre: item.searchGenre || '',
    searchKey: item.searchKey || '',
    scrapeMode: item.scrapeMode || '',
    rangeMode: item.rangeMode || '',
    acquisitionStatus: item.acquisitionStatus || '取得成功',
    excludeReason: item.excludeReason || '',
    detailRetryCount: item.detailRetryCount ?? '',
    listRank: item.listRank ?? ''
  };
}

function escapeCsvValue(value) {
  return `"${String(value ?? '').replace(/"/g, '""')}"`;
}

function buildCsvContent(data) {
  let csv = '\uFEFF' + CSV_HEADERS.join(',') + '\n';
  data.forEach(item => {
    const r = normalizeExportRecord(item);
    csv += [
      escapeCsvValue(r.name),
      escapeCsvValue(r.genre),
      escapeCsvValue(r.searchGenre),
      escapeCsvValue(r.sourceGenre),
      escapeCsvValue(r.prefecture),
      escapeCsvValue(r.city),
      escapeCsvValue(r.address),
      escapeCsvValue(r.phone),
      escapeCsvValue(r.regularHoliday),
      escapeCsvValue(r.businessDays),
      escapeCsvValue(r.openTimeA),
      escapeCsvValue(r.closeTimeA),
      escapeCsvValue(r.openTimeB),
      escapeCsvValue(r.closeTimeB),
      escapeCsvValue(r.rawHours),
      escapeCsvValue(r.url),
      escapeCsvValue(r.hasWebsite),
      escapeCsvValue(r.source),
      escapeCsvValue(r.sourceUrl),
      escapeCsvValue(r.scrapedAt)
    ].join(',') + '\n';
  });
  return csv;
}

// =====================================================================
// ファイル名生成
// 形式: [ジャンル1_ジャンル2_...]_[地域名].csv
// =====================================================================
function buildFilename(query, filterConfig, targetGenres) {
  const trimmed = (query || '').trim();
  const { area } = trimmed ? parseQueryToAreaGenre(trimmed) : { area: '' };
  const areaStr = sanitizeFilename(area);

  let genres = [];
  if (Array.isArray(targetGenres)) {
    genres = targetGenres.map(g => g.trim()).filter(Boolean);
  } else if (typeof targetGenres === 'string' && targetGenres.trim()) {
    genres = targetGenres.split(/[\n,]/).map(g => g.trim()).filter(Boolean);
  }
  const genreStr = genres.map(g => sanitizeFilename(g)).filter(Boolean).join('_');

  if (genreStr && areaStr) return `${genreStr}_${areaStr}.csv`;
  if (genreStr) return `${genreStr}.csv`;
  if (areaStr) return `${areaStr}.csv`;
  return `Googleマップ.csv`;
}

// =====================================================================
// クエリ解析（エリア / ジャンル 分離）
// =====================================================================
function parseQueryToAreaGenre(query) {
  if (query.includes('✖️') || query.includes('×')) {
    const sep = query.includes('✖️') ? '✖️' : '×';
    const parts = query.split(sep).map(s => s.trim()).filter(Boolean);
    const ai = parts.findIndex(p => isAreaToken(p));
    if (ai !== -1) return { area: parts[ai], genre: parts.find((_, i) => i !== ai) || '' };
    return { area: parts[0] || '', genre: parts[1] || '' };
  }

  const tokens = query.split(/[\s\u3000]+/).filter(Boolean);
  if (!tokens.length) return { area: '', genre: '' };
  if (tokens.length === 1) {
    return isAreaToken(tokens[0])
      ? { area: tokens[0], genre: '' }
      : { area: '', genre: tokens[0] };
  }

  let areaTokens = [], genreTokens = [], switched = false;
  for (const t of tokens) {
    if (!switched && isAreaToken(t)) areaTokens.push(t);
    else { switched = true; genreTokens.push(t); }
  }
  if (!areaTokens.length) { areaTokens = [tokens[0]]; genreTokens = tokens.slice(1); }
  return { area: areaTokens.join(''), genre: genreTokens.join('') };
}

function isAreaToken(token) {
  if (/[市区町村都府道県]$/.test(token)) return true;
  const list = [
    '北海道', '東京', '大阪', '京都', '神奈川', '愛知', '福岡', '沖縄',
    '埼玉', '千葉', '兵庫', '静岡', '茨城', '広島', '宮城',
    '渋谷', '新宿', '池袋', '銀座', '品川', '秋葉原', '浅草', '上野',
    '吉祥寺', '横浜', '梅田', '難波', '心斎橋', '天王寺', '栄',
    '名古屋', '博多', '天神', '札幌', '仙台', '神戸', '川崎', '船橋',
  ];
  return list.includes(token);
}

function sanitizeFilename(str) {
  return String(str || '')
    .replace(/[\\/:*?"<>|]/g, '')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '')
    .slice(0, 50);
}

function formatTimestamp(date = new Date()) {
  const z = n => String(n).padStart(2, '0');
  return `${date.getFullYear()}${z(date.getMonth() + 1)}${z(date.getDate())}_${z(date.getHours())}${z(date.getMinutes())}`;
}

function splitAreaText(area) {
  const text = String(area || '').replace(/\s+/g, '').trim();
  const m = text.match(/^((?:北海道|東京都|大阪府|京都府|.{2,3}県))?((?:.+?郡.+?[町村]|.+?市.+?区|.+?[市区町村]))?/);
  return {
    prefecture: m?.[1] || '',
    city: m?.[2] || text
  };
}

function dedupeByUrl(data) {
  const seen = new Set();
  return data.filter(item => {
    const url = item?.url || '';
    if (!url) return true;
    if (seen.has(url)) return false;
    seen.add(url);
    return true;
  });
}

function groupForCsvDownloads(data, fallback = {}) {
  const groups = new Map();
  const fallbackArea = splitAreaText(fallback.area || '');

  data.forEach(item => {
    const r = normalizeExportRecord(item);
    const pref = r.prefecture || fallback.prefecture || fallbackArea.prefecture || '';
    const city = r.city || fallback.city || fallbackArea.city || '';
    const genre = r.genre || fallback.genre || r.searchGenre || 'ジャンル';
    const key = [pref, city, genre].join('\u0001');

    if (!groups.has(key)) {
      groups.set(key, { prefecture: pref, city, genre, items: [] });
    }
    groups.get(key).items.push(item);
  });

  return Array.from(groups.values())
    .map(group => ({ ...group, items: dedupeByUrl(group.items) }))
    .filter(group => group.items.length > 0);
}

async function downloadGroupedCsvFiles(data, fallback = {}) {
  const timestamp = formatTimestamp();
  const groups = groupForCsvDownloads(data, fallback);

  for (const group of groups) {
    const filename = [
      'googlemaps',
      group.prefecture,
      group.city,
      group.genre,
      timestamp
    ].map(sanitizeFilename).filter(Boolean).join('_') + '.csv';
    await downloadCsvFile(group.items, filename);
    await appendV3Log(`CSV出力完了: ${filename} (${group.items.length}件)`);
  }

  return groups.length;
}

async function appendV3Log(message) {
  console.log(message);
  try {
    const current = await chrome.storage.local.get(['v3_logs']);
    const logs = Array.isArray(current.v3_logs) ? current.v3_logs : [];
    const d = new Date();
    const z = n => String(n).padStart(2, '0');
    const entry = { t: `${z(d.getHours())}:${z(d.getMinutes())}:${z(d.getSeconds())}`, msg: message };
    logs.push(entry);
    if (logs.length > 500) logs.splice(0, logs.length - 500);
    await chrome.storage.local.set({ v3_logs: logs });
    chrome.runtime.sendMessage({ action: 'v3_logPush', entry }).catch(() => { });
  } catch (_) { }
}

async function downloadTextContent(content, filename, mime = 'text/csv;charset=utf-8') {
  const encodedUri = `data:${mime},` + encodeURIComponent(content);
  const downloadId = await new Promise((resolve, reject) => {
    try {
      chrome.downloads.download({ url: encodedUri, filename, saveAs: false }, id => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message || 'chrome.downloads.download failed'));
          return;
        }
        resolve(id);
      });
    } catch (e) { reject(e); }
  });
  if (downloadId == null) throw new Error(`ダウンロードを開始できませんでした: ${filename}`);
  return downloadId;
}


// =====================================================================
// v5.6 クリニック収集専用CSV（29列固定）
// 照合・UUID付与・コムデスク28列化は別アプリで行う。
// =====================================================================
const CLINIC_RAW_HEADERS = [
  'UUID', '種別', '名前', 'カナ', '郵便番号', '都道府県', '住所１', '住所２', '住所カナ',
  'Tel1', 'Tel2', 'Tel3', 'Tel4', 'FAX', 'URL', '備考', '旧社名', 'リードソース', '履歴', '記事名',
  '休診日', '診療日', '午前始', '午前終', '午後始', '午後終', '院長名', '開業日', '整理済みTel1'
];

function cleanClinicWebsiteUrl(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  try {
    const u = new URL(raw);
    const h = u.hostname.toLowerCase().replace(/^www\./, '');
    if (
      /(^|\.)google\.(com|co\.jp)$/.test(h) ||
      h === 'maps.app.goo.gl' || h === 'goo.gl' ||
      /\/maps(?:\/|$)/i.test(u.pathname)
    ) return '';
    if (!/^https?:$/.test(u.protocol)) return '';
    return u.toString();
  } catch (_) {
    return '';
  }
}

function extractPostalCode(address) {
  const m = String(address || '').normalize('NFKC').match(/(?:〒\s*)?(\d{3})[-ー−]?([0-9]{4})/);
  return m ? `${m[1]}-${m[2]}` : '';
}

function normalizeTelForMatch(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  let digits = raw.replace(/[^0-9]/g, '');
  // Google側が +81 表記でも、国内番号の tel_match_key と同じ形に寄せる。
  if (/^\+?81/.test(raw.replace(/\s+/g, '')) && digits.startsWith('81')) {
    digits = digits.slice(2);
  }
  if (digits.startsWith('0')) digits = digits.slice(1);
  return digits;
}

function normalizeClinicRawForExport(items) {
  const map = new Map();
  for (const item of (Array.isArray(items) ? items : [])) {
    if (!item) continue;
    const gmapUrl = String(item.url || '').split('?')[0];
    const fallbackKey = [item.name || '', item.phone || '', item.address || '']
      .map(v => String(v).normalize('NFKC').replace(/\s+/g, ''))
      .join('|');
    const key = gmapUrl ? `url:${gmapUrl}` : `fallback:${fallbackKey}`;
    const rawAddress = String(item.address || '').trim();
    const rawPhone = String(item.phone || '').trim();
    const row = {
      name: String(item.name || '').trim(),
      postalCode: extractPostalCode(rawAddress),
      prefecture: String(item.prefecture || '').trim(),
      address: rawAddress,
      phone: rawPhone,
      cleanedPhone: normalizeTelForMatch(rawPhone),
      websiteUrl: cleanClinicWebsiteUrl(item.websiteUrl),
      regularHoliday: String(item.regularHoliday || '').trim(),
      businessDays: String(item.businessDays || '').trim(),
      openTimeA: String(item.openTimeA || '').trim(),
      closeTimeA: String(item.closeTimeA || '').trim(),
      openTimeB: String(item.openTimeB || '').trim(),
      closeTimeB: String(item.closeTimeB || '').trim()
    };
    const prev = map.get(key);
    if (!prev) map.set(key, row);
    else {
      const merged = { ...prev };
      for (const [k, v] of Object.entries(row)) if (v) merged[k] = v;
      if (prev.websiteUrl && !row.websiteUrl) merged.websiteUrl = prev.websiteUrl;
      map.set(key, merged);
    }
  }
  return [...map.values()];
}

function clinicRawCsv(items) {
  const rows = normalizeClinicRawForExport(items);
  // 必ず29列固定。取得できない項目も列を省略せず空文字セルとして保持する。
  const values = row => [
    '',                         // UUID
    '',                         // 種別
    row.name || '',             // 名前
    '',                         // カナ
    row.postalCode || '',       // 郵便番号
    row.prefecture || '',       // 都道府県
    row.address || '',          // 住所１
    '',                         // 住所２
    '',                         // 住所カナ
    row.phone || '',            // Tel1
    '', '', '',                 // Tel2, Tel3, Tel4
    '',                         // FAX
    row.websiteUrl || '',       // URL
    '',                         // 備考
    '',                         // 旧社名
    '',                         // リードソース
    '',                         // 履歴
    '',                         // 記事名
    row.regularHoliday || '',   // 休診日
    row.businessDays || '',     // 診療日
    row.openTimeA || '',        // 午前始
    row.closeTimeA || '',       // 午前終
    row.openTimeB || '',        // 午後始
    row.closeTimeB || '',       // 午後終
    '',                         // 院長名
    '',                         // 開業日
    row.cleanedPhone || ''      // 整理済みTel1
  ];
  return {
    rows,
    csv: '\uFEFF' + CLINIC_RAW_HEADERS.map(escapeCsvValue).join(',') + '\n' +
      rows.map(row => values(row).map(escapeCsvValue).join(',')).join('\n')
  };
}

function safeFilePart(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, '')
    .slice(0, 60) || '取得結果';
}

async function downloadCsvFile(data, filename) {
  const encodedUri = 'data:text/csv;charset=utf-8,' + encodeURIComponent(buildCsvContent(data));
  // [FIX] 以前は chrome.downloads.download(...) をコールバックなしで await するだけで、
  // 戻り値やエラーを一切確認していなかった。chrome.downloads.download は
  // 「連続自動ダウンロードのブロック」等が発生した場合、例外を投げずに
  // downloadId が undefined のまま解決することがあり、この場合は呼び出し側からは
  // 正常終了したようにしか見えず、CSVが実際には出力されないままだった
  // （食べログ側拡張機能で確認したのと同種の不具合）。
  // downloadId を明示的に確認し、失敗時は例外を投げて呼び出し元（downloadGroupedCsvFiles
  // → triggerV3GenreDownloadハンドラ）のtry/catchで検知できるようにする。
  const downloadId = await new Promise((resolve, reject) => {
    try {
      chrome.downloads.download({ url: encodedUri, filename, saveAs: false }, id => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message || 'chrome.downloads.download failed'));
          return;
        }
        resolve(id);
      });
    } catch (e) {
      reject(e);
    }
  });
  if (downloadId == null) {
    throw new Error(`ダウンロードを開始できませんでした（filename: ${filename}）。Chromeの「複数ファイルの自動ダウンロード」がブロックされている可能性があります。`);
  }
  return downloadId;
}

function safeTabSendMessage(tabId, message) {
  try {
    chrome.tabs.sendMessage(tabId, message, () => {
      if (chrome.runtime.lastError) { /* Receiving end does not exist */ }
    });
  } catch (_) { /* ignore */ }
}

// =====================================================================
// 自動ダウンロード
// =====================================================================
async function handleAutomaticDownload(tabId, data, filterConfig) {
  let query = '';
  try {
    const res = await chrome.tabs.sendMessage(tabId, { action: 'getQuery' });
    query = res?.query || '';
  } catch (e) { /* ignore */ }

  if (!query) {
    const r = await chrome.storage.local.get(['lastQuery']);
    query = r.lastQuery || '';
  }

  const stored = await chrome.storage.local.get(['targetGenres']);
  const targetGenres = stored.targetGenres || '';

  const parsed = parseQueryToAreaGenre(query);
  await downloadGroupedCsvFiles(data, {
    area: parsed.area,
    genre: parsed.genre || (Array.isArray(targetGenres) ? targetGenres[0] : targetGenres),
    media: 'GoogleMap'
  });
}

// =====================================================================
// Service Worker リスナー
// =====================================================================
chrome.runtime.onInstalled.addListener(() => {
  chrome.storage.local.set({
    scrapingState: 'inactive',
    scrapedData: [],
    maxItems: 50,
    targetGenres: ''
  });
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'backgroundSleep') {
    setTimeout(() => sendResponse({ success: true }), request.ms);
    return true;
  }

  if (request.action === 'updateData') {
    chrome.storage.local.get(['scrapedData'], result => {
      const current = Array.isArray(result.scrapedData) ? result.scrapedData : [];
      const incoming = Array.isArray(request.data) ? request.data : [];

      const existingUrls = new Set(current.map(i => i?.url).filter(Boolean));
      const unique = incoming.filter(i => i?.url && !existingUrls.has(i.url));
      const updated = [...current, ...unique];

      chrome.storage.local.set({ scrapedData: updated }, () => {
        // 容量超過(QUOTA_BYTES exceeded)等でset自体が失敗しても、
        // 以前はここでチェックせず常にsuccess:trueを返していたため、
        // データが保存されないまま「保存成功」として処理が続き、
        // 気づかれずに件数が欠落するリスクがあった
        if (chrome.runtime.lastError) {
          console.error('[BG] scrapedData保存失敗:', chrome.runtime.lastError.message);
          sendResponse({ success: false, count: current.length, error: chrome.runtime.lastError.message });
          return;
        }
        sendResponse({ success: true, count: updated.length });
      });
    });
    return true;
  }

  if (request.action === 'setState') {
    chrome.storage.local.set({ scrapingState: request.state }, async () => {
      if (request.state === 'active') {
        chrome.power.requestKeepAwake('display');
        chrome.alarms.create('keepAlive', { periodInMinutes: 0.5 });
        // [FIX] currentRunIdは旧版popup.js（手動1回検索）の実行時にしか
        // chrome.storage.localへ書き込まれておらず、v3オーケストレーター
        // （エリア×ジャンル自動巡回）経由の実行では一度も設定されないまま
        // だった。そのため下のdoneハンドラで毎回 `result.currentRunId || 'default_run'`
        // が必ず固定文字列 'default_run' に落ち、Service Workerが生き続けている限り
        // （keepAliveアラームで維持され続けるため実質ほぼ常時）2回目以降の
        // 巡回が「重複ダウンロードとしてスキップ」され、CSVが一切出力されなく
        // なる不具合の直接の原因になっていた
        // （コンソールログ「[BG] Download for runId default_run already executed.
        // Skipping duplicate.」で実際に確認）。
        // スクレイピング開始のたびに一意なrunIdを新規発行して保存することで、
        // 実行ごとに別キーとして扱われるようにする。
        await new Promise(resolve => {
          chrome.storage.local.set({
            currentRunId: `run_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
          }, resolve);
        });
      } else {
        chrome.power.releaseKeepAwake();
        chrome.alarms.clear('keepAlive');
      }

      if (request.state === 'done' || request.state === 'stopped_by_user') {
        // v4.3 clinic mode:
        // content.js は「1エリア×1検索ジャンル」の終了ごとに setState(done) を送る。
        // 旧ロジックはそのたびに生データをジャンル別CSVへ自動保存していたため、
        // 1回の取得で複数CSVが作られ、しかも20列の旧ヘッダーになっていた。
        // クリニック版では自動生CSVを完全停止し、最終出力は triggerV3Download の
        // コムデスク28列固定CSVだけに統一する。
        if (COMDESK_ONLY_EXPORT) {
          console.log(`[BG] ${request.state}: raw CSV auto-download skipped (Comdesk-only mode).`);
        } else {
          chrome.storage.local.get(['scrapedData', 'filterConfig', 'currentRunId'], async result => {
            const runId = result.currentRunId || 'default_run';
            if (downloadedRunIds.has(runId)) return;
            downloadedRunIds.add(runId);
            const data = Array.isArray(result.scrapedData) ? result.scrapedData : [];
            const filterConfig = result.filterConfig || null;
            if (data.length > 0) {
              const tabId = sender?.tab?.id || (await findActiveMapsTabId());
              if (tabId != null) {
                handleAutomaticDownload(tabId, data, filterConfig)
                  .catch(e => console.error('Download failed:', e));
              }
            }
          });
        }
      }
      sendResponse({ success: true });
    });
    return true;
  }

  if (request.action === 'triggerClinicRawDownload') {
    (async () => {
      try {
        const result = await chrome.storage.local.get(['v3_collectedData', 'v3_city']);
        const data = Array.isArray(result.v3_collectedData) ? result.v3_collectedData : [];
        if (!data.length) {
          sendResponse({ ok: false, error: '取得済みデータがありません。' });
          return;
        }
        const out = clinicRawCsv(data);
        if (!out.rows.length) {
          sendResponse({ ok: false, error: 'CSVへ出力できるデータがありません。' });
          return;
        }
        const filename = `GoogleMaps_Clinic_${safeFilePart(result.v3_city)}_${formatTimestamp()}.csv`;
        await downloadTextContent(out.csv, filename);
        await appendV3Log(`CSV保存: ${filename} / ${out.rows.length}件 / 公式HP ${out.rows.filter(r => r.websiteUrl).length}件`);
        sendResponse({ ok: true, filename, count: out.rows.length, hpCount: out.rows.filter(r => r.websiteUrl).length });
      } catch (e) {
        console.error('[BG] clinic raw download failed:', e);
        sendResponse({ ok: false, error: String(e?.message || e) });
      }
    })();
    return true;
  }

  if (request.action === 'triggerV3Download') {
    sendResponse({ ok: false, error: 'v5.6ではコムデスク照合を拡張機能内で行いません。「現在までのCSVを保存」を使用してください。' });
    return false;
  }

  if (request.action === 'triggerV3GenreDownload') {
    // v5.1: ジャンル別の旧CSVは出力しない。
    // 収集データは「現在までのCSVを保存」から1ファイルだけ出力する。
    sendResponse({ ok: true, skipped: true, reason: 'Comdesk-only export mode' });
    return false;
  }

  return false;
});

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name !== 'keepAlive') return;
  chrome.storage.local.get(['scrapingState'], result => {
    if (result.scrapingState !== 'active') return;
    chrome.tabs.query(
      { url: ['https://www.google.com/maps/*', 'https://www.google.co.jp/maps/*'] },
      tabs => tabs.forEach(tab => safeTabSendMessage(tab.id, { action: 'ping' }))
    );
  });
});

async function findActiveMapsTabId() {
  const tabs = await chrome.tabs.query({
    url: ['https://www.google.com/maps/*', 'https://www.google.co.jp/maps/*']
  });
  return tabs[0]?.id ?? null;
}