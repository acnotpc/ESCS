import test from 'node:test';import assert from 'node:assert/strict';import {authorize,tokenMatches,page,createServer,launchScript} from './server.mjs';
import vm from 'node:vm';
const c={applicationToken:'test-only',allowedUserIds:['9']};
const form=new URLSearchParams({APPLICATION_TOKEN:'test-only',AUTH_ID:'mock-user-token'});
const ok=async(url,options)=>{assert.equal(url,'https://escs.bitrix24.com/rest/user.current.json');assert.equal(options.redirect,'error');return {ok:true,json:async()=>({result:{ID:'9',NAME:'<script>'}})};};
test('reject missing configuration and forged application token before fetching',async()=>{await assert.rejects(authorize(form,{},()=>{throw Error('should not fetch')}));await assert.rejects(authorize(new URLSearchParams(),c,()=>{throw Error('should not fetch')}));});
test('verify current user and enforce allowlist',async()=>{assert.equal((await authorize(form,c,ok)).id,'9');await assert.rejects(authorize(form,{...c,allowedUserIds:['10']},ok));});
test('fail closed on provider errors',async()=>{await assert.rejects(authorize(form,c,async()=>({ok:false})));await assert.rejects(authorize(form,c,async()=>({ok:true,json:async()=>({error:'expired'})})));});
test('escape names and never show tokens',()=>{assert(page({name:'<script>'}).includes('&lt;script&gt;'));assert(!page({name:'User'}).includes('mock-user-token'));assert(!tokenMatches('a','ab'));});
test('GET recovery serves no records and POST still requires valid identity',async(t)=>{
 const server=createServer({applicationId:'106',allowedUserIds:['1']},async()=>{throw Error('unexpected provider call');});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));
 const url='http://127.0.0.1:'+server.address().port;
 const launch=await fetch(url+'/app?DOMAIN=escs.bitrix24.com');assert.equal(launch.status,200);
 assert.match(launch.headers.get('content-security-policy'),/form-action 'self'/);
 assert.match(await launch.text(),/Connecting to your Bitrix24 session/);
 assert.equal((await fetch(url+'/app',{method:'POST',body:'AUTH_ID='})).status,403);
 assert.equal((await fetch(url+'/launch.js')).status,200);
});
test('SDK recovery posts only the access token to the fixed app route',()=>{
 const status={};let submitted=false;let form;
 const document={getElementById:()=>status,body:{append(){}},createElement:tag=>tag==='form'?(form={append(input){this.input=input;},submit(){submitted=true;}}):{}};
 const run=(domain,embedded=true)=>{submitted=false;vm.runInNewContext(launchScript,{document,window:{top:1,self:embedded?2:1},BX24:{init:cb=>cb(),getAuth:()=>({domain,access_token:'ephemeral',refresh_token:'never-transfer'})},setTimeout:()=>1,clearTimeout(){}});};
 run('escs.bitrix24.com');assert(submitted);assert.equal(form.action,'/app');assert.equal(form.method,'POST');assert.equal(form.input.name,'AUTH_ID');assert.equal(form.input.value,'ephemeral');
 run('other.bitrix24.com');assert(!submitted);run('escs.bitrix24.com',false);assert(!submitted);
});
test('app106 recovery keeps app and owner checks enforced',async()=>{
 const config={applicationId:'106',allowedUserIds:['1']};
 const reply=(app,user)=>async url=>({ok:true,json:async()=>({result:url.includes('app.info')?{ID:app}:{ID:user,NAME:'Andy'}})});
 const auth=new URLSearchParams({AUTH_ID:'ephemeral'});
 assert.equal((await authorize(auth,config,reply('106','1'))).id,'1');
 await assert.rejects(authorize(auth,config,reply('107','1')));await assert.rejects(authorize(auth,config,reply('106','2')));
});
