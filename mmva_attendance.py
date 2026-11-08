"""MMVA attendance gateway. Disabled until explicitly configured; no public roster."""
from contextlib import contextmanager
import io
import asyncio
import copy
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, RedirectResponse
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from mmva_monday_courses import MondayCourses, trainer_allowed, is_course_leader
from mmva_trainer_auth import TrainerAccounts
from mmva_management import ManagementChecks
from mmva_trainer_mail import TrainerResetMail
from mmva_attendance_stages import initialise as initialise_stages
from mmva_submission_view import SubmissionViewer, record_access
from mmva_attendance_review import AttendanceReviews, enrich_attendance
from mmva_duplicates import NameReviews, name_revision, apply_name_reviews, DuplicateLinks, candidates as duplicate_candidates, linked_rows
from mmva_completion import CompletionReader, SPECS, report as completion_report
from mmva_feedback_summary import FeedbackRosters, feedback_status, aggregate
from mmva_health_followup import HealthFollowups, enrich as enrich_health_followups
from starlette.concurrency import run_in_threadpool

FORM = '263045888335062'
ACTIONS = ['1. Morning sign-in', '2. Lunch sign-out', '3. Afternoon sign-in (after lunch)', '4. End-of-day sign-out']
ROOT = Path(__file__).parent / 'mmva_attendance_assets'
COURSE_FORMS = {
    'feedback': ('Candidate feedback', '231786383440359', 'organisationName', '', 'Delegate'),
    'registration': ('Learner registration', '232684816706363', 'employersName', '', 'Delegate'),
    'health': ('Pre-course joining instructions', '232685513399366', 'organisationYou', '', 'Delegate'),
    'fitness': ('Post-training fitness', '240482908768368', 'organisationName', '', 'Delegate'),
    'knowledge': ('Physical intervention knowledge assessment', '240691111369353', '', '', 'Delegate'),
    'venue': ('Venue environment risk assessment', '231735606698366', 'clientOrganisation', '', 'Trainer'),
    'incident': ('Accident / incident report', '231734935383361', 'company', '', 'Use when needed'),
    'evaluation': ('Course evaluation / feedback', '213414022784348', '', '', 'Delegate — name optional'),
}


QR_PURPOSE_ORDER=('venue','registration','health','fitness','feedback','incident','knowledge')

def course_form_url(key, source):
    _, form_id, company, trainer, _ = COURSE_FORMS[key]
    fields = {'courseNumber': source['id']}
    if company:
        fields[company] = source.get('client', '')
    if trainer:
        fields[trainer] = source.get('trainer', '')
    if key == 'feedback':
        fields['bookedCourse'] = source.get('courseName') or source['title']
    if key == 'evaluation':
        fields['course'] = source['title']
    return 'https://form.jotform.com/' + form_id + '?' + urlencode(fields)


def london_day(now=None):
    return datetime.fromtimestamp(now if now is not None else time.time(), ZoneInfo('Europe/London')).date().isoformat()


MIN_ATTENDANCE_INTERVAL=30*60

def interval_gate(rows,now):
    if len(rows)>=4:return {'pointOpen':False,'pointMessage':'','nextAllowedAt':None,'waitSeconds':0}
    available=rows[-1]['recorded_at']+MIN_ATTENDANCE_INTERVAL if rows else now
    wait=max(0,math.ceil(available-now))
    clock=datetime.fromtimestamp(available,ZoneInfo('Europe/London')).strftime('%H:%M')
    return {'pointOpen':wait==0,'pointMessage':('Allow at least 30 minutes between attendance points. Your next point is available from '+clock+' (London time). Check attendance status then.') if wait else '',
            'nextAllowedAt':available,'waitSeconds':wait}


