// content.js  v5.5.0
// 改善: 動的待機/カードキュー化/DOM参照排除/詳細パネル継続方式/STOP強制flush/内訳ログ/スキップ理由ログ
// 既存取得項目は完全維持 (店名/ジャンル/取得元ジャンル/都道府県/市区町村/住所/電話番号/定休日/営業日/
//                          営業開始A/B/営業終了A/B/営業時間原文/URL/HP有無/媒体/取得元URL/取得日時)

// =====================================================================
// 基本ユーティリティ
// =====================================================================
function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function backgroundSleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

// DOM要素が現れるまで待つ（動的待機）
function waitForElement(selector, timeoutMs = 3000) {
  return new Promise(resolve => {
    const el = document.querySelector(selector);
    if (el) { resolve(el); return; }
    const deadline = Date.now() + timeoutMs;
    const iv = setInterval(() => {
      const found = document.querySelector(selector);
      if (found || Date.now() >= deadline) {
        clearInterval(iv);
        resolve(found || null);
      }
    }, 60);
  });
}

// 詳細パネルの店名またはURLが前回と異なる値に変わるまで待つ
function waitUntilPanelChanged(previousName, previousUrl, timeoutMs = 4000) {
  return new Promise(resolve => {
    const deadline = Date.now() + timeoutMs;
    const iv = setInterval(() => {
      const currentName = (document.querySelector('[role="main"] h1')?.textContent?.trim()) || '';
      const currentUrl  = window.location.href;
      if (
        Date.now() >= deadline
        || (currentName && currentName !== previousName && currentName !== '結果')
        || (currentUrl !== previousUrl && currentUrl.includes('/maps/place/'))
      ) {
        clearInterval(iv);
        resolve({ name: currentName, url: currentUrl });
      }
    }, 60);
  });
}

function getCurrentPanelName(expectedName = '') {
  const panel = getActiveMainPanel(expectedName);
  return panel?.querySelector('h1')?.textContent?.trim() || '';
}

function getCurrentPanelAddress() {
  // [role="main"] でスコープしないと、検索結果一覧カードや「関連する検索」
  // カルーセルなど、詳細パネル以外の場所にある同じdata-item-id要素を
  // 誤って拾ってしまう（住所・電話番号の店舗取り違えの原因になる）
  const panel = getActiveMainPanel();
  const addrBtn = panel?.querySelector('button[data-item-id="address"]');
  if (!addrBtn) return '';
  const raw = addrBtn.getAttribute('aria-label') || addrBtn.textContent.trim();
  return raw.replace(/^住所[：:]\s*/, '').trim();
}

// 現在パネルに表示されている電話番号を取得（前店舗の電話ボタンが
// まだDOMに残っている「取り残し」状態を検知するための基準値として使用）
function getCurrentPanelPhone() {
  const panel = getActiveMainPanel();
  const phoneBtn = panel?.querySelector('button[data-item-id^="phone:tel:"]');
  if (!phoneBtn) return '';
  const itemId = phoneBtn.getAttribute('data-item-id') || '';
  const raw = itemId.replace('phone:tel:', '').trim() || phoneBtn.textContent.trim();
  return normalizePhoneNumber(raw);
}

