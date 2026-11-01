"""MMVA attendance gateway. Disabled until explicitly configured; no public roster."""
from contextlib import contextmanager
import io
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from mmva_monday_courses import MondayCourses

FORM = '263045888335062'
ACTIONS = ['1. Morning sign-in', '2. Lunch sign-out', '3. Afternoon sign-in (after lunch)', '4. End-of-day sign-out']
ROOT = Path(__file__).parent / 'mmva_attendance_assets'


def london_day(now=None):
    return datetime.fromtimestamp(now if now is not None else time.time(), ZoneInfo('Europe/London')).date().isoformat()


class Ledger:
    def __init__(self, path, secret):
        self.path, self.secret = str(path), secret
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS course(code TEXT PRIMARY KEY,title TEXT NOT NULL,day TEXT NOT NULL,trainer TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS delegate(id TEXT PRIMARY KEY,course TEXT NOT NULL,identity TEXT NOT NULL,first TEXT NOT NULL,last TEXT NOT NULL,email TEXT NOT NULL,organisation TEXT NOT NULL,UNIQUE(course,identity));
            CREATE TABLE IF NOT EXISTS event(id TEXT PRIMARY KEY,delegate TEXT NOT NULL,step INTEGER NOT NULL,recorded_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',submission_id TEXT,UNIQUE(delegate,step));
            ''')
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
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email) or len(email) > 254 or any(not v or len(v) > 150 for v in values):
            raise HTTPException(400, 'Please enter your name, valid email and organisation.')
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
            return {'course': course['title'], 'day': course['day'], 'courseNumber': course['source_id'], 'step': len(rows),
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
            if rows and now - rows[-1]['recorded_at'] < 60:
                raise HTTPException(409, 'Your previous entry was just recorded. Return at the next attendance point.')
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
    trainer_login_rates = {}

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

    async def body(request):
        # JSON only prevents ordinary cross-site form CSRF; never enable permissive CORS.
        if request.headers.get('content-type', '').split(';')[0] != 'application/json':
            raise HTTPException(415, 'JSON required')
        raw = b''
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > 4096:
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
        data['token'] = signer.dumps({'delegate': delegate, 'step': data['step']}) if data['next'] else None
        return data

    def private_response(data):
        return JSONResponse(data, headers={'Cache-Control': 'no-store'})

    @router.get('/attendance', response_class=HTMLResponse)
    async def page():
        return HTMLResponse((ROOT / 'index.html').read_text(), headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer', 'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"})

    def trainer_signer():
        secret = os.getenv('MMVA_ATTENDANCE_SECRET', '')
        access_code = os.getenv('MMVA_ATTENDANCE_TRAINER_ACCESS_CODE', '')
        if len(secret) < 32 or len(access_code) < 12:
            raise HTTPException(503, 'Trainer course picker is not available yet. You can use a known Course ID below.')
        return URLSafeTimedSerializer(secret, salt='mmva-trainer-picker-v1'), access_code

    def require_trainer(request):
        signer, code = trainer_signer()
        try:
            token = signer.loads(request.cookies.get('mmva_trainer', ''), max_age=8 * 3600)
            if not hmac.compare_digest(str(token.get('version', '')), hashlib.sha256(code.encode()).hexdigest()):
                raise BadSignature('Trainer session expired')
        except (BadSignature, SignatureExpired, AttributeError):
            raise HTTPException(401, 'Enter the trainer access code to view course bookings.')

    @router.post('/attendance/trainer/login')
    async def trainer_login(request: Request):
        configured()
        signer, code = trainer_signer()
        ip = request.client.host if request.client else 'unknown'
        now = time.time()
        for old in list(trainer_login_rates):
            if trainer_login_rates[old][0] < now - 300:
                del trainer_login_rates[old]
        started, attempts = trainer_login_rates.get(ip, (now, 0))
        if attempts >= 10:
            raise HTTPException(429, 'Too many attempts. Please wait five minutes.')
        trainer_login_rates[ip] = (started, attempts + 1)
        data = await body(request)
        if not hmac.compare_digest(str(data.get('code', '')).encode(), code.encode()):
            raise HTTPException(401, 'Trainer access code was not recognised.')
        trainer_login_rates.pop(ip, None)
        response = private_response({'signedIn': True})
        response.set_cookie('mmva_trainer', signer.dumps({'version': hashlib.sha256(code.encode()).hexdigest()}), secure=True, httponly=True, samesite='strict', max_age=8 * 3600, path='/attendance/trainer')
        return response

    @router.post('/attendance/trainer/logout')
    async def trainer_logout(request: Request):
        await body(request)
        response = private_response({'signedIn': False})
        response.delete_cookie('mmva_trainer', path='/attendance/trainer', secure=True, httponly=True, samesite='strict')
        return response

    @router.post('/attendance/trainer/courses')
    async def trainer_courses(request: Request):
        configured()
        require_trainer(request)
        data = await body(request)
        start = str(data.get('from') or london_day())
        end = str(data.get('until') or (datetime.strptime(london_day(), '%Y-%m-%d') + timedelta(days=60)).date().isoformat())
        rows = await monday_reader().list_courses(start, end)
        return private_response({'courses': rows, 'from': start, 'until': end})

    @router.post('/attendance/trainer/course')
    async def trainer_course(request: Request):
        configured()
        require_trainer(request)
        data = await body(request)
        source = await monday_reader().resolve(str(data.get('code', '')).strip(), london_day(), enforce_day=False)
        return private_response({k: source[k] for k in ('id', 'title', 'client', 'venue', 'start', 'end')})

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
