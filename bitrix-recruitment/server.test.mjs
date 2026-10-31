import test from 'node:test';import assert from 'node:assert/strict';import {authorize,tokenMatches,page} from './server.mjs';
const c={applicationToken:'test-only',allowedUserIds:['9']};
const form=new URLSearchParams({APPLICATION_TOKEN:'test-only',AUTH_ID:'mock-user-token'});
const ok=async(url,options)=>{assert.equal(url,'https://escs.bitrix24.com/rest/user.current.json');assert.equal(options.redirect,'error');return {ok:true,json:async()=>({result:{ID:'9',NAME:'<script>'}})};};
test('reject missing configuration and forged application token before fetching',async()=>{await assert.rejects(authorize(form,{},()=>{throw Error('should not fetch')}));await assert.rejects(authorize(new URLSearchParams(),c,()=>{throw Error('should not fetch')}));});
test('verify current user and enforce allowlist',async()=>{assert.equal((await authorize(form,c,ok)).id,'9');await assert.rejects(authorize(form,{...c,allowedUserIds:['10']},ok));});
test('fail closed on provider errors',async()=>{await assert.rejects(authorize(form,c,async()=>({ok:false})));await assert.rejects(authorize(form,c,async()=>({ok:true,json:async()=>({error:'expired'})})));});
test('escape names and never show tokens',()=>{assert(page({name:'<script>'}).includes('&lt;script&gt;'));assert(!page({name:'User'}).includes('mock-user-token'));assert(!tokenMatches('a','ab'));});