function normalizePlaceName(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/[（）()[\]【】「」『』"'`´｀]/g, '')
    .replace(/\s+/g, '')
    .trim();
}

function isLikelySamePlaceName(a, b) {
  const aa = normalizePlaceName(a);
  const bb = normalizePlaceName(b);
  if (!aa || !bb) return false;
  return aa === bb || aa.includes(bb) || bb.includes(aa);
}

// v5.3: HP欄の紐付けだけは通常の店舗名判定より厳格にする。
// 法人格の有無だけを吸収し、支店名・院名の違いは同一扱いしない。
function normalizeClinicNameStrict(name) {
  return String(name || '')
    .normalize('NFKC')
    .replace(/^(?:医療法人社団|医療法人財団|医療法人|社会医療法人|一般社団法人|公益社団法人|一般財団法人|公益財団法人)\s*/g, '')
    .replace(/[・･／/\\｜|（）()［］\[\]【】「」『』“”"'`´｀.,，。・:：;；\s　-]/g, '')
    .toLowerCase()
    .trim();
}

function isStrictClinicNameMatch(a, b) {
  const aa = normalizeClinicNameStrict(a);
  const bb = normalizeClinicNameStrict(b);
  if (!aa || !bb) return false;
  return aa === bb;
}

function normalizePanelAddressForFingerprint(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/^日本、?/, '')
    .replace(/〒?\d{3}-?\d{4}/g, '')
    .replace(/[\s　-]/g, '')
    .trim();
}

function getPanelIdentitySnapshot(expectedName = '') {
  const panel = getActiveMainPanel(expectedName);
  if (!panel) return null;
  const name = panel.querySelector('h1')?.textContent?.trim() || '';
  const addressBtn = panel.querySelector('button[data-item-id="address"]');
  const addressRaw = addressBtn ? (addressBtn.getAttribute('aria-label') || addressBtn.textContent || '') : '';
  const address = addressRaw.replace(/^住所[：:]\s*/, '').trim();
  const phoneBtn = panel.querySelector('button[data-item-id^="phone:tel:"]');
  const phoneItem = phoneBtn?.getAttribute('data-item-id') || '';
  const phone = normalizePhoneNumber(phoneItem.replace('phone:tel:', '').trim() || phoneBtn?.textContent || '');
  const urlName = extractNameFromUrl(window.location.href);
  const normalizedName = normalizeClinicNameStrict(name);
  const normalizedAddress = normalizePanelAddressForFingerprint(address);
  const fingerprint = [normalizedName, normalizedAddress, phone, normalizeClinicNameStrict(urlName)].join('|');
  return { panel, name, address, phone, urlName, fingerprint };
}


// =====================================================================
// v5.1: 現在表示中の詳細パネルと「ウェブサイト」欄を厳密に結び付ける
// =====================================================================
function isVisibleDomElement(el) {
  if (!el || !el.isConnected) return false;
  try {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    // a要素自身が小さいUIもあるため、祖先も含めて可視性を確認する
    if (rect.width > 1 && rect.height > 1) return true;
    const parent = el.closest('div[role="button"], button, div');
    if (parent && parent !== el) {
      const pr = parent.getBoundingClientRect();
      return pr.width > 1 && pr.height > 1;
    }
  } catch (_) {}
  return false;
}

function isJunkPanelName(name) {
  const n = String(name || '').normalize('NFKC').trim();
  return !n || n === '結果' || /^スポンサー(?:$|\s|)/.test(n) || /^広告$/.test(n);
}

function getActiveMainPanel(expectedName = '') {
  const expected = String(expectedName || '').trim();
  const panels = Array.from(document.querySelectorAll('[role="main"]'))
    .filter(panel => isVisibleDomElement(panel));

  // 期待する医院名と一致する可視パネルを最優先。
  if (expected && !isJunkPanelName(expected)) {
    const matched = panels.find(panel => {
      const h1 = panel.querySelector('h1');
      const name = h1?.textContent?.trim() || '';
      return !isJunkPanelName(name) && isLikelySamePlaceName(name, expected);
    });
    if (matched) return matched;
  }

  // Google Mapsでは一覧側にもrole=mainが残る場合があるため、
  // 店名h1 + 住所/電話/公式サイトのいずれかを持つ可視パネルを優先する。
  const detailed = panels.filter(panel => {
    const name = panel.querySelector('h1')?.textContent?.trim() || '';
    if (isJunkPanelName(name)) return false;
    return Boolean(panel.querySelector(
      'button[data-item-id="address"], button[data-item-id^="phone:tel:"], a[data-item-id="authority"]'
    ));
  });
  return detailed.at(-1) || panels.at(-1) || null;
}

function unwrapGoogleExternalUrlStrict(rawHref) {
  let href = String(rawHref || '').trim();
  if (!href) return '';
  try {
    let u = new URL(href, location.href);
    if (/^(?:www\.)?google\.(?:com|co\.jp)$/i.test(u.hostname)) {
      const nested = u.searchParams.get('url') || u.searchParams.get('q') || '';
      if (nested) {
        try { u = new URL(decodeURIComponent(nested)); }
        catch (_) { try { u = new URL(nested); } catch (_) { return ''; } }
      }
    }
    const h = u.hostname.toLowerCase().replace(/^www\./, '');
    if (
      /(^|\.)google\.(com|co\.jp)$/.test(h) ||
      h === 'maps.app.goo.gl' || h === 'goo.gl' ||
      /\/maps(?:\/|$)/i.test(u.pathname)
    ) return '';
    if (!/^https?:$/i.test(u.protocol)) return '';
    ['utm_source','utm_medium','utm_campaign','utm_term','utm_content',
     'gclid','gbraid','wbraid','ved','sa','source'].forEach(k => u.searchParams.delete(k));
    return u.toString();
  } catch (_) {
    return '';
  }
}

function getVisibleWebsiteCandidate(expectedName = '', expectedFingerprint = '') {
  const identity = getPanelIdentitySnapshot(expectedName);
  const panel = identity?.panel || null;
  if (!panel) return null;

  // HPだけは、カード名と現在の詳細プロフィール名が厳密一致した場合に限る。
  // 法人格の有無は許容するが、別院・分院名の差は許容しない。
  if (expectedName && !isStrictClinicNameMatch(identity.name, expectedName)) return null;
  if (expectedFingerprint && identity.fingerprint !== expectedFingerprint) return null;

  const selectors = [
    'a[data-item-id="authority"]',
    'a[data-item-id^="authority"]',
    'a[aria-label*="ウェブサイト"]',
    'a[aria-label*="Webサイト"]',
    'a[aria-label*="Website"]',
    'a[aria-label*="website"]',
    'a[data-tooltip*="ウェブサイト"]',
    'a[data-tooltip*="Webサイト"]',
    'a[data-tooltip*="Website"]',
    'a[jsaction*="website"]',
    'a[href][data-value="Website"]'
  ];

  const seen = new Set();
  const candidates = [];
  for (const sel of selectors) {
    for (const a of Array.from(panel.querySelectorAll(sel))) {
      if (seen.has(a)) continue;
      seen.add(a);
      if (!a.isConnected || !panel.contains(a) || !isVisibleDomElement(a)) continue;
      const itemId = a.getAttribute('data-item-id') || '';
      const jsaction = a.getAttribute('jsaction') || '';
      const dataValue = a.getAttribute('data-value') || '';
      const label = `${a.getAttribute('aria-label') || ''} ${a.getAttribute('data-tooltip') || ''} ${a.textContent || ''} ${dataValue}`;
      if (/予約|メニュー|注文|予約サイト|booking|menu|order/i.test(label)) continue;
      if (!/authority/i.test(itemId) && !/website/i.test(jsaction) && !/ウェブサイト|Webサイト|website/i.test(label)) continue;
      const url = unwrapGoogleExternalUrlStrict(a.getAttribute('href') || a.href || '');
      if (!url) continue;
      candidates.push({ element: a, url, label: label.trim(), panel, identityFingerprint: identity.fingerprint });
    }
  }

  // 同一パネル内に複数の「ウェブサイト」候補が見える状態はDOM更新途中の可能性がある。
  // 最大精度優先で、その瞬間は採用しない。
  const uniqueUrls = [...new Set(candidates.map(c => c.url))];
  if (uniqueUrls.length !== 1) return null;
  return candidates.find(c => c.url === uniqueUrls[0]) || null;
}

function getCurrentWebsiteSnapshot(expectedName = '') {
  const identity = getPanelIdentitySnapshot(expectedName);
  const candidate = getVisibleWebsiteCandidate(expectedName, identity?.fingerprint || '');
  return candidate
    ? {
        element: candidate.element,
        url: candidate.url,
        label: candidate.label,
        fingerprint: identity?.fingerprint || '',
        panelName: identity?.name || '',
        address: identity?.address || '',
        phone: identity?.phone || ''
      }
    : {
        element: null, url: '', label: '',
        fingerprint: identity?.fingerprint || '',
        panelName: identity?.name || '',
        address: identity?.address || '',
        phone: identity?.phone || ''
      };
}

async function waitForFreshWebsiteField(options = {}, timeoutMs = 4600) {
  const {
    expectedName = '',
    previousSnapshot = null
  } = options;
  const previousElement = previousSnapshot?.element || null;
  const previousUrl = String(previousSnapshot?.url || '');
  const previousFingerprint = String(previousSnapshot?.fingerprint || '');
  const deadline = Date.now() + timeoutMs;

  let identityFingerprint = '';
  let identityStableSince = 0;
  let noWebsiteSince = 0;
  let lastCandidateUrl = '';
  let candidateStableSince = 0;
  let consecutiveSameReads = 0;

  while (Date.now() < deadline) {
    const identity = getPanelIdentitySnapshot(expectedName);
    if (!identity || isJunkPanelName(identity.name)) {
      identityStableSince = 0;
      noWebsiteSince = 0;
      await sleep(120);
      continue;
    }

    // URL採用時は名前の部分一致を使わない。別院/分院へのずれを最優先で防止する。
    if (expectedName && !isStrictClinicNameMatch(identity.name, expectedName)) {
      identityStableSince = 0;
      noWebsiteSince = 0;
      await sleep(120);
      continue;
    }

    if (identity.fingerprint !== identityFingerprint) {
      identityFingerprint = identity.fingerprint;
      identityStableSince = Date.now();
      noWebsiteSince = 0;
      lastCandidateUrl = '';
      candidateStableSince = 0;
      consecutiveSameReads = 0;
      await sleep(120);
      continue;
    }

    // 店名だけ先に切り替わり、住所・電話・HPが後から追いつくことがあるため、
    // 現在プロフィールの本人情報が一定時間まったく変わらないことを先に要求する。
    if (Date.now() - identityStableSince < 700) {
      await sleep(120);
      continue;
    }

    const candidate = getVisibleWebsiteCandidate(expectedName, identityFingerprint);
    if (!candidate) {
      if (!noWebsiteSince) noWebsiteSince = Date.now();
      // 本人プロフィールが安定している状態でHP欄が1.2秒以上見つからない場合だけ
      // 「ウェブサイト未登録」と確定する。曖昧な瞬間は空欄判定にしない。
      if (Date.now() - noWebsiteSince >= 1200) {
        const confirmIdentity = getPanelIdentitySnapshot(expectedName);
        if (confirmIdentity?.fingerprint === identityFingerprint &&
            (!expectedName || isStrictClinicNameMatch(confirmIdentity.name, expectedName))) {
          return {
            status: 'no_website', url: '', element: null,
            fingerprint: identityFingerprint,
            reason: 'stable_current_profile_has_no_visible_website'
          };
        }
      }
      await sleep(120);
      continue;
    }

    noWebsiteSince = 0;
    if (candidate.url !== lastCandidateUrl) {
      lastCandidateUrl = candidate.url;
      candidateStableSince = Date.now();
      consecutiveSameReads = 1;
      await sleep(160);
      continue;
    }
    consecutiveSameReads += 1;

    // HP URLは「同じURLを複数回」「同じプロフィール指紋のまま」読めた場合だけ採用。
    const candidateStable = Date.now() - candidateStableSince >= 650 && consecutiveSameReads >= 3;
    if (!candidateStable) {
      await sleep(160);
      continue;
    }

    // もう一度、HPを読む直前/直後で医院本人情報が変わっていないことを確認。
    const verifyIdentity = getPanelIdentitySnapshot(expectedName);
    const verifyCandidate = getVisibleWebsiteCandidate(expectedName, identityFingerprint);
    if (!verifyIdentity || verifyIdentity.fingerprint !== identityFingerprint ||
        !verifyCandidate || verifyCandidate.url !== candidate.url) {
      await sleep(160);
      continue;
    }

    const profileChanged = !previousFingerprint || identityFingerprint !== previousFingerprint;
    const nodeChanged = !previousElement || verifyCandidate.element !== previousElement;
    const urlChanged = !previousUrl || verifyCandidate.url !== previousUrl;

    // 前医院と同じURL（同一法人サイト等）はあり得るためURL一致だけでは除外しない。
    // ただし、その場合でも「医院プロフィール自体が確実に別物へ切り替わった」ことを必須にする。
    if (!profileChanged && !nodeChanged && !urlChanged) {
      await sleep(160);
      continue;
    }

    return {
      status: 'fresh',
      url: verifyCandidate.url,
      element: verifyCandidate.element,
      fingerprint: identityFingerprint,
      reason: 'triple_verified_website_field'
    };
  }

  // 最大精度優先: 本人プロフィールとの結び付きを証明できないURLは保存しない。
  return {
    status: 'ambiguous', url: '', element: null, fingerprint: '',
    reason: 'website_profile_binding_not_proven'
  };
}

// v5.4: 取得率と正確率を同時に上げるため、HP欄は1回のDOM読取だけで確定しない。
// 同じ医院プロフィールを独立に開き直し、同じHP URLを複数回確認して合意した時だけ採用する。
// 1回目で曖昧/未表示でも最大4回まで回復を試みるため、空欄率も下げる。
async function getWebsiteByIndependentConsensus(options = {}) {
  const {
    placeUrl = '',
    cardName = '',
    previousSnapshot = null,
    firstField = null,
    maxAttempts = 4
  } = options;

  const urlVotes = new Map();
  let noWebsiteVotes = 0;
  const previousUrl = String(previousSnapshot?.url || '');
  let lastField = firstField;

  async function vote(field) {
    if (!field) return null;
    if (field.status === 'fresh' && field.url) {
      const canonical = unwrapGoogleExternalUrlStrict(field.url);
      if (!canonical) return null;
      const n = (urlVotes.get(canonical) || 0) + 1;
      urlVotes.set(canonical, n);

      // 前医院と同じURLは系列共通HPの可能性もある一方、DOM取り残しの可能性もある。
      // そのケースだけ3回の独立確認を要求。それ以外は2回一致で確定。
      const needed = previousUrl && canonical === previousUrl ? 3 : 2;
      if (n >= needed) {
        return {
          status: 'fresh', url: canonical, element: field.element || null,
          fingerprint: field.fingerprint || '',
          reason: `independent_consensus_${n}_of_${maxAttempts}`
        };
      }
    } else if (field.status === 'no_website') {
      noWebsiteVotes += 1;
      // 「HPなし」も1回で決めず、独立2回一致を要求する。
      if (noWebsiteVotes >= 2) {
        return {
          status: 'no_website', url: '', element: null,
          fingerprint: field.fingerprint || '',
          reason: `independent_no_website_consensus_${noWebsiteVotes}`
        };
      }
    }
    return null;
  }

  const firstDecision = await vote(lastField);
  if (firstDecision) return firstDecision;

  for (let attempt = 2; attempt <= maxAttempts; attempt++) {
    if (stopRequested) break;

    const beforeName = getCurrentPanelName();
    const beforeUrl = window.location.href;
    const beforeAddress = getCurrentPanelAddress();
    const beforePhone = getCurrentPanelPhone();

    // 同じ医院をいったん閉じて、検索結果の「同じplace URL」のカードから再度開く。
    // 前医院のHP DOMを引き継いだ1回限りの表示を排除しつつ、読み込み失敗は再試行で救済する。
    const closed = await closeDetailPanel();
    if (!closed) {
      await sleep(350);
      continue;
    }

    let container = getScrollContainer();
    let cardEl = resolveCardElementByUrl(placeUrl, container);
    if (!cardEl) {
      cardEl = await scrollToCardUrl(placeUrl, container, 24);
      container = getScrollContainer() || container;
    }
    const cardLink = cardEl?.querySelector?.('a[href*="/maps/place/"]')
      || cardEl?.closest?.('a[href*="/maps/place/"]');
    if (!cardLink) {
      await sleep(300);
      continue;
    }

    try { cardEl.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (_) {}
    await sleep(180);
    cardLink.click();

    const panelReady = await waitForDetailPanel(5200);
    if (!panelReady) continue;

    const fieldsReady = await waitForPanelFieldsReady({
      expectedName: cardName,
      expectedUrl: placeUrl,
      previousName: beforeName,
      previousUrl: beforeUrl,
      previousAddress: beforeAddress,
      previousPhone: beforePhone
    }, 8000);
    if (!fieldsReady) continue;

    // 毎回ゼロから現在プロフィールの本人情報を安定させた上でHP欄を読む。
    lastField = await waitForFreshWebsiteField({
      expectedName: cardName,
      previousSnapshot
    }, 6500);

    const decision = await vote(lastField);
    if (decision) return decision;
  }

  // 4回試しても合意できない場合のみ曖昧扱い。単発URLは保存しない。
  return {
    status: 'ambiguous', url: '', element: null, fingerprint: '',
    reason: `independent_consensus_not_reached:urlVotes=${JSON.stringify(Object.fromEntries(urlVotes))};noWebsite=${noWebsiteVotes}`
  };
}

async function waitForPanelFieldsReady(options = {}, timeoutMs = 5500) {
  const {
    expectedName = '',
    expectedUrl = '',
    previousName = '',
    previousUrl = '',
    previousAddress = '',
    previousPhone = ''
  } = options;
  const expectedNames = [expectedName, extractNameFromUrl(expectedUrl)].filter(Boolean);
  const deadline = Date.now() + timeoutMs;
  let identityReadyAt = 0;
  let lastAddress = '';
  let addressStableAt = 0;
  // 電話番号欄は住所欄より遅れて非同期読み込みされることがあり、
  // 前の店舗の電話ボタンがDOMに残ったまま読み取ってしまう（取り違え）
  // 原因になるため、住所と同様に安定するまで待つ
  let lastPhone = null;
  let phoneStableAt = 0;

  while (Date.now() < deadline) {
    const currentName = getCurrentPanelName();
    const currentUrl = window.location.href;
    const currentAddress = getCurrentPanelAddress();
    const currentPhone = getCurrentPanelPhone();
    const usableName = currentName && currentName !== '結果';
    const identityChanged =
      (usableName && currentName !== previousName) ||
      (currentUrl !== previousUrl && currentUrl.includes('/maps/place/'));
    const nameMatches =
      expectedNames.length === 0 ||
      expectedNames.some(name => isLikelySamePlaceName(currentName, name));
    const panelLooksUsable = usableName || currentUrl.includes('/maps/place/');
    // v5.1: 「何か別のパネルに変わった」だけでは不十分。
    // 期待する医院名（カード名またはplace URL内の名前）と一致することを必須にする。
    const expectedIdentityOk = expectedNames.length === 0 ? identityChanged : (identityChanged && nameMatches);

    if (panelLooksUsable && expectedIdentityOk) {
      if (!identityReadyAt) identityReadyAt = Date.now();

      if (currentPhone !== lastPhone) {
        lastPhone = currentPhone;
        phoneStableAt = Date.now();
      }
      // 「前と同じ値のまま一定時間経過したら本物とみなす」猶予ロジックは、
      // Googleのパネル更新が単に遅いだけの場合と、更新が止まったまま前の
      // 店舗のデータが残っている場合を区別できず、後者を誤って「準備完了」
      // としてしまっていた（他店舗の住所・電話番号の混入バグの直接原因）。
      // 前の値が分かっている場合は、実際に値が変わったことを確認できる
      // まで待つ。変わらないまま全体タイムアウトした場合は呼び出し側の
      // フォールバック（パネルを閉じて開き直す／スキップ）に任せる。
      const phoneChanged = !previousPhone || currentPhone !== previousPhone;
      const phoneStable = Date.now() - phoneStableAt >= 240;
      const phoneReady = phoneStable && phoneChanged;

      if (currentAddress) {
        if (currentAddress !== lastAddress) {
          lastAddress = currentAddress;
          addressStableAt = Date.now();
        }
        const addressChanged = !previousAddress || currentAddress !== previousAddress;
        const addressStable = Date.now() - addressStableAt >= 240;
        if (addressStable && addressChanged && phoneReady) return true;
      } else if (!previousAddress && Date.now() - identityReadyAt >= 700 && phoneReady) {
        // 住所欄自体が存在しない店舗（前の店舗の情報も無い＝最初の1件目など）
        // の場合のみ、住所なしを正として許容する
        return true;
      }
    }

    await sleep(100);
  }

  return false;
}

// スクロール後に一覧カード数が増えるまで待つ
function waitUntilResultCardsChanged(previousCount, container, timeoutMs = 2000) {
  return new Promise(resolve => {
    const deadline = Date.now() + timeoutMs;
    const iv = setInterval(() => {
      const count = (container || getScrollContainer())?.querySelectorAll('a[href*="/maps/place/"]').length || 0;
      if (count > previousCount || Date.now() >= deadline) {
        clearInterval(iv);
        resolve(count);
      }
    }, 80);
  });
}

function getScrollableMetrics(container) {
  const target = container || getScrollContainer();
  if (!target) return { top: 0, height: 0, client: 0, remaining: 0, linkCount: 0 };
  const top = target.scrollTop || 0;
  const height = target.scrollHeight || 0;
  const client = target.clientHeight || 0;
  return {
    top,
    height,
    client,
    remaining: Math.max(0, height - client - top),
    linkCount: getResultLinks(target).length
  };
}

function getCurrentQuery() {
  return document.querySelector('input#searchboxinput')?.value?.trim() || '';
}

function extractNameFromUrl(url) {
  try {
    const match = url.match(/\/maps\/place\/([^/]+)\//);
    if (!match) return '';
    return decodeURIComponent(match[1]).replace(/\+/g, ' ').trim();
  } catch (e) { return ''; }
}

// =====================================================================
// 住所解析
// =====================================================================
function normalizeAddressText(value) {
  return String(value || '')
    .normalize('NFKC')
    .replace(/^日本、?/, '')
    .replace(/〒?\d{3}-?\d{4}/g, '')
    .replace(/\s+/g, '')
    .trim();
}

function parseAddress(address) {
  let cleanAddress = normalizeAddressText(address);
  const regex = /^((?:北海道|東京都|大阪府|京都府|.{2,3}県))?((?:.+?郡.+?[町村]|.+?市.+?区|.+?[市区町村]))?(.+)?$/;
  const m = cleanAddress.match(regex);
  if (!m) return { prefecture: '', city: '' };
  return { prefecture: m[1] || '', city: m[2] || '' };
}

function parseTargetArea(searchArea) {
  const normalized = normalizeAddressText(searchArea)
    .replace(/駅周辺|エリア|付近/g, '');
  if (!normalized) return { prefecture: '', city: '' };

  const parsed = parseAddress(normalized);
  const prefecture = parsed.prefecture || '';
  // [FIX] 市区町村名が「〜市/〜区/〜町/〜村」で終わっていない入力（例:「銚子」で
  // 「銚子市」ではない場合）だと parseAddress() 側で市区町村が空になり、
  // 以前はここで normalized（元の文字列全体、都道府県名を含んだまま）を
  // そのままcityにフォールバックしていたため、都道府県名がcityに二重に
  // 混入していた（例:「千葉県銚子」）。エリア判定(matchesTargetArea)は
  // prefecture文字列とcity文字列を個別に住所へ含むかチェックするため、
  // cityに都道府県名が混入していると実際の住所と一致しなくなり、該当エリアの
  // 店舗が全件「エリア外除外」される深刻な不具合の原因になっていた
  // （銚子市データで「指定:千葉県千葉県千葉県銚子」という三重混入を確認）。
  // 都道府県名を除いた残り部分だけをcityフォールバックとして使う。
  const remainder = prefecture ? normalized.slice(prefecture.length) : normalized;
  return {
    prefecture,
    city: parsed.city || remainder
  };
}

// [EFFICIENCY] 一覧カードの生テキストから「〜市/〜区/〜町/〜村」パターンで
// 市区町村名を推測する。詳細パネルを開く前の軽量プレフィルタ専用で、
// あくまで「明確に別エリアだと分かる場合だけ弾く」ための保守的な用途。
// マッチしない/曖昧な場合は null を返し、通常通り詳細取得へ進める。
function guessCityFromSnippet(text) {
  if (!text) return null;
  const normalized = normalizeAddressText(text);
  const m = normalized.match(/(.{1,6}?(?:郡.{1,6}?[町村]|市.{1,6}?区|[市区町村]))/);
  return m ? m[1] : null;
}

function matchesTargetArea(address, targetPrefecture, targetCity) {
  if (!address) return false;

  const normalizedAddress = normalizeAddressText(address);
  const normalizedPrefecture = normalizeAddressText(targetPrefecture);
  const normalizedCity = normalizeAddressText(targetCity);

  if (normalizedPrefecture && !normalizedAddress.includes(normalizedPrefecture)) {
    return false;
  }

  if (normalizedCity && !normalizedAddress.includes(normalizedCity)) {
    return false;
  }

  return true;
}

// =====================================================================
// 詳細パネル判定
// =====================================================================
function isDetailPanelOpen() {
  return !!document.querySelector('[role="main"] button[data-item-id="address"]');
}

async function waitForDetailPanel(timeoutMs = 5000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (isDetailPanelOpen()) return true;
    await sleep(60);
  }
  return false;
}

async function waitForListPanel(timeoutMs = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (!isDetailPanelOpen()) return true;
    await sleep(60);
  }
  return false;
}

let searchPageUrl = '';

async function closeDetailPanel() {
  if (!isDetailPanelOpen()) return true;

  const backButton = Array.from(document.querySelectorAll('button[aria-label], a[aria-label]'))
    .find(el => /戻る|Back/i.test(el.getAttribute('aria-label') || ''));
  if (backButton) {
    backButton.click();
    const listReady = await waitForListPanel(5000);
    if (listReady && getScrollContainer()) return true;
  }

  try {
    window.history.back();
    const listReady = await waitForListPanel(5000);
    if (listReady && getScrollContainer()) return true;
  } catch (_) { }

  try {
    document.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true
    }));
    const listReady = await waitForListPanel(2000);
    if (listReady && getScrollContainer()) return true;
  } catch (_) { }

  const start = Date.now();
  while (Date.now() - start < 3000) {
    if (!isDetailPanelOpen() && getScrollContainer()) return true;
    await sleep(200);
  }
  return false;
}

