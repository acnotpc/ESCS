"""Read-only, course-scoped completion metadata. Never returns health answers."""
import asyncio
import re
from datetime import date,datetime,timedelta
from mmva_health_followup import declaration_flag
import httpx

# Stable question mappings; retain identity/course metadata and minimal health flags only.
SPECS={
 'registration':('232684816706363','63','11','14','Registration'),
 'health':('232685513399366','81','11','14','Pre-course health'),
 'fitness':('240482908768368','78','11','14','Post-training fitness'),
 'knowledge':('240691111369353','4','71','3','Knowledge assessment'),
 'feedback':('231786383440359','107','95','34','Candidate feedback'),
 'venue':('231735606698366','61',None,None,'Venue risk assessment'),
 'incident':('231734935383361','37',None,None,'Accident / incident reports'),
}
REQUIRED=('registration','health','knowledge','fitness','feedback')


def value(answers,qid):
    answer=answers.get(qid,{}) if qid else {}
    return answer.get('answer','') if isinstance(answer,dict) else ''


def clean_name(raw):
    if isinstance(raw,dict):raw=' '.join(str(raw.get(k,'')).strip() for k in ('first','middle','last'))
    return ' '.join(str(raw or '').split())[:200]


def project(row,spec,key=''):
    if not isinstance(row,dict) or str(row.get('status','')).upper()!='ACTIVE':return None
    sid=str(row.get('id',''))
    if not sid.isdigit():raise ValueError('Invalid submission id')
    a=row.get('answers')
    if not isinstance(a,dict):raise ValueError('Missing submission answers')
    email=str(value(a,spec[2]) or '').strip().casefold()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254:email=''
    result={'id':sid,'course':str(value(a,spec[1]) or '').strip(),'email':email,
            'name':clean_name(value(a,spec[3])),'created':str(row.get('created_at',''))[:19]}
    if spec==SPECS['health']:result['health']=declaration_flag(a,key)
    return result


class CompletionReader:
    def __init__(self,key,transport=None,page_size=1000,max_pages=20):
        self.key,self.transport,self.page_size,self.max_pages=key,transport,page_size,max_pages

    async def read_form(self,spec):
        rows=[];seen=set();offset=0
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
                for _ in range(self.max_pages):
                    r=await client.get('https://eu-api.jotform.com/form/'+spec[0]+'/submissions',headers={'APIKEY':self.key},params={'offset':offset,'limit':self.page_size,'orderby':'id'})
                    payload=r.json();batch=payload.get('content')
                    if r.status_code!=200 or payload.get('responseCode')!=200 or not isinstance(batch,list):raise ValueError()
                    for raw in batch:
                        sid=str(raw.get('id',''))
                        if sid in seen:raise ValueError('Pagination overlap; refresh required')
                        seen.add(sid)
                        row=project(raw,spec,self.key)
                        if row:rows.append(row)
                    # Respect a server-imposed smaller page size rather than assuming truncation is the end.
                    limit=int(payload.get('resultSet',{}).get('limit',self.page_size))
                    if limit<=0:raise ValueError()
                    if len(batch)<limit:return {'available':True,'rows':rows}
                    offset+=len(batch)
                raise ValueError('Scan limit reached')
        except (httpx.HTTPError,ValueError,TypeError,AttributeError,KeyError):
            return {'available':False,'rows':[]}

    async def read_health_submission(self,sid):
        if not re.fullmatch(r'[0-9]+',str(sid)):return None
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
                response=await client.get('https://eu-api.jotform.com/submission/'+sid,headers={'APIKEY':self.key})
            payload=response.json();raw=payload.get('content')
            if response.status_code!=200 or payload.get('responseCode')!=200 or not isinstance(raw,dict):return None
            if str(raw.get('form_id'))!=SPECS['health'][0] or str(raw.get('id'))!=sid:return None
            return project(raw,SPECS['health'],self.key)
        except (httpx.HTTPError,ValueError,TypeError,AttributeError):return None

    async def read(self):
        gate=asyncio.Semaphore(2)
        async def one(key,spec):
            async with gate:return key,await self.read_form(spec)
        return dict(await asyncio.gather(*(one(k,s) for k,s in SPECS.items())))