class Ledger:
    def __init__(self, path, secret):
        self.path, self.secret = str(path), secret
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS course(code TEXT PRIMARY KEY,title TEXT NOT NULL,day TEXT NOT NULL,trainer TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS delegate(id TEXT PRIMARY KEY,course TEXT NOT NULL,identity TEXT NOT NULL,first TEXT NOT NULL,last TEXT NOT NULL,email TEXT NOT NULL,organisation TEXT NOT NULL,UNIQUE(course,identity));
            CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,delegate TEXT NOT NULL,step INTEGER NOT NULL,recorded_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',submission_id TEXT,UNIQUE(delegate,step));
            ''')
            initialise_stages(db)
            if 'source_id' not in {r['name'] for r in db.execute('PRAGMA table_info(course)')}:
                db.execute("ALTER TABLE course ADD COLUMN source_id TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def monday_course(self, course):
        # Internal key includes the London day; the delegate always enters the same Monday ID.
        key = course['id'] + ':' + course['day']
        with self.db() as db:
            db.execute("INSERT INTO course(code,title,day,trainer,source_id) VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET title=excluded.title,trainer=excluded.trainer,active=1,source_id=excluded.source_id", (key,course['title'],course['day'],course['trainer'],course['id']))
        return key

    def source_id(self, delegate):
        with self.db() as db:
            r = db.execute('SELECT c.source_id,c.day FROM delegate d JOIN course c ON c.code=d.course WHERE d.id=?', (delegate,)).fetchone()
            if not r or not r['source_id'] or r['day'] != london_day():
                raise HTTPException(401, 'Enter your Monday.com Course ID to continue.')
            return r['source_id']

    def course(self, db, code, now):
        row = db.execute('SELECT * FROM course WHERE code=? AND active=1', (code,)).fetchone()
        if not row or row['day'] != london_day(now):
            raise HTTPException(400, 'Course code is not active today. Please ask your trainer.')
        return row

    def enrol(self, data, now):
        code = str(data.get('code', '')).strip().upper()
        email = str(data.get('email', '')).strip().casefold()
        values = [str(data.get(k, '')).strip() for k in ('first', 'last', 'organisation')]
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254 or any(not v or len(v) > 150 for v in values[:2]):
            raise HTTPException(400, 'Please enter your first name, surname and a valid email address.')
        identity = hmac.new(self.secret.encode(), email.encode(), hashlib.sha256).hexdigest()
        with self.db() as db:
            self.course(db, code, now)
            db.execute('INSERT OR IGNORE INTO delegate VALUES(?,?,?,?,?,?,?)',
                       (secrets.token_urlsafe(24), code, identity, values[0], values[1], email, values[2]))
            row = db.execute('SELECT id FROM delegate WHERE course=? AND identity=?', (code, identity)).fetchone()
            return row['id']

    def status(self, delegate, now):
        with self.db() as db:
            person = db.execute('SELECT * FROM delegate WHERE id=?', (delegate,)).fetchone()
            if not person:
                raise HTTPException(401, 'Please register again.')
            course = self.course(db, person['course'], now)
            rows = db.execute('SELECT * FROM event WHERE delegate=? ORDER BY step', (delegate,)).fetchall()
            return {**interval_gate(rows,now),'course': course['title'], 'day': course['day'], 'courseNumber': course['source_id'], 'step': len(rows),
                    'next': ACTIONS[len(rows)] if len(rows) < 4 else None,
                    'lastRecorded': rows[-1]['recorded_at'] if rows else None,
                    'syncPending': any(r['state'] != 'sent' for r in rows)}

    def record(self, delegate, expected, now):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            person = db.execute('SELECT * FROM delegate WHERE id=?', (delegate,)).fetchone()
            if not person:
                raise HTTPException(401, 'Please register again.')
            course = self.course(db, person['course'], now)
            rows = db.execute('SELECT * FROM event WHERE delegate=? ORDER BY step', (delegate,)).fetchall()
            if expected != len(rows) or expected >= 4:
                raise HTTPException(409, 'This attendance step has already been recorded or is out of order. Refresh to check your status.')
            permission=interval_gate(rows,now)
            if not permission['pointOpen']:
                raise HTTPException(409,permission['pointMessage'])
            event_id = secrets.token_urlsafe(24)
            db.execute('INSERT INTO event(id,delegate,step,recorded_at) VALUES(?,?,?,?)', (event_id, delegate, expected, now))
            return event_id

    async def deliver(self, event_id, key, transport=None):
        # Claim before network I/O. A crash or timeout is uncertain: never blindly retry.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT e.*,d.first,d.last,d.email,d.organisation,c.title,c.day,c.trainer,c.source_id FROM event e JOIN delegate d ON d.id=e.delegate JOIN course c ON c.code=d.course WHERE e.id=?''', (event_id,)).fetchone()
            if not row or row['state'] != 'pending':
                return
            db.execute("UPDATE event SET state='sending' WHERE id=?", (event_id,))
        payload = {
            'submission[3_first]': row['first'], 'submission[3_last]': row['last'],
            'submission[4]': row['email'], 'submission[5]': row['organisation'],
            'submission[6]': row['title'], 'submission[7_year]': row['day'][:4],
            'submission[7_month]': row['day'][5:7], 'submission[7_day]': row['day'][8:10],
            'submission[8]': ACTIONS[row['step']], 'submission[9]': row['trainer'],
            'submission[10]': 'Gateway event ' + event_id + '; recorded ' + datetime.fromtimestamp(row['recorded_at'], ZoneInfo('Europe/London')).isoformat(),
            'submission[11][]': 'I confirm',
        }
        if row['source_id']:
            field = os.getenv('MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD', '')
            if not re.fullmatch(r'[0-9]+', field):
                with self.db() as db:
                    db.execute("UPDATE event SET state='pending' WHERE id=?", (event_id,))
                return
            payload['submission[' + field + ']'] = row['source_id']
            payload['submission[10]'] += '; Monday Course ID ' + row['source_id'] + '; https://exclusive-gang.monday.com/boards/4727626523/pulses/' + row['source_id']
        state, submission_id = 'uncertain', None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=transport) as client:
                response = await client.post('https://eu-api.jotform.com/form/' + FORM + '/submissions', headers={'APIKEY': key}, data=payload)
                result = response.json()
                if response.status_code == 200 and result.get('responseCode') == 200:
                    submission_id = str(result.get('content', {}).get('submissionID', ''))
                    if submission_id.isdigit():
                        state = 'sent'
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            pass
        with self.db() as db:
            db.execute('UPDATE event SET state=?,submission_id=? WHERE id=?', (state, submission_id, event_id))