// =====================================================================
// リスト終端検知
// =====================================================================
function isEndOfList(container) {
  // [FIX] このスクレイパーは詳細パネルを閉じずに次カードをクリックする方式
  // （「パネルを閉じない方式」、約1820行目）を採用しているため、実際の
  // スクレイピング中は詳細パネルが開いたままの時間がほとんどを占める。
  // 以前はisDetailPanelOpen()がtrueの間ずっとfalseを返していたため、
  // 結果リスト側に「リストの最後に到達しました」の文言が既に表示されていても
  // それを検知できず、スクロール終了判定（回数上限50回や新規カード無し8回連続
  // など）に達するまで何度も無駄なスクロールを繰り返す原因になっていた。
  // 呼び出し元は必ず結果リスト用のcontainer（getScrollContainer()の戻り値）を
  // 明示的に渡しているため、containerが渡された場合はそのDOM部分木のテキスト
  // のみを見れば十分であり、詳細パネル（[role="main"]側の別DOM）の内容が
  // 混入する心配はない。container省略時（document.body全体を見る場合）のみ、
  // 詳細パネル側のテキストを誤検知しないよう従来通りガードする。
  if (!container && isDetailPanelOpen()) return false;
  const target = container || document.body;
  const text = target.innerText || '';
  return /リストの最後に到達しました|You've reached the end of the list/.test(text);
}

// =====================================================================
// 営業時間パース (複数枠 A/B 対応)
// =====================================================================
const WEEKDAY_IDX = { '月': 0, '火': 1, '水': 2, '木': 3, '金': 4, '土': 5, '日': 6 };
const IDX_TO_DAY = ['月', '火', '水', '木', '金', '土', '日'];
const NOON_HOUR = 12;

function formatHm(hour, minute) {
  if (hour === '' || hour === null || hour === undefined) return '';
  const h = Number(hour) % 24;
  const m = Number(minute || 0);
  return `${h}:${String(m).padStart(2, '0')}`;
}

function minutesFromParts(hour, minute) {
  return Number(hour) * 60 + Number(minute || 0);
}

function formatMinutes(totalMinutes) {
  if (totalMinutes === '' || totalMinutes === null || totalMinutes === undefined) return '';
  const normalized = ((Number(totalMinutes) % (24 * 60)) + (24 * 60)) % (24 * 60);
  const h = Math.floor(normalized / 60);
  const m = normalized % 60;
  return `${h}:${String(m).padStart(2, '0')}`;
}