def review_link(key,sid):
    # Inbox access still requires the trainer's own Jotform permissions.
    return 'https://eu.jotform.com/inbox/'+SPECS[key][0]+'/'+sid


def report(source,scans,attendance,today):
    course=source['id'];people={};unmatched=[];course_forms={}
    def person(email,name=''):
        p=people.setdefault(email,{'email':email,'name':name or email,'names':set(),'forms':{},'attendance':{},'issues':[]})
        if name:p['names'].add(name.casefold());p['name']=name
        return p
    for row in attendance:
        p=person(row['email'],row['name']);day=p['attendance'].setdefault(row['day'],{})
        if row['step'] is not None:day[row['step']]={'time':row['time'],'sync':row['sync']}
    for key in REQUIRED:
        for row in scans[key]['rows']:
            if row['course']!=course:continue
            entry={'id':row['id'],'created':row['created'],'url':review_link(key,row['id'])}
            if key=='health':entry['courseMatch']=True;entry['health']=row.get('health',{'state':'unknown','reasons':['Declaration answers could not be confirmed'],'revision':''})
            if not row['email']:
                unmatched.append({'form':SPECS[key][4],'reason':'Course matches, but no valid delegate email','name':row['name'],**entry});continue
            p=person(row['email'],row['name']);p['forms'].setdefault(key,[]).append(entry)
    # Uncoded responses never count as completed. Flag only for an already-known email near this course.
    first=(date.fromisoformat(source['start'])-timedelta(days=30)).isoformat()
    last=(date.fromisoformat(source['end'])+timedelta(days=30)).isoformat()
    for key in REQUIRED:
        for row in scans[key]['rows']:
            if not row['course'] and row['email'] in people and first<=row['created'][:10]<=last:
                unmatched.append({'form':SPECS[key][4],'reason':'Possible match: Course Number is blank; verify manually','name':row['name'],'id':row['id'],'created':row['created'],'url':review_link(key,row['id']),**({'health':row['health']} if key=='health' and 'health' in row else {})})
    start,end=date.fromisoformat(source['start']),date.fromisoformat(source['end'])
    if (end-start).days>90:raise ValueError('Course date range too long for attendance review')
    days=[(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)]
    for p in people.values():
        for key in REQUIRED:
            entries=p['forms'].get(key,[])
            p['forms'][key]={'state':'received' if entries else ('missing' if scans[key]['available'] else 'unavailable'),'entries':entries}
            if len(entries)>1:p['issues'].append('Multiple '+SPECS[key][4]+' submissions; review')
        if len(p.pop('names'))>1:p['issues'].append('Different names use this email; review identity')
        p['attendance']=[{'day':day,'future':day>today,'steps':[p['attendance'].get(day,{}).get(i) for i in range(4)]} for day in days]
        if any(step and step['sync']!='sent' for day in p['attendance'] for step in day['steps']):p['issues'].append('Attendance copy awaiting Jotform confirmation')
        p['outstanding']=[SPECS[k][4] for k in REQUIRED if p['forms'][k]['state']!='received']
        if any(not all(d['steps']) for d in p['attendance'] if not d['future']):p['outstanding'].append('Attendance')
        p['complete']=not p['outstanding'] and not p['issues'] and today>=source['end']
    for key in ('venue','incident'):
        entries=[{'id':r['id'],'created':r['created'],'url':review_link(key,r['id'])} for r in scans[key]['rows'] if r['course']==course]
        course_forms[key]={'available':scans[key]['available'],'entries':entries,'required':key=='venue'}
    return {'course':{k:source[k] for k in ('id','title','client','venue','start','end')},
      'delegates':sorted(people.values(),key=lambda p:(p['name'].casefold(),p['email'])),
      'bookingStudentNames':source.get('studentNames',''),'unmatched':unmatched,'courseForms':course_forms,'formLabels':{k:SPECS[k][4] for k in REQUIRED},
      'sourceErrors':[SPECS[k][4] for k,s in scans.items() if not s['available']],
      'checkedAt':datetime.now().astimezone().isoformat(),
      'note':'This list includes delegates found in course-linked forms or protected attendance. Compare it with your expected delegate list: a person with no records cannot appear here. Received means submitted, not passed or approved. Health follow-up flags and trainer action notes are shown; detailed health answers remain in the source form.'}
