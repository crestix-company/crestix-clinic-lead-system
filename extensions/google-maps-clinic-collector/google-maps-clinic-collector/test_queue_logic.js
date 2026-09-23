const assert=require('assert');const q=require('./queue-logic.js');
assert.equal(q.telMatchKey('03-1234-5678'),'312345678');assert.equal(q.telMatchKey('0312345678'),'312345678');assert.equal(q.telMatchKey('312345678'),'312345678');
let p=q.prepareQueue('clinic_id,医院名,電話番号,住所,施設区分\n1,青空クリニック,03-1234-5678,東京都千代田区1-1,診療所\n2,青空病院,03-9999-9999,東京都,病院\n3,健診センター,03-8888-8888,東京都,診療所\n');
assert.equal(p.queue.length,3);assert.equal(p.queue[1].exclude_reason,'hospital');assert.equal(p.queue[2].exclude_reason,'center');
let e=q.evaluateCandidate(p.queue[0],{name:'青空クリニック',phone:'0312345678',address:'東京都千代田区1-1'});assert(e.phoneMatch&&e.nameMatch&&e.addressMatch);
const csv=q.resultsToCsv([{internal_clinic_id:'1',maps_match_status:'MAPS_NOT_FOUND'}]);assert(csv.includes('maps_profile_url')&&csv.includes('MAPS_NOT_FOUND'));
console.log('queue-logic tests passed');
