"""Versioned follow-up actions and escaped, self-contained evidence snapshots."""
import json,sqlite3,uuid,html
from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from mmva_management_records import https

class FollowupTasks:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as db:db.execute('CREATE TABLE IF NOT EXISTS mmva_management_task(course TEXT,id TEXT,version INTEGER,payload TEXT,actor TEXT,recorded_at TEXT,PRIMARY KEY(course,id,version))')
    def db(self):return sqlite3.connect(self.path,timeout=10)
    def latest(self,course):
        with self.db() as db:rows=db.execute('SELECT t.id,t.version,t.payload,t.actor,t.recorded_at FROM mmva_management_task t WHERE course=? AND t.version=(SELECT MAX(n.version) FROM mmva_management_task n WHERE n.course=t.course AND n.id=t.id) ORDER BY t.recorded_at',(course,)).fetchall()
        return [dict(json.loads(r[2]),id=r[0],version=r[1],actor=r[3],recordedAt=r[4]) for r in rows]
    def history(self,course):
        with self.db() as db:rows=db.execute('SELECT id,version,payload,actor,recorded_at FROM mmva_management_task WHERE course=? ORDER BY recorded_at DESC',(course,)).fetchall()
        return [dict(data=json.loads(r[2]),id=r[0],version=r[1],actor=r[3],recordedAt=r[4]) for r in rows]
    def save(self,course,data,actor,accounts):
        title=data.get('title','');note=data.get('note','');owner=data.get('owner','');status=data.get('status');deadline=data.get('due');version=data.get('version');link=data.get('evidence','');task_id=data.get('id') or str(uuid.uuid4())
        if not isinstance(title,str) or not title.strip() or len(title)>200 or not isinstance(note,str) or len(note)>2000:raise HTTPException(400,'Enter an action title and a note of up to 2,000 characters.')
        if not isinstance(owner,str) or owner not in {a['email'] for a in accounts}:raise HTTPException(400,'Choose an enabled trainer or manager as the action owner.')
        if status not in {'Open','In progress','Blocked','Done'} or type(version) is not int or version<0:raise HTTPException(400,'Choose a valid action status and saved version.')
        if status=='Done' and not note.strip():raise HTTPException(400,'Record the completed action before marking Done.')
        try:
            if date.fromisoformat(deadline).isoformat()!=deadline:raise ValueError()
        except (ValueError,TypeError):raise HTTPException(400,'Enter a valid action due date.')
        if not isinstance(task_id,str) or len(task_id)>80:raise HTTPException(400,'Invalid action identifier.')
        if not isinstance(link,str) or len(link)>2000:raise HTTPException(400,'Enter an evidence link of up to 2,000 characters.')
        if link:https(link)
        payload=dict(title=title.strip(),note=note.strip(),owner=owner,status=status,due=deadline,evidence=link)
        timestamp=datetime.now(timezone.utc).isoformat()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');current=db.execute('SELECT COALESCE(MAX(version),0) FROM mmva_management_task WHERE course=? AND id=?',(course,task_id)).fetchone()[0]
            if version!=current:raise HTTPException(409,'This action changed elsewhere. Reload before saving.')
            db.execute('INSERT INTO mmva_management_task VALUES(?,?,?,?,?,?)',(course,task_id,current+1,json.dumps(payload),actor['email'],timestamp))
        return dict(payload,id=task_id,version=current+1,actor=actor['email'],recordedAt=timestamp)

