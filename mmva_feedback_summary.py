"""Trainer-only feedback aggregation and durable confirmation of the class roster."""
import hashlib,json,sqlite3
from collections import Counter
from datetime import datetime,timezone
from fastapi import HTTPException

RATINGS={
 '109':'Course objectives explained','110':'Course objectives met','111':'Course length',
 '112':'Would recommend the course','113':'Venue suitability','115':'Room temperature',
 '116':'Confidence managing challenging situations','117':'Felt safe and respected',
 '119':'Reasonable adjustments / accessibility needs met',
 '97':'Preventing distress and reducing restrictive practices','98':'Human rights, dignity and trauma',
 '99':'Learning and accessibility needs','100':'Safety during practical training','101':'Able to raise concerns or stop an activity'}
COMMENTS={'24':'Most beneficial subject areas','25':'Suggested improvements','28':'Additional remarks / unmet training needs',
 '102':'Planned changes in practice','103':'Further support or training needs','114':'Venue comments','118':'Safe and respected comments'}

def selected_answers(answers):
    # Explicit allowlist excludes identity, signatures and health/adjustment detail (q104).
    result={}
    for q in RATINGS|COMMENTS:
        v=answers.get(q,{})
        v=v.get('answer') if isinstance(v,dict) else None
        if isinstance(v,str) and v.strip():result[q]=v.strip()[:10000]
    return result

def roster_hash(report):
    data={'emails':sorted(p['email'] for p in report['delegates']),
          'booking':report.get('bookingStudentList',[])}
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

class FeedbackRosters:
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path,timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_feedback_roster (course TEXT NOT NULL, version INTEGER NOT NULL, roster_hash TEXT NOT NULL, actor TEXT NOT NULL, recorded_at TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY(course,version))')
    def latest(self,course):
        with sqlite3.connect(self.path,timeout=10) as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM mmva_feedback_roster WHERE course=? ORDER BY version DESC LIMIT 1',(course,)).fetchone()
        return dict(row) if row else None
    def confirm(self,report,actor,version,note=''):
        if not report['delegates'] or report['sourceErrors']:raise HTTPException(409,'Complete the source checks and class list before confirming.')
        if not isinstance(note,str) or len(note)>1000:raise HTTPException(400,'Use a reason of up to 1,000 characters.')
        if report.get('bookingStudentList') and len(report['bookingStudentList'])!=len(report['delegates']) and not note.strip():
            raise HTTPException(400,'Explain the difference between the booking names and actual delegate count (for example, a no-show).')
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT MAX(version) FROM mmva_feedback_roster WHERE course=?',(report['course']['id'],)).fetchone()[0] or 0
            if type(version)!=int or version!=old:raise HTTPException(409,'The class confirmation changed. Refresh before confirming again.')
            db.execute('INSERT INTO mmva_feedback_roster VALUES (?,?,?,?,?,?)',(report['course']['id'],old+1,roster_hash(report),actor['email'],datetime.now(timezone.utc).isoformat(),note.strip()))
        return old+1

def feedback_status(report,confirmation):
    people=report['delegates'];received=sum(bool(p['forms']['feedback']['entries']) for p in people)
    confirmed=bool(confirmation and confirmation['roster_hash']==roster_hash(report))
    unmatched=any(u['form']=='Candidate feedback' for u in report['unmatched'])
    identity=any('Different names use this email; review identity' in p['issues'] for p in people)
    if report['sourceErrors']:reason='Source checks are incomplete. Refresh before reviewing feedback.'
    elif not people:reason='No delegates found yet. Check the expected class list.'
    elif not confirmed:reason='Confirm that the delegate list includes everyone expected to provide feedback.'
    elif identity or unmatched:reason='Resolve the feedback matching or delegate identity issues before viewing the summary.'
    elif received<len(people):reason=f'Waiting for {len(people)-received} delegate feedback submission(s).'
    else:reason='All expected candidate feedback has been received.'
    ready=bool(people and confirmed and not report['sourceErrors'] and not unmatched and not identity and received==len(people))
    return {'ready':ready,'confirmed':confirmed,'version':confirmation['version'] if confirmation else 0,
            'expected':len(people),'received':received,'reason':reason,
            'confirmedAt':confirmation['recorded_at'] if confirmed else None}

def aggregate(rows,course):
    latest={};duplicates=0
    for row in rows:
        if row['course']!=course or not row['email']:continue
        old=latest.get(row['email'])
        if old:duplicates+=1
        if not old or (row['created'],int(row['id']))>(old['created'],int(old['id'])):latest[row['email']]=row
    rows=list(latest.values());questions=[];comments=[]
    for q,label in RATINGS.items():
        counts=Counter(r.get('feedback',{}).get(q) for r in rows);counts.pop(None,None)
        if counts:questions.append({'label':label,'answered':sum(counts.values()),'counts':dict(sorted(counts.items()))})
    for q,label in COMMENTS.items():
        values=sorted(r['feedback'][q] for r in rows if q in r.get('feedback',{}))
        if values:comments.append({'label':label,'comments':values})
    return {'responses':len(rows),'duplicates':duplicates,'ratings':questions,'comments':comments}
