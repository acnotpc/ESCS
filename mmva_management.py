"""Append-only management evidence checks, separate from source booking outcomes."""
import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit
from fastapi import HTTPException

LABELS = ['Trainer allocation', 'Attendance evidence', 'Candidate feedback', 'Certificates', 'Supporting records']
STATES = {'Unverified', 'In progress', 'Verified', 'Not required'}

class ManagementChecks:
    def __init__(self, path):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_management_checks(course TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,actor TEXT NOT NULL,recorded_at TEXT NOT NULL,PRIMARY KEY(course,version))')

    def latest(self):
        with sqlite3.connect(self.path) as db:
            rows = db.execute('SELECT c.course,c.version,c.payload,c.actor,c.recorded_at FROM mmva_management_checks c WHERE c.version=(SELECT MAX(n.version) FROM mmva_management_checks n WHERE n.course=c.course)').fetchall()
        return {r[0]:dict(json.loads(r[2]),version=r[1],actor=r[3],recordedAt=r[4]) for r in rows}

    def save(self, code, data, actor):
        checks = data.get('checks')
        if not isinstance(checks,list) or len(checks)!=len(LABELS):
            raise HTTPException(400,'Complete all five evidence checks.')
        clean = []
        for label, check in zip(LABELS, checks):
            if not isinstance(check,dict) or check.get('label')!=label or check.get('state') not in STATES:
                raise HTTPException(400,'Invalid evidence check.')
            note, link = check.get('note',''), check.get('reference','')
            if not isinstance(note,str) or len(note)>2000 or not isinstance(link,str) or len(link)>2000:
                raise HTTPException(400,'Evidence notes and links must each be under 2,000 characters.')
            try:parts=urlsplit(link)
            except ValueError:raise HTTPException(400,'Enter a valid HTTPS evidence link.')
            if link and (parts.scheme!='https' or not parts.netloc or parts.username or parts.password):
                raise HTTPException(400,'Use an HTTPS evidence link without embedded credentials.')
            if check['state']=='Verified' and (not link or not note.strip()):
                raise HTTPException(400,'Verified checks need an evidence link and review note.')
            if check['state']=='Not required' and not note.strip():
                raise HTTPException(400,'Explain why an evidence check is not required.')
            clean.append(dict(label=label,state=check['state'],reference=link,note=note.strip()))
        leader, followup=data.get('leader',''),data.get('followup','')
        if not isinstance(leader,str) or len(leader)>250 or not isinstance(followup,str) or len(followup)>3000:
            raise HTTPException(400,'Check the course leader and follow-up text lengths.')
        if type(data.get('version')) is not int or data['version']<0:
            raise HTTPException(400,'A saved version is required.')
        payload=dict(checks=clean,leader=leader.strip(),followup=followup.strip())
        now=datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            version=db.execute('SELECT COALESCE(MAX(version),0) FROM mmva_management_checks WHERE course=?',(code,)).fetchone()[0]
            if version!=data['version']:
                raise HTTPException(409,'Another manager changed these checks. Refresh before saving.')
            db.execute('INSERT INTO mmva_management_checks VALUES(?,?,?,?,?)',(code,version+1,json.dumps(payload),actor['email'],now))
        return dict(payload,version=version+1,actor=actor['email'],recordedAt=now)
