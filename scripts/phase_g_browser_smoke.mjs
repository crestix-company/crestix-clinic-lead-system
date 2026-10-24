// Loopback-only browser verification for the real-data, read-only Phase G UI.
const cdpBase = "http://127.0.0.1:9223";
const url = "http://127.0.0.1:8514/";
const fs = await import("node:fs/promises");
const csvFixture = "/private/tmp/phase_g_upload_fixture.csv";
await fs.writeFile(csvFixture, "clinic_id,Clinic UUID,案件ID\n999999,unmapped-fixture,GAS-CSV-FIXTURE\n", { mode: 0o600 });
const created = await fetch(`${cdpBase}/json/new?${encodeURIComponent(url)}`, { method: "PUT" });
if (!created.ok) throw new Error("Chrome CDP page could not be created");
const page = await created.json();
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let id = 0;
const pending = new Map();
ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    const p = pending.get(message.id); pending.delete(message.id);
    if (message.error) p.reject(new Error(message.error.message)); else p.resolve(message.result);
  }
};
const call = (method, params = {}) => {
  const n = ++id; ws.send(JSON.stringify({ id: n, method, params }));
  return new Promise((resolve, reject) => pending.set(n, { resolve, reject }));
};
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
await call("Page.enable"); await call("Runtime.enable");
await call("Page.navigate", { url }); await wait(6000);
const body = async () => (await call("Runtime.evaluate", { expression: "document.body.innerText", returnByValue: true })).result.value;
let text = await body();
if (!text.includes("営業案件管理（実データ Read Only）") || !text.includes("328医院進捗 行")) throw new Error("Live read-only UI did not load expected sections");
await call("DOM.enable");
const document = await call("DOM.getDocument", { depth: -1 });
const fileInput = await call("DOM.querySelector", { nodeId: document.root.nodeId, selector: "input[type=file]" });
if (!fileInput.nodeId) throw new Error("CSV upload input not found in browser UI");
await call("DOM.setFileInputFiles", { nodeId: fileInput.nodeId, files: [csvFixture] });
await wait(1000);
const clicked = await call("Runtime.evaluate", { expression: `(() => {const b=[...document.querySelectorAll('button')].find(x=>x.innerText.includes('全対象タブの同期プレビューを計算')); if(!b)return false; b.click(); return true;})()`, returnByValue: true });
if (!clicked.result.value) throw new Error("Import preview control not found");
await wait(1800); text = await body();
const actionMatch = text.match(/Sheets\/DB書込なしのプレビュー/);
if (!actionMatch) throw new Error("Import preview did not render action counts");
const dataframes = await call("Runtime.evaluate", { expression: `document.querySelectorAll('[data-testid="stDataFrame"]').length`, returnByValue: true });
if (dataframes.result.value < 2) throw new Error("Real case rows and source rows are not present in the browser view");
const counts = await call("Runtime.evaluate", { expression: `(() => {const out={}; for(const e of document.querySelectorAll('[data-testid="stMetric"]')) {const k=e.querySelector('[data-testid="stMetricLabel"]')?.innerText?.trim(); const v=e.querySelector('[data-testid="stMetricValue"]')?.innerText?.trim(); if(['INSERT','UPDATE','NOOP','HOLD'].includes(k)) out[k]=v;} return JSON.stringify(out);})()`, returnByValue: true });
const png = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
await fs.writeFile("/private/tmp/phase_g_local_ui.png", Buffer.from(png.data, "base64"));
await call("Runtime.evaluate", { expression: "document.querySelector('[data-testid=stDataFrame]')?.scrollIntoView({block:'center'})", returnByValue: true }); await wait(400);
const tablePng = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
await fs.writeFile("/private/tmp/phase_g_local_ui_cases.png", Buffer.from(tablePng.data, "base64"));
console.log(JSON.stringify({ page: url, title: "Clinic Lead System 営業案件管理", dataframes: dataframes.result.value, uploadedCsv: true, previewCounts: JSON.parse(counts.result.value), previewRendered: true, screenshots: ["/private/tmp/phase_g_local_ui.png", "/private/tmp/phase_g_local_ui_cases.png"] }));
await call("Page.close");
ws.close();
