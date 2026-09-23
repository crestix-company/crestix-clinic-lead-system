(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ClinicQueueLogic = api;
})(typeof self !== 'undefined' ? self : globalThis, function () {
  const RESULT_HEADERS = [
    'internal_clinic_id','medical_institution_number','source_clinic_name','source_phone','source_address','source_prefecture',
    'maps_match_status','maps_match_method','maps_name','maps_phone','maps_address','maps_profile_url','maps_website_url',
    'website_status','phone_match','name_match','address_match','exclude_reason','scrape_status','scraped_at',
    '休診日','診療日','午前始','午前終','午後始','午後終','営業時間原文'
  ];

  function text(v) { return String(v == null ? '' : v).trim(); }
  function nk(v) { return text(v).normalize('NFKC'); }
  function digits(v) { return nk(v).replace(/\D/g, ''); }
  function telMatchKey(v) {
    let d = digits(v);
    if (d.startsWith('81') && d.length >= 10) d = '0' + d.slice(2);
    if (d.startsWith('0')) d = d.slice(1);
    return d;
  }
  function normalizeName(v) {
    return nk(v)
      .replace(/^(?:医療法人社団|医療法人財団|医療法人|社会医療法人|一般社団法人|公益社団法人|一般財団法人|公益財団法人)\s*/g, '')
      .replace(/[・･／/\\｜|（）()［］\[\]【】「」『』“”"'`´｀.,，。:：;；\s　\-ー]/g, '')
      .toLowerCase();
  }
  function normalizeAddress(v) {
    return nk(v).replace(/^日本、?/, '').replace(/〒?\d{3}-?\d{4}/g, '').replace(/[\s　,，・･\-−‐ー]/g, '').toLowerCase();
  }
  function bigrams(s) {
    const out = [];
    for (let i = 0; i < s.length - 1; i++) out.push(s.slice(i, i + 2));
    return out;
  }
  function dice(a, b) {
    a = text(a); b = text(b);
    if (!a || !b) return 0;
    if (a === b) return 1;
    if (a.length === 1 || b.length === 1) return a === b ? 1 : 0;
    const aa = bigrams(a), bb = bigrams(b), counts = new Map();
    for (const x of aa) counts.set(x, (counts.get(x) || 0) + 1);
    let overlap = 0;
    for (const x of bb) {
      const n = counts.get(x) || 0;
      if (n) { overlap++; counts.set(x, n - 1); }
    }
    return 2 * overlap / (aa.length + bb.length);
  }
  function nameScore(a, b) {
    const aa = normalizeName(a), bb = normalizeName(b);
    if (!aa || !bb) return 0;
    if (aa === bb) return 1;
    if ((aa.includes(bb) || bb.includes(aa)) && Math.min(aa.length, bb.length) >= 4) return 0.94;
    return dice(aa, bb);
  }
  function addressScore(a, b) {
    const aa = normalizeAddress(a), bb = normalizeAddress(b);
    if (!aa || !bb) return 0;
    if (aa === bb) return 1;
    if ((aa.includes(bb) || bb.includes(aa)) && Math.min(aa.length, bb.length) >= 8) return 0.95;
    return dice(aa, bb);
  }
  function parseCsv(src) {
    src = String(src || '').replace(/^\uFEFF/, '');
    const rows = []; let row = [], field = '', quoted = false;
    for (let i = 0; i < src.length; i++) {
      const ch = src[i];
      if (quoted) {
        if (ch === '"' && src[i + 1] === '"') { field += '"'; i++; }
        else if (ch === '"') quoted = false;
        else field += ch;
      } else {
        if (ch === '"') quoted = true;
        else if (ch === ',') { row.push(field); field = ''; }
        else if (ch === '\n') { row.push(field.replace(/\r$/, '')); rows.push(row); row = []; field = ''; }
        else field += ch;
      }
    }
    if (field.length || row.length) { row.push(field.replace(/\r$/, '')); rows.push(row); }
    return rows.filter(r => r.some(v => text(v)));
  }
  function headerKey(v) { return nk(v).replace(/[\s_]/g, '').toLowerCase(); }
  const ALIASES = {
    internal_clinic_id:['internalclinicid','clinicid','内部clinicid','内部id','管理番号'],
    medical_institution_number:['medicalinstitutionnumber','医療機関番号','医療機関コード','医療機関no'],
    clinic_name:['clinicname','医院名','クリニック名','名前','医療機関名'],
    phone:['phone','電話番号','tel1','電話'],
    address:['address','住所','住所1','住所１','所在地'],
    prefecture:['prefecture','都道府県'],
    facility_type:['facilitytype','施設区分','施設種別','種別'],
    status:['status','稼働状態','営業状態']
  };
  function findCol(headers, names) {
    const wanted = new Set(names);
    const keys = headers.map(headerKey);
    const idx = keys.findIndex(k => wanted.has(k));
    return idx >= 0 ? idx : null;
  }
  function inferColumns(headers) {
    const out = {};
    for (const [field, aliases] of Object.entries(ALIASES)) out[field] = findCol(headers, aliases);
    return out;
  }
  function exclusion(record) {
    const name = nk(record.source_clinic_name);
    const type = nk(record.facility_type);
    if (type === '病院' || name.includes('病院')) return 'hospital';
    if (name.includes('センター')) return 'center';
    return '';
  }
  function queueKey(record, index=0) {
    return text(record.internal_clinic_id) || (text(record.medical_institution_number) ? 'med:' + text(record.medical_institution_number) : '') ||
      ('row:' + telMatchKey(record.source_phone) + ':' + normalizeName(record.source_clinic_name) + ':' + index);
  }
  function prepareQueue(csvText) {
    const rows = parseCsv(csvText);
    if (!rows.length) throw new Error('CSVが空です。');
    const headers = rows[0].map(text), m = inferColumns(headers);
    if (m.clinic_name == null) throw new Error('医院名の列が見つかりません。clinic_name / 医院名 / 名前 のいずれかが必要です。');
    if (m.phone == null) throw new Error('電話番号の列が見つかりません。phone / 電話番号 / Tel1 のいずれかが必要です。');
    const queue = rows.slice(1).map((r, i) => {
      const get = f => m[f] == null ? '' : text(r[m[f]]);
      const rec = {
        internal_clinic_id:get('internal_clinic_id'), medical_institution_number:get('medical_institution_number'),
        source_clinic_name:get('clinic_name'), source_phone:get('phone'), source_address:get('address'),
        source_prefecture:get('prefecture'), facility_type:get('facility_type'), source_status:get('status'),
        source_row:i + 2
      };
      rec.exclude_reason = exclusion(rec);
      rec.queue_key = queueKey(rec, i + 2);
      return rec;
    }).filter(r => r.source_clinic_name);
    const seen = new Set();
    const deduped = queue.filter(r => !seen.has(r.queue_key) && seen.add(r.queue_key));
    return { headers, mapping:m, queue:deduped };
  }
  function evaluateCandidate(source, maps) {
    const phoneMatch = !!telMatchKey(source.source_phone) && telMatchKey(source.source_phone) === telMatchKey(maps.phone);
    const n = nameScore(source.source_clinic_name, maps.name);
    const a = addressScore(source.source_address, maps.address);
    return { phoneMatch, nameScore:n, addressScore:a, nameMatch:n >= 0.82, addressMatch:a >= 0.78 };
  }
  function csvEscape(v) {
    const s = String(v == null ? '' : v);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function resultsToCsv(results) {
    const lines = [RESULT_HEADERS.join(',')];
    for (const r of results) lines.push(RESULT_HEADERS.map(h => csvEscape(r[h] ?? '')).join(','));
    return '\uFEFF' + lines.join('\r\n');
  }
  return { RESULT_HEADERS, telMatchKey, normalizeName, normalizeAddress, nameScore, addressScore, parseCsv, inferColumns, prepareQueue, exclusion, evaluateCandidate, resultsToCsv };
});
