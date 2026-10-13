"""OAuth call-card application. Disabled until installed and routing verified."""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from switchboard_bridge import phone

NAMES = {1: 'Control Room Team', 2: 'Control Room Team', 3: 'Accounts Team',
         4: 'Recruitment Administrator', 5: 'Control Room Team'}
LABELS = {1: 'Bookings', 2: 'Customer Services', 3: 'Accounts',
          4: 'Recruitment and HR', 5: 'Anything else'}
BASE = 'https://escs.onrender.com'
PORTAL = 'https://escs.bitrix24.com'
HEADERS = {'Content-Security-Policy': "default-src 'self'; style-src 'unsafe-inline'; frame-ancestors https://escs.bitrix24.com; form-action 'self'", 'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'}


def page(content):
    return HTMLResponse('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>ESCS Call Actions</title><style>body{font:16px system-ui;color:#203344;background:#f6f8fa;margin:24px;max-width:850px}article,form{background:white;padding:20px;border-radius:12px;margin-bottom:16px}label{display:block;margin:12px 0}input,textarea,select{display:block;width:95%;padding:10px;margin-top:6px;border:1px solid #c4d0d9;border-radius:6px;font:inherit}button{background:#126979;color:white;padding:12px 20px;border:0;border-radius:6px}a{color:#126979}</style></head><body><h1>ESCS Call Actions</h1>'+content+'</body></html>', headers=HEADERS)


