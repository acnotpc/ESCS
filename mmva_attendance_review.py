"""Audited trainer attendance evidence; never rewrites delegate scans."""
import hashlib,json,sqlite3,time
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from mmva_attendance_stages import LABELS
from mmva_duplicates import DuplicateLinks,canonical

def revision(email,day,step,original):
    return hashlib.sha256(json.dumps([email,day,step,{'time':original.get('time')} if original else None],sort_keys=True).encode()).hexdigest()

class AttendanceReviews:
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path,timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_attendance_review (course TEXT,email TEXT,day TEXT,step INTEGER,version INTEGER,source_revision TEXT,state TEXT,actual_time REAL,reason TEXT,actor TEXT,actor_name TEXT,recorded_at REAL,PRIMARY KEY(course,email,day,step,version))')
    def history(self,course):
        links=DuplicateLinks(self.path).links(course)
        with sqlite3.connect(self.path,timeout=10) as db:
            db.row_factory=sqlite3.Row
            result={}
            for row in db.execute('SELECT * FROM mmva_attendance_review WHERE course=? ORDER BY recorded_at,version',(course,)):
                r=dict(row);result.setdefault((canonical(r['email'],links),r['day'],r['step']),[]).append(r)
            return result
    def save(self,course,card,state,actual,reason,actor,version,now=None):
        now=time.time() if now is None else now
        if not isinstance(state,str) or state not in {'verified','absent','unverified'}:raise HTTPException(400,'Choose an attendance outcome.')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:raise HTTPException(400,'Record the evidence or reason (up to 2,000 characters).')
        timestamp=None
        if actual:
            if state!='verified' or not isinstance(actual,str):raise HTTPException(400,'Only provide an actual time for verified attendance.')
            try:
                stamp=datetime.strptime(card['day']+' '+actual,'%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Europe/London'))
                timestamp=stamp.timestamp()
                if timestamp>now:raise ValueError()
            except (ValueError,TypeError):raise HTTPException(400,'Enter a valid actual time in London time, not a future time.')
        if timestamp is not None:
            if card.get('previousTime') is not None and timestamp//60<card['previousTime']//60 or card.get('nextTime') is not None and timestamp//60>card['nextTime']//60:
                raise HTTPException(400,'The actual time conflicts with another recorded attendance point. Review the neighbouring points first.')
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT MAX(version) FROM mmva_attendance_review WHERE course=? AND email=? AND day=? AND step=?',(course,card['email'],card['day'],card['step'])).fetchone()[0] or 0
            if type(version)!=int or version!=old:raise HTTPException(409,'Another trainer updated this attendance review. Refresh before saving.')
            db.execute('INSERT INTO mmva_attendance_review VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(course,card['email'],card['day'],card['step'],old+1,card['revision'],state,timestamp,reason.strip(),actor['email'],actor['name'],now))

def enrich_attendance(report,path,today):
    course=report['course']['id'];store=AttendanceReviews(path);history=store.history(course)
    days={d['day'] for p in report['delegates'] for d in p['attendance']}
    started={day:any(any(d['steps']) for p in report['delegates'] for d in p['attendance'] if d['day']==day) for day in days}
    highest_by_day={day:max([i for p in report['delegates'] for d in p['attendance'] if d['day']==day for i,step in enumerate(d['steps']) if step]+[-1]) for day in days}
    for (email,day,step),records in history.items():
        if day in highest_by_day and records[-1]['state']=='verified':highest_by_day[day]=max(highest_by_day[day],step)
    cards=[]
    for p in report['delegates']:
        for d in p['attendance']:
            # A completed past day requires all points; today's opened points are due.
            highest=highest_by_day[d['day']]
            d['due']=[bool(started[d['day']] and d['day']<=today and (d['day']<today or i<=highest)) for i in range(4)]
            original=list(d['steps'])
            timeline=[]
            for i,raw in enumerate(original):
                records=history.get((p['email'],d['day'],i),[]);last=records[-1] if records else None
                if last and last['source_revision']==revision(p['email'],d['day'],i,raw):timeline.append(last['actual_time'] if last['state']=='verified' else None)
                else:timeline.append(raw['time'] if raw else None)
            for step,raw in enumerate(original):
                records=history.get((p['email'],d['day'],step),[]);last=records[-1] if records else None
                due=d['due'][step]
                if d['day']>today or not started[d['day']]:continue
                card={'due':due,'email':p['email'],'name':p['name'],'day':d['day'],'step':step,'label':LABELS[step],
                    'original':raw,'revision':revision(p['email'],d['day'],step,raw),'version':max([r['version'] for r in records if r['email']==p['email']]+[0]),'history':records,
                    'previousTime':next((t for t in reversed(timeline[:step]) if t is not None),None),
                    'nextTime':next((t for t in timeline[step+1:] if t is not None),None)}
                if last and last['source_revision']!=card['revision']:
                    card['state']='unverified'
                    p['issues'].append('Attendance records or identity changed after trainer review; review again')
                elif last:
                    card['state']=last['state']
                    if last['state']=='verified':d['steps'][step]={'time':last['actual_time'],'sync':'trainer_verified','manual':True,'trainer':last['actor_name'],'reviewedAt':last['recorded_at']}
                    else:d['steps'][step]=None
                else:card['state']='scanned' if raw else 'missing'
                card['needsAction']=due and card['state'] in {'missing','unverified'}
                cards.append(card)
        p['outstanding']=[x for x in p['outstanding'] if x!='Attendance']
        if any(d['day']<=today and not started[d['day']] for d in p['attendance']) or any(due and not d['steps'][i] for d in p['attendance'] for i,due in enumerate(d['due'])):p['outstanding'].append('Attendance')
        p['complete']=not p['outstanding'] and not p['issues'] and today>=report['course']['end'] and all(all(d['steps']) for d in p['attendance'])
    report['attendanceReviews']=cards;report['attendanceReviewOutstanding']=sum(c['needsAction'] for c in cards)
    report['attendanceStartedDays']=sorted(day for day in days if started[day])
    return report
