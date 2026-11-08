"""Versioned certificate records, source imports and management audit events."""
import json,sqlite3,re,hashlib
from datetime import date,datetime,timezone
from urllib.parse import urlsplit
from fastapi import HTTPException
from zoneinfo import ZoneInfo

def now():return datetime.now(timezone.utc).isoformat()
def https(value):
    try:p=urlsplit(value)
    except (ValueError,TypeError):raise HTTPException(400,'Use a valid HTTPS evidence link.')
    if not isinstance(value,str) or len(value)>2000 or p.scheme!='https' or not p.netloc or p.username or p.password:raise HTTPException(400,'Use an HTTPS evidence link without credentials.')
    return value

class ManagementRecords:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_management_event(id INTEGER PRIMARY KEY AUTOINCREMENT,course TEXT,kind TEXT,payload TEXT,actor TEXT,recorded_at TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS mmva_certificate(course TEXT,email TEXT,version INTEGER,payload TEXT,actor TEXT,recorded_at TEXT,PRIMARY KEY(course,email,version))')
            db.execute('CREATE TABLE IF NOT EXISTS mmva_legacy_record(source TEXT,record_id TEXT,digest TEXT,payload TEXT,actor TEXT,recorded_at TEXT,PRIMARY KEY(source,record_id,digest))')
    def db(self):return sqlite3.connect(self.path,timeout=10)
    def event(self,course,kind,payload,actor):
        with self.db() as db:db.execute('INSERT INTO mmva_management_event(course,kind,payload,actor,recorded_at) VALUES(?,?,?,?,?)',(course,kind,json.dumps(payload),actor['email'],now()))
    def history(self,course):
        with self.db() as db:rows=db.execute('SELECT id,kind,payload,actor,recorded_at FROM mmva_management_event WHERE course=? ORDER BY id DESC LIMIT 200',(course,)).fetchall()
        return [dict(id=r[0],kind=r[1],data=json.loads(r[2]),actor=r[3],recordedAt=r[4]) for r in rows]
    def certificates(self,course):
        with self.db() as db:rows=db.execute('SELECT c.email,c.version,c.payload,c.actor,c.recorded_at FROM mmva_certificate c WHERE course=? AND c.version=(SELECT MAX(n.version) FROM mmva_certificate n WHERE n.course=c.course AND n.email=c.email)',(course,)).fetchall()
        return [dict(json.loads(r[2]),email=r[0],version=r[1],actor=r[3],recordedAt=r[4]) for r in rows]
    def certificate_history(self,course):
        with self.db() as db:rows=db.execute('SELECT email,version,payload,actor,recorded_at FROM mmva_certificate WHERE course=? ORDER BY recorded_at DESC',(course,)).fetchall()
        return [dict(data=json.loads(r[2]),email=r[0],version=r[1],actor=r[3],recordedAt=r[4]) for r in rows]
    def delivery_policy(self,course):
        history=self.legacy(str(course))
        if not history:return dict(mode='mmva')
        latest=history[-1];record=latest['record']
        checks=record.get('checks',[])
        labels=['Attendance evidence','Feedback','Certificates','Supporting records']
        explicit=isinstance(record.get('notes'),str) and record['notes'].startswith('THIRD-PARTY COURSE — STAFFING ONLY.')
        states={c.get('label'):c.get('state') for c in checks if isinstance(c,dict)}
        if explicit and all(states.get(label)=='Not required' for label in labels):
            return dict(mode='third_party',source=record['monday'],digest=latest['digest'],note='Staffing only. The third-party provider manages all delegate paperwork; MMVA delegate forms, attendance and certificates are not required.')
        return dict(mode='mmva')

    def save_certificate(self,source,data,actor,delegates):
        email=str(data.get('email','')).strip().casefold();person=next((p for p in delegates if p['email']==email),None)
        if not person:raise HTTPException(400,'Select a delegate linked to this course.')
        if any('identity' in issue.casefold() or 'different names' in issue.casefold() for issue in person.get('issues',[])):raise HTTPException(409,'Resolve this delegate identity in course review before recording an outcome.')
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm you checked the assessment evidence and delegate identity.')
        outcome=data.get('outcome');reason=data.get('reason','');version=data.get('version')
        if outcome not in {'Pass','Refer','Fail','Voided'} or not isinstance(reason,str) or not reason.strip() or len(reason)>2000 or type(version) is not int or version<0:raise HTTPException(400,'Choose an outcome, record the evidence review and refresh the version.')
        payload=dict(name=person['name'],outcome=outcome,assessmentEvidence=https(data.get('assessmentEvidence','')),reason=reason.strip())
        if outcome=='Pass':
            try:
                issued=date.fromisoformat(data.get('issued',''));expires=date.fromisoformat(data.get('expires',''))
                if issued.isoformat()<source['end'] or issued>datetime.now(ZoneInfo('Europe/London')).date() or expires<=issued:raise ValueError()
            except (ValueError,TypeError):raise HTTPException(400,'Check the issue and expiry dates against the course end date.')
            number=data.get('number','')
            if not isinstance(number,str) or not number.strip() or len(number)>100:raise HTTPException(400,'Enter the certificate reference.')
            payload.update(issued=issued.isoformat(),expires=expires.isoformat(),number=number.strip(),certificateLink=https(data.get('certificateLink','')))
        timestamp=now()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');current=db.execute('SELECT COALESCE(MAX(version),0) FROM mmva_certificate WHERE course=? AND email=?',(source['id'],email)).fetchone()[0]
            if current!=version:raise HTTPException(409,'This certificate record changed. Refresh before saving.')
            if outcome=='Pass':
                for other_email,other_payload in db.execute('SELECT email,payload FROM mmva_certificate WHERE course=?',(source['id'],)):
                    if other_email!=email and json.loads(other_payload).get('number')==payload['number']:raise HTTPException(409,'This certificate reference is already recorded for another delegate.')
            db.execute('INSERT INTO mmva_certificate VALUES(?,?,?,?,?,?)',(source['id'],email,current+1,json.dumps(payload),actor['email'],timestamp))
            db.execute('INSERT INTO mmva_management_event(course,kind,payload,actor,recorded_at) VALUES(?,?,?,?,?)',(source['id'],'Assessment / certificate recorded',json.dumps(dict(email=email,version=current+1,outcome=outcome)),actor['email'],timestamp))
        return dict(payload,email=email,version=current+1,actor=actor['email'],recordedAt=timestamp)
    def import_legacy(self,records,actor):
        if not isinstance(records,list) or not 1<=len(records)<=100:raise HTTPException(400,'Import between 1 and 100 booking records.')
        prepared=[]
        for record in records:
            if not isinstance(record,dict):raise HTTPException(400,'Invalid import record.')
            rid=str(record.get('id',''));url=record.get('monday','');match=re.fullmatch(r'https://exclusive-gang\.monday\.com/boards/4727626523/pulses/([1-9][0-9]{0,19})',url)
            if not match or match[1]!=rid:raise HTTPException(400,'Every imported record must have a matching MMVA Monday booking ID.')
            encoded=json.dumps(record,sort_keys=True,ensure_ascii=True)
            if len(encoded)>60000:raise HTTPException(400,'Import record is too large.')
            prepared.append((rid,encoded,hashlib.sha256(encoded.encode()).hexdigest()))
        added=0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for rid,encoded,digest in prepared:
                added+=db.execute('INSERT OR IGNORE INTO mmva_legacy_record VALUES(?,?,?,?,?,?)',('Training Bookings & Delivery Board',rid,digest,encoded,actor['email'],now())).rowcount
        return dict(imported=added,duplicates=len(prepared)-added)
    def legacy(self,course):
        with self.db() as db:rows=db.execute('SELECT source,digest,payload,actor,recorded_at FROM mmva_legacy_record WHERE record_id=? ORDER BY recorded_at',(course,)).fetchall()
        return [dict(source=r[0],digest=r[1],record=json.loads(r[2]),importedBy=r[3],importedAt=r[4]) for r in rows]

def closeout_blockers(source,report,management,certificates,expected,today,policy=None):
    if policy and policy.get('mode')=='third_party':
        blockers=[]
        if source['end']>today:blockers.append('The staffing booking has not ended.')
        if not source.get('trainer') or not source.get('trainerEmails'):blockers.append('Confirm named trainers and their matching login emails in Monday.')
        if not management or not management.get('leader') or management.get('followup'):blockers.append('Record the course leader and resolve management follow-up.')
        checks=management.get('checks',[]) if management else []
        if len(checks)!=5 or checks[0]['state']!='Verified' or any(c['state']!='Not required' or not c.get('note') for c in checks[1:]):blockers.append('Verify trainer allocation and explicitly document all four third-party paperwork exceptions.')
        return blockers
    blockers=[];people=report.get('delegates',[])
    if source['end']>today:blockers.append('The course has not ended.')
    if report.get('sourceErrors'):blockers.append('One or more live evidence sources are unavailable.')
    if not people:blockers.append('No linked delegate roster is available.')
    if type(expected) is not int or expected<1 or len(people)!=expected:blockers.append('Confirm the full expected roster and reconcile its learner count.')
    if report.get('unmatched'):blockers.append('Unmatched submissions need review.')
    if any(not p.get('complete') for p in people):blockers.append('Attendance, required forms or delegate follow-up remain incomplete.')
    if report.get('healthFollowupOutstanding') or report.get('postCourseFollowupOutstanding'):blockers.append('Declaration follow-up remains outstanding.')
    venue=report.get('courseForms',{}).get('venue',{})
    if not venue.get('available') or not venue.get('entries'):blockers.append('Venue risk assessment evidence is missing or unavailable.')
    if not management or not management.get('leader') or management.get('followup') or not all(c['state'] in {'Verified','Not required'} for c in management.get('checks',[])):blockers.append('Finish the management checklist, course leader and follow-up actions.')
    outcomes={c['email']:c for c in certificates}
    if any(outcomes.get(p['email'],{}).get('outcome') not in {'Pass','Refer','Fail'} for p in people):blockers.append('Record a verified assessment outcome for every expected learner.')
    for certificate in certificates:
        if certificate.get('outcome')=='Pass' and (not certificate.get('certificateLink') or not certificate.get('number') or certificate.get('issued','')<source['end'] or certificate.get('expires','')<=certificate.get('issued','')):
            blockers.append('Recheck certificate references and dates against the current course dates.');break
    return blockers
