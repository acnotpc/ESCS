'use strict';
const $=id=>document.getElementById(id);
const views=['All','New','Monitoring','Formal Stage','Action Required','Deadline Approaching','Evidence Outstanding','Submitted','Closed','Needs Review'];
const kinds=['Tender','Framework','Dynamic market','DPS','Bid','Certification','Training evidence','Lead','Marketing'];
const stages=views.filter(v=>!['All','Deadline Approaching'].includes(v));
const fields=[
['ref','Reference'],['business','Business',['ESCS','MMVA','Both']],['kind','Record type',kinds],['org','Organisation'],['scope','Scope'],['stage','Current stage',stages],['confidence','Evidence classification',['Unverified','Official notice','Exploratory lead','Reported internally','Documented request','Verified in source']],['owner','Owner'],['deadline','Deadline (UK date)',null,'date'],['deadlineType','Deadline time and purpose (UK)'],['delivery','Delivery status'],['verified','Last verified (date and time with zone, if known)'],['link','Evidence link',null,'url'],['route','Submission route'],['documents','Portal documents and versions'],['evidence','Latest evidence'],['uncertainty','Uncertainty or conflict'],['gap','Outstanding evidence'],['next','Next action']];
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const val=v=>esc(v||'Unknown');
const key=r=>r.ref.trim().toLowerCase().replace(/\s+/g,' ');
const storageKey='escs-mmva-opportunity-register-v1';
let records=seed,view='All';
try{const saved=localStorage.getItem(storageKey);if(saved)records=JSON.parse(saved)}catch{}
const fingerprint=r=>JSON.stringify(fields.filter(([id])=>id!=='verified').map(([id])=>r[id]||''));
const ukToday=()=>new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/London',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const dayDelta=r=>(Date.parse(r.deadline)-Date.parse(ukToday()))/86400000;
function matches(r,v){
 if(v==='All')return true;
 if(v==='Deadline Approaching')return !['Closed','Submitted'].includes(r.stage)&&dayDelta(r)>=0&&dayDelta(r)<=14;
 if(v==='Evidence Outstanding')return !!r.gap||r.stage===v;
 if(v==='Needs Review')return !!r.uncertainty||r.stage===v;
 if(v==='Action Required')return r.stage===v||(!['Closed','Submitted'].includes(r.stage)&&dayDelta(r)<0);
 return r.stage===v;
}
function render(){
 $('views').innerHTML=views.map(v=>'<button data-view="'+v+'" aria-pressed="'+(view===v)+'">'+v+' · '+records.filter(r=>matches(r,v)).length+'</button>').join('');
 $('metrics').innerHTML=['All','Formal Stage','Action Required','Deadline Approaching','Evidence Outstanding'].map(v=>'<button data-view="'+v+'"><strong>'+records.filter(r=>matches(r,v)).length+'</strong>'+(v==='All'?'Tracked records':v)+'</button>').join('');
 const q=$('search').value.trim().toLowerCase();
 const shown=records.filter(r=>matches(r,view)&&($('business').value==='All businesses'||r.business===$('business').value)&&(!$('kind').value||r.kind===$('kind').value)&&fields.some(([id])=>String(r[id]||'').toLowerCase().includes(q))).sort((a,b)=>(a.deadline||'9999').localeCompare(b.deadline||'9999'));
 $('rows').innerHTML=shown.map(r=>'<tr><td><button data-record="'+records.indexOf(r)+'">'+esc(r.scope)+'</button><small>'+esc(r.ref)+' · '+esc(r.org)+'</small><small>'+esc(r.business)+' / '+esc(r.kind)+' · '+val(r.confidence)+'</small></td><td><span class="pill">'+esc(r.stage)+'</span><small>'+val(r.delivery)+'</small></td><td>'+val(r.deadline)+'<small>'+esc(r.deadlineType)+'</small>'+(!['Closed','Submitted'].includes(r.stage)&&dayDelta(r)<0?'<span class="pill warn">Past date</span>':'')+'</td><td>'+val(r.owner)+'</td><td>'+val(r.next)+'</td></tr>').join('')||'<tr><td colspan="5">No records in this view.</td></tr>';
 $('status').textContent=shown.length+' of '+records.length+' records · '+view;
}
function dialog(html){$('panel').innerHTML='<button class="close" aria-label="Close dialog">×</button>'+html;if(!$('dialog').open)$('dialog').showModal()}
function link(s){try{const u=new URL(s);if(['https:','http:'].includes(u.protocol))return '<a target="_blank" rel="noopener noreferrer" href="'+esc(u.href)+'">'+esc(s)+'</a>'}catch{}return val(s)}
function history(changes){return changes.length?'<ol>'+changes.map(c=>'<li><b>'+esc(c.ref)+' · '+esc(c.at)+'</b><div>'+esc(c.summary)+'</div></li>').join('')+'</ol>':'<p>No changes recorded.</p>'}
function details(i){
 const r=records[i];dialog('<small>'+esc(r.business)+' / '+esc(r.kind)+'</small><h2>'+esc(r.scope)+'</h2><dl>'+fields.map(([id,label])=>'<div><dt>'+label+'</dt><dd>'+(id==='link'?link(r[id]):id==='route'&&String(r[id]).startsWith('https://')?link(r[id].split(' · ')[0])+esc(r[id].includes(' · ')?' · '+r[id].split(' · ').slice(1).join(' · '):''):val(r[id]))+'</dd></div>').join('')+'</dl><button data-edit="'+i+'">Edit tracker record</button><h3>Material change history</h3>'+history(r.changes||[]));
}
function editor(i){
 const r=records[i]||{};
 dialog('<h2>'+(r.ref?'Edit tracker record':'Add tracker record')+'</h2><p>Use a stable procurement or case reference. Record amendments under the existing reference.</p><form><div class="grid">'+fields.map(([id,label,opts,type])=>{
 const wide=['scope','documents','evidence','uncertainty','gap','next','route','link'].includes(id);
 const required=['ref','org','scope'].includes(id)?' required':'';
 return '<label class="field'+(wide?' wide':'')+'">'+label+(opts?'<select name="'+id+'">'+opts.map(o=>'<option'+(o===r[id]?' selected':'')+'>'+esc(o)+'</option>').join('')+'</select>':wide&&id!=='link'?'<textarea name="'+id+'"'+required+'>'+esc(r[id])+'</textarea>':'<input name="'+id+'" type="'+(type||'text')+'" value="'+esc(r[id])+'"'+required+'>')+'</label>';
 }).join('')+'</div><p><button class="primary">Save tracker record</button></p><p role="status" id="editStatus"></p></form>');
 $('panel').querySelector('form').onsubmit=e=>{
  e.preventDefault();const entry=Object.fromEntries([...new FormData(e.target)].map(([k,v])=>[k,v.trim()]));
  if(!entry.ref||!entry.org||!entry.scope){$('editStatus').textContent='Enter a reference, organisation and scope.';return}
  if(records.some((other,j)=>i!==j&&key(other)===key(entry))){$('editStatus').textContent='This reference is already tracked. Open the existing record to record any changes.';return}
  entry.changes=[...(r.changes||[])];
  if(r.ref&&fingerprint(r)!==fingerprint(entry)){
   const changed=fields.filter(([id])=>id!=='verified'&&(r[id]||'')!==(entry[id]||''));
   entry.changes.unshift({ref:entry.ref,at:new Date().toISOString(),summary:changed.map(([id,label])=>label+' — '+(r[id]||'Unknown')+' → '+(entry[id]||'Unknown')).join(' | ')});
  }
  const updated=records.slice();if(r.ref)updated[i]=entry;else updated.push(entry);
  try{save(updated);details(r.ref?i:records.length-1)}catch{$('editStatus').textContent='Browser storage is unavailable. The record was not saved.'}
 };
}
function save(next){localStorage.setItem(storageKey,JSON.stringify(next));records=next;render()}
$('kind').innerHTML='<option value="">All record types</option>'+kinds.map(k=>'<option>'+k+'</option>').join('');
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.view){view=b.dataset.view;render()}if(b.dataset.record!==undefined)details(+b.dataset.record);if(b.dataset.edit!==undefined)editor(+b.dataset.edit);if(b.classList.contains('close'))$('dialog').close()});
$('add').onclick=()=>editor(-1);
$('changes').onclick=()=>dialog('<h2>Material changes</h2><p>Repeated imports and verification-time-only edits do not add events. Changes appear here without sending notifications.</p>'+history(records.flatMap(r=>r.changes||[]).sort((a,b)=>b.at.localeCompare(a.at))));
$('export').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify({version:1,records},null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='opportunity-register.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
$('import').onchange=async e=>{
 const file=e.target.files[0];e.target.value='';if(!file)return;
 try{
  const data=JSON.parse(await file.text());if(data.version!==1||!Array.isArray(data.records))throw Error('Select a version 1 register export.');
  const next=records.slice();let added=0,skipped=0,conflicts=0;
  for(const r of data.records){
   if(!r||fields.some(([id])=>r[id]!==undefined&&typeof r[id]!=='string')||!r.ref?.trim()||!r.org?.trim()||!r.scope?.trim()||!stages.includes(r.stage)||!kinds.includes(r.kind)||!['ESCS','MMVA','Both'].includes(r.business)||!Array.isArray(r.changes)||r.changes.some(c=>!c||['ref','at','summary'].some(k=>typeof c[k]!=='string')))throw Error('The file contains an invalid record.');
   const old=next.find(o=>key(o)===key(r));if(!old){next.push(r);added++}else if(fingerprint(r)===fingerprint(old)){skipped++}else{conflicts++}
  }
  save(next);$('status').textContent=added+' imported · '+skipped+' unchanged skipped · '+conflicts+' conflicting records skipped. Compare conflicts against the existing record before editing.';
 }catch(error){$('status').textContent='Import failed. '+error.message}
};
for(const id of ['search','business','kind'])$(id).addEventListener('input',render);
render();
