import http from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {pathToFileURL} from 'node:url';
export function tokenMatches(a,b){if(!a||!b)return false;const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);}
export const launchScript = `(() => {
 const status = document.getElementById('status');
 const fail = () => { status.textContent = 'Unable to confirm your Bitrix24 session. Close this application and reopen ESCS Recruitment from the Bitrix24 menu.'; };
 if (window.top === window.self || typeof BX24 === 'undefined') return fail();
 const timer = setTimeout(fail, 20000);
 BX24.init(() => {
  const auth = BX24.getAuth();
  if (!auth || auth.domain !== 'escs.bitrix24.com' || !auth.access_token) { clearTimeout(timer); return fail(); }
  clearTimeout(timer);
  status.textContent = 'Session received. Verifying access and loading recruitment records…';
  const form = document.createElement('form');
  form.method = 'POST'; form.action = '/app';
  const input = document.createElement('input');
  input.type = 'hidden'; input.name = 'AUTH_ID'; input.value = auth.access_token;
  form.append(input); document.body.append(form); form.submit();
 });
})();`;
export function launchPage(){return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ESCS Recruitment</title><style>body{font:16px system-ui;background:#f2f5f8;color:#162638;max-width:900px;margin:auto;padding:28px}main{background:white;padding:24px;border-radius:12px}</style><main><h1>ESCS Recruitment</h1><p id="status" role="status">Connecting to your Bitrix24 session…</p><p>Access is checked before any recruitment records are displayed.</p><noscript>Enable JavaScript and reopen this application from Bitrix24.</noscript></main><script src="https://api.bitrix24.com/api/v1/"></script><script src="/launch.js"></script></html>';}
export async function authorize(form,config,fetcher=fetch){
 if(!(config.applicationToken||config.applicationId)||!config.allowedUserIds?.length)throw Error('App access is not configured');
 if(config.applicationToken&&!tokenMatches(form.get('APPLICATION_TOKEN'),config.applicationToken))throw Error('Invalid application identity');
 const token=form.get('AUTH_ID');if(!token)throw Error('Missing user authorization');
 if(config.applicationId){const check=await fetcher('https://escs.bitrix24.com/rest/app.info.json',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({auth:token}),redirect:'error',signal:AbortSignal.timeout(10000)});if(!check.ok)throw Error('App verification failed');const info=await check.json();if(info.error||String(info.result?.ID)!==String(config.applicationId))throw Error('Wrong application');}
 const response=await fetcher('https://escs.bitrix24.com/rest/user.current.json',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({auth:token}),redirect:'error',signal:AbortSignal.timeout(10000)});
 if(!response.ok)throw Error('User verification failed');const result=await response.json();
 if(result.error||!result.result?.ID||!config.allowedUserIds.includes(String(result.result.ID)))throw Error('User is not authorised for recruitment');
 return {id:String(result.result.ID),name:String(result.result.NAME||'Recruitment user')};
}
const escape=s=>s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function connections(live){
 if(!live)return '';
 const str=v=>escape(String(v??''));
 const reg=live.register, jf=live.jotform;
 let html='<section><h2>Applicant register</h2>';
 if(reg?.status==='connected'&&Array.isArray(reg.items)){html+='<p>'+str(reg.count)+' active records. '+str(reg.interpretation)+'</p><table><thead><tr><th>Name</th><th>Group / stage</th><th>Outstanding selected requirements</th></tr></thead><tbody>'+reg.items.filter(r=>/^\d+$/.test(r.id)).map(r=>'<tr><td><a target="_blank" rel="noopener noreferrer" href="https://exclusive-gang.monday.com/boards/424237712/pulses/'+r.id+'">'+str(r.name)+'</a></td><td>'+str(r.group)+' / '+str(r.stage)+'</td><td>'+str((r.outstanding||[]).join(', ')||'None recorded')+'</td></tr>').join('')+'</tbody></table>';}else html+='<p>Applicant register unavailable. No complete register is confirmed.</p>';
 html+='</section><section><h2>Jotform evidence connection</h2><p>'+str(jf?.status||'unavailable')+'</p>';
 if(jf?.status==='connected')html+='<p>'+str(Object.keys(jf.forms||{}).length)+' live checklist schemas checked. '+str(jf.scope)+'</p><ul>'+(jf.receiptIndex||[]).map(r=>'<li>'+str(r.kind)+': '+(r.ready?'index ready':'index incomplete')+'; last successful scan '+str(r.last_success)+'</li>').join('')+'</ul>';
 html+='</section><section><h2>Recruitment correspondence</h2>';
 if(Array.isArray(live.mailboxes))for(const box of live.mailboxes){html+='<h3>'+str(box.email)+'</h3><p>'+str(box.status)+' — '+str(box.reason||box.scope)+'</p>';if(Array.isArray(box.messages))html+='<ul>'+box.messages.map(m=>'<li>'+str(m.date)+' — '+str(m.subject)+'</li>').join('')+'</ul>';if(box.hasMore)html+='<p>More messages exist; this is not complete correspondence coverage.</p>';}
 else html+='<p>Mailbox connections unavailable.</p>';
 return html+'</section>';
}
export function page(user,live=null){const view=live?'<section><h2>Live recruitment review queue</h2><p>'+escape(String(live.scope))+'</p><p>Last read: '+escape(String(live.asOf))+' · Queue: '+escape(String(live.count))+'</p><ul>'+live.reviews.map(r=>'<li><a href="'+escape(r.sourceUrl)+'" target="_blank" rel="noopener noreferrer">Monday record '+escape(r.itemId)+'</a> — '+escape(r.state)+'; confirmed emails: '+escape(String(r.confirmedEmails))+'</li>').join('')+'</ul>'+(live.truncated?'<p>Showing the first 100 records.</p>':'')+'</section>':'<section><h2>Live review queue unavailable</h2><p>No current data could be verified. Do not interpret this as an empty queue.</p></section>';return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ESCS Recruitment</title><style>body{font:16px system-ui;background:#f2f5f8;color:#162638;max-width:1000px;margin:auto;padding:28px}h1{color:#163b62}.notice,section{background:white;padding:22px;border-radius:12px;margin:16px 0}small{color:#526477}li{margin:12px 0}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd;vertical-align:top}a{color:#164e80}</style><h1>ESCS Recruitment</h1><p>Welcome, '+escape(user.name)+'.</p><div class="notice"><strong>Read-only recruitment</strong><p>Bitrix24 identity verified. The live review queue is shown below when its read succeeds. See the source status below. Receipt and index status do not establish professional verification or complete inbox reconciliation.</p></div>'+connections(live)+view+'<section><h2>Application reconciliation</h2><p>Compare Indeed notification emails, Jotform applications and Monday records. Keep uncertain matches for review.</p><small>Connection required: both recruitment mailboxes, Jotform and Monday.com.</small></section><section><h2>Evidence review</h2><ul><li>References and consent for each referee</li><li>Ten-year history and unexplained gaps</li><li>Required documents and verification status</li><li>Driving licence dates and renewal checkpoints</li></ul></section><section><h2>Follow-up controls</h2><p>Respect manual pauses and uncertain identities. Allow at least 72 hours between accepted record-check emails, with no more than three reminders. Review existing correspondence before recommending contact.</p><p>No sending, record updates or automated campaigns are enabled.</p></section></html>';}
export function createServer(config,fetcher=fetch){return http.createServer(async(req,res)=>{
 const headers={'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors https://escs.bitrix24.com; form-action 'none'","X-Content-Type-Options":"nosniff"};
 if(new URL(req.url,'http://localhost').pathname==='/health'){res.writeHead(200,{'Content-Type':'application/json'});return res.end('{"status":"up","dataConnections":"unconfigured"}');}
 const path=new URL(req.url,'http://localhost').pathname;
 if(path==='/launch.js'&&req.method==='GET'){res.writeHead(200,{...headers,'Content-Type':'application/javascript; charset=utf-8'});return res.end(launchScript);}
 if(path==='/app'&&req.method==='GET'){res.writeHead(200,{...headers,'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'self' https://api.bitrix24.com/api/v1/; frame-ancestors https://escs.bitrix24.com; form-action 'self'; base-uri 'none'"});return res.end(launchPage());}
 if(path!=='/app'||req.method!=='POST'){res.writeHead(404,headers);return res.end('Open ESCS Recruitment from the Bitrix24 menu.');}
 let chunks=[],size=0;try{for await(const c of req){size+=c.length;if(size>32768){res.writeHead(413,headers);return res.end('Request too large');}chunks.push(c);}
 const form=new URLSearchParams(Buffer.concat(chunks).toString());const user=await authorize(form,config,fetcher);let live=null;try{const reply=await fetcher('https://escs.onrender.com/bitrix/recruitment/reviews',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({AUTH_ID:form.get('AUTH_ID'),dashboard:'1'}),redirect:'error',signal:AbortSignal.timeout(75000)});if(reply.ok){const data=await reply.json();if(data.readOnly===true&&Array.isArray(data.reviews)&&data.reviews.length<=100&&data.reviews.every(r=>typeof r.itemId==='string'&&/^\d+$/.test(r.itemId)&&r.sourceUrl==='https://exclusive-gang.monday.com/boards/424237712/pulses/'+r.itemId))live=data;}}catch{}res.writeHead(200,headers);res.end(page(user,live));
 }catch{res.writeHead(403,headers);res.end('Access denied. Confirm application configuration and recruitment user access.');}
 });}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){const config={applicationToken:process.env.BITRIX_APPLICATION_TOKEN,applicationId:process.env.BITRIX_APPLICATION_ID,allowedUserIds:(process.env.BITRIX_ALLOWED_USER_IDS||'').split(',').map(s=>s.trim()).filter(Boolean)};createServer(config).listen(Number(process.env.PORT||3000),'0.0.0.0');}
