"""Isolated MMVA Ecwid course-date housekeeping. No order/customer endpoints."""
import asyncio
import copy
import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Depends, HTTPException

STORE_ID = '76123907'
LONDON = ZoneInfo('Europe/London')
DATE_NAMES = {'date', 'dates', 'course date', 'course dates', 'training date', 'training dates'}
MONTHS = {name: i for i, name in enumerate(('january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december'), 1)}
DATE_RE = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(' + '|'.join(MONTHS) + r')\b(?:\s+(20\d{2}))?', re.I)
log = logging.getLogger('mmva.course_dates')


def start_date(label):
    if not isinstance(label, str) or len(label) > 500:
        return None
    match = DATE_RE.search(label)
    if not match:
        return None
    years = re.findall(r'\b20\d{2}\b', label)
    year = match[3] or (years[-1] if years else None)
    try:
        return date(int(year), MONTHS[match[2].lower()], int(match[1]))
    except (ValueError, TypeError):
        return None


def plan_product(product, today):
    options = copy.deepcopy(product.get('options', []))
    removed, warnings = [], []
    for index, option in enumerate(options):
        if (str(option.get('name', '')).strip().lower() not in DATE_NAMES
                or option.get('type') not in {'SELECT', 'RADIO', 'CHECKBOX'}
                or not isinstance(option.get('choices'), list)):
            continue
        choices = option['choices']
        starts = [start_date(c.get('text')) for c in choices]
        expired = [i for i, d in enumerate(starts) if d is not None and d < today]
        if not expired:
            continue
        if any(d is None for d in starts):
            warnings.append('Unrecognised date text; option left unchanged.')
            continue
        kept = [i for i in range(len(choices)) if i not in expired]
        if not kept:
            warnings.append('All course dates have passed; future dates or enquiry-only availability need review.')
            continue
        old_default = option.get('defaultChoice')
        option['choices'] = [choices[i] for i in kept]
        if isinstance(old_default, int) and not isinstance(old_default, bool) and old_default >= 0:
            option['defaultChoice'] = kept.index(old_default) if old_default in kept else None
        removed.append({'option_index': index, 'option_name': option['name'],
                        'removed': [choices[i]['text'] for i in expired], 'remaining': len(kept)})
    return {'id': product['id'], 'name': product.get('name', ''), 'options': options,
            'removals': removed, 'warnings': warnings, 'changed': bool(removed)}


