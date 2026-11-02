"""Minimal declaration flags and append-only trainer follow-up records."""
import hashlib
import hmac
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime,timezone
from fastapi import HTTPException

FIELDS=('80','78','76','51','77','66')
NEGATIVE={'no','none','none relevant','none of the above','no adjustments required','n/a','not applicable'}

def strings(value):
    if isinstance(value,dict):return [t for v in value.values() for t in strings(v)]
    if isinstance(value,list):return [t for v in value for t in strings(v)]
    return [' '.join(str(value or '').split()).casefold()] if str(value or '').strip() else []

def declaration_flag(answers,key):
    values={qid:answers.get(qid,{}).get('answer','') if isinstance(answers.get(qid,{}),dict) else '' for qid in FIELDS}
    reasons=[]
    for qid in FIELDS:
        replies=strings(values[qid])
        if any(reply not in NEGATIVE for reply in replies):
            reason={'80':'Safe participation adjustments need review','78':'Confidential discussion requested','76':'Relevant information declared','51':'Relevant information declared','77':'Support or adjustments information supplied','66':'Additional participation information supplied'}[qid]
            if reason not in reasons:reasons.append(reason)
    current_clear=strings(values['80'])==['no adjustments required'] and strings(values['78'])==['no']
    legacy_clear=not strings(values['80']) and strings(values['78']) in ([],['no']) and any(strings(values[q]) for q in ('76','51'))
    clear=current_clear or legacy_clear
    return {'state':'attention' if reasons else 'clear' if clear else 'unknown',
            'reasons':reasons or ([] if clear else ['Declaration answers could not be confirmed; review the form']),
            'revision':hmac.new(key.encode(),json.dumps(values,sort_keys=True,ensure_ascii=True,default=str).encode(),hashlib.sha256).hexdigest()}

POST_FITNESS_CLEAR='i declare that i am fit from injury or incident at the end of training'
POST_FITNESS_ISSUE='i declare that during training injuries or accidents occurred'
POST_NEGATIVE=NEGATIVE | {'nil','no injuries','no accidents','no injuries or accidents','no incidents','no issues'}

def matrix_strings(value):
    # Jotform encodes each matrix row as JSON even inside a decoded object.
    if isinstance(value,str):
        try:
            decoded=json.loads(value)
            if isinstance(decoded,(list,dict)) or decoded is None:return matrix_strings(decoded)
        except (ValueError,TypeError):pass
    if isinstance(value,dict):return [t for v in value.values() for t in matrix_strings(v)]
    if isinstance(value,list):return [t for v in value for t in matrix_strings(v)]
    return strings(value)

def post_course_flag(answers,key):
    values={qid:answers.get(qid,{}).get('answer','') if isinstance(answers.get(qid,{}),dict) else '' for qid in ('74','76')}
    matrix=values['76']
    if isinstance(matrix,str):
        try:
            decoded=json.loads(matrix)
            if isinstance(decoded,(list,dict)) or decoded is None:matrix=decoded
        except ValueError:pass
    declaration=strings(values['74']);details=matrix_strings(matrix)
    reasons=[]
    if declaration and declaration!=[POST_FITNESS_CLEAR]:reasons.append('Injury or incident declaration needs follow-up')
    if any(t not in POST_NEGATIVE for t in details):reasons.append('Accident or injury information supplied')
    clear=declaration==[POST_FITNESS_CLEAR]
    return {'state':'attention' if reasons else 'clear' if clear else 'unknown',
            'reasons':reasons or ([] if clear else ['Post-training declaration answers could not be confirmed; review the form']),
            'revision':hmac.new(key.encode(),json.dumps({'postCourse':values},sort_keys=True,ensure_ascii=True,default=str).encode(),hashlib.sha256).hexdigest()}

class HealthFollowups:
    def __init__(self,path):
        self.path=path
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mmva_health_note(
              course TEXT NOT NULL,submission TEXT NOT NULL,seq INTEGER NOT NULL,
              declaration_revision TEXT NOT NULL,note TEXT NOT NULL,state TEXT NOT NULL,
              actor_email TEXT NOT NULL,actor_name TEXT NOT NULL,recorded_at REAL NOT NULL,
              PRIMARY KEY(course,submission,seq))''')
    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()
    def save(self,course,submission,revision,note,state,actor,expected_version):
        if not isinstance(note,str) or not note.strip() or len(note)>2000:
            raise HTTPException(400,'Enter the action taken or follow-up needed (up to 2,000 characters).')
        if not isinstance(state,str) or state not in {'open','in_progress','resolved'} or type(expected_version) is not int or expected_version<0:
            raise HTTPException(400,'Choose a valid follow-up status and refresh the record.')
        if not re.fullmatch(r'[0-9]+',course) or not re.fullmatch(r'[0-9]+',submission) or not re.fullmatch(r'[0-9a-f]{64}',revision):
            raise HTTPException(400,'Invalid declaration reference.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            latest=db.execute('SELECT COALESCE(MAX(seq),0) FROM mmva_health_note WHERE course=? AND submission=?',(course,submission)).fetchone()[0]
            if latest!=expected_version:raise HTTPException(409,'Another trainer updated this record. Refresh and review their note before saving.')
            db.execute('INSERT INTO mmva_health_note VALUES(?,?,?,?,?,?,?,?,?)',(course,submission,latest+1,revision,note.strip(),state,actor['email'],actor['name'],time.time()))
        return latest+1
    def notes(self,course):
        with self.db() as db:rows=db.execute('SELECT * FROM mmva_health_note WHERE course=? ORDER BY submission,seq',(course,)).fetchall()
        result={}
        for r in rows:
            result.setdefault(r['submission'],[]).append({'version':r['seq'],'revision':r['declaration_revision'],'note':r['note'],'state':r['state'],'trainer':r['actor_name'],'trainerEmail':r['actor_email'],'time':datetime.fromtimestamp(r['recorded_at'],timezone.utc).isoformat()})
        return result

def enrich(report,notes,form='health'):
    assert form in {'health','fitness'}
    prefix='health' if form=='health' else 'postCourse'
    # Operate on a response copy so note updates never mutate the cached base report.
    cards=[]
    def add(entry,name,email):
        health=entry.get('health',{'state':'unknown','reasons':['Review declaration'],'revision':''})
        history=notes.get(entry['id'],[]);latest=history[-1] if history else None
        current=latest if latest and latest['revision']==health['revision'] else None
        entry['followup']={'version':latest['version'] if latest else 0,'state':current['state'] if current else 'open','history':history,
                           'changed':bool(latest and not current)}
        needs=health['state']!='clear' or bool(latest and not current)
        outstanding=needs and (not current or current['state']!='resolved')
        if needs or history:cards.append({'submission':entry['id'],'name':name or 'Delegate identity needs matching','email':email,'url':entry['url'],'health':health,'followup':entry['followup'],'outstanding':outstanding,'canRecord':entry.get('courseMatch',False)})
        return outstanding
    for person in report['delegates']:
        outstanding=False
        for entry in person['forms'][form]['entries']:
            outstanding=add(entry,person['name'],person['email']) or outstanding
        person[prefix+'FollowupOutstanding']=outstanding
        if outstanding:
            person['complete']=False;person['issues'].append(('Health declaration' if form=='health' else 'Post-training declaration')+' follow-up needs recording')
    for entry in report['unmatched']:
        if 'health' in entry and entry.get('followupForm','health')==form:add(entry,entry.get('name',''),'')
    report[prefix+'Followups']=cards
    report[prefix+'FollowupOutstanding']=sum(c['outstanding'] for c in cards)
    return report
