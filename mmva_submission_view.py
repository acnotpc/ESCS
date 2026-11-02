"""Fresh, authenticated course-scoped submission views without Jotform login."""
import html,json,re,sqlite3,time
from html.parser import HTMLParser
import httpx
from fastapi import HTTPException
from mmva_completion import SPECS,value

class Plain(HTMLParser):
    def __init__(self):super().__init__();self.parts=[]
    def handle_data(self,data):self.parts.append(data)
def plain(value):
    p=Plain();p.feed(str(value));return html.unescape(' '.join(p.parts)).strip()

def display(value,depth=0):
    if depth>12:return '[Nested answer could not be displayed]'
    if value is None:return ''
    if isinstance(value,bool):return 'Yes' if value else 'No'
    if isinstance(value,str):
        if value.lstrip().startswith(('[','{')):
            try:return display(json.loads(value),depth+1)
            except (ValueError,TypeError):pass
        return value.strip()
    if isinstance(value,list):return '\n'.join(f'{i+1}: {text}' for i,v in enumerate(value) if (text:=display(v,depth+1)))
    if isinstance(value,dict):return '\n'.join(f'{plain(k)}: {text}' for k,v in value.items() if (text:=display(v,depth+1)))
    return str(value)

def project_details(raw,form,course):
    if str(raw.get('form_id'))!=SPECS[form][0] or str(raw.get('status','')).upper()!='ACTIVE':raise HTTPException(404,'This submission is not available.')
    answers=raw.get('answers')
    if not isinstance(answers,dict):raise HTTPException(503,'The submission answers could not be checked.')
    if str(value(answers,SPECS[form][1]) or '').strip()!=course:
        raise HTTPException(403,'This submission is not confirmed for the selected course. Ask an administrator to correct its Course Number before viewing it here.')
    rows=[];omitted=0
    for q in sorted(answers.values(),key=lambda a:int(a.get('order',0)) if isinstance(a,dict) and str(a.get('order',0)).isdigit() else 0):
        if not isinstance(q,dict) or 'answer' not in q:continue
        kind=str(q.get('type',''));label=plain(q.get('text') or 'Answer')
        if kind in {'control_head','control_text','control_button','control_captcha','control_hidden','control_pagebreak'}:continue
        answer=display(q['answer'])
        if not answer:continue
        if kind in {'control_signature','control_fileupload','control_image'} or 'signature' in label.casefold():
            omitted+=1;rows.append({'question':label,'answer':'Recorded in the original submission. Signature images and attachments are not displayed in this viewer.'});continue
        if answer.lstrip().startswith('data:image/') or len(answer)>50000:
            omitted+=1;rows.append({'question':label,'answer':'This answer is too large or contains an image. Administrator review is required.'});continue
        rows.append({'question':label,'answer':answer})
    return {'submission':str(raw['id']),'form':form,'title':SPECS[form][4],'course':course,
            'submittedAt':str(raw.get('created_at',''))[:19],'answers':rows,'omitted':omitted}

class SubmissionViewer:
    def __init__(self,key,transport=None):self.key,self.transport=key,transport
    async def read(self,form,sid,course):
        if not isinstance(form,str) or form not in SPECS or not re.fullmatch(r'[0-9]{1,30}',str(sid)):
            raise HTTPException(400,'Choose a valid course submission.')
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
                response=await client.get('https://eu-api.jotform.com/submission/'+str(sid),headers={'APIKEY':self.key})
            if response.status_code!=200 or len(response.content)>2_000_000:raise ValueError()
            payload=response.json();raw=payload.get('content')
            if payload.get('responseCode')!=200 or not isinstance(raw,dict) or str(raw.get('id'))!=str(sid):raise ValueError()
        except (httpx.HTTPError,ValueError,TypeError,AttributeError):raise HTTPException(503,'The submission could not be retrieved. Try again shortly.')
        return project_details(raw,form,course)

def record_access(path,course,form,sid,actor):
    with sqlite3.connect(path,timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS mmva_submission_access (course TEXT,form TEXT,submission TEXT,actor TEXT,viewed_at REAL)')
        db.execute('INSERT INTO mmva_submission_access VALUES (?,?,?,?,?)',(course,form,sid,actor['email'],time.time()))