class CallActions:
    def __init__(self, bridge):
        self.bridge = bridge
        self.enabled = os.getenv('SWITCHBOARD_POPUPS_ENABLED') == 'true'
        self.menu_field = os.getenv('SWITCHBOARD_POPUP_MENU_FIELD', '')
        self.answerers = json.loads(os.getenv('SWITCHBOARD_POPUP_ANSWERER_MAP', '{}'))
        self.line = phone(os.getenv('SWITCHBOARD_ESCS_LINE', '08435236264'))
        self.lock = asyncio.Lock()
        self.last_error = None
        self.initialized = False
        self.target_cache = None
        self.target_cache_at = 0
        self.app_secret = os.getenv('SWITCHBOARD_APP_SESSION_SECRET','')
        self.serializer = URLSafeTimedSerializer(self.app_secret, salt='sbf-call-actions') if self.app_secret else None
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256((self.app_secret+'/oauth-v1').encode()).digest())) if self.app_secret else None

    def require_secret(self):
        if not self.app_secret:
            raise HTTPException(503,'Application security configuration is pending')

    def db(self):
        db = self.bridge.connect()
        if not self.initialized:
            db.executescript('''CREATE TABLE IF NOT EXISTS sbf_app_auth(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sbf_call_actions(ref TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                department INTEGER NOT NULL, call_id TEXT, state TEXT NOT NULL DEFAULT 'open',
                entity_type TEXT, entity_id INTEGER, activity_id INTEGER, caller_name TEXT,
                enquiry TEXT, outcome TEXT, callback_at TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sbf_action_audit(id INTEGER PRIMARY KEY, ref TEXT, user_id INTEGER, value TEXT, created REAL, published INTEGER DEFAULT 0);''')
            db.commit()
            self.initialized = True
        return db

    def auth(self):
        row = self.db().execute('SELECT value FROM sbf_app_auth WHERE id=1').fetchone()
        if row:
            self.require_secret()
        return json.loads(self.cipher.decrypt(row[0].encode())) if row else None

    def save_auth(self, value):
        self.require_secret()
        self.db().execute('INSERT INTO sbf_app_auth VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value', (self.cipher.encrypt(json.dumps(value).encode()).decode(),))
        self.db().commit()

    def install_key(self):
        self.require_secret()
        return hmac.new(self.app_secret.encode(), b'sbf-app-install-v1', hashlib.sha256).hexdigest()

    async def raw_api(self, token, method, params):
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(PORTAL+'/rest/'+method, json={**params, 'auth': token})
            response.raise_for_status()
            result = response.json()
        if 'error' in result or 'result' not in result:
            raise RuntimeError('Application API request failed')
        return result['result']

    async def api(self, method, params):
        auth = self.auth()
        if not auth:
            raise RuntimeError('Telephony application is not installed')
        if auth['expires_at'] < time.time()+90:
            client_secret = os.getenv('SWITCHBOARD_APP_CLIENT_SECRET', '')
            if not client_secret:
                raise RuntimeError('Application renewal credential is not configured')
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post('https://oauth.bitrix.info/oauth/token/', data={'grant_type':'refresh_token', 'client_id':auth['client_id'], 'client_secret':client_secret, 'refresh_token':auth['refresh_token']})
                response.raise_for_status()
                fresh = response.json()
            if fresh.get('domain') != 'escs.bitrix24.com' or not fresh.get('access_token') or not fresh.get('refresh_token'):
                raise RuntimeError('Invalid OAuth renewal')
            auth.update(access_token=fresh['access_token'], refresh_token=fresh['refresh_token'], expires_at=time.time()+int(fresh.get('expires_in',3600)))
            self.save_auth(auth)
        return await self.raw_api(auth['access_token'], method, params)

    async def install(self, data):
        def get(key, fallback=''):
            auth = data.get('auth')
            return (auth.get(key, fallback) if isinstance(auth,dict) else data.get('auth['+key+']',fallback))
        token = get('access_token') or data.get('AUTH_ID','')
        refresh = get('refresh_token') or data.get('REFRESH_ID','')
        domain = get('domain') or data.get('DOMAIN','')
        application_token = get('application_token') or data.get('APPLICATION_TOKEN','')
        if domain != 'escs.bitrix24.com' or not all((token,refresh,application_token)):
            raise HTTPException(400,'Invalid installation')
        profile = await self.raw_api(token,'profile',{})
        info = await self.raw_api(token,'app.info',{})
        scopes = set(await self.raw_api(token,'scope',{}))
        if str(profile.get('ID')) != '1' or not str(info.get('CODE','')).startswith('local.') or not {'crm','telephony','placement'}.issubset(scopes) or not scopes.intersection({'user_brief','user_basic','user'}):
            raise HTTPException(403,'Install as Andy with CRM, Telephony, widget and brief user-directory permissions')
        existing = self.auth()
        if existing and existing['client_id'] != info['CODE']:
            raise HTTPException(409,'A different application is already configured')
        self.save_auth({'access_token':token,'refresh_token':refresh,'application_token':application_token,
                        'client_id':info['CODE'],'expires_at':time.time()+int(get('expires_in',data.get('AUTH_EXPIRES',3600)))})
        return info['CODE']

    async def users(self):
        if self.target_cache is not None and time.time()-self.target_cache_at < 60:
            return self.target_cache
        users = {}
        start = 0
        # Bounded directory enumeration; unique active exact names only.
        for _ in range(20):
            rows = await self.api('user.get',{'FILTER':{'ACTIVE':True},'start':start})
            for row in rows:
                name = ' '.join(filter(None,[row.get('NAME'),row.get('LAST_NAME')])).strip().casefold()
                if name in {n.casefold() for n in NAMES.values()}:
                    users.setdefault(name,[]).append(int(row['ID']))
            if len(rows)<50:
                break
            start += 50
        result = {}
        for option,name in NAMES.items():
            matches = list(set(users.get(name.casefold(),[])))
            if len(matches)==1:
                result[option]=matches[0]
        self.target_cache=result
        self.target_cache_at=time.time()
        return result

    def department(self, data):
        value = data.get('_department')
        if value is not None and str(value) in {'1','2','3','4','5'}:
            return int(value)
        answerer = data.get('answeredBy') if data.get('item')=='inbound_call' else data.get('callOriginator')
        mapped = self.answerers.get(str(answerer or ''))
        return int(mapped) if str(mapped) in {'1','2','3','4','5'} else None

    async def bind_widget(self):
        await self.api('placement.bind',{'PLACEMENT':'CALL_CARD','HANDLER':BASE+'/switchboard/app/frame','TITLE':'ESCS Call Outcome','DESCRIPTION':'Confirm caller and record call outcome'})

    async def flush(self):
        async with self.lock:
            if not self.enabled or not self.auth():
                return
            targets = await self.users()
            db = self.db()
            rows = db.execute("SELECT * FROM calls WHERE state IN ('waiting','pending','done') ORDER BY received DESC LIMIT 100").fetchall()
            for row in rows:
                data = json.loads(row['payload'])
                number = phone(data.get('callersNumber') if row['item']=='inbound_call' else data.get('numberDialled'))
                line = phone(data.get('calledNumber') if row['item']=='inbound_call' else data.get('cliDisplayed'))
                if line != self.line or not number:
                    continue
                received = data.get('_event_received_at') or datetime.fromisoformat(row['received']).replace(tzinfo=timezone.utc).timestamp()
                if time.time()-float(received)>600:
                    continue  # Never replay historical call pop-ups.
                option = self.department(data)
                if option not in targets:
                    continue
                uid = targets[option]
                action = db.execute('SELECT * FROM sbf_call_actions WHERE ref=?',(row['ref'],)).fetchone()
                if action is None:
                    db.execute('INSERT INTO sbf_call_actions(ref,user_id,department,created) VALUES(?,?,?,?)',(row['ref'],uid,option,time.time()))
                    db.commit()
                    action = db.execute('SELECT * FROM sbf_call_actions WHERE ref=?',(row['ref'],)).fetchone()
                if action['user_id'] != uid or action['department'] != option:
                    self.last_error='Conflicting call route requires review'
                    continue
                if not action['call_id']:
                    matches = await self.bridge.api('crm.duplicate.findbycomm',{'type':'PHONE','values':[number]})
                    matches = {} if matches==[] else matches
                    if not isinstance(matches,dict) or len(matches.get('CONTACT',[]))>1 or len(matches.get('COMPANY',[]))>1:
                        continue
                    entity = 'contact' if matches.get('CONTACT') else 'company' if matches.get('COMPANY') else None
                    entity_id = (matches.get('CONTACT') or matches.get('COMPANY') or [None])[0]
                    params={'USER_ID':uid,'PHONE_NUMBER':number,'TYPE':2 if row['item']=='inbound_call' else 1,'CRM_CREATE':0,'SHOW':0 if row['state']=='done' else 1,'ADD_TO_CHAT':0,'LINE_NUMBER':self.line,'EXTERNAL_CALL_ID':row['ref']}
                    if entity:
                        params.update(CRM_ENTITY_TYPE=entity.upper(),CRM_ENTITY_ID=entity_id)
                    result = await self.effect(row['ref'],'register','telephony.externalCall.register',params)
                    db.execute('UPDATE sbf_call_actions SET call_id=?,entity_type=?,entity_id=? WHERE ref=?',(result['CALL_ID'],entity,entity_id,row['ref']))
                    db.commit()
                    action=db.execute('SELECT * FROM sbf_call_actions WHERE ref=?',(row['ref'],)).fetchone()
                if row['state']=='done':
                    if not action['entity_id']:
                        matched = await self.bridge.api('crm.duplicate.findbycomm',{'type':'PHONE','values':[number]})
                        matched = {} if matched==[] else matched
                        if isinstance(matched,dict) and len(matched.get('CONTACT',[]))<=1 and len(matched.get('COMPANY',[]))<=1:
                            entity='contact' if matched.get('CONTACT') else 'company' if matched.get('COMPANY') else None
                            entity_id=(matched.get('CONTACT') or matched.get('COMPANY') or [None])[0]
                            db.execute('UPDATE sbf_call_actions SET entity_type=?,entity_id=? WHERE ref=?',(entity,entity_id,row['ref']));db.commit()
                            action=db.execute('SELECT * FROM sbf_call_actions WHERE ref=?',(row['ref'],)).fetchone()
                    outcome = data.get('Result',data.get('result','Unknown'))
                    await self.effect(row['ref'],'finish','telephony.externalCall.finish',{'CALL_ID':action['call_id'],'USER_ID':uid,'DURATION':int(data.get('talkDuration',data.get('totalCallDuration',0))),'STATUS_CODE':'200' if outcome=='Answered' else '304'})
                    if action['department']!=4 and action['entity_id'] and not action['activity_id'] and action['state']!='resolved':
                        activity = await self.effect(row['ref'],'todo','crm.activity.todo.add',{'ownerTypeId':3 if action['entity_type']=='contact' else 4,'ownerId':action['entity_id'],'deadline':(datetime.now(timezone.utc)+timedelta(minutes=15)).isoformat(),'title':'Record '+LABELS[option]+' call outcome','description':'Open ESCS Call Actions to confirm the caller, record the enquiry and resolve or schedule follow-up. Call reference: '+row['ref'],'responsibleId':uid,'pingOffsets':[0]})
                        activity_id=activity.get('id') if isinstance(activity,dict) else activity
                        db.execute('UPDATE sbf_call_actions SET activity_id=? WHERE ref=?',(activity_id,row['ref']))
                        db.commit()
            await self.publish_outcomes()
            self.last_error=None

    async def publish_outcomes(self):
        rows=self.db().execute('SELECT * FROM sbf_action_audit WHERE published=0 ORDER BY id LIMIT 20').fetchall()
        for audit in rows:
            action=self.db().execute('SELECT * FROM sbf_call_actions WHERE ref=?',(audit['ref'],)).fetchone()
            if action['department']==4:
                # HR summaries stay in the restricted app worklist. Do not copy
                # them into general CRM timelines whose HR ACL is unverified.
                self.db().execute('UPDATE sbf_action_audit SET published=1 WHERE id=?',(audit['id'],));self.db().commit()
                continue
            if not action['entity_id']:
                continue
            fields=json.loads(audit['value'])
            if action['entity_type']=='contact':
                contact=await self.api('crm.contact.get',{'id':action['entity_id']})
                if isinstance(contact,dict) and contact.get('ORIGINATOR_ID')=='SwitchboardFREE' and str(contact.get('NAME','')).startswith('Telephone caller '):
                    await self.effect(audit['ref'],'verify-name:'+str(audit['id']),'crm.contact.update',{'id':action['entity_id'],'fields':{'NAME':fields['caller_name']}})
            summary='\n'.join(['ESCS call outcome recorded by Bitrix user '+str(audit['user_id']),
                'Caller: '+fields['caller_name'],'Enquiry: '+fields['enquiry'],
                'Outcome: '+fields['outcome'],'Callback: '+fields['callback_at'],
                'Call reference: '+audit['ref']]).replace('[','(').replace(']',')')
            await self.effect(audit['ref'],'outcome:'+str(audit['id']),'crm.timeline.comment.add',{'fields':{'ENTITY_TYPE':action['entity_type'],'ENTITY_ID':action['entity_id'],'COMMENT':summary}})
            if action['activity_id'] and fields['outcome']=='resolved':
                await self.effect(audit['ref'],'complete:'+str(audit['id']),'crm.activity.update',{'id':action['activity_id'],'fields':{'COMPLETED':'Y'}})
            self.db().execute('UPDATE sbf_action_audit SET published=1 WHERE id=?',(audit['id'],));self.db().commit()

    async def effect(self, ref, kind, method, params):
        # Share the durable intent ledger with the original bridge.
        db=self.db()
        kind='popup:'+kind
        prior=db.execute('SELECT * FROM effects WHERE ref=? AND kind=?',(ref,kind)).fetchone()
        if prior:
            if prior['state']=='done':
                return json.loads(prior['result'])
            raise RuntimeError('Uncertain call action requires review')
        db.execute('INSERT INTO effects VALUES(?,?,?,?)',(ref,kind,'started',None));db.commit()
        result=await self.api(method,params)
        db.execute('UPDATE effects SET state=?,result=? WHERE ref=? AND kind=?',('done',json.dumps(result),ref,kind));db.commit()
        return result

    async def loop(self):
        while True:
            try:
                await self.flush()
            except Exception:
                self.last_error='Call pop-up setup, permissions or delivery requires review'
            await asyncio.sleep(3)

    def status(self):
        installed=self.auth()
        return {'enabled':self.enabled,'installed':bool(installed),'renewal_configured':bool(os.getenv('SWITCHBOARD_APP_CLIENT_SECRET')),
                'routing_configured':bool(self.menu_field or self.answerers),'last_error':self.last_error,
                'outstanding':self.db().execute("SELECT COUNT(*) FROM sbf_call_actions WHERE state!='resolved'").fetchone()[0]}

    def issue_session(self, user_id):
        self.require_secret()
        return self.serializer.dumps({'user_id':user_id})

    def user_session(self, token):
        self.require_secret()
        try:
            return int(self.serializer.loads(token,max_age=1800)['user_id'])
        except (BadSignature,SignatureExpired,KeyError,ValueError,TypeError):
            raise HTTPException(401,'Open this page from Bitrix24 again')

    async def record_outcome(self, user_id, ref, fields):
        async with self.lock:
            row=self.db().execute('SELECT * FROM sbf_call_actions WHERE ref=?',(ref,)).fetchone()
            if row is None or user_id not in {1,row['user_id']}:
                raise HTTPException(403,'This call belongs to another department')
            name=str(fields.get('caller_name','')).strip()
            enquiry=str(fields.get('enquiry','')).strip()
            outcome=fields.get('outcome')
            callback=fields.get('callback_at','')
            if not 1<=len(name)<=120 or not 3<=len(enquiry)<=2000 or outcome not in {'resolved','callback','transferred'}:
                raise HTTPException(400,'Caller name, enquiry summary and outcome are required')
            if outcome=='callback':
                try:
                    dt=datetime.fromisoformat(callback)
                    if dt.tzinfo is None or dt<=datetime.now(timezone.utc):
                        raise ValueError()
                except (ValueError,TypeError):
                    raise HTTPException(400,'Use a future callback date with timezone')
            if row['state']=='resolved':
                raise HTTPException(409,'This call outcome is already recorded')
            value={'caller_name':name,'enquiry':enquiry,'outcome':outcome,'callback_at':callback}
            self.db().execute('INSERT INTO sbf_action_audit(ref,user_id,value,created) VALUES(?,?,?,?)',(ref,user_id,json.dumps(value),time.time()))
            self.db().execute('UPDATE sbf_call_actions SET state=?,caller_name=?,enquiry=?,outcome=?,callback_at=? WHERE ref=?',('resolved' if outcome=='resolved' else 'followup',name,enquiry,outcome,callback,ref))
            self.db().commit()
            # The local outstanding state is authoritative; dismissing a native
            # card or completing a CRM reminder does not close this worklist.
            return {'recorded':True,'state':'resolved' if outcome=='resolved' else 'followup'}


