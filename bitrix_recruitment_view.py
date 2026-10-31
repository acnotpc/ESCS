"""Owner-only live review queue. No mutations or permanent credential export."""
import os
import asyncio
import json
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

async def verify_owner(auth, transport=None):
    if not auth or len(auth)>4096:
        return False
    try:
        async with httpx.AsyncClient(timeout=10,follow_redirects=False,transport=transport) as client:
            app=await client.post('https://escs.bitrix24.com/rest/app.info.json',data={'auth':auth})
            user=await client.post('https://escs.bitrix24.com/rest/user.current.json',data={'auth':auth})
        a,u=app.json(),user.json()
        return (app.is_success and user.is_success and not a.get('error') and not u.get('error')
                and str(a.get('result',{}).get('ID'))=='106'
                and str(u.get('result',{}).get('ID'))=='1')
    except (httpx.HTTPError,ValueError,TypeError,AttributeError):
        return False

def build_bitrix_recruitment_view(worker,transport=None):
    router=APIRouter()
    @router.post('/bitrix/recruitment/reviews')
    async def reviews(request:Request):
        headers={'Cache-Control':'no-store'}
        if request.headers.get('content-length','').isdigit() and int(request.headers['content-length'])>32768:
            return JSONResponse({'error':'Request too large'},status_code=413,headers=headers)
        body=await request.body()
        if len(body)>32768:
            return JSONResponse({'error':'Request too large'},status_code=413,headers=headers)
        from urllib.parse import parse_qs
        form=parse_qs(body.decode('utf-8',errors='replace'))
        if not await verify_owner(form.get('AUTH_ID',[''])[0],transport):
            return JSONResponse({'error':'Access denied'},status_code=403,headers=headers)
        if os.getenv('BITRIX_RECRUITMENT_VIEW_ENABLED','false').lower()!='true':
            return JSONResponse({'error':'Live review view disabled'},status_code=503,headers=headers)
        rows=list(worker.ledger.db.execute('SELECT id,reason FROM checklist_review ORDER BY id LIMIT 101'))
        total=worker.ledger.db.execute('SELECT count(*) FROM checklist_review').fetchone()[0]
        result=[]
        for row in rows[:100]:
            # Avoid free-text reasons and assessment details in this first live view.
            candidate=str(row['id'])
            if not candidate.isdigit():
                continue
            c=worker.ledger.db.execute('SELECT state,sends FROM checklist_candidate WHERE id=?',(candidate,)).fetchone()
            result.append({'itemId':candidate,'state':c['state'] if c else 'not_enrolled',
                           'confirmedEmails':c['sends'] if c else 0,
                           'sourceUrl':'https://exclusive-gang.monday.com/boards/424237712/pulses/'+candidate})
        extra=await live_sources(worker) if form.get('dashboard')==['1'] and os.getenv('BITRIX_RECRUITMENT_FULL_VIEW_ENABLED','false').lower()=='true' else {}
        return JSONResponse({**extra,'asOf':datetime.now(timezone.utc).isoformat(),'source':'Live ESCS recruitment workflow ledger',
                             'scope':'Review queue only; not the full applicant register or inbox reconciliation',
                             'count':total,'truncated':total>100,'reviews':result,'readOnly':True},headers=headers)
    return router


async def applicant_register(worker):
    from checklist_workflow import MondayFollowup, REQUEST, RECEIVED, LABELS
    client=MondayFollowup()
    fields='id name state group {id title} column_values(ids:["status53","'+REQUEST+'","'+RECEIVED+'"]) {id text value}'
    query='query {boards(ids:[424237712]){id name items_page(limit:100){cursor items{'+fields+'}}}}'
    boards=(await client.call(query))['boards']
    if len(boards)!=1 or str(boards[0]['id'])!='424237712':
        raise RuntimeError('Board identity mismatch')
    page=boards[0]['items_page'];rows=[];seen=set();cursors=set()
    for _ in range(50):
        for item in page['items']:
            iid=str(item['id'])
            if not iid.isdigit() or iid in seen:raise RuntimeError('Ambiguous board page')
            seen.add(iid)
            if item.get('state')!='active':continue
            cols={c['id']:c for c in item['column_values']}
            requested=json.loads(cols.get(REQUEST,{}).get('value') or '{}').get('ids',[])
            received=json.loads(cols.get(RECEIVED,{}).get('value') or '{}').get('ids',[])
            missing=[LABELS[i][1] if i in LABELS else 'Unmapped requirement '+str(i) for i in requested if i not in received]
            rows.append({'id':iid,'name':str(item.get('name','')),'group':str(item.get('group',{}).get('title','')),
                         'stage':cols.get('status53',{}).get('text','Unknown'),'requested':cols.get(REQUEST,{}).get('text',''),
                         'received':cols.get(RECEIVED,{}).get('text',''),'outstanding':missing,
                         'sourceUrl':'https://exclusive-gang.monday.com/boards/424237712/pulses/'+iid})
        cursor=page.get('cursor')
        if not cursor:return {'status':'connected','board':boards[0]['name'],'count':len(rows),'complete':True,'items':rows,
                              'interpretation':'Staff-selected requested and received fields; receipt is not professional verification.'}
        if cursor in cursors:raise RuntimeError('Repeated cursor')
        cursors.add(cursor)
        page=(await client.call('query($cursor:String!){next_items_page(limit:100,cursor:$cursor){cursor items{'+fields+'}}}',{'cursor':cursor}))['next_items_page']
    raise RuntimeError('Applicant scan incomplete')