def evidence_html(source,report,checks,certificates,tasks,policy,actor,created_at):
    e=lambda value:html.escape(str(value if value is not None else 'Unverified'))
    def table(headers,rows):
        return '<table><thead><tr>'+''.join('<th>'+e(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+e(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table>'
    out=['<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MMVA course evidence summary</title><style>body{font:14px system-ui,sans-serif;color:#18374a;max-width:1100px;margin:30px auto;padding:0 20px}h1,h2{color:#076f82}p{line-height:1.6}table{width:100%;border-collapse:collapse;margin:18px 0}td,th{border:1px solid #cad9df;padding:9px;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#edf4f7}.notice{padding:15px;background:#fff2dc}small{color:#526b77}@media print{body{margin:0}tr{break-inside:avoid}h2{break-after:avoid}}</style><h1>MMVA course evidence summary</h1>']
    out.append('<p>Generated '+e(created_at)+' by '+e(actor['name'])+' ('+e(actor['email'])+'). Evidence checked '+e(report.get('checkedAt'))+'.</p>')
    out.append(table(['Booking field','Recorded value'],[(k,source.get(v)) for k,v in [('Course ID','id'),('Client','client'),('Course','title'),('Dates from','start'),('Dates until','end'),('Venue','venue'),('Status','status'),('Allocated trainers','trainer')]]))
    out.append('<p>Source booking: '+e(source['url'])+'</p>')
    if policy['mode']=='third_party':out.append('<div class="notice"><b>Third-party paperwork — staffing only.</b> '+e(policy['note'])+'</div>')
    else:
        out.append('<div class="notice">This snapshot covers course-linked delegates and any manager-recorded expected list. Compare it with the full expected roster; learners with no records appear only when included in that list. Submitted evidence does not establish a pass, medical clearance or certification. Unverified and unavailable records remain unresolved.</div>')
        out.append('<p>Unavailable sources: '+e(', '.join(report.get('sourceErrors',[])) or 'None reported')+' · Unmatched submissions: '+e(len(report.get('unmatched',[])))+'</p>')
        out.append('<p>Manager-recorded expected roster: '+e('revision '+str(report.get('expectedRosterVersion')) if report.get('expectedRosterRecorded') else 'Not recorded')+'</p>')
        out.append('<h2>Delegate records</h2>')
        rows=[]
        for p in report.get('delegates',[]):
            forms='; '.join(key+': '+value['state'] for key,value in p.get('forms',{}).items())
            attendance=[]
            for day in p.get('attendance',[]):
                points=[]
                for step in day.get('steps',[]):
                    if not step:points.append('Missing');continue
                    stamp=step.get('time');points.append(datetime.fromtimestamp(stamp,ZoneInfo('Europe/London')).strftime('%H:%M') if isinstance(stamp,(int,float)) else 'Recorded')
                attendance.append(day['day']+': '+', '.join(points))
            rows.append((p['name'],p['email'],forms,'; '.join(attendance),'; '.join(p.get('outstanding',[])+p.get('issues',[])) or 'No outstanding linked records'))
        out.append(table(['Delegate','Email','Required forms','Attendance points','Outstanding / review'],rows))
    out.append('<h2>Management evidence checks</h2>')
    out.append(table(['Check','State','Evidence','Review note'],[(c['label'],c['state'],c.get('reference',''),c.get('note','')) for c in (checks or {}).get('checks',[])]))
    out.append('<p>Course leader: '+e((checks or {}).get('leader') or 'Unverified')+' · Follow-up: '+e((checks or {}).get('followup') or 'None recorded')+'</p>')
    out.append('<h2>Verified outcome and certificate register</h2>')
    out.append(table(['Learner','Outcome','Assessment evidence','Certificate','Issued','Expires','Certificate link'],[(c['name'],c['outcome'],c.get('assessmentEvidence',''),c.get('number',''),c.get('issued',''),c.get('expires',''),c.get('certificateLink','')) for c in certificates]))
    out.append('<h2>Assigned follow-up actions</h2>')
    out.append(table(['Action','Owner','Due','Status','Notes / evidence'],[(t['title'],t['owner'],t['due'],t['status'],t['note']+' '+t.get('evidence','')) for t in tasks]))
    out.append('<p><small>Dated review snapshot. Source systems and later corrections remain authoritative. Retain and share using MMVA’s normal access controls.</small></p></html>')
    return ''.join(out)

class ExpectedRoster:
    """Manager-recorded expected names/emails; never creates attendance or submissions."""
    def __init__(self,path):
        self.path=str(path)
        with sqlite3.connect(self.path,timeout=10) as db:db.execute('CREATE TABLE IF NOT EXISTS mmva_expected_roster(course TEXT,version INTEGER,payload TEXT,reason TEXT,actor TEXT,recorded_at TEXT,PRIMARY KEY(course,version))')
    def latest(self,course):
        with sqlite3.connect(self.path,timeout=10) as db:
            row=db.execute('SELECT version,payload,reason,actor,recorded_at FROM mmva_expected_roster WHERE course=? ORDER BY version DESC LIMIT 1',(course,)).fetchone()
        return dict(version=row[0],people=json.loads(row[1]),reason=row[2],actor=row[3],recordedAt=row[4]) if row else None
    def save(self,course,data,actor):
        import re
        people=data.get('people');version=data.get('version');reason=data.get('reason','')
        if data.get('confirm') is not True or type(version) is not int or version<0 or not isinstance(people,list) or not 1<=len(people)<=500 or not isinstance(reason,str) or not reason.strip() or len(reason)>2000:raise HTTPException(400,'Confirm the expected class list, saved version and source evidence for these names and emails.')
        clean=[];seen=set()
        for person in people:
            if not isinstance(person,dict):raise HTTPException(400,'Enter each delegate name and email.')
            name=person.get('name');email=person.get('email')
            if not isinstance(name,str) or not name.strip() or len(name)>200 or not isinstance(email,str):raise HTTPException(400,'Enter a full name and valid email for each expected delegate.')
            email=email.strip().casefold()
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254 or email in seen:raise HTTPException(400,'Each expected delegate needs a distinct valid email. Verify shared addresses before proceeding.')
            seen.add(email);clean.append(dict(name=name.strip(),email=email))
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE');current=db.execute('SELECT COALESCE(MAX(version),0) FROM mmva_expected_roster WHERE course=?',(course,)).fetchone()[0]
            if current!=version:raise HTTPException(409,'The expected class list changed. Reload before saving.')
            db.execute('INSERT INTO mmva_expected_roster VALUES(?,?,?,?,?,?)',(course,current+1,json.dumps(clean),reason.strip(),actor['email'],datetime.now(timezone.utc).isoformat()))
        return self.latest(course)
