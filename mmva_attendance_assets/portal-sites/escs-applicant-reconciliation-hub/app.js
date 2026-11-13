'use strict';
const views = ['All cases','New','Match Confirmed','Possible Match','Missing Jotform','Missing Monday Record','Applicant Error Reported','Needs Follow-up','Completed','Needs Review'];
const initial = [
 ['basharat','Basharat Hussain','Support Worker · Stoke-on-Trent','8 Nov 2026, 17:55 GMT. Recruiter mailbox contains an Indeed message saying “Error”. The failed step was unspecified.'],
 ['ligi','Ligi Robin',null,'6 Nov 2026, 22:10 GMT. Recruiter mailbox confirms a link error and a later reply supplying the replacement form.'],
 ['mojeed','Mojeed Oladunjoye',null,'6 Nov 2026, 17:10 GMT. Recruiter mailbox confirms a link error and a later reply supplying the replacement form.'],
 ['sarah','Sarah Carroll','Support Worker · Stoke-on-Trent','6 Nov 2026. Recruiter mailbox confirms a missing link and a reply providing the replacement form.']
].map(([id,name,vacancy,notification]) => ({
 id,name,vacancy,notification,status:'Applicant Error Reported',views:['Needs Review','Needs Follow-up'],
 discrepancy:'Application access issue reported',next:'Open the original correspondence, confirm vacancy and application date in Indeed, then compare Jotform and Monday records using a corroborating identifier.',
 links:[{label:'Search Exclusive Recruitment mailbox for this name',url:'https://mail.google.com/mail/u/?authuser=info%40exclusive-recruitment.co.uk#search/'+encodeURIComponent('"'+name+'"')},{label:'Replacement application form',url:'https://form.jotform.com/260903309464356'}]
}));
const threads = {basharat:'1a1bc4d41ce4fbd9',ligi:'1a1b2e9bfe818d1a',mojeed:'1a1b1d76be3e8b85',sarah:'1a1b0f46b7010899'};
for(const row of initial){
 row.verified = '10 Nov 2026, 10:57 GMT · mailbox only';
 row.links.unshift({label:'Open original recruiter mailbox thread',url:'https://mail.google.com/mail/u/?authuser=info%40exclusive-recruitment.co.uk#all/'+threads[row.id]});
}
const fields = {vacancy:'Vacancy',applicationDate:'Application date',notification:'Indeed notification or recruiter message',jotform:'Jotform submission',monday:'Monday.com record',identity:'Possible identity match',discrepancy:'Discrepancy classification',status:'Current status',verified:'Last verified time',next:'Next action'};
const $ = id => document.getElementById(id);
const node = (tag,value) => Object.assign(document.createElement(tag),{textContent:value ?? 'Unknown / unchecked'});
let records = initial, active = views[0];
function render(){
 const query = $('search').value.trim().toLowerCase();
 const filtered = records.filter(row => (active === views[0] || row.status === active || row.views?.includes(active)) && JSON.stringify(row).toLowerCase().includes(query));
 $('count').textContent = filtered.length+' of '+records.length+' cases';
 $('rows').replaceChildren(...filtered.map(row => {
  const tr = node('tr',''), cell = node('td',''), button = node('button',row.name);
  button.onclick = () => openCase(row); cell.append(button); tr.append(cell);
  for(const key of ['vacancy','applicationDate','status','verified']) tr.append(node('td',row[key]));
  return tr;
 }));
 for(const button of $('views').children) button.setAttribute('aria-pressed',String(button.textContent === active));
}
function openCase(row){
 $('caseTitle').textContent=row.name;
 const list = node('dl','');
 for(const [key,label] of Object.entries(fields)) list.append(node('dt',label),node('dd',row[key]));
 const links = node('ul','');
 for(const link of row.links ?? []){
  if(!/^https:\/\//i.test(link?.url)) continue;
  const item = node('li',''), a = node('a',link.label || link.url);
  a.href=link.url; a.target='_blank'; a.rel='noopener noreferrer'; item.append(a); links.append(item);
 }
 $('caseBody').replaceChildren(list,node('h3','Supporting links'),links.children.length ? links : node('p','No links supplied.'));
 if(row.conflicts){
  $('caseBody').append(node('h3','Conflicting evidence supplied for this case'));
  for(const version of row.conflicts){
   const variant = node('dl','');
   variant.append(node('dt','Name supplied'),node('dd',version.name));
   for(const [key,label] of Object.entries(fields)) variant.append(node('dt',label),node('dd',version[key]));
   $('caseBody').append(variant);
  }
 }
 $('detail').showModal();
}
for(const view of views){
 const button = node('button',view); button.onclick=()=>{active=view;render();}; $('views').append(button);
}
$('search').oninput=render;
$('close').onclick=()=>$('detail').close();
$('reset').onclick=()=>{records=initial;active=views[0];$('file').value='';$('importStatus').textContent='Initial cases restored.';render();};
$('file').onchange=async event=>{
 const file=event.target.files[0]; if(!file) return;
 try {
  const parsed=JSON.parse(await file.text()), grouped=new Map();
  if(!Array.isArray(parsed)) throw Error('Use an array of case records.');
  for(const row of parsed){
   if(!row || typeof row.id!=='string' || !row.id.trim() || typeof row.name!=='string' || !row.name.trim()) throw Error('Each case needs a non-empty text ID and name.');
   for(const key of Object.keys(fields)) if(row[key]!=null && typeof row[key]!=='string') throw Error('Case fields must be text or null.');
   if(row.views!=null && !Array.isArray(row.views) || row.links!=null && !Array.isArray(row.links)) throw Error('Views and links must be arrays.');
   const clean={id:row.id.trim(),name:row.name.trim()};
   for(const key of Object.keys(fields)) clean[key]=row[key] ?? null;
   clean.views=row.views ?? []; clean.links=row.links ?? [];
   const group=grouped.get(clean.id) ?? new Map(); group.set(JSON.stringify(clean),clean); grouped.set(clean.id,group);
  }
  records=Array.from(grouped,([id,group])=>{
   const cases=[...group.values()]; if(cases.length===1) return cases[0];
   return {id,name:[...new Set(cases.map(row=>row.name))].join(' / '),status:'Needs Review',discrepancy:'Conflicting entries share the same case ID',next:'Compare the evidence for each version before confirming the match.',links:cases.flatMap(row=>row.links),conflicts:cases};
  });
  active=views[0]; $('importStatus').textContent=records.length+' case IDs loaded for this session.'; render();
 } catch(error){$('importStatus').textContent='Snapshot could not be opened. '+error.message;}
};
render();