async def form_connections(worker):
    from recruitment_checklist_access import ChecklistRead
    source=ChecklistRead(os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY',''))
    # Verify the live checklist schemas; do not download private form answers.
    forms=await source.inspect_schemas()
    keys={r['name'] for r in worker.ledger.db.execute("PRAGMA table_info(auto_source_state)")}
    state=[]
    if keys:
        state=[dict(r) for r in worker.ledger.db.execute('SELECT kind,ready,last_success FROM auto_source_state ORDER BY kind')]
    return {'status':'connected','forms':forms,'receiptIndex':state,
            'scope':'Live checklist schemas and existing receipt-index checkpoints; not a complete application submission reconciliation.'}

async def mailbox_connections():
    from hr_mail_readiness import HRMailReadiness
    from recruitment_mail import RecruitmentMail
    from datetime import timedelta
    metadata=HRMailReadiness()
    source=RecruitmentMail(os.getenv('BITRIX_RECRUITMENT_MAIL_REST_URL',''),1)
    now=datetime.now(timezone.utc)
    async def one(email):
        try:
            rows=(await metadata.call('mail.mailbox.list',{'email':email,'pagination':{'page':1,'limit':100}})).get('items')
            if not isinstance(rows,list) or len(rows)>=100:raise RuntimeError('Incomplete mailbox list')
            exact=[r for r in rows if str(r.get('email','')).lower()==email]
            if len(exact)!=1:return {'email':email,'status':'connection_required','reason':'Exact mailbox not uniquely available through the current Bitrix24 connection.'}
            mid=exact[0]['id']
            verified=(await metadata.call('mail.mailbox.get',{'id':mid,'select':['id','email']})).get('item',{})
            if verified.get('id')!=mid or str(verified.get('email','')).lower()!=email:raise RuntimeError('Mailbox mismatch')
            result=await source._call('mail.message.list',{'mailboxId':mid,'dateFrom':(now-timedelta(days=7)).isoformat(),'dateTo':now.isoformat(),'pagination':{'page':1,'limit':25}})
            messages=result.get('items')
            if not isinstance(messages,list):raise RuntimeError('Invalid message result')
            out=[]
            for row in messages:
                if row.get('mailboxId')!=mid or str(row.get('mailboxEmail','')).lower()!=email:raise RuntimeError('Message scope mismatch')
                out.append({'id':row.get('id'),'subject':str(row.get('subject',''))[:300],'date':row.get('date')})
            return {'email':email,'status':'connected','mailboxId':mid,'messages':out,'scope':'First 25 messages returned in the past 7 days; inbox and sent coverage not fully reconciled.','hasMore':len(messages)==25}
        except Exception:
            return {'email':email,'status':'unavailable','reason':'Mailbox read could not be verified.'}
    return await asyncio.gather(one('jobs@securecareservices.co.uk'),one('info@exclusive-recruitment.co.uk'))

async def live_sources(worker):
    async def guarded(fn):
        try:return await asyncio.wait_for(fn(),timeout=50)
        except Exception:return {'status':'unavailable','reason':'Source read could not be verified; retained data must not be treated as current.'}
    board,forms,mail=await asyncio.gather(guarded(lambda:applicant_register(worker)),guarded(lambda:form_connections(worker)),guarded(mailbox_connections))
    return {'register':board,'jotform':forms,'mailboxes':mail,'fullyReconciled':False}
