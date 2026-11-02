"""Trainer-controlled, course/day attendance points with an append-only change log."""
import time
from fastapi import HTTPException

LABELS=['Morning sign-in','Lunch sign-out','Afternoon sign-in','End-of-day sign-out','Attendance closed']

def initialise(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS mmva_attendance_stage(course TEXT NOT NULL,day TEXT NOT NULL,stage INTEGER NOT NULL,version INTEGER NOT NULL,actor TEXT NOT NULL,changed_at REAL NOT NULL,PRIMARY KEY(course,day));
    CREATE TABLE IF NOT EXISTS mmva_attendance_stage_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,course TEXT NOT NULL,day TEXT NOT NULL,previous_stage INTEGER NOT NULL,stage INTEGER NOT NULL,actor_email TEXT NOT NULL,actor_name TEXT NOT NULL,changed_at REAL NOT NULL,note TEXT NOT NULL);
    ''')

def current(db,course,day):
    row=db.execute('SELECT stage,version,actor,changed_at FROM mmva_attendance_stage WHERE course=? AND day=?',(course,day)).fetchone()
    return {'stage':row['stage'] if row else 0,'version':row['version'] if row else 0,
            'updatedBy':row['actor'] if row else None,'updatedAt':row['changed_at'] if row else None,
            'label':LABELS[row['stage'] if row else 0],'day':day,'course':course}

def change(ledger,course,day,stage,version,actor,note=''):
    if type(stage) is not int or stage not in range(5) or type(version) is not int or version<0:
        raise HTTPException(400,'Choose a valid attendance point and refresh its current status.')
    if not isinstance(note,str) or len(note)>500:raise HTTPException(400,'Keep the reason to 500 characters.')
    with ledger.db() as db:
        db.execute('BEGIN IMMEDIATE');old=current(db,course,day)
        if old['version']!=version:raise HTTPException(409,'Another trainer changed the attendance point. Refresh before continuing.')
        if stage<old['stage'] and not note.strip():raise HTTPException(400,'Record a reason when reopening an earlier attendance point.')
        now=time.time()
        db.execute('INSERT INTO mmva_attendance_stage VALUES(?,?,?,?,?,?) ON CONFLICT(course,day) DO UPDATE SET stage=excluded.stage,version=excluded.version,actor=excluded.actor,changed_at=excluded.changed_at',(course,day,stage,version+1,actor['name'],now))
        db.execute('INSERT INTO mmva_attendance_stage_audit(course,day,previous_stage,stage,actor_email,actor_name,changed_at,note) VALUES(?,?,?,?,?,?,?,?)',(course,day,old['stage'],stage,actor['email'],actor['name'],now,note.strip()))
        return current(db,course,day)

def gate(stage,step):
    if step>=4:return {'pointOpen':False,'pointMessage':''}
    if stage['stage']==step:return {'pointOpen':True,'pointMessage':''}
    if stage['stage']==4:message='Your trainer has closed attendance. Please speak to your trainer.'
    elif step>stage['stage']:message='Wait for your trainer to open '+LABELS[step]+'. Scan again or check attendance status at that point.'
    else:message='An earlier attendance point is missing and is now closed. Speak to your trainer before continuing.'
    return {'pointOpen':False,'pointMessage':message}
