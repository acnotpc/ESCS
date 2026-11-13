const views = ['Upcoming','Awaiting Confirmation','Trainer Allocated','Ready to Deliver','Course Ongoing','Evidence Outstanding','Certificate Due','Completed','Needs Attention'];
const records = [
 ['horizon','New Horizon Care Home','9–10 November 2026','Level 2 PI – Breakaway, Holding & Escorting · 09:00 start','Dates and venue confirmed in booking correspondence. Delivery status unverified.','Pending for both days','3rd Floor, Belmont Building, Belmont Road, Uxbridge UB8 1HE','SPACES, The Charter Building, Charter Place, Suite 065, Uxbridge UB8 1JG. PO recorded as N/A. Terms: 14 days. Invoice name unresolved (New Care Home Ltd or New Horizon Care Home Ltd). Fee and alternative payment arrangements unconfirmed.','Confirm both days’ learner numbers, invoice name, fee and trainer allocation.'],
 ['tutor12','TutorCare · London Metropolitan University','12 November 2026','09:00–16:00 · TCE640681','Scheduled in booking records.','Unverified','London Metropolitan University. Room and arrival arrangements unverified.','£600 per trainer/day recorded. PO and invoice routing unverified.','Confirm named trainer, learner count and how TutorCare will provide evidence. TutorCare manages the paperwork for this session.'],
 ['tutor23','TutorCare · Peterborough','23 November 2026','Physical intervention · 10:00–17:00 · TCE642479','Scheduled in booking records.','Unverified','Peterborough. Full address and access details unverified.','Fee, PO and invoice routing unverified.','Confirm venue address, learner count, trainer, commercial details and paperwork ownership.'],
 ['corran','Corran Support Ltd','7 December 2026','Rescheduled client training','7 December is the replacement date. The 3 November session was cancelled. Course scope and timings for the replacement date need confirmation.','12 plus Adam reported. Final count and Adam’s participation need confirmation.','Worcester was associated with the earlier booking. Confirm venue and address for December.','Fee, PO and invoice details unverified.','Confirm scope, timings, headcount and venue with booker Adam Bull. Track Cath’s review of the three-day theory material separately from delivery confirmation.'],
 ['tutor09','TutorCare · Southend','9 December 2026','Physical intervention · 09:30–16:00 · TCE642535','Scheduled in booking records.','Unverified','Southend. Full address and access details unverified.','Fee, PO and invoice routing unverified.','Confirm venue address, learner count, trainer, commercial details and paperwork ownership.']
];
const checks = [
 ['Before delivery',['Confirm learner numbers for each day','Confirm venue, access and start time','Confirm payment terms, invoice details and PO or record N/A','Confirm trainer allocation and course leader','Prepare trainer paperwork and confirm who holds it']],
 ['After delivery',['Verify attendance and file the register','Collect and reconcile feedback','Verify certificate eligibility and record issue','File supporting records and evidence references']]
];
const storageKey = 'mmva-readiness-v1';
let saved;
try { saved = JSON.parse(localStorage.getItem(storageKey)) || {}; } catch { saved = {}; }
let active = 'Upcoming';
function state(id) {
 return saved[id] ??= { status:'Awaiting Confirmation', allocated:false, checks:{}, notes:'' };
}
function persist() {
 try { localStorage.setItem(storageKey,JSON.stringify(saved)); } catch { document.querySelector('#count').textContent='Browser storage is unavailable. Edits are temporary.'; }
}
function matches(s) {
 if(active==='Upcoming') return ['Awaiting Confirmation','Ready to Deliver'].includes(s.status);
 if(active==='Trainer Allocated') return s.allocated;
 if(active==='Needs Attention') return ['Awaiting Confirmation','Evidence Outstanding','Certificate Due','Needs Attention'].includes(s.status);
 return s.status===active;
}
function field(label,value) {return '<dt>'+label+'</dt><dd>'+value+'</dd>';}
function render() {
 document.querySelector('nav').innerHTML = views.map(v=>'<button aria-pressed="'+(active===v)+'">'+v+'</button>').join('');
 document.querySelectorAll('nav button').forEach(button=>button.onclick=()=>{active=button.textContent;render();});
 const visible = records.filter(([id])=>matches(state(id)));
 document.querySelector('#count').textContent = visible.length+' of '+records.length+' bookings'+(visible.length?'':' · No bookings in this view.');
 document.querySelector('#bookings').replaceChildren(...visible.map(record=>{
  const [id,name,date,course,booking,learners,venue,invoice,next]=record;
  const s=state(id);
  const card=document.createElement('details');
  card.innerHTML='<summary>'+name+'<span>'+date+' · '+s.status+'</span></summary><p>'+course+'</p><div class="section"><section><div class="panel"><h3>Booking brief</h3><dl>'+field('Booking status',booking)+field('Learner numbers',learners)+field('Trainer allocation',id==='tutor12'?'One trainer required. Name unverified.':'Unverified.')+field('Course leader','Unverified.')+field('Venue',venue)+field('Invoice and PO',invoice)+'</dl></div><div class="panel"><h3>Evidence and follow-up</h3><dl>'+field('Attendance and feedback','Evidence unverified.')+field('Certificates','Eligibility and issue unverified.')+field('Supporting records',id==='tutor12'?'TutorCare manages paperwork. Filing references unverified.':'Filing references unverified.')+'</dl><p class="action">'+next+'</p><label>Working status<select>'+views.filter(v=>!['Upcoming','Trainer Allocated'].includes(v)).map(v=>'<option>'+v+'</option>').join('')+'</select></label><label><input type="checkbox" data-allocation> Trainer allocation verified</label><label>Updates, named roles and evidence references<textarea placeholder="Add confirmed details, pending items or links"></textarea></label></div></section><section>'+checks.map(([title,items],group)=>'<div class="panel"><h3>'+title+'</h3>'+items.map((item,index)=>'<label><input type="checkbox" data-check="'+group+'-'+index+'"> '+item+'</label>').join('')+'</div>').join('')+'</section></div>';
  const select=card.querySelector('select');
  select.value=s.status;
  select.onchange=()=>{s.status=select.value;persist();render();const same=document.getElementById(id);if(same) same.open=true;};
  card.id=id;
  const allocation=card.querySelector('[data-allocation]');
  allocation.checked=s.allocated;
  allocation.onchange=()=>{s.allocated=allocation.checked;persist();if(active==='Trainer Allocated')render();};
  const notes=card.querySelector('textarea');
  notes.value=s.notes;
  notes.oninput=()=>{s.notes=notes.value;persist();};
  card.querySelectorAll('[data-check]').forEach(input=>{
   input.checked=Boolean(s.checks[input.dataset.check]);
   input.onchange=()=>{s.checks[input.dataset.check]=input.checked;persist();};
  });
  return card;
 }));
}
render();