def option_signature(options):
    """Ignore vendor timestamps/default omissions, but retain all functional fields."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in {'changeTime', 'id'}}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return clean(options)


class Ecwid:
    def __init__(self, token, transport=None):
        self.token = token
        self.transport = transport

    async def request(self, path, method='GET', payload=None):
        if not re.fullmatch(r'/products(?:/\d+(?:/combinations)?)?', path):
            raise RuntimeError('Only MMVA catalogue product endpoints are permitted')
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=self.transport) as client:
                response = await client.request(method, f'https://app.ecwid.com/api/v3/{STORE_ID}{path}',
                    headers={'Authorization': f'Bearer {self.token}'}, json=payload)
            if not response.is_success:
                raise RuntimeError(f'Ecwid catalogue request failed: HTTP {response.status_code}')
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Ecwid catalogue connection or response unavailable') from None

    async def products(self):
        result = []
        # The current shop has five products. Bound pagination and fail closed.
        for offset in range(0, 1000, 100):
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=self.transport) as client:
                    response = await client.get(f'https://app.ecwid.com/api/v3/{STORE_ID}/products',
                        headers={'Authorization': f'Bearer {self.token}'}, params={'limit': 100, 'offset': offset})
                if not response.is_success:
                    raise RuntimeError(f'Ecwid catalogue request failed: HTTP {response.status_code}')
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                raise RuntimeError('Ecwid catalogue connection or response unavailable') from None
            items = payload.get('items')
            if not isinstance(items, list) or not isinstance(payload.get('total'), int):
                raise RuntimeError('Invalid Ecwid catalogue page')
            result.extend(items)
            if len(result) >= payload['total']:
                if len(result) != payload['total']:
                    raise RuntimeError('Catalogue changed during pagination; retry required')
                if len({p['id'] for p in result}) != len(result):
                    raise RuntimeError('Duplicate catalogue products; retry required')
                return result
            if len(items) != 100:
                raise RuntimeError('Incomplete Ecwid catalogue page')
        raise RuntimeError('Catalogue safety limit exceeded')


class CourseDateWorker:
    def __init__(self, environ=None, reader=None, now=None):
        self.env = os.environ if environ is None else environ
        self.reader = reader
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = asyncio.Lock()
        self.db = None
        self.last_result = None
        self.last_error = None

    @property
    def enabled(self):
        return self.env.get('MMVA_COURSE_DATES_ENABLED') == 'true'

    @property
    def write_enabled(self):
        return self.env.get('MMVA_COURSE_DATES_WRITE_ENABLED') == 'true'

    @property
    def configured(self):
        return self.reader is not None or bool(self.env.get('MMVA_ECWID_API_TOKEN'))

    def ready(self):
        if self.db is None:
            path = Path(self.env.get('MMVA_COURSE_DATES_DB', '/var/data/mmva_course_dates.sqlite3'))
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(path), timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS mmva_date_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS mmva_date_runs (id TEXT PRIMARY KEY, day TEXT NOT NULL, result TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS mmva_date_updates (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, product_id INTEGER NOT NULL,
                before_options TEXT NOT NULL, after_options TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            os.chmod(path, 0o600)
        return self.db

    def status(self):
        paused = None
        if self.db is not None:
            row = self.db.execute("SELECT value FROM mmva_date_state WHERE key='paused'").fetchone()
            paused = bool(row and row['value'] == 'true')
        return {'enabled': self.enabled, 'write_enabled': self.write_enabled,
                'connection_configured': self.configured, 'store_id': STORE_ID,
                'schedule': 'Daily from 06:00 Europe/London; failed reads retry after five minutes',
                'expiry_rule': 'Course start date before today in Europe/London',
                'paused_for_review': paused, 'last_result': self.last_result, 'error': self.last_error}

    def set_state(self, key, value):
        self.ready().execute('INSERT OR REPLACE INTO mmva_date_state VALUES (?,?)', (key, value))
        self.db.commit()

    async def run(self, preview=False, scheduled=False):
        async with self.lock:
            current = self.now()
            if current.tzinfo is None:
                raise ValueError('Timezone-aware clock required')
            local = current.astimezone(LONDON)
            day = local.date().isoformat()
            if not preview and (not self.enabled or not self.write_enabled):
                return {'state': 'disabled', 'changes': 0}
            if not self.configured:
                self.last_error = 'Ecwid catalogue credential is not configured'
                return {'state': 'not_connected', 'changes': 0}
            if scheduled and local.hour < 6:
                return {'state': 'before_daily_window', 'changes': 0}
            db = self.ready()
            if scheduled:
                done = db.execute("SELECT value FROM mmva_date_state WHERE key='last_success_day'").fetchone()
                if done and done['value'] == day:
                    return {'state': 'already_checked', 'changes': 0}
            paused = db.execute("SELECT value FROM mmva_date_state WHERE key='paused'").fetchone()
            uncertain = db.execute("SELECT 1 FROM mmva_date_updates WHERE status IN ('prepared','requires_review') LIMIT 1").fetchone()
            if not preview and ((paused and paused['value'] == 'true') or uncertain):
                return {'state': 'review_required', 'changes': 0}
            owner = uuid.uuid4().hex
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute("SELECT value FROM mmva_date_state WHERE key='lease'").fetchone()
            if lease and datetime.fromisoformat(json.loads(lease['value'])['expires']) > current:
                db.rollback()
                return {'state': 'busy', 'changes': 0}
            db.execute('INSERT OR REPLACE INTO mmva_date_state VALUES (?,?)',
                ('lease', json.dumps({'owner': owner, 'expires': (current + timedelta(minutes=10)).isoformat()})))
            db.commit()
            result = {'state': 'preview' if preview else 'completed', 'day': day,
                      'checked_at': current.isoformat(), 'products_checked': 0, 'changes': 0,
                      'updates': [], 'warnings': [], 'failures': []}
            reader = self.reader or Ecwid(self.env['MMVA_ECWID_API_TOKEN'])
            try:
                rows = await reader.products()
                result['products_checked'] = len(rows)
                for product in rows:
                    plan = plan_product(product, local.date())
                    result['warnings'].extend({'id': product['id'], 'name': product.get('name'), 'message': w} for w in plan['warnings'])
                    if not plan['changed']:
                        continue
                    if preview:
                        result['updates'].append({k: plan[k] for k in ('id', 'name', 'removals')})
                        continue
                    fresh = await reader.request(f'/products/{product["id"]}')
                    if fresh.get('options', []) != product.get('options', []):
                        result['failures'].append({'id': product['id'], 'message': 'Course changed during check; no update made'})
                        continue
                    variants = await reader.request(f'/products/{product["id"]}/combinations')
                    if not isinstance(variants, list):
                        raise RuntimeError('Unexpected Ecwid variation response; no update made')
                    if variants:
                        result['warnings'].append({'id': product['id'], 'message': 'Course has product variations; review required'})
                        continue
                    audit_id = uuid.uuid4().hex
                    with db:
                        db.execute('INSERT INTO mmva_date_updates VALUES (?,?,?,?,?,?,?)',
                            (audit_id, owner, product['id'], json.dumps(fresh.get('options', [])),
                             json.dumps(plan['options']), 'prepared', current.isoformat()))
                    try:
                        await reader.request(f'/products/{product["id"]}', 'PUT', {'options': plan['options']})
                        after = await reader.request(f'/products/{product["id"]}')
                        if (option_signature(after.get('options', [])) != option_signature(plan['options'])
                                or after.get('price') != fresh.get('price')
                                or after.get('enabled') != fresh.get('enabled')):
                            raise RuntimeError('Date update readback mismatch')
                    except Exception:
                        with db:
                            db.execute("UPDATE mmva_date_updates SET status='requires_review' WHERE id=?", (audit_id,))
                        self.set_state('paused', 'true')
                        result['failures'].append({'id': product['id'], 'message': 'Update outcome requires review; further writes paused'})
                        break
                    with db:
                        db.execute("UPDATE mmva_date_updates SET status='verified' WHERE id=?", (audit_id,))
                    result['updates'].append({k: plan[k] for k in ('id', 'name', 'removals')})
                    result['changes'] += sum(len(r['removed']) for r in plan['removals'])
                if result['failures']:
                    result['state'] = 'review_required'
                elif not preview:
                    self.set_state('last_success_day', day)
                self.last_error = None
            except Exception:
                # Provider exceptions may include credentials; expose only this safe classification.
                result['state'] = 'connection_or_schema_error'
                self.last_error = 'MMVA date check needs catalogue connection or schema review'
            finally:
                with db:
                    lease = db.execute("SELECT value FROM mmva_date_state WHERE key='lease'").fetchone()
                    if lease and json.loads(lease['value'])['owner'] == owner:
                        db.execute("DELETE FROM mmva_date_state WHERE key='lease'")
                    db.execute('INSERT OR REPLACE INTO mmva_date_runs VALUES (?,?,?)', (owner, day, json.dumps(result)))
                self.last_result = result
            return result

    async def reconcile(self):
        """Read Ecwid to classify uncertain prior writes; never writes to Ecwid."""
        async with self.lock:
            if not self.configured:
                return {'state': 'not_connected'}
            db = self.ready()
            reader = self.reader or Ecwid(self.env['MMVA_ECWID_API_TOKEN'])
            results = []
            for row in db.execute("SELECT * FROM mmva_date_updates WHERE status IN ('prepared','requires_review')").fetchall():
                fresh = await reader.request(f'/products/{row["product_id"]}')
                options = option_signature(fresh.get('options', []))
                if options == option_signature(json.loads(row['after_options'])):
                    state = 'verified'
                elif options == option_signature(json.loads(row['before_options'])):
                    state = 'not_applied'
                else:
                    state = 'requires_review'
                with db:
                    db.execute('UPDATE mmva_date_updates SET status=? WHERE id=?', (state, row['id']))
                results.append({'product_id': row['product_id'], 'state': state})
            remaining = db.execute("SELECT 1 FROM mmva_date_updates WHERE status IN ('prepared','requires_review') LIMIT 1").fetchone()
            if not remaining:
                self.set_state('paused', 'false')
            return {'state': 'review_required' if remaining else 'reconciled', 'updates': results, 'ecwid_writes': 0}

    async def loop(self):
        while True:
            try:
                if self.enabled and self.write_enabled:
                    result = await self.run(scheduled=True)
                    if result['state'] not in {'already_checked', 'before_daily_window'}:
                        log.info('MMVA course dates: state=%s changes=%s', result['state'], result.get('changes', 0))
            except Exception:
                self.last_error = 'MMVA course-date worker needs review'
                log.warning('MMVA course-date worker needs review')
            await asyncio.sleep(300)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None


def build_router(worker, require_key):
    router = APIRouter(prefix='/mmva/course-dates', dependencies=[Depends(require_key)])

    @router.get('/status')
    async def status():
        return worker.status()

    @router.post('/preview')
    async def preview():
        return await worker.run(preview=True)

    @router.post('/run')
    async def run():
        return await worker.run()

    @router.post('/reconcile')
    async def reconcile():
        try:
            return await worker.reconcile()
        except Exception:
            raise HTTPException(503, 'Ecwid catalogue reconciliation unavailable') from None

    return router