// v5.5: Google Business Profile が「2つの診療枠」を明示している場合は、
// その2枠をそのまま午前/午後へ保存する。
// 1枠しかない場合だけ12:00を境に分割する。
// 例: 09:00-13:30 / 15:00-21:00 -> 09:00,13:30,15:00,21:00
//     16:00-23:30                 -> 午前空欄, 午後16:00-23:30
//     09:00-20:00                 -> 09:00-12:00 / 12:00-20:00
function splitClinicSessions(times) {
  const noon = NOON_HOUR * 60;
  const normalized = (times || [])
    .map(t => ({
      open: minutesFromParts(t.open, t.openMinute),
      close: minutesFromParts(t.close, t.closeMinute)
    }))
    .filter(t => Number.isFinite(t.open) && Number.isFinite(t.close) && t.close > t.open)
    .sort((a, b) => a.open - b.open || a.close - b.close);

  // Google側の重複表記を除く。
  const unique = [];
  const seen = new Set();
  for (const t of normalized) {
    const key = `${t.open}-${t.close}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(t);
  }

  if (!unique.length) return { openA: '', closeA: '', openB: '', closeB: '' };

  // Googleが2枠以上を明示している場合は、昼休憩を推測せず実データを優先する。
  // 典型的な 09:00-13:30 / 15:00-18:30 を、そのままA/Bへ入れる。
  if (unique.length >= 2) {
    const first = unique[0];
    const second = unique[1];

    // 1枠目が午前から始まるなら1枠目=A、2枠目=B。
    if (first.open < noon) {
      return {
        openA: formatMinutes(first.open),
        closeA: formatMinutes(first.close),
        openB: formatMinutes(second.open),
        closeB: formatMinutes(second.close)
      };
    }

    // 2枠とも午後開始なら、午前は空欄。CSVの4列では午後2枠を表現できないため、
    // 最初の午後枠をBへ入れ、全枠は rawHours に残す。
    return {
      openA: '', closeA: '',
      openB: formatMinutes(first.open),
      closeB: formatMinutes(first.close)
    };
  }

  // 1枠だけの場合。
  const only = unique[0];
  if (only.close <= noon) {
    return {
      openA: formatMinutes(only.open), closeA: formatMinutes(only.close),
      openB: '', closeB: ''
    };
  }
  if (only.open >= noon) {
    return {
      openA: '', closeA: '',
      openB: formatMinutes(only.open), closeB: formatMinutes(only.close)
    };
  }

  // 1枠が正午をまたぐ場合だけ12:00で分割。
  return {
    openA: formatMinutes(only.open),
    closeA: formatMinutes(noon),
    openB: formatMinutes(noon),
    closeB: formatMinutes(only.close)
  };
}

function parseOpeningHours(rows) {
  if (!rows || !rows.length) {
    return { businessDays: '', openTimeA: '', closeTimeA: '', openTimeB: '', closeTimeB: '', regularHoliday: '' };
  }

  const blocks = [], explicitlyClosed = new Set();

  for (const row of rows) {
    const dayMatch = row.match(/^([月火水木金土日])曜日/);
    if (!dayMatch) continue;
    const dayIdx = WEEKDAY_IDX[dayMatch[1]];
    if (dayIdx === undefined) continue;

    if (/定休日|休業|休診/.test(row)) {
      explicitlyClosed.add(dayIdx);
      continue;
    }

    const times = [];
    let m;
    const re1 = /(\d{1,2})時(\d{1,2})分[〜～~ー－\-]\s*(\d{1,2})時(\d{1,2})分/g;
    while ((m = re1.exec(row)) !== null) {
      let open = parseInt(m[1], 10), close = parseInt(m[3], 10);
      if (close < open) close += 24;
      times.push({ open, close, openMinute: parseInt(m[2], 10), closeMinute: parseInt(m[4], 10) });
    }
    if (!times.length) {
      const re2 = /(\d{1,2}):(\d{2})\s*[〜～~ー－\-]\s*(\d{1,2}):(\d{2})/g;
      while ((m = re2.exec(row)) !== null) {
        let open = parseInt(m[1], 10), close = parseInt(m[3], 10);
        if (close < open) close += 24;
        times.push({ open, close, openMinute: parseInt(m[2], 10), closeMinute: parseInt(m[4], 10) });
      }
    }

    if (/24時間営業/.test(row) && !times.length) {
      times.push({ open: 0, close: 24, openMinute: 0, closeMinute: 0 });
    }

    if (times.length > 0) {
      const sessions = splitClinicSessions(times);
      blocks.push({ dayIdx, ...sessions });
    }
  }

  const activeDays = new Set(blocks.map(b => b.dayIdx));
  let regularHoliday = '';
  if (activeDays.size === 7) {
    regularHoliday = '無休';
  } else if (activeDays.size > 0) {
    regularHoliday = IDX_TO_DAY.filter((_, i) => !activeDays.has(i)).join('・');
  } else if (explicitlyClosed.size > 0) {
    regularHoliday = IDX_TO_DAY.filter((_, i) => explicitlyClosed.has(i)).join('・');
  }

  const businessDays = activeDays.size > 0
    ? [...activeDays].sort((a, b) => a - b).map(i => IDX_TO_DAY[i]).join('・')
    : '';

  // 曜日ごとに営業時間が異なる場合、最も多くの曜日で採用されている時間帯を
  // コムデスクの午前/午後4項目へ入れる。既存コムデスク値があれば後段で上書きしない。
  let representative = { openA: '', closeA: '', openB: '', closeB: '' };
  if (blocks.length) {
    const counts = new Map();
    for (const b of blocks) {
      const sig = [b.openA, b.closeA, b.openB, b.closeB].join('|');
      const current = counts.get(sig) || { count: 0, firstDay: b.dayIdx, block: b };
      current.count++;
      current.firstDay = Math.min(current.firstDay, b.dayIdx);
      counts.set(sig, current);
    }
    representative = [...counts.values()]
      .sort((a, b) => b.count - a.count || a.firstDay - b.firstDay)[0].block;
  }

  return {
    businessDays,
    openTimeA: representative.openA || '',
    closeTimeA: representative.closeA || '',
    openTimeB: representative.openB || '',
    closeTimeB: representative.closeB || '',
    regularHoliday
  };
}

// =====================================================================
// ジャンル正規化
// =====================================================================
const GENRE_NORMALIZE_MAP = {
  'カフェ': 'カフェ',
  '喫茶': '喫茶店',
  '喫茶店': '喫茶店',
  '珈琲': '喫茶店',
  'コーヒーショップ': '喫茶店',

  '居酒屋': '居酒屋',

  'スナック': 'スナック',
  'ラウンジ': 'スナック',
  '軽食店': 'スナック',

  'バー': 'Bar',
  'Bar': 'Bar',
  'BAR': 'Bar',
  'バル': 'Bar',
  'ワインバー': 'Bar',
  'ビアバー': 'Bar',

  'パン': 'パン屋',
  'パン屋': 'パン屋',
  'ベーカリー': 'パン屋',
  'サンドイッチ': 'パン屋',

  '焼鳥': '焼き鳥',
  '焼き鳥': '焼き鳥',
  '焼きとり': '焼き鳥',
  '鳥料理': '焼き鳥',
  '串焼き': '焼き鳥',

  'お好み焼き': 'お好み焼き',
  'お好み焼': 'お好み焼き',
  'もんじゃ': 'お好み焼き',
  'たこ焼き': 'お好み焼き',
  '鉄板焼き': 'お好み焼き',

  '焼肉': '焼肉',
  '焼き肉': '焼肉',
  'ホルモン': '焼肉',

  'スイーツ': 'スイーツ',
  'デザート': 'スイーツ',
  'ケーキ': 'スイーツ',
  '洋菓子': 'スイーツ',
  '和菓子': 'スイーツ',

  '中華': '中華',
  '中華料理': '中華',
  '餃子': '中華',
  '台湾料理': '中華',
  '四川料理': '中華',

  'ハンバーガー': 'ハンバーガー',
  'バーガー': 'ハンバーガー',

  'そば': '蕎麦・うどん',
  '蕎麦': '蕎麦・うどん',
  'うどん': '蕎麦・うどん',

  '寿司': '寿司',
  '鮨': '寿司',
  'すし': '寿司',
  '回転寿司': '寿司',

  '和食': '和食',
  '日本料理': '和食',
  '割烹': '和食',
  '懐石': '和食',
  '海鮮': '和食',
  '魚介': '和食',
  'うなぎ': '和食',
  '天ぷら': '和食',
  'しゃぶしゃぶ': '和食',
  'すき焼き': '和食',
  '鍋': '和食',
  'とんかつ': '和食',

  '洋食': '洋食',
  'イタリアン': '洋食',
  'イタリア料理': '洋食',
  'フレンチ': '洋食',
  'フランス料理': '洋食',
  'ビストロ': '洋食',
  'スペイン料理': '洋食',
  'ステーキ': '洋食',
  'ハンバーグ': '洋食',
  'パスタ': '洋食',
  'ピザ': '洋食',
  'カレー': '洋食',
  'ファミレス': '洋食',
  'ファミリーレストラン': '洋食',

  '定食': '定食・食堂',
  '定食屋': '定食・食堂',
  '食堂': '定食・食堂',

  '韓国': '韓国',
  '韓国料理': '韓国',

  '弁当': 'テイクアウト専門店',
  '弁当屋': 'テイクアウト専門店',
  'べんとう': 'テイクアウト専門店',
  '仕出し': 'テイクアウト専門店',
  'テイクアウト': 'テイクアウト専門店',
  'テイクアウト専門店': 'テイクアウト専門店',
  '持ち帰り': 'テイクアウト専門店',

  'ラーメン': 'ラーメン',
  '中華そば': 'ラーメン',
  '拉麺': 'ラーメン',
  'つけ麺': 'ラーメン',
  '油そば': 'ラーメン',
  'まぜそば': 'ラーメン'
};

const GENRE_ALLOWED_MAP = {
  'カフェ': ['カフェ', '喫茶店', 'コーヒーショップ', 'コーヒーショップ・喫茶店', 'ドッグカフェ', 'カフェテリア', 'コーヒー焙煎所'],
  '喫茶店': ['カフェ', '喫茶店', 'コーヒーショップ', 'コーヒーショップ・喫茶店', 'ドッグカフェ', 'カフェテリア', 'コーヒー焙煎所'],
  '居酒屋': ['居酒屋', '焼き鳥', 'Bar', 'スナック'],
  '焼き鳥': ['焼き鳥', '居酒屋'],
  '和食': ['和食', '寿司'],
  '洋食': ['洋食'],
  '蕎麦・うどん': ['蕎麦・うどん'],
  '中華': ['中華'],
  '韓国': ['韓国'],
  'テイクアウト専門店': ['弁当', 'テイクアウト専門店']
};

// Googleマップ側から実ジャンルが取得できない/対応表に無い場合に、検索キーワードへ
// フォールバックする前に店名から優先的に拾うジャンルキーワード。具体的な複合語ほど
// 誤爆しにくいので先に判定させる。実データで「カフェ」検索時にラーメン屋・うどん屋等が
// 検索語のままカフェに確定してしまう不具合を確認済みのための対応。
const NAME_GENRE_PRIORITY_LIST = [
  ['中華そば', 'ラーメン'],
  ['油そば', 'ラーメン'],
  ['まぜそば', 'ラーメン'],
  ['つけ麺', 'ラーメン'],
  ['拉麺', 'ラーメン'],
  ['タンメン', 'ラーメン'],
  ['ラーメン', 'ラーメン'],
  ['麺屋', 'ラーメン'],
  ['麺や', 'ラーメン'],
  ['讃岐', '蕎麦・うどん'],
  ['うどん', '蕎麦・うどん'],
  ['そば', '蕎麦・うどん'],
  ['お好み焼き', 'お好み焼き'],
  ['もんじゃ', 'お好み焼き'],
  ['焼き鳥', '焼き鳥'],
  ['焼鳥', '焼き鳥'],
  ['焼き肉', '焼肉'],
  ['焼肉', '焼肉'],
  ['スナック', 'スナック'],
  ['寿司', '寿司'],
  ['鮨', '寿司'],
  ['うなぎ', '和食'],
  ['鰻', '和食'],
  ['天ぷら', '和食'],
  ['中華', '中華'],
  ['餃子', '中華'],
  ['韓国', '韓国'],
  ['ハンバーガー', 'ハンバーガー'],
  ['ベーカリー', 'パン屋'],
  ['パン屋', 'パン屋'],
  ['スイーツ', 'スイーツ'],
  ['弁当', 'テイクアウト専門店']
];

function findGenreFromStoreName(storeName) {
  const nameText = String(storeName || '').normalize('NFKC');
  if (!nameText) return '';
  const hit = NAME_GENRE_PRIORITY_LIST.find(pair => nameText.includes(pair[0]));
  return hit ? hit[1] : '';
}

function normalizeGenre(sourceGenre, searchGenre = '', storeName = '') {
  const nameText = String(storeName || '').normalize('NFKC');

  // ★最優先ルール: 店名に「居酒屋」と明記されている店は、Googleマップ側の
  // カテゴリ判定が何であっても（Bar等に分類されていても）必ず「居酒屋」に
  // 確定させる。他の広い受け皿ジャンルに絶対に紛れさせないための最優先チェック。
  if (nameText.includes('居酒屋')) return '居酒屋';

  // ★最優先ルール: 店名にカフェ関連キーワード（カフェ/coffee/珈琲/喫茶等）が
  // 含まれる場合も、Googleマップ側のジャンル抽出結果に関わらず必ず「カフェ」に
  // 確定させる。実データで「検索:カフェ」でヒットした店が、抽出不良により
  // 取得元ジャンルが「洋食」等の無関係な値になり、店名が明らかにカフェ
  // （例:「MIFUNEYAMA COFFEE」「cafe Bohemian」等）でも洋食に混入する不具合を
  // 88件規模で確認済み。居酒屋と同様、店名の方が抽出結果より信頼できるため
  // 最優先で判定する。
  if (isCafeRelated('', nameText)) return 'カフェ';

  // ★最優先ルール: 店名にラーメン・蕎麦うどん・寿司・焼肉等の具体的なジャンル語が
  // 含まれる場合も、Googleマップ側の抽出結果が既に「有効な別ジャンル」に見えていても
  // 店名を優先する。実データで「横浜ラーメン寺田家」「竹岡式ラーメンたか木屋白井店」等、
  // 店名に「ラーメン」と明記された店が、抽出不良で「カフェ」等の別の有効なジャンルに
  // なってしまい、raw未取得時のみ店名を見る従来ロジックでは拾えないケースを確認した
  // ため、居酒屋・カフェと同様に無条件の最優先チェックへ格上げする。
  const nameGenrePriority = findGenreFromStoreName(nameText);
  if (nameGenrePriority) return nameGenrePriority;

  const raw = String(sourceGenre || '').normalize('NFKC').trim();

  // Googleマップ側から実際のジャンルが全く取得できなかった場合は検索語を使う
  // （店名からの判定は上の最優先ルールで既に試行済み）。
  if (!raw) {
    return (searchGenre || '').normalize('NFKC').trim();
  }

  if (GENRE_NORMALIZE_MAP[raw]) return GENRE_NORMALIZE_MAP[raw];
  for (const [key, normalized] of Object.entries(GENRE_NORMALIZE_MAP)) {
    if (raw.includes(key)) {
      return normalized;
    }
  }

  // それでも決まらなければ、検索語で上書きせず実際に取得した表記をそのまま使う。
  return raw;
}

function isCafeRelated(sourceGenre, storeName = '') {
  const text = `${sourceGenre || ''} ${storeName || ''}`.normalize('NFKC').toLowerCase();
  const cafeKeywords = [
    'カフェ',
    '喫茶',
    '珈琲',
    'コーヒー',
    'coffee',
    'cafe',
    'カフェテリア',
    'ドッグカフェ',
    'コーヒーショップ',
    'コーヒー焙煎所'
  ];
  return cafeKeywords.some(keyword => text.includes(keyword.toLowerCase()));
}

function scoreCandidateForDetail(item, searchGenre, searchArea, scrapeOptions = {}) {
  if (scrapeOptions.scrapeMode === 'exhaustive') return 999;

  const text = `${item.name || ''} ${item.listText || ''}`.normalize('NFKC').toLowerCase();
  const genre = String(searchGenre || '').normalize('NFKC').toLowerCase();
const excluded = [
  'ホテル', 'キャンプ場', '観光案内所', '公園', '駐車場', '神社', '寺', '道の駅', '観光施設', 'レジャー施設',
  'ビリヤード', 'ボウリング', 'カラオケ', 'ゲームセンター', 'パチンコ', 'スロット', '雀荘', '麻雀',
  'スーパー銭湯', '温浴施設', '銭湯', 'フィットネス', 'スポーツジム', '映画館', 'ネットカフェ', '漫画喫茶'
];  let score = 0;

  if (genre && text.includes(genre)) score += 50;
  if ((genre === 'カフェ' || genre === '喫茶店') && isCafeRelated(text, item.name)) score += 40;
  if (item.name && genre && String(item.name).normalize('NFKC').toLowerCase().includes(genre)) score += 40;
  if (searchArea) {
    const target = parseTargetArea(searchArea);
    const areaHints = [target.city, scrapeOptions.subArea, scrapeOptions.subAreaLabel].filter(Boolean);
    const normalizedText = normalizeAddressText(text);
    if (areaHints.some(hint => normalizedText.includes(normalizeAddressText(hint)))) {
      score += 10;
    } else if (target.city) {
      // [EFFICIENCY] カードのテキストに、対象と明確に異なる市区町村名が
      // 書かれている場合は、詳細パネルを開く前に強めの減点で弾く。
      // 曖昧/情報なしの場合は減点しない（誤って正しい候補を落とさないため）。
      const guessedCity = guessCityFromSnippet(text);
      if (guessedCity && guessedCity !== normalizeAddressText(target.city)) {
        score -= 60;
      }
    }
  }
  if (excluded.some(word => text.includes(word.normalize('NFKC').toLowerCase()))) score -= 100;

  return score;
}

function normalizePhoneNumber(value) {
  const normalized = String(value || '').normalize('NFKC');
  const match = normalized.match(/0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}/);
  return match ? match[0].replace(/\s+/g, '-') : normalized.replace(/^電話番号[：:]\s*/, '').trim();
}

function extractRawGenreFromPanel() {
  const spans = Array.from(
    document.querySelectorAll('[role="main"] .W4Efsd span')
  );

  const candidates = spans
    .map(el => el.textContent.trim())
    .filter(t =>
      t.length >= 2 &&
      t.length <= 30 &&
      t !== '·' && t !== '・' && t !== ',' && t !== '/' &&
      !/^[¥￥\d,\s〜～\-－・]+$/.test(t) &&
      !/^\d(?:\.\d)?\s*\(\s*[\d,]+\s*\)$/.test(t) &&
      !/^\d(?:\.\d)?\s*[★☆]?\s*[\d,]*$/.test(t) &&
      !/^\d{1,2}:\d{2}/.test(t) &&
      !t.includes('クチコミ') &&
      !t.includes('口コミ') &&
      !t.includes('営業') &&
      !t.includes('定休') &&
      !t.includes('★') &&
      !t.includes('レビュー')
    );

  for (const candidate of candidates) {
    if (GENRE_NORMALIZE_MAP[candidate]) return candidate;
  }
  for (const candidate of candidates) {
    const hit = Object.keys(GENRE_NORMALIZE_MAP).find(
      key => candidate.includes(key) || key.includes(candidate)
    );
    if (hit) return candidate;
  }

  if (candidates.length > 0) return candidates[0];

  const h1El = document.querySelector('[role="main"] h1');
  if (h1El) {
    let el = h1El.parentElement;
    for (let depth = 0; depth < 3; depth++) {
      if (!el) break;
      const siblings = Array.from(el.children);
      const h1Idx = siblings.findIndex(c => c.contains(h1El));
      for (let i = h1Idx + 1; i < Math.min(h1Idx + 4, siblings.length); i++) {
        const text = siblings[i]?.textContent?.trim() || '';
        if (
          text.length >= 2 && text.length <= 40 &&
          !/^[\d¥￥,円〜～\s・]+$/.test(text) &&
          !text.includes('クチコミ') && !text.includes('★') &&
          !text.includes('営業') && !text.includes('定休')
        ) return text;
      }
      el = el.parentElement;
    }
  }

  return '';
}

// =====================================================================
// 詳細パネルスクレイピング (改善版: 動的待機・phoneStatus/hasWebsiteStatus付き)
// =====================================================================
async function scrapeDetailPanel(placeUrl, cardName = '', searchGenre = '', websiteField = null) {
  // 店名確認: 前店舗でないか、正しいパネルが開いているか確認（動的待機）
  const expectedFromUrl = extractNameFromUrl(placeUrl);
  const expectedForPanel = !isJunkPanelName(cardName) ? cardName : expectedFromUrl;
  let activePanel = getActiveMainPanel(expectedForPanel);
  if (!activePanel) {
    await waitForElement('[role="main"] h1', 2500);
    activePanel = getActiveMainPanel(expectedForPanel);
  }
  let name = activePanel?.querySelector('h1')?.textContent?.trim() || '';
  if (isJunkPanelName(name)) name = expectedFromUrl || (!isJunkPanelName(cardName) ? cardName : '');

  const googleGenre = extractRawGenreFromPanel();
  // クリニック版では検索ジャンルを表示・内部分類の基準にする。
  // Google UIの評価値（例: 5.0(3)）をジャンルとして誤取得してもCSV名・判定へ流さない。
  const genre = String(searchGenre || '').trim() || normalizeGenre(googleGenre, searchGenre, name);

  // 営業時間トグルをクリック（動的待機）
  activePanel = getActiveMainPanel(expectedForPanel) || activePanel;
  const hoursToggle = activePanel?.querySelector('button[data-item-id="oh"]');
  if (hoursToggle && hoursToggle.getAttribute('aria-expanded') !== 'true') {
    hoursToggle.click();
  }

  // 住所（動的待機）
  // [role="main"] でスコープ: 検索結果一覧やカルーセルにある別店舗の
  // 同名data-item-id要素を拾ってしまう取り違えを防ぐ
  let address = '';
  let addrBtn = activePanel?.querySelector('button[data-item-id="address"]');
  if (!addrBtn) {
    await sleep(300);
    activePanel = getActiveMainPanel(expectedForPanel) || activePanel;
    addrBtn = activePanel?.querySelector('button[data-item-id="address"]');
  }
  if (addrBtn) {
    const raw = addrBtn.getAttribute('aria-label') || addrBtn.textContent.trim();
    address = raw.replace(/^住所[：:]\s*/, '').trim();
  }

  // 電話番号 + phoneStatus（動的待機）
  // 同様に[role="main"]でスコープ。一覧カードの「発信」ボタンにも
  // button[data-item-id^="phone:tel:"] と同じ属性が付くことがあり、
  // スコープなしだと無関係な店舗の電話番号を拾ってしまっていた
  let phone = '';
  let phoneStatus = 'missing';
  let phoneBtn = activePanel?.querySelector('button[data-item-id^="phone:tel:"]');
  if (!phoneBtn) {
    await sleep(300);
    activePanel = getActiveMainPanel(expectedForPanel) || activePanel;
    phoneBtn = activePanel?.querySelector('button[data-item-id^="phone:tel:"]');
  }
  if (phoneBtn) {
    const itemId = phoneBtn.getAttribute('data-item-id') || '';
    phone = itemId.replace('phone:tel:', '').trim() || phoneBtn.textContent.trim();
    phone = normalizePhoneNumber(phone);
    phoneStatus = phone ? 'complete' : 'missing';
  } else {
    // 電話番号要素が存在しない: 店名・住所が取れていれば掲載なしと判断
    if (address && name && name !== '結果') {
      phoneStatus = 'not_available';
    } else {
      phoneStatus = 'missing';
    }
  }

  // Google Business Profile の「ウェブサイト」欄を取得。
  // v5.3では「医院名の厳密一致 + 住所/電話を含むプロフィール指紋の安定 +
  // 同じHP URLの複数回一致」をすべて満たしたURLだけを採用する。
  // 少しでも紐付けが曖昧なら、誤URLより空欄を優先する。
  let hasWebsite = '無';
  let hasWebsiteStatus = 'no_website';
  let websiteUrl = '';
  const wf = websiteField || { status: 'ambiguous', url: '' };
  if (wf.status === 'fresh' && wf.url) {
    websiteUrl = unwrapGoogleExternalUrlStrict(wf.url);
    if (websiteUrl) {
      hasWebsite = '有';
      hasWebsiteStatus = 'has_website';
    }
  } else if (wf.status === 'no_website') {
    hasWebsiteStatus = 'no_website';
  } else {
    // 不明・更新未確認。後で再取得できるようunknownにする。
    hasWebsiteStatus = 'unknown';
  }

  // 営業時間 (動的待機でtableが開くのを待つ)
  // ここも[role="main"]でスコープし、他要素のtrを拾わないようにする
  if (hoursToggle) {
    await sleep(350);
    activePanel = getActiveMainPanel(expectedForPanel) || activePanel;
  }

  const rawHourRows = Array.from((activePanel || document).querySelectorAll('tr'))
    .map(tr => tr.textContent.trim())
    .filter(t => /^[月火水木金土日]曜日/.test(t));

  const rawHours = rawHourRows.join('\n').replace(/\ue000|\ue001/g, '').trim();
  const hourRowsForParse = rawHourRows.map(t => t.replace(/\s+/g, ''));
  const parsed = parseOpeningHours(hourRowsForParse);

  if (!name || name === '結果') {
    const h1Text = getActiveMainPanel(expectedForPanel)?.querySelector('h1')?.textContent?.trim() || '';
    if (h1Text && h1Text !== '結果') name = h1Text;
    else name = extractNameFromUrl(placeUrl);
  }

  return {
    name, genre, googleGenre, address, phone, phoneStatus,
    websiteUrl, hasWebsite, hasWebsiteStatus, rawHours, ...parsed
  };
}

// =====================================================================
// コンテナ取得
// =====================================================================
function scoreScrollContainer(el) {
  if (!el || el === document.body) return 0;
  const linkCount = el.querySelectorAll('a[href*="/maps/place/"]').length;
  if (linkCount === 0) return 0;

  const rect = el.getBoundingClientRect();
  const scrollable = Math.max(0, el.scrollHeight - el.clientHeight);
  const style = window.getComputedStyle(el);
  const overflowScore = /auto|scroll/.test(style.overflowY) ? 800 : 0;
  const roleScore = el.getAttribute('role') === 'feed' ? 1200 : 0;
  const sizeScore = rect.height > 200 ? 300 : 0;

  return roleScore + overflowScore + sizeScore + scrollable + linkCount * 20;
}

function getScrollContainer() {
  const links = Array.from(document.querySelectorAll('a[href*="/maps/place/"]'));
  if (!links.length) return null;

  const candidates = new Set([
    ...document.querySelectorAll('div[role="feed"], .m6QErb[aria-label], .m6QErb.ecceSd')
  ]);

  for (const link of links) {
    let el = link.parentElement;
    while (el && el !== document.body) {
      candidates.add(el);
      el = el.parentElement;
    }
  }

  let best = null;
  let bestScore = 0;
  for (const el of candidates) {
    const score = scoreScrollContainer(el);
    if (score > bestScore) {
      best = el;
      bestScore = score;
    }
  }

  return best;
}

function getResultLinks(container) {
  return Array.from((container || document).querySelectorAll('a[href*="/maps/place/"]'));
}

function normalizeGoogleMapsPlaceUrl(url) {
  try {
    const parsed = new URL(url, location.href);
    if (!parsed.pathname.includes('/maps/place/')) return '';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  } catch (_) {
    return String(url || '').split('?')[0];
  }
}

async function collectAllPlaceUrlsFromResults(options = {}) {
  const {
    maxEmptyScrolls = 15,
    scrollDelayMs = 2200,
    maxScrolls = 600,
    maxResults = 0,
    verificationPasses = 3
  } = options;

  let feed = getScrollContainer()
    || document.querySelector('div[role="feed"]')
    || document.querySelector('[aria-label*="検索結果"]')
    || document.querySelector('[aria-label*="Results"]');
  if (!feed) return { items: [], reason: 'scroll_container_not_found', scrolls: 0, passes: 0 };

  const seen = new Map();
  let totalScrolls = 0;
  let reason = 'max_scroll_count';
  let stableVerificationPasses = 0;
  let completedPasses = 0;
  const requiredPasses = Math.max(1, Number(verificationPasses) || 1);

  // 速度より網羅性を優先。トップ→最下部を複数回走査し、
  // 2回連続で新規URLが増えないことを確認してから全件収集完了とする。
  for (let pass = 1; pass <= requiredPasses && !stopRequested; pass++) {
    completedPasses = pass;
    feed = getScrollContainer() || feed;
    try {
      feed.scrollTo(0, 0);
      feed.scrollTop = 0;
      feed.dispatchEvent(new Event('scroll', { bubbles: true }));
    } catch (_) {}
    await sleep(1200);

    const beforePassCount = seen.size;
    let emptyAtBottomCount = 0;
    let lastCount = seen.size;
    let passEndSeen = false;

    reportV3Log(`検索結果の網羅確認 pass ${pass}/${requiredPasses} 開始 / 既知URL=${seen.size}`);

    for (let i = 0; i < maxScrolls && !stopRequested; i++) {
      feed = getScrollContainer() || feed;
      totalScrolls++;

      // 結果リスト内だけを対象にする。詳細パネル等の /maps/place/ リンクは混ぜない。
      const links = getResultLinks(feed);
      const beforeCount = seen.size;
      for (const link of links) {
        const url = normalizeGoogleMapsPlaceUrl(link.href);
        if (!url || seen.has(url)) continue;
        seen.set(url, {
          url,
          name: link.getAttribute('aria-label') || extractNameFromUrl(url),
          listText: link.closest('.Nv2PK, [role="article"]')?.textContent || link.textContent || '',
          listRank: seen.size + 1,
          firstSeenIndex: seen.size + 1,
          scrollBatch: totalScrolls,
          visibleTop: 0,
          queuedAt: Date.now(),
          skipReason: ''
        });
      }

      const added = seen.size - beforeCount;
      const metrics = getScrollableMetrics(feed);
      const atBottom = metrics.remaining < 90;
      const endMessage = isEndOfList(feed);
      reportV3Log(`検索結果収集中: pass=${pass} scroll=${i + 1} / uniqueUrls=${seen.size} / added=${added} / bottom=${atBottom ? 'yes' : 'no'}`);

      if (maxResults > 0 && seen.size >= maxResults) {
        reason = 'max_results_reached';
        passEndSeen = true;
        break;
      }

      if (endMessage) {
        reason = 'end_of_list_message';
        passEndSeen = true;
        // 終端表示後も一度待ってDOMの遅延追加を拾う。
        await sleep(1800);
        feed = getScrollContainer() || feed;
        for (const link of getResultLinks(feed)) {
          const url = normalizeGoogleMapsPlaceUrl(link.href);
          if (!url || seen.has(url)) continue;
          seen.set(url, {
            url,
            name: link.getAttribute('aria-label') || extractNameFromUrl(url),
            listText: link.closest('.Nv2PK, [role="article"]')?.textContent || link.textContent || '',
            listRank: seen.size + 1,
            firstSeenIndex: seen.size + 1,
            scrollBatch: totalScrolls,
            visibleTop: 0,
            queuedAt: Date.now(),
            skipReason: ''
          });
        }
        break;
      }

      if (seen.size === lastCount && atBottom) {
        emptyAtBottomCount++;
      } else {
        emptyAtBottomCount = 0;
        lastCount = seen.size;
      }

      // 「新規がない」だけでは途中終了しない。最下部で長時間増えない場合だけ1パス完了。
      if (emptyAtBottomCount >= maxEmptyScrolls) {
        reason = 'stable_bottom_no_new_url';
        passEndSeen = true;
        break;
      }

      try {
        const distance = Math.max(900, Math.floor((feed.clientHeight || 900) * 0.85));
        feed.scrollBy({ top: distance, behavior: 'auto' });
        feed.scrollTop = Math.min((feed.scrollTop || 0) + distance, feed.scrollHeight || 0);
        feed.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: distance }));
        feed.dispatchEvent(new Event('scroll', { bubbles: true }));
      } catch (_) {
        feed.scrollTop = Math.min((feed.scrollTop || 0) + 1000, feed.scrollHeight || 0);
      }

      await sleep(scrollDelayMs);
    }

    const addedThisPass = seen.size - beforePassCount;
    if (addedThisPass === 0 && passEndSeen) stableVerificationPasses++;
    else stableVerificationPasses = 0;

    reportV3Log(`検索結果の網羅確認 pass ${pass} 完了 / URL=${seen.size} / 今回追加=${addedThisPass} / 連続安定=${stableVerificationPasses}`);

    // 初回収集後、2回連続の再走査で新規0なら十分に安定したとみなす。
    if (pass >= 3 && stableVerificationPasses >= 2) {
      reason = 'verified_complete_two_stable_passes';
      break;
    }
  }

  if (stopRequested) reason = 'stopped_by_user';
  reportV3Log(`検索結果全件収集終了: uniqueUrls=${seen.size} / reason=${reason} / passes=${completedPasses} / scrolls=${totalScrolls}`);
  return { items: Array.from(seen.values()), reason, scrolls: totalScrolls, passes: completedPasses };
}

async function resetResultsScrollToTop(container) {
  const target = container || getScrollContainer();
  if (!target) return;
  try {
    target.scrollTo(0, 0);
    target.scrollTop = 0;
    target.dispatchEvent(new Event('scroll', { bubbles: true }));
  } catch (_) {}
  await sleep(700);
}

// =====================================================================
// カード情報取得 (カード基準 .Nv2PK)
// =====================================================================
function getResultCardElements(container) {
  const root = container || document;
  const cards = Array.from(root.querySelectorAll('.Nv2PK'));
  if (cards.length) {
    return cards.filter(card => {
      const rect = card.getBoundingClientRect();
      return rect.height > 20 && rect.width > 100;
    });
  }
  // フォールバック: リンクの親要素
  const fallbackCards = [];
  for (const link of getResultLinks(root)) {
    const card = link.closest('[role="article"], div[jsaction][data-result-index]') || link.parentElement;
    if (card) fallbackCards.push(card);
  }
  return Array.from(new Set(fallbackCards)).filter(card => {
    const rect = card.getBoundingClientRect();
    return rect.height > 20 && rect.width > 100;
  });
}

// =====================================================================
// cardItemの情報のみ（DOM参照なし）を構築
// クリック直前に再解決する
// =====================================================================
function buildCardInfo(card, seenOrder, rankState, queueBatchNo) {
  const link = card.querySelector('a[href*="/maps/place/"]');
  const url = (link?.href || '').split('?')[0];
  const rect = card.getBoundingClientRect();

  let name = '';
  const label = link?.getAttribute('aria-label') || card?.getAttribute?.('aria-label') || '';
  if (label && !/(^結果|について$|のルート|^地図|口コミ$)/.test(label)) {
    name = label.trim();
  }
  if (!name) {
    const headline = card?.querySelector('.fontHeadlineSmall, [class*="fontHeadline"]');
    if (headline) name = headline.textContent.trim();
  }
  if (!name || isJunkPanelName(name)) name = extractNameFromUrl(url);

  if (!seenOrder.has(url)) {
    seenOrder.set(url, rankState.next++);
  }
  const listRank = seenOrder.get(url);

  return {
    url,
    name,
    listRank,
    firstSeenIndex: listRank,
    scrollBatch: queueBatchNo,
    visibleTop: rect.top,
    queuedAt: Date.now(),
    skipReason: ''
  };
}

// クリック直前にURLまたは店舗名で画面内のカード要素を再解決
function resolveCardElementByUrl(url, container) {
  const root = container || getScrollContainer() || document;
  const normalizedUrl = normalizeGoogleMapsPlaceUrl(url);
  const cards = getResultCardElements(root);
  for (const card of cards) {
    const link = card.querySelector('a[href*="/maps/place/"]');
    if (link && normalizeGoogleMapsPlaceUrl(link.href) === normalizedUrl) {
      return card;
    }
  }
  return null;
}

async function scrollToCardUrl(url, container, maxScrolls = 30) {
  let target = container || getScrollContainer();
  if (!target) return null;

  for (let i = 0; i < maxScrolls && !stopRequested; i++) {
    const found = resolveCardElementByUrl(url, target);
    if (found) {
      try { found.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (_) {}
      await sleep(120);
      return found;
    }

    const before = getScrollableMetrics(target);
    await scrollResultsList(target);
    target = getScrollContainer() || target;
    const after = getScrollableMetrics(target);

    if (isEndOfList(target) || (!after.remaining && after.top === before.top && after.linkCount === before.linkCount)) {
      break;
    }
  }

  return resolveCardElementByUrl(url, target);
}

// =====================================================================
// スクロール処理 (最後に処理したカード基準)
// =====================================================================
async function scrollResultsList(container, lastProcessedItem = null) {
  const target = container || getScrollContainer() || document.querySelector('[role="feed"]');
  if (!target) return false;

  const before = getScrollableMetrics(target);

  let distance = Math.max(900, Math.floor((target.clientHeight || 600) * 1.05));

  // 最後に処理したカードを基準にスクロール量を決定
  if (lastProcessedItem) {
    const cardEl = resolveCardElementByUrl(lastProcessedItem.url, target);
    if (cardEl) {
      const targetRect = target.getBoundingClientRect();
      const cardRect = cardEl.getBoundingClientRect();
      const cardOffset = cardRect.bottom - targetRect.top;
      if (Number.isFinite(cardOffset) && cardOffset > 0) {
        distance = Math.max(distance, Math.min(cardOffset + 180, (target.clientHeight || 600) * 1.35));
      }
    }
  }

  const wheelTargets = [
    target,
    target.closest('[role="feed"]'),
    target.querySelector('[role="feed"]'),
    document.querySelector('[role="feed"]'),
    document.scrollingElement,
    document.body
  ].filter(Boolean);

  try {
    target.focus?.();
    target.scrollBy({ top: distance, behavior: 'auto' });
    target.scrollTop = Math.min((target.scrollTop || 0) + distance, target.scrollHeight || 0);
  } catch (_) {
    target.scrollTop = before.top + distance;
  }

  for (const wheelTarget of new Set(wheelTargets)) {
    wheelTarget.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: distance }));
    wheelTarget.dispatchEvent(new KeyboardEvent('keydown', { key: 'PageDown', code: 'PageDown', keyCode: 34, which: 34, bubbles: true }));
  }
  window.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: distance }));
  document.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: distance }));
  target.dispatchEvent(new Event('scroll', { bubbles: true }));

  const lastLink = getResultLinks(target).at(-1);
  try { lastLink?.scrollIntoView({ block: 'end', behavior: 'auto' }); } catch (_) { }
  await sleep(250);
  try {
    target.scrollBy({ top: Math.floor(distance * 0.7), behavior: 'auto' });
    target.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: Math.floor(distance * 0.7) }));
  } catch (_) {}

  // 動的待機: カード数が増えるか、最大3秒待つ
  await waitUntilResultCardsChanged(before.linkCount, target, 3000);
  const after = getScrollableMetrics(target);

  return {
    moved: after.top !== before.top || after.height !== before.height || after.linkCount !== before.linkCount,
    before,
    after
  };
}

// =====================================================================
// スキップ・品質ログ
// =====================================================================
function msToSec(ms) {
  return (Math.max(0, ms) / 1000).toFixed(2);
}

function addTiming(stats, key, ms) {
  if (!stats.timings[key]) stats.timings[key] = { total: 0, count: 0 };
  stats.timings[key].total += Math.max(0, ms);
  stats.timings[key].count++;
}

function avgTiming(stats, key) {
  const t = stats.timings[key];
  if (!t || !t.count) return '0.00';
  return (t.total / t.count / 1000).toFixed(2);
}

const skipLogged = new Set();

function logSkip(item, reason, detail = '') {
  const key = `${item.url || item.listRank || '?'}:${reason}`;
  if (skipLogged.has(key)) return;
  skipLogged.add(key);
  reportV3Log(`一覧順位${item.listRank || '-'} / スキップ / 理由: ${reason}${detail ? ` / ${detail}` : ''}`);
}

function formatRate(count, total) {
  if (!total) return '0.0%';
  return `${((count / total) * 100).toFixed(1)}%`;
}

function createSpeedStats({ searchArea, searchGenre, searchKey, maxItems }) {
  return {
    startedAt: Date.now(),
    searchArea, searchGenre, searchKey, maxItems,
    urlCollected: 0, detailFetched: 0, saved: 0,
    skippedComplete: 0, skippedNotAvailable: 0, refetchPartial: 0, failed: 0,
    addressMissing: 0, areaExcluded: 0, genreExcluded: 0,
    saveCount: 0, lastSavedAt: 0,
    nameCount: 0, addressCount: 0, phoneCount: 0,
    hoursCount: 0, hpJudgedCount: 0, hpNotAvailableCount: 0, phoneNotAvailableCount: 0,
    scrollCount: 0, scrollNoIncreaseCount: 0, scrollLastIncreaseAt: Date.now(),
    scrollEndReason: '', scrollMaxStableCount: 3,
    timings: {
      search:    { total: 0, count: 0 },
      click:     { total: 0, count: 0 },
      panelWait: { total: 0, count: 0 },
      scrape:    { total: 0, count: 0 },
      back:      { total: 0, count: 0 },
      save:      { total: 0, count: 0 },
      scroll:    { total: 0, count: 0 }
    }
  };
}

function markRecordQuality(stats, record) {
  if (record.name)    stats.nameCount++;
  if (record.address) stats.addressCount++;
  if (record.phone)   stats.phoneCount++;
  if (record.rawHours) stats.hoursCount++;
  if (record.hasWebsiteStatus === 'has_website' || record.hasWebsiteStatus === 'no_website') stats.hpJudgedCount++;
  if (record.hasWebsiteStatus === 'no_website')  stats.hpNotAvailableCount++;
  if (record.phoneStatus === 'not_available')    stats.phoneNotAvailableCount++;
}

function buildSpeedSummary(stats, label = '速度ログ') {
  const elapsedSec = Math.max(1, Math.round((Date.now() - stats.startedAt) / 1000));
  const perItem = stats.detailFetched ? (elapsedSec / stats.detailFetched).toFixed(1) : '-';
  const hourly  = stats.detailFetched ? Math.round(stats.detailFetched / elapsedSec * 3600) : 0;
  const savedAt = stats.lastSavedAt ? new Date(stats.lastSavedAt).toLocaleTimeString() : '-';

  return [
    `${label}: ${stats.searchArea || '-'} | ジャンル: ${stats.searchGenre || '-'} | キーワード: ${stats.searchKey || '-'}`,
    `経過${elapsedSec}秒 / URL${stats.urlCollected}件 / 詳細${stats.detailFetched}件 / 失敗${stats.failed}件`,
    `除外 住所未取得${stats.addressMissing}件 / エリア外${stats.areaExcluded}件 / ジャンル不一致${stats.genreExcluded}件 / CSV出力${stats.saved}件`,
    `平均${perItem}秒/件 / 推定${hourly}件/時 / 保存${stats.saveCount}回(最終${savedAt}) / 完全スキップ${stats.skippedComplete}件 / 掲載なしスキップ${stats.skippedNotAvailable}件`,
    `内訳平均 探索${avgTiming(stats,'search')}秒 / パネル待ち${avgTiming(stats,'panelWait')}秒 / 情報取得${avgTiming(stats,'scrape')}秒 / 戻る${avgTiming(stats,'back')}秒 / スクロール${avgTiming(stats,'scroll')}秒 / 保存${avgTiming(stats,'save')}秒`,
    `取得率 店名${formatRate(stats.nameCount,stats.detailFetched)} 住所${formatRate(stats.addressCount,stats.detailFetched)} 電話${formatRate(stats.phoneCount,stats.detailFetched)} 営業時間${formatRate(stats.hoursCount,stats.detailFetched)} HP判定${formatRate(stats.hpJudgedCount,stats.detailFetched)}`,
    `スクロール${stats.scrollCount}回 / 増加なし連続${stats.scrollNoIncreaseCount}回 / 終了理由:${stats.scrollEndReason || '未確定'}`
  ].join(' | ');
}

// =====================================================================
// 完全取得済み・欠損・掲載なし判定
// =====================================================================
function getMissingFields(record) {
  const missing = [];
  if (!record?.name)        missing.push('name');
  if (!record?.address)     missing.push('address');
  if (!record?.url)         missing.push('url');
  if (!record?.source)      missing.push('source');
  if (!record?.scrapedAt)   missing.push('scrapedAt');
  // phoneStatus が 'missing' の場合のみ再取得対象（'not_available' はスキップ対象外）
  if (record?.phoneStatus === 'missing') missing.push('phone(missing)');
  // hasWebsiteStatus が 'unknown' の場合のみ再取得対象
  if (record?.hasWebsiteStatus === 'unknown') missing.push('hasWebsite(unknown)');
  return missing;
}

function isCompleteRecord(record) {
  return getMissingFields(record).length === 0;
}

async function loadExistingRecordsByUrl() {
  return new Promise(resolve => {
    try {
      chrome.storage.local.get(['scrapedData', 'v3_collectedData'], result => {
        const map = new Map();
        const all = [
          ...(Array.isArray(result.scrapedData) ? result.scrapedData : []),
          ...(Array.isArray(result.v3_collectedData) ? result.v3_collectedData : [])
        ];
        for (const record of all) {
          if (record?.url) map.set(record.url, record);
        }
        resolve(map);
      });
    } catch (_) {
      resolve(new Map());
    }
  });
}

// =====================================================================
// クエリ解析
// =====================================================================
function parseSearchMeta(query, overrideGenre = '') {
  const normalized = query.replace(/\s+/g, ' ').trim();
  if (overrideGenre) {
    const area = normalized.replace(overrideGenre, '').trim();
    return {
      searchGenre: overrideGenre,
      searchKey: area && overrideGenre ? `${area}×${overrideGenre}` : normalized
    };
  }
  if (/[×✖️]/.test(normalized)) {
    const parts = normalized.split(/[×✖️]/).map(s => s.trim()).filter(Boolean);
    const area = parts[0] || '';
    const genre = parts[1] || '';
    return {
      searchGenre: genre,
      searchKey: area && genre ? `${area}×${genre}` : normalized
    };
  }
  const tokens = normalized.split(/[\s\u3000]+/).filter(Boolean);
  if (tokens.length >= 2) {
    const genre = tokens[tokens.length - 1];
    const area = tokens.slice(0, tokens.length - 1).join('');
    return { searchGenre: genre, searchKey: `${area}×${genre}` };
  }
  return { searchGenre: normalized, searchKey: normalized };
}

// =====================================================================
// ジャンルフィルタ
// =====================================================================
function matchesTargetGenres(detail, targetGenres) {
  if (!targetGenres || targetGenres.length === 0) return true;
  return targetGenres.some(g => {
    const normalizedTarget = normalizeGenre(g, g);
    if (normalizedTarget === 'カフェ' && isCafeRelated(detail.googleGenre || detail.genre, detail.name)) {
      return true;
    }
    const values = [
      g,
      normalizedTarget,
      detail.genre,
      detail.googleGenre,
      normalizeGenre(detail.googleGenre, detail.searchGenre || '', detail.name)
    ].map(v => String(v || '').normalize('NFKC').toLowerCase()).filter(Boolean);

    const targetValues = [g, normalizedTarget]
      .map(v => String(v || '').normalize('NFKC').toLowerCase())
      .filter(Boolean);

    const allowedValues = (GENRE_ALLOWED_MAP[normalizedTarget] || [])
      .map(v => String(v || '').normalize('NFKC').toLowerCase());

    return (
      targetValues.some(target =>
        values.some(value => value.includes(target) || target.includes(value))
      ) ||
      allowedValues.some(allowed => values.includes(allowed))
    );
  });
}

// =====================================================================
// エリアフィルタ
// =====================================================================
function matchesSearchArea(detail, searchArea) {
  if (!searchArea || !searchArea.trim()) return true;
  const target = parseTargetArea(searchArea);
  return matchesTargetArea(detail.address || '', target.prefecture, target.city);
}

// =====================================================================
// メインループ
// =====================================================================
let isScrapingActive = false;
let stopRequested = false;
let currentFlushPromise = null;

async function flushBatch(pendingBatch) {
  if (!pendingBatch.length) return 0;
  const payload = [...pendingBatch];
  pendingBatch.length = 0;
  const result = await new Promise(res => {
    try {
      chrome.runtime.sendMessage({ action: 'updateData', data: payload }, response => {
        if (chrome.runtime.lastError) {
          res({ success: false, error: chrome.runtime.lastError.message });
          return;
        }
        res(response && typeof response === 'object' ? response : { success: true, count: payload.length });
      });
    } catch (e) { res({ success: false, error: String(e) }); }
  });

  if (!result.success) {
    // 保存失敗(容量超過等)を以前は検知せず「保存成功」扱いにしていたため、
    // 取得したはずのデータが気づかれないまま消えるリスクがあった。
    // 失敗時はバッファに戻して次回flushで再試行し、ログでも警告する。
    pendingBatch.push(...payload);
    reportV3Log(`⚠️ データ保存失敗: ${payload.length}件を再試行キューに戻しました / ${result.error || '原因不明'}`);
    return 0;
  }

  return payload.length;
}

function reportV3Log(message) {
  try {
    chrome.runtime.sendMessage({ action: 'v3_contentLog', message }, () => {
      if (chrome.runtime.lastError) { /* ignore */ }
    });
  } catch (_) { }
}

// =====================================================================
// スクレイピング本体 (v4.0)
// =====================================================================
// =====================================================================
// [ZOOM-FIX2] 縮尺の強制補正
// URLに @lat,lng,zoom を指定しても、該当ジャンルの店が少ないエリアでは
// Googleマップ側が自動的に検索範囲を広げ、指定した縮尺を無視してしまう
// ことがある。ページ読み込み後、実際の縮尺がまだ広すぎる場合は
// ズームインボタンを操作して物理的に縮尺を戻す。
// =====================================================================
function getCurrentZoomFromUrl() {
  const m = window.location.href.match(/,(\d+(?:\.\d+)?)z/);
  return m ? parseFloat(m[1]) : null;
}

async function enforceMinZoom(targetZoom, maxAdjustSteps = 15) {
  if (!targetZoom) return;
  let zoom = getCurrentZoomFromUrl();
  if (zoom === null || zoom >= targetZoom) return;

  reportV3Log(`🔍 縮尺が広すぎるため補正中（現在${zoom}z → 目標${targetZoom}z）`);

  const zoomInSelectors = [
    'button[aria-label="ズームイン"]',
    'button[aria-label="Zoom in"]',
    '#widget-zoom-in',
    'button.widget-zoom-in'
  ];

  let steps = 0;
  while (zoom !== null && zoom < targetZoom && steps < maxAdjustSteps) {
    let btn = null;
    for (const sel of zoomInSelectors) {
      btn = document.querySelector(sel);
      if (btn) break;
    }
    if (btn) {
      btn.click();
    } else {
      // ボタンが見つからない場合はマップ中央でホイールズームイベントをシミュレート
      const mapEl = document.querySelector('[role="main"]') || document.body;
      const rect = mapEl.getBoundingClientRect();
      mapEl.dispatchEvent(new WheelEvent('wheel', {
        bubbles: true,
        cancelable: true,
        clientX: rect.left + rect.width / 2,
        clientY: rect.top + rect.height / 2,
        deltaY: -200
      }));
    }
    await sleep(350);
    zoom = getCurrentZoomFromUrl();
    steps++;
  }

  reportV3Log(zoom !== null ? `🔍 縮尺補正完了: ${zoom}z` : '🔍 縮尺補正: URL取得不可のため確認できず');
}

async function startScraping(maxItems, targetGenres = [], searchArea = '', searchGenre = '', scrapeOptions = {}) {
  isScrapingActive = true;
  stopRequested = false;
  searchPageUrl = window.location.href;
  await reportState('active');
  const effectiveMaxItems = Number(maxItems) > 0 ? Number(maxItems) : Number.MAX_SAFE_INTEGER;

  const query = getCurrentQuery();
  const { searchGenre: parsedGenre, searchKey } = parseSearchMeta(query, searchGenre);
  const effectiveGenre = searchGenre || parsedGenre;
  const speedStats = createSpeedStats({ searchArea, searchGenre: effectiveGenre, searchKey, maxItems: effectiveMaxItems });
  const targetArea = parseTargetArea(searchArea);

  reportV3Log(`検索開始: ${searchArea || '-'} | ジャンル: ${effectiveGenre || '-'} | キーワード: ${searchKey || query || '-'}`);

  await sleep(400);
  await enforceMinZoom(scrapeOptions.targetZoom);

  let container = getScrollContainer();
  if (!container) {
    reportV3Log('エラー: コンテナが見つかりません');
    isScrapingActive = false;
    await reportState('done');
    return;
  }

  const existingRecords = await loadExistingRecordsByUrl();
  const processedUrls = new Set();
  const seenOrder = new Map();
  const queuedOrProcessingUrls = new Set();
  const attemptCounts = new Map();
  const pendingBatch = [];
  const BATCH_SIZE = 5;
  let totalProcessed = 0;
  let acquiredOrder = 0;
  let queueBatchNo = 0;
  let noNewCardCount = 0;
  let scrollAttempts = 0;
  let lastProcessedItem = null;
  const rankState = { next: 1 };
  const startTime = Date.now();

  reportV3Log('検索結果URLの全件収集を開始');
  const collectedResult = await collectAllPlaceUrlsFromResults({
    scrollDelayMs: scrapeOptions.scrollDelayMs || 2200,
    maxScrolls: scrapeOptions.maxScrolls || 600,
    maxEmptyScrolls: scrapeOptions.maxEmptyScrolls || 15,
    maxResults: 0,
    verificationPasses: scrapeOptions.verificationPasses || 3
  });
  speedStats.urlCollected = collectedResult.items.length;
  speedStats.scrollEndReason = collectedResult.reason;
  speedStats.scrollCount = collectedResult.scrolls;
  await resetResultsScrollToTop(container);
  container = getScrollContainer() || container;

  reportV3Log(`詳細取得フェーズ開始: URL${collectedResult.items.length}件`);

  // 詳細パネルを閉じない方式のため、前回パネル情報を追跡
  let prevPanelName = '';
  let prevPanelUrl = window.location.href;
  // 直前に確定保存したレコード（店名が違うのに住所・電話番号が完全一致する
  // ケースを検知するための最終防衛ライン。fieldsReadyのチェックをすり抜けて
  // 前店舗のデータが混入した場合の保険）
  let lastPushedRecord = null;

  const cardQueue = [];
  const precollectedItems = collectedResult.items;
  let precollectedIndex = 0;

  // =====================================================================
  // カードキュー構築（DOM参照を持たず情報のみ）
  // =====================================================================
  function buildCardQueue() {
    queueBatchNo++;
    if (precollectedIndex < precollectedItems.length) {
      const addedToQueue = [];
      while (precollectedIndex < precollectedItems.length && addedToQueue.length < 10) {
        const sourceItem = precollectedItems[precollectedIndex++];
        const item = { ...sourceItem, scrollBatch: queueBatchNo };
        const url = item.url;

        if (!url || processedUrls.has(url) || queuedOrProcessingUrls.has(url)) continue;

        const existing = existingRecords.get(url);
        if (existing && isCompleteRecord(existing)) {
          speedStats.skippedComplete++;
          logSkip(item, '完全取得済み', item.name || existing.name || url);
          continue;
        }

        if (existing && existing.phoneStatus === 'not_available' && existing.hasWebsiteStatus !== 'unknown' && isCompleteRecord(existing)) {
          speedStats.skippedNotAvailable++;
          logSkip(item, '掲載なしスキップ', item.name || existing.name || url);
          continue;
        }

        if (existing && !isCompleteRecord(existing)) {
          const missing = getMissingFields(existing).join(',');
          speedStats.refetchPartial++;
          reportV3Log(`欠損あり再取得: 一覧順位${item.listRank} / 欠損:${missing} / ${item.name || existing.name || url}`);
        }

        if ((attemptCounts.get(url) || 0) >= 3) {
          logSkip(item, '詳細取得失敗(3回)', item.name || url);
          continue;
        }

        const score = scoreCandidateForDetail(item, effectiveGenre, searchArea, scrapeOptions);
        item.preDetailScore = score;
        if (score < (scrapeOptions.minScore ?? 0)) {
          speedStats.genreExcluded++;
          logSkip(item, '詳細取得前フィルタ', `${item.name || url} / score:${score}`);
          continue;
        }

        queuedOrProcessingUrls.add(url);
        addedToQueue.push(item);
      }

      reportV3Log(`URLキュー補充batch${queueBatchNo}: キュー${addedToQueue.length}件 / 残り${precollectedItems.length - precollectedIndex}件`);
      return { items: addedToQueue, newSeenCount: addedToQueue.length };
    }

    const root = container || getScrollContainer();
    const viewportHeight = window.innerHeight || 900;
    const visibleCards = getResultCardElements(root)
      .map(card => ({ card, rect: card.getBoundingClientRect() }))
      .filter(({ rect }) => rect.top >= -100 && rect.top <= viewportHeight + 120)
      .sort((a, b) => (a.rect.top - b.rect.top) || (a.rect.left - b.rect.left));

    let newSeenCount = 0;
    const batchUrls = new Set();
    const addedToQueue = [];

    for (const { card } of visibleCards) {
      const link = card.querySelector('a[href*="/maps/place/"]');
      const url = (link?.href || '').split('?')[0];

      if (!url) {
        // URLなしスキップ（ログは初回のみ）
        continue;
      }

      if (!seenOrder.has(url)) {
        seenOrder.set(url, rankState.next++);
        newSeenCount++;
      }

      const item = buildCardInfo(card, seenOrder, { next: seenOrder.get(url) }, queueBatchNo);
      item.listRank = seenOrder.get(url);

      if (batchUrls.has(url)) {
        logSkip(item, 'URL重複（同batch内）', item.name || url);
        continue;
      }
      batchUrls.add(url);

      if (processedUrls.has(url) || queuedOrProcessingUrls.has(url)) {
        continue; // 既処理は静かにスキップ
      }

      const existing = existingRecords.get(url);
      if (existing && isCompleteRecord(existing)) {
        speedStats.skippedComplete++;
        logSkip(item, '完全取得済み', item.name || existing.name || url);
        continue;
      }

      // phoneStatus/hasWebsiteStatus が 'not_available' で他の主要項目が揃っていればスキップ
      if (existing && existing.phoneStatus === 'not_available' && existing.hasWebsiteStatus !== 'unknown' && isCompleteRecord(existing)) {
        speedStats.skippedNotAvailable++;
        logSkip(item, '掲載なしスキップ', item.name || existing.name || url);
        continue;
      }

      if (existing && !isCompleteRecord(existing)) {
        const missing = getMissingFields(existing).join(',');
        speedStats.refetchPartial++;
        reportV3Log(`欠損あり再取得: 一覧順位${item.listRank} / 欠損:${missing} / ${item.name || existing.name || url}`);
      }

      if ((attemptCounts.get(url) || 0) >= 3) {
        logSkip(item, '詳細取得失敗(3回)', item.name || url);
        continue;
      }

      queuedOrProcessingUrls.add(url);
      addedToQueue.push(item);
    }

    speedStats.urlCollected = seenOrder.size;
    reportV3Log(`カード収集batch${queueBatchNo}: キュー${addedToQueue.length}件 / 新規${newSeenCount}件`);
    return { items: addedToQueue, newSeenCount };
  }

  // =====================================================================
  // 精密モード用リトライ
  // =====================================================================
  function scheduleDetailRetry(item, reason, detail = '') {
    const url = item?.url || '';
    const attempts = attemptCounts.get(url) || 0;
    processedUrls.delete(url);

    if (url && attempts < 3 && !stopRequested) {
      queuedOrProcessingUrls.add(url);
      cardQueue.push({ ...item });
      reportV3Log(`再確認予約 ${attempts}/3: ${reason} / ${detail || item?.name || url}`);
      return true;
    }

    queuedOrProcessingUrls.delete(url);
    logSkip(item, `${reason}(3回確認後も取得不可)`, detail || item?.name || url);
    return false;
  }

  // =====================================================================
  // メインループ
  // =====================================================================
  while (!stopRequested && totalProcessed < effectiveMaxItems) {
    container = getScrollContainer() || container;
    if (!container) {
      speedStats.scrollEndReason = 'scroll_container_not_found';
      break;
    }

    // キューが空なら補充
    if (!cardQueue.length) {
      const { items, newSeenCount } = buildCardQueue();
      cardQueue.push(...items);

      if (newSeenCount > 0) {
        noNewCardCount = 0;
        speedStats.scrollNoIncreaseCount = 0;
      }
    }

    // キューが空→スクロール
    if (!cardQueue.length) {
      if (isEndOfList(container)) {
        speedStats.scrollEndReason = 'end_of_list_message';
        break;
      }
      if (precollectedIndex >= precollectedItems.length) {
        speedStats.scrollEndReason = speedStats.scrollEndReason || 'precollected_urls_drained';
        break;
      }
      if (scrollAttempts >= 50) {
        speedStats.scrollEndReason = 'max_scroll_count';
        break;
      }
      const metrics = getScrollableMetrics(container);
      if (noNewCardCount >= 8 && metrics.remaining < 80) {
        speedStats.scrollEndReason = 'no_new_visible_cards';
        break;
      }

      const scrollStartedAt = performance.now();
      const scrollResult = await scrollResultsList(container, lastProcessedItem);
      addTiming(speedStats, 'scroll', performance.now() - scrollStartedAt);
      speedStats.scrollCount++;
      scrollAttempts++;
      container = getScrollContainer() || container;

      const afterMetrics = scrollResult?.after || getScrollableMetrics(container);
      if (scrollResult?.moved || afterMetrics.remaining > 80) {
        noNewCardCount = Math.max(0, noNewCardCount - 1);
      } else {
        noNewCardCount++;
      }
      speedStats.scrollNoIncreaseCount = noNewCardCount;

      if (scrollAttempts % 3 === 0) {
        reportV3Log(`スクロール継続確認: ${scrollAttempts}回 / 残り${Math.round(afterMetrics.remaining)}px / 新規なし連続${noNewCardCount}`);
      }
      continue;
    }

    // =====================================================================
    // カードを1件ずつ処理
    // =====================================================================
    while (cardQueue.length && !stopRequested && totalProcessed < effectiveMaxItems) {
      const item = cardQueue.shift();
      const { url, name: cardName } = item;

      if (!url || processedUrls.has(url)) {
        queuedOrProcessingUrls.delete(url);
        continue;
      }

      const itemStartedAt = performance.now();
      attemptCounts.set(url, (attemptCounts.get(url) || 0) + 1);

      try {
        // --- 1. カード再解決 ---
        const searchStartedAt = performance.now();
        container = getScrollContainer() || container;
        let cardEl = resolveCardElementByUrl(url, container);

        // 画面外なら scrollIntoView
        if (!cardEl) {
          cardEl = await scrollToCardUrl(url, container, 35);
          container = getScrollContainer() || container;
        }
        if (cardEl) {
          const rect = cardEl.getBoundingClientRect();
          const vh = window.innerHeight || 900;
          if (rect.top < 0 || rect.bottom > vh) {
            try { cardEl.scrollIntoView({ block: 'center', behavior: 'auto' }); } catch (_) {}
            await sleep(150);
            cardEl = resolveCardElementByUrl(url, container);
          }
        }
        addTiming(speedStats, 'search', performance.now() - searchStartedAt);

        const cardLink = cardEl?.querySelector?.('a[href*="/maps/place/"]')
          || cardEl?.closest?.('a[href*="/maps/place/"]');
        if (!cardLink) {
          speedStats.failed++;
          scheduleDetailRetry(item, 'カード再取得不可', cardName || url);
          continue;
        }

        // --- 2. クリック（詳細パネルを閉じない方式）---
        const clickStartedAt = performance.now();
        const previousPanelName = prevPanelName || getCurrentPanelName();
        const previousPanelUrl = prevPanelUrl || window.location.href;
        const previousPanelAddress = getCurrentPanelAddress();
        const previousPanelPhone = getCurrentPanelPhone();
        const previousWebsiteSnapshot = getCurrentWebsiteSnapshot(previousPanelName);

        // 詳細パネルが既に開いている場合、パネルを閉じずに次カードをクリック
        if (isDetailPanelOpen()) {
          cardLink.click();
          addTiming(speedStats, 'click', performance.now() - clickStartedAt);

          // パネル内容の切り替わりを確認（前店舗名/URLと異なることを確認）
          const panelWaitStarted = performance.now();
          const changed = await waitUntilPanelChanged(previousPanelName, previousPanelUrl, 6500);
          const fieldWaitStarted = performance.now();
          const fieldsReady = await waitForPanelFieldsReady({
            expectedName: cardName,
            expectedUrl: url,
            previousName: previousPanelName,
            previousUrl: previousPanelUrl,
            previousAddress: previousPanelAddress,
            previousPhone: previousPanelPhone
          }, 6500);
          addTiming(speedStats, 'panelWait', performance.now() - fieldWaitStarted);

          const panelDidNotChange = changed.name === previousPanelName && changed.url === previousPanelUrl;
          // 住所・電話番号の更新が確認できなかった場合（fieldsReady=false）も、
          // 以前は「取得は続行」として前店舗のデータをそのまま使ってしまっていた。
          // これが「別店舗の電話番号・住所が混入する」バグの主因だったため、
          // パネル名/URLが変わらなかった場合と同じフォールバック（閉じて開き直す）
          // を必ず実行するように変更。それでも確認できなければスキップする。
          if (panelDidNotChange || !fieldsReady) {
            reportV3Log(panelDidNotChange
              ? `パネル切り替わらず フォールバック: ${cardName || url}`
              : `詳細欄更新未確認 フォールバック: ${cardName || url}`);
            const backMs0 = performance.now();
            await closeDetailPanel();
            addTiming(speedStats, 'back', performance.now() - backMs0);

            // [FIX] closeDetailPanel()は「戻る」クリック/履歴操作/Escapeキーなど
            // DOMを大きく書き換える処理を含むため、closeDetailPanel呼び出し前に
            // 解決していたcardLinkは既にDOMから切り離されている（detached）
            // 可能性が高い。detached要素へのclick()は何も起こさず、以降の
            // waitForDetailPanel/waitForPanelFieldsReadyが「変化なし」のまま
            // 停止して見える原因になっていたため、クリック直前に要素を再解決する。
            container = getScrollContainer() || container;
            let fallbackCardEl = resolveCardElementByUrl(url, container);
            if (!fallbackCardEl) {
              fallbackCardEl = await scrollToCardUrl(url, container, 20);
              container = getScrollContainer() || container;
            }
            const fallbackCardLink = fallbackCardEl?.querySelector?.('a[href*="/maps/place/"]')
              || fallbackCardEl?.closest?.('a[href*="/maps/place/"]');
            if (!fallbackCardLink) {
              speedStats.failed++;
              scheduleDetailRetry(item, 'フォールバック・カード再取得不可', cardName || url);
              continue;
            }
            fallbackCardLink.click();
            const panelWaitFallback = performance.now();
            const fallbackReady = await waitForDetailPanel(6500);
            const fallbackFieldsReady = fallbackReady && await waitForPanelFieldsReady({
              expectedName: cardName,
              expectedUrl: url,
              previousName: previousPanelName,
              previousUrl: previousPanelUrl,
              previousAddress: previousPanelAddress,
              previousPhone: previousPanelPhone
            }, 6500);
            addTiming(speedStats, 'panelWait', performance.now() - panelWaitFallback);
            if (!fallbackReady || !fallbackFieldsReady) {
              speedStats.failed++;
              scheduleDetailRetry(item, 'フォールバック詳細更新未確認', cardName || url);
              continue;
            }
          }
        } else {
          // 詳細パネルが閉じている: 通常通りクリック
          cardLink.click();
          addTiming(speedStats, 'click', performance.now() - clickStartedAt);

          const panelWaitStarted = performance.now();
          const panelReady = await waitForDetailPanel(7000);
          addTiming(speedStats, 'panelWait', performance.now() - panelWaitStarted);

          if (!panelReady) {
            speedStats.failed++;
            await closeDetailPanel();
            scheduleDetailRetry(item, 'パネル未表示', cardName || url);
            continue;
          }

          const fieldWaitStarted = performance.now();
          const fieldsReady = await waitForPanelFieldsReady({
            expectedName: cardName,
            expectedUrl: url,
            previousName: previousPanelName,
            previousUrl: previousPanelUrl,
            previousAddress: previousPanelAddress,
            previousPhone: previousPanelPhone
          }, 6500);
          addTiming(speedStats, 'panelWait', performance.now() - fieldWaitStarted);
          // 住所・電話番号の更新が確認できない場合、以前は「取得は続行」として
          // 未確認のまま(=前の内容の可能性がある)データを使ってしまっていた。
          // 別店舗のデータ混入を防ぐため、確認できなければこの店舗はスキップする
          if (!fieldsReady) {
            speedStats.failed++;
            scheduleDetailRetry(item, '詳細欄更新未確認', cardName || url);
            continue;
          }
        }

        // --- 3. 情報取得 ---
        // 住所・電話が切り替わっても、Google Mapsの「ウェブサイト」ボタンだけ
        // 前医院のDOMが遅れて残ることがある。URLは別途「新しいプロフィール用DOMへ
        // 更新された」と確認できるまで待ち、確認できなければ空欄にする。
        const expectedWebsiteName = !isJunkPanelName(cardName) ? cardName : extractNameFromUrl(url);
        const firstWebsiteField = await waitForFreshWebsiteField({
          expectedName: expectedWebsiteName,
          previousSnapshot: previousWebsiteSnapshot
        }, 8000);
        const websiteField = await getWebsiteByIndependentConsensus({
          placeUrl: url,
          cardName: expectedWebsiteName,
          previousSnapshot: previousWebsiteSnapshot,
          firstField: firstWebsiteField,
          maxAttempts: 4
        });
        if (websiteField.status === 'ambiguous') {
          reportV3Log(`HP独立再確認4回でも合意せず→URL空欄: ${expectedWebsiteName || url}`);
        } else if (websiteField.status === 'fresh') {
          reportV3Log(`HP独立確認OK: ${expectedWebsiteName || url} → ${websiteField.url}`);
        }

        const scrapeStartedAt = performance.now();
        const detail = await scrapeDetailPanel(url, cardName, effectiveGenre, websiteField);
        detail.searchGenre = effectiveGenre;
        detail.websiteFieldStatus = websiteField.status;
        addTiming(speedStats, 'scrape', performance.now() - scrapeStartedAt);

        // パネル情報を更新（次回の切り替わり検知用）
        prevPanelName = detail.name || '';
        prevPanelUrl = window.location.href;

        if (!detail.name || detail.name === '結果' || isJunkPanelName(detail.name)) {
          speedStats.failed++;
          scheduleDetailRetry(item, '店名未取得', `URL:${url}`);
          continue;
        }

        const identityNames = [cardName, extractNameFromUrl(url)]
          .filter(n => n && !isJunkPanelName(n));
        if (identityNames.length && !identityNames.some(n => isLikelySamePlaceName(detail.name, n))) {
          // 最大精度優先: 予定していた医院と開いたプロフィール名が一致しなければ保存しない。
          speedStats.failed++;
          scheduleDetailRetry(item, '医院本人確認不一致', `${detail.name} / expected:${identityNames.join('|')}`);
          continue;
        }

        if (!detail.address) {
          // 「項目が取れない医院を行ごと落とす」より、医院自体は残して空欄セルにする。
          // アプリ側の厚生局/コムデスク照合で後から補完できる。
          speedStats.addressMissing++;
          reportV3Log(`住所未取得だが行は保存: ${detail.name}`);
        }

        reportV3Log(`ジャンル変換: ${detail.googleGenre || '(未取得)'} → ${detail.genre || '(空欄)'} / 検索:${effectiveGenre || '-'}`);

        // ジャンルフィルタ
        if (!matchesTargetGenres(detail, targetGenres)) {
          speedStats.genreExcluded++;
          logSkip(item, 'ジャンル不一致', `${detail.name}|${detail.genre}(${detail.googleGenre})`);
          queuedOrProcessingUrls.delete(url);
          continue;
        }

        // エリアフィルタ
        if (detail.address && !matchesSearchArea(detail, searchArea)) {
          speedStats.areaExcluded++;
          logSkip(
            item,
            'エリア外除外',
            `店舗名:${detail.name} / 住所:${detail.address} / 指定:${targetArea.prefecture}${targetArea.city}`
          );
          queuedOrProcessingUrls.delete(url);
          continue;
        }

        const parsedAddr = parseAddress(detail.address);

        const record = {
          name: detail.name,
          genre: detail.genre,
          sourceGenre: detail.googleGenre,
          prefecture: parsedAddr.prefecture,
          city: parsedAddr.city,
          subArea: scrapeOptions.subAreaLabel || scrapeOptions.subArea || '',
          address: detail.address,
          phone: detail.phone,
          phoneStatus: detail.phoneStatus,
          regularHoliday: detail.regularHoliday,
          businessDays: detail.businessDays,
          openTimeA: detail.openTimeA,
          closeTimeA: detail.closeTimeA,
          openTimeB: detail.openTimeB,
          closeTimeB: detail.closeTimeB,
          rawHours: detail.rawHours,
          url: url,
          websiteUrl: detail.websiteUrl || '',
          hasWebsite: detail.hasWebsite,
          hasWebsiteStatus: detail.hasWebsiteStatus,
          source: 'GoogleMap',
          sourceUrl: searchPageUrl,
          scrapedAt: new Date().toISOString(),
          searchGenre: effectiveGenre,
          searchKey,
          scrapeMode: scrapeOptions.scrapeModeLabel || scrapeOptions.scrapeMode || '',
          rangeMode: scrapeOptions.rangeMode || '',
          acquisitionStatus: '取得成功',
          excludeReason: '',
          detailRetryCount: Math.max(0, (attemptCounts.get(url) || 1) - 1),
          listRank: item.listRank,
          acquiredOrder: ++acquiredOrder,
          scrollBatchNo: item.scrollBatch
        };

        // 最終防衛ライン: 店名が違うのに住所が直前レコードと完全一致している
        // 場合、パネルが更新されないまま前店舗のデータを読んでしまった疑いが
        // 強いため、このレコードは保存せずスキップする
        // （fieldsReadyのチェックをすり抜けた場合の保険）。
        // 電話番号は両方とも「掲載なし」で空欄同士のケースもあるため、
        // 住所の一致だけでも十分怪しいと判断する（電話も一致すればなお確実）。
        const suspectedStaleCopy = lastPushedRecord
          && record.name !== lastPushedRecord.name
          && record.address && record.address === lastPushedRecord.address;
        if (suspectedStaleCopy) {
          speedStats.failed++;
          queuedOrProcessingUrls.delete(url);
          logSkip(item, '前店舗とデータ完全一致(混入疑い)', `${record.name} / ${record.address} / ${record.phone}`);
          continue;
        }

        processedUrls.add(url);
        totalProcessed++;
        speedStats.detailFetched++;
        markRecordQuality(speedStats, record);
        lastProcessedItem = item;
        lastPushedRecord = record;

        const elapsed = ((Date.now() - startTime) / 1000).toFixed(0);
        const perItem = totalProcessed > 0 ? (elapsed / totalProcessed).toFixed(1) : '-';

        // 1件ごとの内訳ログ
        const totalMs = performance.now() - itemStartedAt;
        reportV3Log([
          `詳細取得ログ: 一覧順位${record.listRank} / 取得順位${record.acquiredOrder} / batch${record.scrollBatchNo} / ${record.name || url}`,
          `合計${msToSec(totalMs)}秒 / 次カード探索${avgTiming(speedStats,'search')}秒(avg) / クリック${avgTiming(speedStats,'click')}秒(avg) / パネル待ち${avgTiming(speedStats,'panelWait')}秒(avg) / 情報取得${avgTiming(speedStats,'scrape')}秒(avg)`,
          `電話:${detail.phoneStatus} / HP:${detail.hasWebsiteStatus} / 平均${perItem}秒/件`
        ].join(' | '));

        // --- 4. バッファ保存 ---
        pendingBatch.push(record);
        existingRecords.set(url, record);

        if (pendingBatch.length >= BATCH_SIZE || totalProcessed >= effectiveMaxItems || !isScrapingActive) {
          const saveStartedAt = performance.now();
          currentFlushPromise = flushBatch(pendingBatch);
          const flushed = await currentFlushPromise;
          currentFlushPromise = null;
          addTiming(speedStats, 'save', performance.now() - saveStartedAt);
          if (flushed) {
            speedStats.saved += flushed;
            speedStats.saveCount++;
            speedStats.lastSavedAt = Date.now();
            reportV3Log(buildSpeedSummary(speedStats, '中間速度ログ'));
          }
        }

        try {
          chrome.runtime.sendMessage({ action: 'progress', count: totalProcessed }, () => {
            if (chrome.runtime.lastError) { /* ignore */ }
          });
        } catch (_) { }

        queuedOrProcessingUrls.delete(url);

      } catch (err) {
        console.error('[Scraper] エラー:', err);
        speedStats.failed++;
        // エラー時はいったん詳細パネルを閉じ、最大3回まで同じ医院を独立再確認する。
        if (isDetailPanelOpen()) {
          await closeDetailPanel().catch(() => {});
          prevPanelName = '';
          prevPanelUrl = window.location.href;
        }
        scheduleDetailRetry(item, '詳細取得例外', `${cardName || url} / ${err?.message || err}`);
        await sleep(800);
      }
    }

    if (stopRequested || totalProcessed >= effectiveMaxItems) break;
  }

  // =====================================================================
  // 終了処理（強制flush）
  // =====================================================================
  if (!speedStats.scrollEndReason) {
    speedStats.scrollEndReason = stopRequested
      ? 'stopped_by_user'
      : (totalProcessed >= effectiveMaxItems ? 'target_count_reached' : 'scraping_stopped');
  }

  if (pendingBatch.length > 0) {
    reportV3Log(`未保存バッファを保存中... ${pendingBatch.length}件`);
    const saveStartedAt = performance.now();
    const flushed = await flushBatch(pendingBatch);
    addTiming(speedStats, 'save', performance.now() - saveStartedAt);
    if (flushed) {
      speedStats.saved += flushed;
      speedStats.saveCount++;
      speedStats.lastSavedAt = Date.now();
      reportV3Log(`未保存バッファ保存完了: ${flushed}件`);
    }
  }

  reportV3Log(buildSpeedSummary(speedStats, stopRequested ? 'ユーザー停止速度ログ' : 'コンボ完了速度ログ'));

  isScrapingActive = false;
  const finalState = stopRequested ? 'stopped_by_user' : 'done';
  if (stopRequested) {
    reportV3Log(`一時停止: 取得済み${totalProcessed}件を保存します`);
  }
  await reportState(finalState);
}

async function reportState(state) {
  return new Promise(r => {
    try {
      chrome.runtime.sendMessage({ action: 'setState', state }, () => {
        if (chrome.runtime.lastError) { }
        r();
      });
    } catch (_) { r(); }
  });
}

// =====================================================================
// メッセージリスナー
// =====================================================================
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'ping') { sendResponse({ alive: true }); return false; }
  if (request.action === 'getQuery') { sendResponse({ query: getCurrentQuery() }); return false; }

  if (request.action === 'startScraping') {
    if (isScrapingActive) { sendResponse({ success: false, reason: 'already running' }); return false; }

    skipLogged.clear();

    const incomingGenres  = Array.isArray(request.targetGenres) ? request.targetGenres : [];
    const incomingArea    = typeof request.searchArea  === 'string' ? request.searchArea.trim()  : '';
    const incomingGenre   = typeof request.searchGenre === 'string' ? request.searchGenre.trim() : '';
    const incomingOptions = request.scrapeOptions && typeof request.scrapeOptions === 'object' ? request.scrapeOptions : {};

    startScraping(request.maxItems ?? 50, incomingGenres, incomingArea, incomingGenre, incomingOptions).catch(err => {
      console.error('[Scraper] 致命的エラー:', err);
      isScrapingActive = false;
      reportState(stopRequested ? 'stopped_by_user' : 'done');
    });
    sendResponse({ success: true });
    return false;
  }

  if (request.action === 'stopScraping') {
    stopRequested = true;
    isScrapingActive = false;
    reportV3Log('一時停止要求を受信: 処理中の医院があれば保存してから停止します');

    // 安全タイムアウト（最大15秒）で強制flush
    const STOP_TIMEOUT = 15000;
    const stopAt = Date.now() + STOP_TIMEOUT;

    const waitAndFlush = async () => {
      // 現在flush中なら完了を待つ（最大STOP_TIMEOUT）
      if (currentFlushPromise) {
        await Promise.race([
          currentFlushPromise,
          new Promise(r => setTimeout(r, STOP_TIMEOUT))
        ]);
      }
      if (Date.now() > stopAt) {
        reportV3Log('stop_timeout: 最大待機時間に達しました。現在のバッファで安全停止します。');
      }
      // 残バッファがあれば強制flush
      // 注: pendingBatchは関数スコープ外なので、STOP時のflushはstartScrapingの終了処理に委ねる
      sendResponse({ success: true });
    };
    waitAndFlush();
    return true;
  }

  if (request.action === 'getGenresFromPage') {
    const spans = Array.from(document.querySelectorAll('[role="main"] .W4Efsd span, .S9kvJb'))
      .map(el => el.textContent.trim())
      .filter(t => t.length >= 2 && t.length <= 20 && GENRE_NORMALIZE_MAP[t]);
    sendResponse({ genres: [...new Set(spans)] });
    return false;
  }

  return false;
});
// =====================================================================
// v5.7 厚生局キュー方式: 現在表示中プロフィールの本人情報と公式ウェブサイト欄を返す
// =====================================================================
async function clinicQueueStableProfile() {
  const deadline = Date.now() + 9000;
  let last = '', stableSince = 0, snap = null;
  while (Date.now() < deadline) {
    const cur = getPanelIdentitySnapshot('');
    if (cur && cur.name && !isJunkPanelName(cur.name)) {
      if (cur.fingerprint === last) {
        if (!stableSince) stableSince = Date.now();
        if (Date.now() - stableSince >= 800) { snap = cur; break; }
      } else { last = cur.fingerprint; stableSince = Date.now(); }
    }
    await sleep(120);
  }
  if (!snap) return null;
  const wf = await waitForFreshWebsiteField({ expectedName: snap.name, previousSnapshot: null }, 6500);
  const detail = await scrapeDetailPanel(window.location.href, snap.name, '', wf);
  return {
    name: detail.name || snap.name || '', phone: detail.phone || snap.phone || '', address: detail.address || snap.address || '',
    profileUrl: window.location.href, websiteUrl: detail.websiteUrl || '',
    websiteStatus: detail.websiteUrl ? 'MAPS_WEBSITE_CONFIRMED' : (detail.hasWebsiteStatus === 'no_website' ? 'MAPS_NO_WEBSITE_CONFIRMED' : 'MAPS_WEBSITE_AMBIGUOUS'),
    regularHoliday: detail.regularHoliday || '', businessDays: detail.businessDays || '',
    openTimeA: detail.openTimeA || '', closeTimeA: detail.closeTimeA || '', openTimeB: detail.openTimeB || '', closeTimeB: detail.closeTimeB || '',
    rawHours: detail.rawHours || ''
  };
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'clinicQueueProbe') {
    (async () => {
      const bodyText = String(document.body?.innerText || '');
      const blocked = /unusual traffic|通常と異なるトラフィック|CAPTCHA|ロボットではありません/i.test(bodyText);
      if (blocked) { sendResponse({ ok:true, blocked:true, reason:'GOOGLE_ACCESS_RESTRICTION' }); return; }
      let profile = null;
      if (isDetailPanelOpen() || getActiveMainPanel('')?.querySelector('h1')) {
        try { profile = await clinicQueueStableProfile(); } catch (_) { profile = null; }
      }
      const candidates = [];
      const seen = new Set();
      for (const a of Array.from(document.querySelectorAll('a[href*="/maps/place/"]'))) {
        const url = a.href || a.getAttribute('href') || '';
        if (!url || seen.has(url)) continue;
        seen.add(url);
        const name = a.getAttribute('aria-label') || extractNameFromUrl(url) || '';
        candidates.push({ url, name });
        if (candidates.length >= 12) break;
      }
      sendResponse({ ok:true, blocked:false, profile, candidates });
    })().catch(e => sendResponse({ok:false,error:String(e?.message||e)}));
    return true;
  }
  if (request.action === 'clinicQueueExtract') {
    clinicQueueStableProfile().then(profile => sendResponse({ok:!!profile,profile})).catch(e => sendResponse({ok:false,error:String(e?.message||e)}));
    return true;
  }
  return false;
});