def build_attendance_router(auth):
    router = APIRouter()
    ledger_cache = {}
    rates = {}
    trainer_accounts = {}

    def configured():
        secret = os.getenv('MMVA_ATTENDANCE_SECRET', '')
        path = os.getenv('MMVA_ATTENDANCE_DB', '/var/data/mmva-attendance.sqlite3')
        key = os.getenv('MMVA_ATTENDANCE_JOTFORM_API_KEY', '')
        # Explicit gate includes verified private destination and mapping, not just an API key.
        if os.getenv('MMVA_ATTENDANCE_ENABLED') != 'true' or os.getenv('MMVA_ATTENDANCE_DESTINATION_VERIFIED') != 'true' or len(secret) < 32 or not key or not Path(path).is_absolute() or not Path(path).parent.is_dir():
            raise HTTPException(503, 'Attendance registration is not open yet. Please speak to your trainer.')
        if os.getenv('MMVA_ATTENDANCE_COURSE_SOURCE') == 'monday' and not re.fullmatch(r'[0-9]+', os.getenv('MMVA_ATTENDANCE_JOTFORM_COURSE_FIELD', '')):
            raise HTTPException(503, 'Course connection is not ready. Please speak to your trainer.')
        if path not in ledger_cache:
            ledger_cache[path] = Ledger(path, secret)
        return ledger_cache[path], URLSafeTimedSerializer(secret, salt='mmva-attendance-v1'), key

    def monday_enabled():
        return os.getenv('MMVA_ATTENDANCE_COURSE_SOURCE') == 'monday'

    def monday_reader():
        return MondayCourses(os.getenv('MMVA_ATTENDANCE_MONDAY_API_TOKEN') or os.getenv('MONDAY_RECRUITMENT_API_TOKEN', ''))

    async def verify_delegate(db, delegate):
        if monday_enabled():
            await monday_reader().resolve(db.source_id(delegate), london_day())

    def session(request, signer):
        try:
            return signer.loads(request.cookies.get('mmva_attendance', ''), max_age=18 * 3600)['delegate']
        except (BadSignature, SignatureExpired, KeyError):
            raise HTTPException(401, 'Enter your details to continue.')

    async def body(request, max_size=4096):
        # JSON only prevents ordinary cross-site form CSRF; never enable permissive CORS.
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, 'JSON required')
        raw = b''
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > max_size:
                raise HTTPException(413, 'Request too large')
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except ValueError:
            raise HTTPException(400, 'Invalid request')

    def view(db, signer, delegate):
        data = db.status(delegate, time.time())
        data['token'] = signer.dumps({'delegate': delegate, 'step': data['step']}) if data['next'] and data['pointOpen'] else None
        return data

    def private_response(data):
        return JSONResponse(data, headers={'Cache-Control': 'no-store'})

    def form_signer():
        configured()
        return URLSafeTimedSerializer(os.environ['MMVA_ATTENDANCE_SECRET'], salt='mmva-course-form-v1')

    def read_form_token(token):
        try:
            data = form_signer().loads(token, max_age=90 * 86400)
            if data['form'] not in COURSE_FORMS or not re.fullmatch(r'[1-9][0-9]{0,19}', data['course']):
                raise ValueError()
            return data
        except (BadSignature, KeyError, TypeError, ValueError):
            raise HTTPException(400, 'This course form link is invalid or expired. Ask your trainer for a new QR.')

    @router.get('/attendance/course-form')
    async def open_course_form(token: str):
        data = read_form_token(token)
        source = await monday_reader().resolve(data['course'], london_day(), enforce_day=False)
        if data['form']=='knowledge' and source.get('knowledgeRequired') is not True:
            raise HTTPException(400,'The knowledge assessment is only required for course options 1, 4 and 10. Ask your trainer to check the selected course option.')
        return RedirectResponse(course_form_url(data['form'], source), status_code=302,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    @router.get('/attendance/course-form-qr')
    async def course_form_qr(token: str):
        read_form_token(token)
        import qrcode
        import qrcode.image.svg
        url = 'https://escs.onrender.com/attendance/course-form?' + urlencode({'token': token})
        image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=4)
        output = io.BytesIO(); image.save(output)
        return Response(output.getvalue(), media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})

    @router.get('/attendance', response_class=HTMLResponse)
    async def page():
        return HTMLResponse((ROOT / 'index.html').read_text(), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    def trainer_registry():
        ledger, _, _ = configured()
        if ledger.path not in trainer_accounts:
            trainer_accounts[ledger.path] = TrainerAccounts(ledger.path, ledger.secret)
        return trainer_accounts[ledger.path], URLSafeTimedSerializer(ledger.secret, salt='mmva-trainer-account-v1')

    def require_trainer(request):
        registry, signer = trainer_registry()
        try:
            token = signer.loads(request.cookies.get('mmva_trainer', ''), max_age=8 * 3600)
        except (BadSignature, SignatureExpired):
            raise HTTPException(401, 'Please sign in to view course bookings.')
        return registry.check(token)

    async def trainer_source(request,code,review=False):
        actor=require_trainer(request)
        source=await monday_reader().resolve(str(code).strip(),london_day(),enforce_day=False,review=review)
        if not trainer_allowed(source,actor['email']):
            raise HTTPException(403,'This course is not assigned to your trainer login email in Monday. Ask the course leader to check Trainer Allocated and Trainer login email(s).')
        return source

    @router.post('/attendance/trainer/login')
    async def trainer_login(request: Request):
        registry, signer = trainer_registry()
        data = await body(request)
        user = await run_in_threadpool(registry.authenticate, data.get('email', ''), data.get('password', ''), request.client.host if request.client else 'unknown')
        response = private_response({'signedIn': True, 'name': user['name']})
        response.set_cookie('mmva_trainer', signer.dumps({'email': user['email'], 'version': user['version']}), secure=True, httponly=True, samesite='strict', max_age=8 * 3600, path='/attendance/trainer')
        return response

    @router.get('/attendance/trainer/setup', response_class=HTMLResponse)
    async def trainer_setup_page():
        return HTMLResponse((ROOT / 'trainer-setup.html').read_text(), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/setup')
    async def trainer_setup(request: Request):
        registry, _ = trainer_registry()
        data = await body(request)
        result = await run_in_threadpool(registry.setup, data.get('token', ''), data.get('password', ''), request.client.host if request.client else 'unknown')
        return private_response(result)

    @router.get('/attendance/trainer/forgot', response_class=HTMLResponse)
    async def trainer_forgot_page():
        return HTMLResponse((ROOT / 'trainer-forgot.html').read_text(), headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/forgot')
    async def trainer_forgot(request: Request, tasks: BackgroundTasks):
        registry, _ = trainer_registry()
        data = await body(request)
        async def deliver():
            try:
                invite = await run_in_threadpool(registry.request_reset,data.get('email',''),request.client.host if request.client else 'unknown')
                if invite:
                    await TrainerResetMail().send(invite)
            except Exception:
                # Never log email addresses, tokens, provider payloads, or secret URLs.
                logging.getLogger('mmva.trainer.reset').warning('Trainer recovery email could not be confirmed; no automatic retry.')
        tasks.add_task(deliver)
        return private_response({'message':'If this email has an enabled trainer account, a password reset link will be emailed. Please check your inbox and junk folder.'})

    @router.get('/attendance/trainer/reset', response_class=HTMLResponse)
    async def trainer_reset_page():
        return HTMLResponse((ROOT / 'trainer-reset.html').read_text(), headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/reset')
    async def trainer_reset(request: Request):
        registry, _ = trainer_registry()
        data = await body(request)
        result = await run_in_threadpool(registry.reset_password,data.get('token',''),data.get('password',''),request.client.host if request.client else 'unknown')
        response = private_response(result)
        response.delete_cookie('mmva_trainer',path='/attendance/trainer',secure=True,httponly=True,samesite='strict')
        return response

    @router.post('/attendance/admin/trainers', dependencies=[Depends(auth)])
    async def invite_trainer(request: Request):
        registry, _ = trainer_registry()
        data = await body(request)
        return private_response(registry.invite(data.get('email', ''), data.get('name', ''), reset=data.get('reset') is True))

    @router.post('/attendance/admin/trainers/disable', dependencies=[Depends(auth)])
    async def disable_trainer(request: Request):
        registry, _ = trainer_registry()
        data = await body(request)
        return private_response(registry.disable(data.get('email', '')))

    @router.get('/attendance/trainer/qr')
    async def standard_trainer_qr():
        import qrcode
        import qrcode.image.svg
        image = qrcode.make('https://escs.onrender.com/attendance/trainer', image_factory=qrcode.image.svg.SvgPathImage, border=4)
        output = io.BytesIO(); image.save(output)
        return Response(output.getvalue(), media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400', 'Content-Disposition': 'inline; filename="MMVA-standard-trainer-QR.svg"'})

    @router.post('/attendance/trainer/logout')
    async def trainer_logout(request: Request):
        await body(request)
        response = private_response({'signedIn': False})
        response.delete_cookie('mmva_trainer', path='/attendance/trainer', secure=True, httponly=True, samesite='strict')
        return response

    def course_range(data,default_category):
        category=str(data.get('category',default_category))
        if category not in {'all','upcoming','completed'}:raise HTTPException(400,'Choose upcoming or completed courses.')
        today=datetime.strptime(london_day(),'%Y-%m-%d').date()
        start=str(data.get('from') or (today-timedelta(days=90 if category=='completed' else 30 if category=='all' else 0)).isoformat())
        end=str(data.get('until') or (today+timedelta(days=0 if category=='completed' else 60)).isoformat())
        return category,start,end

    def require_manager(request):
        actor=require_trainer(request)
        if not is_course_leader(actor['email']):
            raise HTTPException(403,'Management access is restricted to the course leader and Noel Pusey.')
        return actor

    @router.get('/attendance/trainer/management', response_class=HTMLResponse)
    async def management_page(request: Request):
        try:require_manager(request)
        except HTTPException as exc:
            if exc.status_code==401:
                return RedirectResponse('/attendance/trainer?next=management',status_code=303,headers={'Cache-Control':'no-store'})
            raise
        return HTMLResponse((ROOT / 'trainer-management.html').read_text(),headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/management-courses')
    async def management_courses(request: Request):
        ledger,_,_=configured();actor=require_manager(request)
        data=await body(request)
        category,start,end=course_range(data,'all')
        rows=await monday_reader().list_courses(start,end,review=True,trainer_email=actor['email'],category=category,on_day=london_day(),management=True)
        checks=ManagementChecks(ledger.path).latest()
        return private_response({'courses':[{**row,'management':checks.get(row['id'])} for row in rows], 'from':start,'until':end,'checkedAt':datetime.now(ZoneInfo('Europe/London')).isoformat(),'manager':actor['name']})

    @router.post('/attendance/trainer/management-checks')
    async def management_checks(request: Request):
        ledger,_,_=configured();actor=require_manager(request)
        data=await body(request,max_size=20000)
        source=await trainer_source(request,data.get('code',''),review=True)
        result=ManagementChecks(ledger.path).save(source['id'],data,actor)
        return private_response(result)

    @router.post('/attendance/trainer/courses')
    async def trainer_courses(request: Request):
        configured()
        actor=require_trainer(request)
        data = await body(request)
        category,start,end=course_range(data,'upcoming')
        rows = await monday_reader().list_courses(start,end,review=True,trainer_email=actor['email'],category=category,on_day=london_day())
        return private_response({'courses':rows,'from':start,'until':end,'category':category})

    @router.post('/attendance/trainer/attendance-point')
    async def attendance_point(request: Request):
        configured();require_trainer(request)
        raise HTTPException(410,'Attendance now advances in order with a minimum 30-minute gap. Use course review for verified missed scans.')

    @router.post('/attendance/trainer/course')
    async def trainer_course(request: Request):
        configured()
        actor=require_trainer(request)
        data = await body(request)
        source = await trainer_source(request,data.get('code',''),review=True)
        result = {k: source[k] for k in ('id', 'title', 'client', 'venue', 'start', 'end')}
        signer = form_signer()
        result['forms'] = []
        result['completed']=source.get('completed',False)
        result['knowledgeRequired']=source.get('knowledgeRequired')
        result['courseOption']=source.get('courseOption')
        for key in QR_PURPOSE_ORDER:
            title, _, _, _, audience = COURSE_FORMS[key]
            if key=='knowledge' and source.get('knowledgeRequired') is not True:continue
            query = urlencode({'token': signer.dumps({'course': source['id'], 'form': key})})
            result['forms'].append({'key': key, 'title': title, 'audience': audience,
                'url': 'https://escs.onrender.com/attendance/course-form?' + query,
                'qr': '/attendance/course-form-qr?' + query})
        return private_response(result)

    def followup_report(base,path,code):
        notes=HealthFollowups(path).notes(code)
        result=enrich_health_followups(copy.deepcopy(base),notes)
        result=enrich_health_followups(result,notes,form='fitness')
        result['possibleDuplicates']=duplicate_candidates(base)
        result['identityLinks']=DuplicateLinks(path).history(code)
        result['feedbackSummary']=feedback_status(base,FeedbackRosters(path).latest(code))
        from mmva_feedback_summary import roster_hash
        result['feedbackSummary']['roster']=roster_hash(base)
        return result

    completion_cache = {}
    completion_lock = asyncio.Lock()
    identity_lock = asyncio.Lock()

    @router.get('/attendance/trainer/points', response_class=HTMLResponse)
    async def attendance_points_page():
        return RedirectResponse('/attendance/trainer/review',status_code=303,headers={'Cache-Control':'no-store'})

    @router.get('/attendance/trainer/review', response_class=HTMLResponse)
    async def review_page():
        return HTMLResponse((ROOT / 'trainer-review.html').read_text(), headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/review-courses')
    async def review_courses(request: Request):
        configured(); actor=require_trainer(request)
        data = await body(request)
        category,start,end=course_range(data,'all')
        rows=await monday_reader().list_courses(start,end,review=True,trainer_email=actor['email'],category=category,on_day=london_day())
        return private_response({'courses':rows,'from':start,'until':end,'category':category})

    async def completion_base(source,ledger,key,fresh=False):
        code = source['id']
        async with completion_lock:
            now = time.time()
            cached = completion_cache.get(code)
            if not fresh and cached and now-cached[0]<60:
                return copy.deepcopy(cached[1])
            scans = await CompletionReader(key).read()
            with ledger.db() as db:
                rows = db.execute("SELECT d.email,d.first,d.last,c.day,e.step,e.recorded_at,e.state FROM delegate d JOIN course c ON c.code=d.course LEFT JOIN event e ON e.delegate=d.id WHERE c.source_id=?",(code,)).fetchall()
            attendance = [{'email':r['email'],'name':r['first']+' '+r['last'],'day':r['day'],'step':r['step'],'time':r['recorded_at'],'sync':r['state']} for r in rows]
            links=DuplicateLinks(ledger.path).links(code)
            scans={k:{**v,'rows':linked_rows(v['rows'],code,links)} for k,v in scans.items()}
            attendance=linked_rows(attendance,code,links)
            try:
                result = completion_report(source,scans,attendance,london_day())
                result = apply_name_reviews(result,NameReviews(ledger.path).history(code),london_day())
                result = enrich_attendance(result,ledger.path,london_day())
            except ValueError:
                raise HTTPException(400,'Course dates need checking before reviewing attendance.')
            # Cache only the selected course's minimal report, never raw answers.
            for old in list(completion_cache):
                if now-completion_cache[old][0]>=60:del completion_cache[old]
            completion_cache[code]=(now,result)
            return copy.deepcopy(result)

    @router.post('/attendance/trainer/completion')
    async def completion(request: Request):
        ledger, _, key = configured(); actor=require_trainer(request)
        data = await body(request)
        source = await trainer_source(request,data.get('code',''),review=True)
        result=await completion_base(source,ledger,key)
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.get('/attendance/trainer/submission',response_class=HTMLResponse)
    async def submission_page():
        return HTMLResponse((ROOT / 'trainer-submission.html').read_text(),headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/submission-data')
    async def submission_data(request: Request):
        ledger, _, key=configured();actor=require_trainer(request)
        data=await body(request)
        source=await trainer_source(request,data.get('code',''),review=True)
        result=await SubmissionViewer(key).read(data.get('form'),str(data.get('submission','')),source['id'])
        record_access(ledger.path,source['id'],result['form'],result['submission'],actor)
        return private_response(result)

    @router.post('/attendance/trainer/historic-attendance-day')
    async def historic_attendance_day(request: Request):
        ledger, _, key=configured();actor=require_trainer(request)
        data=await body(request,max_size=16384)
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm you are enabling an evidence review, not creating delegate scans.')
        source=await trainer_source(request,data.get('code',''),review=True)
        async with identity_lock:
            base=await completion_base(source,ledger,key,fresh=True)
            if base['sourceErrors'] or not base['delegates']:raise HTTPException(409,'Check all sources and link the delegate records before adding historic attendance evidence.')
            AttendanceReviews(ledger.path).open_day(source,data.get('day'),london_day(),actor,data.get('reason'))
            completion_cache.pop(source['id'],None)
        result=await completion_base(source,ledger,key,fresh=True)
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.post('/attendance/trainer/attendance-review')
    async def attendance_review(request: Request):
        ledger, _, key=configured();actor=require_trainer(request)
        data=await body(request,max_size=16384)
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm you have checked this attendance outcome.')
        source=await trainer_source(request,data.get('code',''),review=True)
        async with identity_lock:
            base=await completion_base(source,ledger,key,fresh=True)
            if base['sourceErrors']:raise HTTPException(503,'Complete all source checks before recording attendance evidence.')
            card=next((c for c in base['attendanceReviews'] if c['email']==data.get('email') and c['day']==data.get('day') and type(data.get('step')) is int and c['step']==data['step']),None)
            if not card or card['revision']!=data.get('revision'):raise HTTPException(409,'This attendance point changed or is not due. Refresh before recording evidence.')
            AttendanceReviews(ledger.path).save(source['id'],card,data.get('state'),data.get('actualTime'),data.get('reason'),actor,data.get('version'))
            completion_cache.pop(source['id'],None)
        result=await completion_base(source,ledger,key,fresh=True)
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.post('/attendance/trainer/verify-names')
    async def verify_names(request: Request):
        ledger, _, key=configured();actor=require_trainer(request)
        data=await body(request)
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm you have verified that the recorded names belong to this delegate.')
        source=await trainer_source(request,data.get('code',''),review=True)
        async with identity_lock:
            base=await completion_base(source,ledger,key,fresh=True)
            if base['sourceErrors']:raise HTTPException(503,'All sources must be checked before verifying names.')
            person=next((p for p in base['delegates'] if p['email']==data.get('email')),None)
            if not person or name_revision(person)!=data.get('revision') or 'Different names use this email; review identity' not in person['issues']:
                raise HTTPException(409,'These names changed or no longer need verification. Refresh and review them again.')
            NameReviews(ledger.path).save(source['id'],person,actor,data.get('name'),data.get('reason'),data.get('version'))
            completion_cache.pop(source['id'],None)
        result=await completion_base(source,ledger,key,fresh=True)
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.post('/attendance/trainer/combine-delegates')
    async def combine_delegates(request: Request):
        ledger, _, key=configured();actor=require_trainer(request)
        data=await body(request)
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm you have verified that both entries belong to the same person.')
        source=await trainer_source(request,data.get('code',''),review=True)
        async with identity_lock:
            base=await completion_base(source,ledger,key,fresh=True)
            if base['sourceErrors']:raise HTTPException(503,'All sources must be checked before combining records.')
            pair={str(data.get('source','')),str(data.get('target',''))}
            match=next((p for p in duplicate_candidates(base) if set(p['emails'])==pair and p['revision']==data.get('revision')),None)
            if not match:raise HTTPException(409,'These records changed or are not a suggested duplicate. Refresh and review them again.')
            DuplicateLinks(ledger.path).save(source['id'],data['source'],data['target'],actor,data.get('reason'))
            completion_cache.pop(source['id'],None)
        result=await completion_base(source,ledger,key,fresh=True)
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.post('/attendance/trainer/feedback-roster')
    async def confirm_feedback_roster(request: Request):
        ledger, _, key=configured(); actor=require_trainer(request)
        data=await body(request)
        if data.get('confirm') is not True:raise HTTPException(400,'Confirm that this is the full expected delegate list.')
        source=await trainer_source(request,data.get('code',''),review=True)
        result=await completion_base(source,ledger,key,fresh=True)
        # The browser must have shown exactly this roster before the trainer confirms it.
        from mmva_feedback_summary import roster_hash
        if data.get('roster')!=roster_hash(result):raise HTTPException(409,'The delegate list changed. Refresh and check the full list again.')
        FeedbackRosters(ledger.path).confirm(result,actor,data.get('version'),data.get('note',''))
        return private_response(followup_report(result,ledger.path,source['id']))

    @router.get('/attendance/trainer/feedback-summary',response_class=HTMLResponse)
    async def feedback_summary_page():
        return HTMLResponse((ROOT / 'trainer-feedback.html').read_text(),headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/trainer/feedback-summary-data')
    async def feedback_summary_data(request: Request):
        ledger, _, key=configured(); actor=require_trainer(request)
        data=await body(request)
        source=await trainer_source(request,data.get('code',''),review=True)
        result=await completion_base(source,ledger,key,fresh=True)
        status=feedback_status(result,FeedbackRosters(ledger.path).latest(source['id']))
        if not status['ready']:raise HTTPException(409,status['reason'])
        scan=await CompletionReader(key).read_form(SPECS['feedback'],feedback_course=source['id'])
        if not scan['available']:raise HTTPException(503,'Feedback could not be fully checked. Try again shortly.')
        scan['rows']=linked_rows(scan['rows'],source['id'],DuplicateLinks(ledger.path).links(source['id']))
        expected={(e['id'],p['email']) for p in result['delegates'] for e in p['forms']['feedback']['entries']}
        actual={(r['id'],r['email']) for r in scan['rows'] if r['course']==source['id']}
        if actual!=expected:raise HTTPException(409,'Feedback records changed during the check. Refresh the course review and try again.')
        return private_response({'course':result['course'],'checkedAt':datetime.now().astimezone().isoformat(),
            'expected':status['expected'],**aggregate(scan['rows'],source['id'])})

    async def save_declaration_followup(request: Request,form):
        ledger, _, key = configured()
        actor = require_trainer(request)
        data = await body(request,max_size=16384)
        source = await trainer_source(request,data.get('code',''),review=True)
        sid = str(data.get('submission',''))
        reader=CompletionReader(key)
        declaration = await (reader.read_health_submission(sid) if form=='health' else reader.read_post_course_submission(sid))
        if not declaration:
            raise HTTPException(503,'The declaration could not be verified. Refresh and try again.')
        if declaration['course']!=source['id']:
            raise HTTPException(400,'This declaration does not belong to the selected course.')
        revision = declaration['health']['revision']
        if revision!=data.get('revision'):
            completion_cache.pop(source['id'],None)
            raise HTTPException(409,'The health answers have changed. Refresh and review the declaration before saving.')
        version = HealthFollowups(ledger.path).save(source['id'],sid,revision,data.get('note'),data.get('state'),actor,data.get('version'))
        return private_response({'saved':True,'version':version})

    @router.post('/attendance/trainer/health-followup')
    async def health_followup(request: Request):
        return await save_declaration_followup(request,'health')

    @router.post('/attendance/trainer/post-course-followup')
    async def post_course_followup(request: Request):
        return await save_declaration_followup(request,'fitness')

    @router.get('/attendance/trainer', response_class=HTMLResponse)
    async def trainer_page():
        return HTMLResponse((ROOT / 'trainer.html').read_text(), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    @router.post('/attendance/course-info')
    async def course_info(request: Request):
        configured()
        if not monday_enabled():
            raise HTTPException(503, 'Monday course connection is not ready.')
        data = await body(request)
        # Exact code only, never a public course list or learner roster.
        ip = request.client.host if request.client else 'unknown'
        rate_key = 'course-info:' + ip
        now = time.time()
        for old in list(rates):
            if rates[old][0] < now - 60:
                del rates[old]
        count = rates.get(rate_key, (now, 0))[1]
        if count >= 60:
            raise HTTPException(429, 'Please wait a minute and try again.')
        rates[rate_key] = (now, count + 1)
        source = await monday_reader().resolve(str(data.get('code', '')).strip(), london_day(), enforce_day=False)
        return private_response({k: source[k] for k in ('id', 'title', 'start', 'end')})

    @router.get('/attendance/qr')
    async def course_qr(course: str):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', course):
            raise HTTPException(400, 'Invalid Course ID')
        import qrcode
        import qrcode.image.svg
        image = qrcode.make('https://escs.onrender.com/attendance?course=' + course, image_factory=qrcode.image.svg.SvgPathImage, border=4)
        output = io.BytesIO()
        image.save(output)
        return Response(output.getvalue(), media_type='image/svg+xml', headers={'Cache-Control': 'public, max-age=86400', 'Content-Disposition': 'inline; filename="MMVA-course-' + course + '-QR.svg"'})

    @router.get('/attendance/logo.jpeg')
    async def logo():
        return FileResponse(ROOT / 'logo.jpeg', media_type='image/jpeg')

    @router.post('/attendance/register')
    async def register(request: Request):
        db, signer, key = configured()
        # Bound in-memory throttling; proxy IP headers are intentionally not trusted.
        ip = request.client.host if request.client else 'unknown'
        now = time.time()
        for old in list(rates):
            if rates[old][0] < now - 60:
                del rates[old]
        count = rates.get(ip, (now, 0))[1]
        if count >= 120:
            raise HTTPException(429, 'Please wait a minute and try again.')
        rates[ip] = (now, count + 1)
        data = await body(request)
        if monday_enabled():
            source = await monday_reader().resolve(str(data.get('code', '')).strip(), london_day(now))
            data['code'] = db.monday_course(source)
            # The booking company is authoritative; ignore any delegate-supplied value.
            data['organisation'] = source.get('client', '')
        delegate = db.enrol(data, now)
        response = private_response(view(db, signer, delegate))
        response.set_cookie('mmva_attendance', signer.dumps({'delegate': delegate}), secure=True, httponly=True, samesite='strict', max_age=18 * 3600, path='/attendance')
        return response

    @router.get('/attendance/status')
    async def status(request: Request):
        db, signer, key = configured()
        delegate = session(request, signer)
        await verify_delegate(db, delegate)
        return private_response(view(db, signer, delegate))

    @router.post('/attendance/record')
    async def record(request: Request):
        db, signer, key = configured()
        delegate = session(request, signer)
        data = await body(request)
        if data.get('confirm') is not True:
            raise HTTPException(400, 'Please confirm that this is your own attendance.')
        try:
            token = signer.loads(str(data.get('token', '')), max_age=600)
            if token['delegate'] != delegate or type(token['step']) is not int:
                raise BadSignature('Invalid attendance token')
        except (BadSignature, SignatureExpired, KeyError):
            raise HTTPException(409, 'Refresh your attendance status and try again.')
        await verify_delegate(db, delegate)
        event = db.record(delegate, token['step'], time.time())
        await db.deliver(event, key)
        return private_response({'recorded': True, 'message': 'Attendance saved. Scan again at your next attendance point.'})

    @router.post('/attendance/admin/courses', dependencies=[Depends(auth)])
    async def create_course(request: Request):
        db, signer, key = configured()
        if monday_enabled():
            raise HTTPException(409, 'Use the existing Monday.com Course ID. Separate attendance codes are disabled.')
        data = await body(request)
        try:
            day = datetime.strptime(data['day'], '%Y-%m-%d').date().isoformat()
            title, trainer = str(data['title']).strip(), str(data.get('trainer', '')).strip()
            if not title or len(title) > 150 or len(trainer) > 150:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HTTPException(400, 'Provide course title and date (YYYY-MM-DD).')
        code = secrets.token_hex(4).upper()
        with db.db() as conn:
            conn.execute('INSERT INTO course(code,title,day,trainer) VALUES(?,?,?,?)', (code, title, day, trainer))
        return private_response({'code': code, 'day': day, 'title': title})

    @router.post('/attendance/admin/check-course', dependencies=[Depends(auth)])
    async def check_course(request: Request):
        configured()
        data = await body(request)
        # Read-only preview for trainers; never opens attendance outside the booked dates.
        result = await monday_reader().resolve(str(data.get('code', '')).strip(), london_day(), enforce_day=False)
        result['scheduledToday'] = result['start'] <= london_day() <= result['end']
        return private_response(result)

    @router.get('/attendance/admin/status', dependencies=[Depends(auth)])
    async def admin_status():
        # No secrets, personal details or public roster.
        try:
            db, signer, key = configured()
        except HTTPException:
            return private_response({'ready': False})
        with db.db() as conn:
            counts = {r['state']: r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM event GROUP BY state')}
        return private_response({'ready': True, 'deliveryCounts': counts})

    return router
