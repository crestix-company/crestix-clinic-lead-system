// Local-only Chrome DevTools helper for the Phase F Streamlit fixture.
// Both browser control and target page are restricted to 127.0.0.1.
const base = "http://127.0.0.1:9223";
const pageUrl = "http://127.0.0.1:8513/";
if (!base.startsWith("http://127.0.0.1:") || !pageUrl.startsWith("http://127.0.0.1:")) {
  throw new Error("Phase F browser smoke is loopback-only");
}
const targets = await (await fetch(`${base}/json/list`)).json();
let target = targets.find((item) => item.type === "page");
if (!target) {
  await fetch(`${base}/json/new?${encodeURIComponent(pageUrl)}`, { method: "PUT" });
  const refreshed = await (await fetch(`${base}/json/list`)).json();
  target = refreshed.find((item) => item.type === "page");
}
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let nextId = 0;
const pending = new Map();
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(message.error.message)); else resolve(message.result);
  }
};
function cdp(method, params = {}) {
  const id = ++nextId;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
await cdp("Page.enable");
await cdp("Runtime.enable");
await cdp("Page.navigate", { url: pageUrl });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await pause(5000);
const body = await cdp("Runtime.evaluate", {
  expression: "document.body.innerText",
  returnByValue: true,
});
const controls = await cdp("Runtime.evaluate", {
  expression: `JSON.stringify({buttons:[...document.querySelectorAll('button')].map(e=>e.innerText.trim()).filter(Boolean),
    inputs:[...document.querySelectorAll('input')].map(e=>({type:e.type,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),parent:e.parentElement?.parentElement?.innerText?.slice(0,100)})),
    labels:[...document.querySelectorAll('label')].map(e=>e.innerText.trim()).filter(Boolean)})`,
  returnByValue: true,
});
const evalJS = async (expression) => cdp("Runtime.evaluate", { expression, returnByValue: true });
const resetMock = await evalJS(`(() => {const e=[...document.querySelectorAll('button')].find(x=>x.innerText.includes('Mock状態を初期化'));if(!e)return 'not-found';e.scrollIntoView({block:'center'});e.click();return 'clicked';})()`);
console.log("RESET_MOCK=" + resetMock.result.value);
await pause(800);
const clickSelector = async (selector) => {
  const box = await evalJS(`(() => {const e=${selector}; if(!e||!e.getClientRects().length)return 'missing'; e.scrollIntoView({block:'center'}); const r=e.getBoundingClientRect(); return r?JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height}):'missing';})()`);
  if (!box.result.value || box.result.value === "missing") return false;
  const r = JSON.parse(box.result.value);
  await cdp("Input.dispatchMouseEvent", { type: "mousePressed", x: r.x + r.w / 2, y: r.y + r.h / 2, button: "left", clickCount: 1 });
  await cdp("Input.dispatchMouseEvent", { type: "mouseReleased", x: r.x + r.w / 2, y: r.y + r.h / 2, button: "left", clickCount: 1 });
  return true;
};
const typeInto = async (selector, value) => {
  if (!await clickSelector(selector)) return false;
  await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "A", code: "KeyA", modifiers: 2 });
  await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "A", code: "KeyA", modifiers: 2 });
  await cdp("Input.insertText", { text: value });
  await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  return true;
};
const tabInfo = await evalJS(`JSON.stringify([...document.querySelectorAll('[role=tab]')].map(e=>({text:e.innerText.trim(),tag:e.tagName})))`);
console.log("TABS=" + tabInfo.result.value);
const clickTab = await clickSelector(`([...document.querySelectorAll('[role=tab]')].find(e=>e.innerText.trim()==='④ 生成結果'))`);
console.log("CLICK_TAB=" + clickTab);
await pause(1500);
const activeTabs = await evalJS(`JSON.stringify([...document.querySelectorAll('[role=tab]')].map(e=>({text:e.innerText.trim(),selected:e.getAttribute('aria-selected'),style:getComputedStyle(e).display})))`);
const opened = await clickSelector(`([...document.querySelectorAll('input')].find(x=>x.getClientRects().length&&x.getAttribute('aria-label')?.includes('下書き保存対象')))`);
console.log("OPEN_MULTIMSELECT=" + opened);
await cdp("Input.dispatchKeyEvent", { type: "keyDown", key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 });
await cdp("Input.dispatchKeyEvent", { type: "keyUp", key: "ArrowDown", code: "ArrowDown", windowsVirtualKeyCode: 40 });
await pause(800);
const options = await evalJS(`JSON.stringify({options:[...document.querySelectorAll('[role=option]')].map(e=>e.innerText.trim()), lists:[...document.querySelectorAll('[role=listbox]')].map(e=>e.innerText), field:[...document.querySelectorAll('[data-testid=stMultiSelect]')].map(e=>e.innerText)})`);
const selected = await clickSelector(`([...document.querySelectorAll('[role=option]')].find(x=>x.getClientRects().length&&x.innerText.includes('Clinic 901')))`);
console.log("SELECT_CLINIC=" + selected);
await pause(700);
const checks = ['既存下書き・送信予約・送信済みとの重複がないことを確認しました','医院名・院長名・宛先を照合しました','正式テンプレート本文を全文確認しました','実掲載記事または類似記事URLを確認しました','医院詳細ページURLを確認しました'];
const checkInfo = await evalJS(`JSON.stringify([...document.querySelectorAll('input[type=checkbox]')].map(e=>({label:e.getAttribute('aria-label'),parent:e.parentElement?.parentElement?.innerText,rect:e.getClientRects().length})))`);
const checked = [];
for (const label of checks) {
  checked.push(await clickSelector(`([...document.querySelectorAll('input[type=checkbox]')].find(x=>x.getClientRects().length&&x.getAttribute('aria-label')===${JSON.stringify(label)}))`));
  await pause(600);
}
const reviewer = await typeInto(`([...document.querySelectorAll('input')].find(x=>x.getClientRects().length&&x.getAttribute('aria-label')==='下書き保存確認担当者'))`, "Mockブラウザ確認者");
const confirmation = await typeInto(`([...document.querySelectorAll('input')].find(x=>x.getClientRects().length&&x.getAttribute('aria-label')==='下書き保存の確認語'))`, "CREATE_GMAIL_DRAFTS");
console.log("REVIEW_ENTRY=" + JSON.stringify({reviewer, checked, confirmation}));
await pause(800);
const previewCheck = await evalJS(`JSON.stringify({body:[...document.querySelectorAll('textarea')].some(e=>e.value.includes('Mock正式本文です。送信は行わず、画面確認用の合成文面です。')),confirm:[...document.querySelectorAll('input')].find(e=>e.getAttribute('aria-label')==='下書き保存の確認語')?.value,checked:[...document.querySelectorAll('input[type=checkbox]')].map(e=>e.checked),button:[...document.querySelectorAll('button')].filter(e=>e.innerText.includes('Gmail下書きに保存')).map(e=>({text:e.innerText,disabled:e.disabled}))})`);
const blockers = await evalJS(`JSON.stringify({reviewer:[...document.querySelectorAll('input')].find(e=>e.getAttribute('aria-label')==='下書き保存確認担当者')?.value,operation:[...document.querySelectorAll('input')].find(e=>e.getAttribute('aria-label')?.includes('案件ID / operation_key'))?.value,text:document.body.innerText.slice(document.body.innerText.indexOf('保存前確認が未完了')-100,document.body.innerText.indexOf('保存前確認が未完了')+600)})`);
const save = await evalJS(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.getClientRects().length&&e.innerText.includes('Gmail下書きに保存')); if(!b||b.disabled) return 'save-disabled'; b.click(); return 'clicked';})()`);
console.log("MOCK_SAVE=" + save.result.value);
await pause(3000);
const saveResult = await evalJS(`document.body.innerText`);
const resultLines = saveResult.result.value.split(/\r?\n/).filter((line)=>/mock-draft|Gmail状態|保存結果|HOLD理由|共通Draft API/.test(line));
console.log("SAVE_RESULT=" + JSON.stringify({draft:saveResult.result.value.includes('mock-draft-0001'),lines:resultLines}));
const draftInputs = await evalJS(`JSON.stringify([...document.querySelectorAll('input')].filter(e=>e.getClientRects().length).map(e=>({aria:e.getAttribute('aria-label'),value:e.value})))`);
console.log("VISIBLE_INPUTS_AFTER_SAVE=" + draftInputs.result.value);
const secondKey = await typeInto(`([...document.querySelectorAll('input')].find(x=>x.getClientRects().length&&x.getAttribute('aria-label')?.includes('案件ID / operation_key')))`, "manual-second-operation");
console.log("SECOND_OPERATION_KEY=" + secondKey);
await pause(1000);
const duplicateSave = await evalJS(`(() => {const b=[...document.querySelectorAll('button')].find(e=>e.getClientRects().length&&e.innerText.includes('Gmail下書きに保存')); if(!b||b.disabled) return 'save-disabled'; b.click(); return 'clicked';})()`);
console.log("DUPLICATE_ATTEMPT=" + duplicateSave.result.value);
await pause(2500);
let currentBody = await evalJS(`document.body.innerText`);
console.log("DUPLICATE_HOLD=" + (currentBody.result.value.includes('同一医院・同一キャンペーン') && currentBody.result.value.includes('HOLD')));
const casesTab = await evalJS(`(() => {const e=[...document.querySelectorAll('[role=tab]')].find(x=>x.getClientRects().length&&x.innerText.includes('⑥ 案件')); if(!e)return 'tab-not-found'; e.click(); return 'clicked';})()`);
await pause(1000);
currentBody = await evalJS(`document.body.innerText`);
console.log("CASE_SCREEN=" + JSON.stringify({tab:casesTab.result.value, uuid:currentBody.result.value.includes('fixture-clinic-uuid-1'), gasCase:currentBody.result.value.includes('GAS-DEMO-001'), mock:currentBody.result.value.includes('LOCAL MOCK MODE')}));
const importTab = await evalJS(`(() => {const e=[...document.querySelectorAll('[role=tab]')].find(x=>x.getClientRects().length&&x.innerText.includes('⑦ 取込')); if(!e)return 'tab-not-found'; e.click(); return 'clicked';})()`);
await pause(800);
const mockCsvButton = await evalJS(`(() => {const e=[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.includes('Mock CSV全件をプレビュー')); if(!e)return 'button-not-found';e.click();return 'clicked';})()`);
await pause(1400);
currentBody = await evalJS(`document.body.innerText`);
console.log("IMPORT_SCREEN=" + JSON.stringify({tab:importTab.result.value,button:mockCsvButton.result.value,summary:currentBody.result.value.split(/\r?\n/).filter(line=>/全 \d+ 行|INSERT|UPDATE|NOOP|HOLD/.test(line)).slice(-8)}));
await evalJS(`(() => {const e=[...document.querySelectorAll('button')].find(x=>x.getClientRects().length&&x.innerText.includes('この全件CSVをローカルセッションに保存')); if(e)e.click(); return 'done';})()`);
await pause(500);
const png = await cdp("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
await (await import("node:fs/promises")).writeFile("/private/tmp/phase_f_local_ui.png", Buffer.from(png.data, "base64"));
await new Promise((resolve) => { ws.close(); resolve(); });
