// Google Maps Clinic Collector 5.7 - 厚生局母集団を1医院ずつ本人確認するキュー処理
const QK = {
  state:'clinicq_state', queue:'clinicq_queue', index:'clinicq_index', results:'clinicq_results',
  tabId:'clinicq_tab_id', startedAt:'clinicq_started_at', updatedAt:'clinicq_updated_at', current:'clinicq_current', logs:'clinicq_logs'
};
let clinicQueueRunning = false;
const qget = keys => new Promise(r => chrome.storage.local.get(keys, r));
const qset = obj => new Promise(r => chrome.storage.local.set(obj, r));
const delay = ms => new Promise(r => setTimeout(r, ms));
function qstamp(){ return new Date().toISOString(); }
async function qlog(msg){ const s=await qget([QK.logs]); const logs=Array.isArray(s[QK.logs])?s[QK.logs]:[]; logs.push({t:new Date().toLocaleTimeString('ja-JP',{hour12:false}),msg}); if(logs.length>500)logs.splice(0,logs.length-500); await qset({[QK.logs]:logs}); }
function mapsSearchUrl(q){ return 'https://www.google.com/maps/search/?api=1&query='+encodeURIComponent(q); }
async function ensureClinicTab(){
  const s=await qget([QK.tabId]); let id=s[QK.tabId];
  if(id){ try{ await chrome.tabs.get(id); return id; }catch(_){} }
  const tab=await chrome.tabs.create({url:'https://www.google.com/maps',active:false}); id=tab.id; await qset({[QK.tabId]:id}); return id;
}
async function waitTab(tabId, timeout=25000){
  const start=Date.now();
  while(Date.now()-start<timeout){ try{ const t=await chrome.tabs.get(tabId); if(t.status==='complete'){ await delay(1200); return true; } }catch(_){ return false; } await delay(250); }
  return false;
}
async function nav(tabId,url){ await chrome.tabs.update(tabId,{url,active:false}); if(!await waitTab(tabId))return false; for(let i=0;i<30;i++){ const p=await safeTabSendMessage(tabId,{action:'ping'}); if(p?.alive)return true; await delay(250);} return false; }
async function probe(tabId){ return await safeTabSendMessage(tabId,{action:'clinicQueueProbe'}); }
async function extract(tabId){ return await safeTabSendMessage(tabId,{action:'clinicQueueExtract'}); }
function resultBase(item){ return {
  internal_clinic_id:item.internal_clinic_id||'',medical_institution_number:item.medical_institution_number||'',source_clinic_name:item.source_clinic_name||'',
  source_phone:item.source_phone||'',source_address:item.source_address||'',source_prefecture:item.source_prefecture||'',maps_match_status:'',maps_match_method:'',
  maps_name:'',maps_phone:'',maps_address:'',maps_profile_url:'',maps_website_url:'',website_status:'',phone_match:'',name_match:'',address_match:'',
  exclude_reason:item.exclude_reason||'',scrape_status:'',scraped_at:qstamp(),'休診日':'','診療日':'','午前始':'','午前終':'','午後始':'','午後終':'','営業時間原文':''
}; }
function fillMatched(base, profile, evaled, method){
  base.maps_match_method=method;base.maps_name=profile.name||'';base.maps_phone=profile.phone||'';base.maps_address=profile.address||'';base.maps_profile_url=profile.profileUrl||'';
  base.maps_website_url=profile.websiteUrl||'';base.website_status=profile.websiteStatus|| (profile.websiteUrl?'MAPS_WEBSITE':'NO_WEBSITE');
  base.phone_match=evaled.phoneMatch?'1':'0';base.name_match=evaled.nameMatch?'1':'0';base.address_match=evaled.addressMatch?'1':'0';
  base['休診日']=profile.regularHoliday||'';base['診療日']=profile.businessDays||'';base['午前始']=profile.openTimeA||'';base['午前終']=profile.closeTimeA||'';
  base['午後始']=profile.openTimeB||'';base['午後終']=profile.closeTimeB||'';base['営業時間原文']=profile.rawHours||'';
  base.maps_match_status=profile.websiteUrl?'MAPS_MATCHED_WEBSITE':'MAPS_MATCHED_NO_WEBSITE';base.scrape_status='DONE'; return base;
}
async function inspectCandidate(tabId, url, source){
  if(!await nav(tabId,url)) return null;
  const p=await extract(tabId); if(!p?.ok||!p.profile)return null;
  const e=ClinicQueueLogic.evaluateCandidate(source,p.profile); return {profile:p.profile,e};
}
async function runSearch(tabId, source, query, method){
  if(!await nav(tabId,mapsSearchUrl(query))) return {kind:'error',reason:'MAPS_LOAD_FAILED'};
  await delay(1300);
  const scan=await probe(tabId);
  if(scan?.blocked)return {kind:'blocked',reason:scan.reason||'GOOGLE_BLOCKED'};
  const candidates=[];
  if(scan?.profile){ const e=ClinicQueueLogic.evaluateCandidate(source,scan.profile); candidates.push({profile:scan.profile,e}); }
  const urls=[...new Set((scan?.candidates||[]).map(x=>x.url).filter(Boolean))].slice(0,10);
  for(const u of urls){
    if(candidates.some(x=>x.profile?.profileUrl===u)) continue;
    const got=await inspectCandidate(tabId,u,source); if(got)candidates.push(got);
  }
  const exact=candidates.filter(x=>x.e.phoneMatch);
  if(exact.length===1){
    const x=exact[0];
    if(x.e.nameScore>=0.45 || x.e.addressScore>=0.55) return {kind:'matched',...x,method:'phone'};
  }
  if(exact.length>1){
    const ranked=exact.slice().sort((a,b)=>(b.e.nameScore+b.e.addressScore)-(a.e.nameScore+a.e.addressScore));
    if(ranked[0] && (!ranked[1] || (ranked[0].e.nameScore+ranked[0].e.addressScore)-(ranked[1].e.nameScore+ranked[1].e.addressScore)>=0.25))
      return {kind:'matched',...ranked[0],method:'phone'};
    return {kind:'ambiguous',candidates};
  }
  if(method==='name_address_fallback'){
    const strong=candidates.filter(x=>x.e.nameScore>=0.88 && x.e.addressScore>=0.82);
    if(strong.length===1)return {kind:'matched',...strong[0],method};
    if(strong.length>1)return {kind:'ambiguous',candidates:strong};
  }
  return {kind:candidates.length?'ambiguous':'not_found',candidates};
}
async function processItem(tabId,item){
  const base=resultBase(item);
  if(item.exclude_reason){ base.maps_match_status=item.exclude_reason==='hospital'?'EXCLUDED_HOSPITAL':'EXCLUDED_CENTER';base.scrape_status='EXCLUDED';return {result:base}; }
  const q1=[item.source_clinic_name,item.source_phone].filter(Boolean).join(' ');
  let r=await runSearch(tabId,item,q1,'phone');
  if(r.kind==='blocked')return {blocked:true,result:{...base,maps_match_status:'ERROR',scrape_status:'ERROR',website_status:'',exclude_reason:r.reason}};
  if(r.kind==='matched')return {result:fillMatched(base,r.profile,r.e,r.method)};
  const q2=[item.source_clinic_name,item.source_address].filter(Boolean).join(' ');
  if(q2 && q2!==q1){ const f=await runSearch(tabId,q2,'name_address_fallback'); if(f.kind==='blocked')return {blocked:true,result:{...base,maps_match_status:'ERROR',scrape_status:'ERROR',exclude_reason:f.reason}}; if(f.kind==='matched')return {result:fillMatched(base,f.profile,f.e,f.method)}; if(f.kind==='ambiguous')r=f; }
  base.maps_match_status=r.kind==='ambiguous'?'MAPS_AMBIGUOUS':'MAPS_NOT_FOUND';base.scrape_status='DONE';base.website_status=''; return {result:base};
}
async function runClinicQueue(){
  if(clinicQueueRunning)return; clinicQueueRunning=true;
  try{
    if(typeof ensureOffscreen==='function')await ensureOffscreen();
    let s=await qget(Object.values(QK)); let queue=Array.isArray(s[QK.queue])?s[QK.queue]:[],results=Array.isArray(s[QK.results])?s[QK.results]:[],idx=Number(s[QK.index]||0);
    if(!queue.length){await qset({[QK.state]:'idle'});return;}
    const tabId=await ensureClinicTab(); await qset({[QK.state]:'running'});
    for(;idx<queue.length;idx++){
      s=await qget([QK.state]); if(s[QK.state]!=='running')break;
      const item=queue[idx]; await qset({[QK.current]:item,[QK.index]:idx,[QK.updatedAt]:qstamp()}); await qlog(`${idx+1}/${queue.length} ${item.source_clinic_name}`);
      const out=await processItem(tabId,item);
      const pos=results.findIndex(x=>String(x.internal_clinic_id||'')===String(item.internal_clinic_id||'') && item.internal_clinic_id);
      if(pos>=0)results[pos]=out.result; else results.push(out.result);
      await qset({[QK.results]:results,[QK.index]:idx+1,[QK.updatedAt]:qstamp()});
      if(out.blocked){await qset({[QK.state]:'paused'});await qlog('Google側のアクセス制限を検知したため一時停止しました。');break;}
      await delay(700);
    }
    s=await qget([QK.state]); if(idx>=queue.length && s[QK.state]==='running'){await qset({[QK.state]:'done',[QK.current]:null});await qlog('全医院の処理が完了しました。');}
  }catch(e){console.error(e);await qset({[QK.state]:'paused'});await qlog('エラーで一時停止: '+String(e?.message||e));}
  finally{clinicQueueRunning=false;}
}
async function status(){ const s=await qget(Object.values(QK)); const q=Array.isArray(s[QK.queue])?s[QK.queue]:[],r=Array.isArray(s[QK.results])?s[QK.results]:[]; const counts={total:q.length,done:r.length,remaining:Math.max(0,q.length-r.length),matched:r.filter(x=>/^MAPS_MATCHED/.test(x.maps_match_status)).length,website:r.filter(x=>x.maps_match_status==='MAPS_MATCHED_WEBSITE').length,noWebsite:r.filter(x=>x.maps_match_status==='MAPS_MATCHED_NO_WEBSITE').length,notFound:r.filter(x=>x.maps_match_status==='MAPS_NOT_FOUND').length,ambiguous:r.filter(x=>x.maps_match_status==='MAPS_AMBIGUOUS').length,excluded:r.filter(x=>/^EXCLUDED_/.test(x.maps_match_status)).length,error:r.filter(x=>x.maps_match_status==='ERROR').length};return {state:s[QK.state]||'idle',index:Number(s[QK.index]||0),current:s[QK.current]||null,results:r,counts,logs:s[QK.logs]||[]}; }
chrome.runtime.onMessage.addListener((req,sender,sendResponse)=>{
  if(req?.action==='clinicQueueLoad'){(async()=>{const queue=Array.isArray(req.queue)?req.queue:[];const results=[];for(const item of queue.filter(x=>x.exclude_reason)){const b=resultBase(item);b.maps_match_status=item.exclude_reason==='hospital'?'EXCLUDED_HOSPITAL':'EXCLUDED_CENTER';b.scrape_status='EXCLUDED';results.push(b);}await qset({[QK.queue]:queue,[QK.results]:results,[QK.index]:0,[QK.state]:'paused',[QK.logs]:[],[QK.startedAt]:qstamp(),[QK.current]:null});sendResponse({ok:true,total:queue.length,excluded:results.length});})().catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;}
  if(req?.action==='clinicQueueStart'||req?.action==='clinicQueueResume'){qset({[QK.state]:'running'}).then(()=>{runClinicQueue();sendResponse({ok:true});});return true;}
  if(req?.action==='clinicQueuePause'){qset({[QK.state]:'paused'}).then(()=>sendResponse({ok:true}));return true;}
  if(req?.action==='clinicQueueReset'){qset({[QK.state]:'idle',[QK.queue]:[],[QK.results]:[],[QK.index]:0,[QK.logs]:[],[QK.current]:null}).then(()=>sendResponse({ok:true}));return true;}
  if(req?.action==='clinicQueueStatus'){status().then(x=>sendResponse({ok:true,...x}));return true;}
  if(req?.action==='clinicQueueDownload'){(async()=>{const s=await status();const csv=ClinicQueueLogic.resultsToCsv(s.results);const url='data:text/csv;charset=utf-8,'+encodeURIComponent(csv);const name=`GoogleMaps_Clinic_Results_${new Date().toISOString().slice(0,10).replace(/-/g,'')}.csv`;await chrome.downloads.download({url,filename:name,saveAs:true});sendResponse({ok:true,filename:name,count:s.results.length});})().catch(e=>sendResponse({ok:false,error:String(e?.message||e)}));return true;}
  return false;
});
setTimeout(async()=>{const s=await qget([QK.state]);if(s[QK.state]==='running')runClinicQueue();},800);
