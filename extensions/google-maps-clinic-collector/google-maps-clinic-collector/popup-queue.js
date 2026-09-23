const $=id=>document.getElementById(id);
const send=msg=>new Promise(r=>chrome.runtime.sendMessage(msg,x=>r(chrome.runtime.lastError?{ok:false,error:chrome.runtime.lastError.message}:(x||{}))));
$('file').addEventListener('change',async e=>{const f=e.target.files?.[0];if(!f)return;try{const txt=await f.text();const p=ClinicQueueLogic.prepareQueue(txt);const res=await send({action:'clinicQueueLoad',queue:p.queue});if(!res.ok)throw new Error(res.error||'読込失敗');$('loadmsg').textContent=`${res.total}医院を読み込み。検索前除外 ${res.excluded}件。`;refresh();}catch(err){$('loadmsg').textContent='エラー: '+err.message;}});
$('start').onclick=async()=>{const r=await send({action:'clinicQueueStart'});if(!r.ok)alert(r.error||'開始失敗');refresh();};
$('pause').onclick=async()=>{await send({action:'clinicQueuePause'});refresh();};
$('resume').onclick=async()=>{await send({action:'clinicQueueResume'});refresh();};
$('download').onclick=async()=>{const r=await send({action:'clinicQueueDownload'});if(!r.ok)alert(r.error||'保存失敗');};
$('reset').onclick=async()=>{if(confirm('取得済み結果と進捗を削除しますか？')){await send({action:'clinicQueueReset'});refresh();}};
async function refresh(){const r=await send({action:'clinicQueueStatus'});if(!r.ok)return;const c=r.counts||{};$('state').textContent='状態: '+({idle:'待機中',paused:'一時停止',running:'実行中',done:'完了'}[r.state]||r.state);$('current').textContent=r.current?`${r.index+1}/${c.total||0} ${r.current.source_clinic_name||''}`:'-';for(const [id,k] of [['total','total'],['done','done'],['matched','matched'],['website','website'],['noweb','noWebsite'],['notfound','notFound'],['amb','ambiguous'],['excluded','excluded'],['error','error'],['remaining','remaining']])$(id).textContent=c[k]||0;$('logs').innerHTML=(r.logs||[]).slice(-120).map(x=>`[${esc(x.t)}] ${esc(x.msg)}<br>`).join('');$('logs').scrollTop=$('logs').scrollHeight;$('pause').disabled=r.state!=='running';$('resume').disabled=r.state!=='paused';$('start').disabled=!['idle','paused','done'].includes(r.state)||!c.total;$('download').disabled=!c.done;}
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
refresh();setInterval(refresh,1200);
