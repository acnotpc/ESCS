import http from 'node:http';
import {timingSafeEqual} from 'node:crypto';
import {pathToFileURL} from 'node:url';
export function tokenMatches(a,b){if(!a||!b)return false;const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&timingSafeEqual(x,y);}
export async function authorize(form,config,fetcher=fetch){
 if(!config.applicationToken||!config.allowedUserIds?.length)throw Error('App access is not configured');
 if(!tokenMatches(form.get('APPLICATION_TOKEN'),config.applicationToken))throw Error('Invalid application identity');
 const token=form.get('AUTH_ID');if(!token)throw Error('Missing user authorization');
 const response=await fetcher('https://escs.bitrix24.com/rest/user.current.json',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({auth:token}),redirect:'error',signal:AbortSignal.timeout(10000)});
 if(!response.ok)throw Error('User verification failed');const result=await response.json();
 if(result.error||!result.result?.ID||!config.allowedUserIds.includes(String(result.result.ID)))throw Error('User is not authorised for recruitment');
 return {id:String(result.result.ID),name:String(result.result.NAME||'Recruitment user')};
}
const escape=s=>s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function page(user){return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>ESCS Recruitment</title><style>body{font:17px system-ui;background:#f2f5f8;color:#162638;max-width:1000px;margin:auto;padding:28px}h1{color:#163b62}.notice,section{background:white;padding:22px;border-radius:12px;margin:16px 0}small{color:#526477}li{margin:12px 0}</style><h1>ESCS Recruitment</h1><p>Welcome, '+escape(user.name)+'.</p><div class="notice"><strong>Read-only setup pilot</strong><p>Bitrix24 identity verified. Applicant data connections are not configured. No applicant records have been loaded.</p></div><section><h2>Application reconciliation</h2><p>Compare Indeed notification emails, Jotform applications and Monday records. Keep uncertain matches for review.</p><small>Connection required: both recruitment mailboxes, Jotform and Monday.com.</small></section><section><h2>Evidence review</h2><ul><li>References and consent for each referee</li><li>Ten-year history and unexplained gaps</li><li>Required documents and verification status</li><li>Driving licence dates and renewal checkpoints</li></ul></section><section><h2>Follow-up controls</h2><p>Respect manual pauses and uncertain identities. Allow at least 72 hours between accepted record-check emails, with no more than three reminders. Review existing correspondence before recommending contact.</p><p>No sending, record updates or automated campaigns are enabled.</p></section></html>';}
export function createServer(config,fetcher=fetch){return http.createServer(async(req,res)=>{
 const headers={'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors https://escs.bitrix24.com; form-action 'none'","X-Content-Type-Options":"nosniff"};
 if(req.url==='/health'){res.writeHead(200,{'Content-Type':'application/json'});return res.end('{"status":"up","dataConnections":"unconfigured"}');}
 if(req.url!=='/app'||req.method!=='POST'){res.writeHead(404,headers);return res.end('Open this application from Bitrix24 after configuration.');}
 let chunks=[],size=0;try{for await(const c of req){size+=c.length;if(size>32768){res.writeHead(413,headers);return res.end('Request too large');}chunks.push(c);}
 const form=new URLSearchParams(Buffer.concat(chunks).toString());const user=await authorize(form,config,fetcher);res.writeHead(200,headers);res.end(page(user));
 }catch{res.writeHead(403,headers);res.end('Access denied. Confirm application configuration and recruitment user access.');}
 });}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){const config={applicationToken:process.env.BITRIX_APPLICATION_TOKEN,allowedUserIds:(process.env.BITRIX_ALLOWED_USER_IDS||'').split(',').map(s=>s.trim()).filter(Boolean)};createServer(config).listen(Number(process.env.PORT||3000),'0.0.0.0');}
