"""Owner-only live review queue. No mutations or permanent credential export."""
import os
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
        return JSONResponse({'asOf':datetime.now(timezone.utc).isoformat(),'source':'Live ESCS recruitment workflow ledger',
                             'scope':'Review queue only; not the full applicant register or inbox reconciliation',
                             'count':total,'truncated':total>100,'reviews':result,'readOnly':True},headers=headers)
    return router