async def request_data(request):
    body=await request.body()
    if len(body)>65536:
        raise HTTPException(413,'Request too large')
    if 'application/json' in request.headers.get('content-type',''):
        try:
            value=json.loads(body)
        except ValueError:
            raise HTTPException(400,'Invalid request')
        if not isinstance(value,dict):
            raise HTTPException(400,'Invalid request')
        return value
    return dict(await request.form())


def build_app_router(service):
    router=APIRouter(prefix='/switchboard/app')

    @router.get('/health')
    async def health():
        return {'installed':bool(service.auth()),'enabled':service.enabled,'routing_configured':bool(service.menu_field or service.answerers)}

    @router.get('/admin-status')
    async def admin_status(request:Request):
        if not secrets_equal(request.headers.get('x-sbf-app-key',''),service.app_secret):
            raise HTTPException(401,'Unauthorized')
        return service.status()

    @router.post('/install/{install_key}')
    async def install(request:Request,install_key:str):
        if not secrets_equal(install_key,service.install_key()):
            raise HTTPException(401,'Unauthorized installation')
        await service.install(await request_data(request))
        await service.bind_widget()
        # Interface application installation must be completed inside Bitrix.
        return HTMLResponse('<!doctype html><html><body>ESCS Call Actions installed.<script src="https://api.bitrix24.com/api/v1/"></script><script>BX24.init(function(){BX24.installFinish();});</script></body></html>',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})

    @router.post('/frame')
    async def frame(request:Request):
        data=await request_data(request)
        saved=service.auth()
        if not saved:
            return page('<p>Application installation is pending. No call pop-ups are enabled.</p>')
        if not secrets_equal(str(data.get('APPLICATION_TOKEN','')),saved['application_token']):
            raise HTTPException(401,'Open this application inside ESCS Bitrix24')
        token=data.get('AUTH_ID','')
        profile=await service.raw_api(token,'profile',{})
        info=await service.raw_api(token,'app.info',{})
        if info.get('CODE')!=saved['client_id']:
            raise HTTPException(403,'Wrong application')
        uid=int(profile['ID'])
        session=service.issue_session(uid)
        options=data.get('PLACEMENT_OPTIONS','{}')
        try:
            options=json.loads(options) if isinstance(options,str) else options
        except ValueError:
            raise HTTPException(400,'Invalid call-card context')
        context_call_id=options.get('CALL_ID') if isinstance(options,dict) else None
        if data.get('PLACEMENT')=='CALL_CARD' and context_call_id:
            rows=service.db().execute("SELECT * FROM sbf_call_actions WHERE call_id=? AND (user_id=? OR ?=1)",(str(context_call_id),uid,uid)).fetchall()
            if not rows:
                raise HTTPException(403,'This call belongs to another department')
        else:
            rows=service.db().execute("SELECT * FROM sbf_call_actions WHERE state!='resolved' AND (user_id=? OR ?=1) ORDER BY created DESC LIMIT 50",(uid,uid)).fetchall()
        content='<p>Record a caller name, enquiry summary and outcome for each call. Callbacks and transfers remain outstanding until resolved.</p>'
        for row in rows:
            esc=html.escape
            if row['state']=='resolved':
                content+='<article>This call outcome has already been recorded.</article>'
                continue
            call=service.db().execute('SELECT item,payload FROM calls WHERE ref=?',(row['ref'],)).fetchone()
            payload=json.loads(call['payload']) if call else {}
            number=phone(payload.get('callersNumber') if call and call['item']=='inbound_call' else payload.get('numberDialled')) or 'Number unavailable'
            entity_link=f'<p><a target="_blank" rel="noopener" href="{PORTAL}/crm/{esc(row["entity_type"])}/details/{int(row["entity_id"])}/">Open matching {esc(row["entity_type"])}</a></p>' if row['entity_id'] else '<p>Caller identity needs verification.</p>'
            content+=f'<form method="post" action="{BASE}/switchboard/app/outcome"><h2>{esc(LABELS[row["department"]])}</h2><p>Caller number: {esc(number)}</p>{entity_link}<p>Call reference: {esc(row["ref"])}</p><input type="hidden" name="session" value="{esc(session)}"><input type="hidden" name="ref" value="{esc(row["ref"])}"><label>Confirmed caller name<input name="caller_name" required maxlength="120" value="{esc(row["caller_name"] or "")}"></label><label>Enquiry summary<textarea name="enquiry" required minlength="3" maxlength="2000">{esc(row["enquiry"] or "")}</textarea></label><label>Outcome<select name="outcome" required><option value="">Select outcome</option><option value="resolved">Resolved</option><option value="callback">Callback required</option><option value="transferred">Transferred — follow-up required</option></select></label><label>Callback date/time (with timezone)<input name="callback_at" placeholder="2026-10-13T14:30:00+01:00"></label><button>Record outcome</button></form>'
        if not rows:
            content+='<article>No outstanding call actions for your account.</article>'
        return page(content)

    @router.get('/frame')
    async def frame_without_auth():
        return page('<p>Open ESCS Call Actions from inside Bitrix24. This page requires your Bitrix24 identity.</p>')

    @router.post('/outcome')
    async def outcome(request:Request):
        data=await request_data(request)
        uid=service.user_session(data.get('session',''))
        result=await service.record_outcome(uid,data.get('ref',''),data)
        return page('<article>Outcome recorded. '+('This call is resolved.' if result['state']=='resolved' else 'Follow-up remains outstanding.')+' Reopen ESCS Call Actions in Bitrix24 to view the worklist.</article>')

    return router


def secrets_equal(left,right):
    return bool(left and right) and hmac.compare_digest(left,right)
