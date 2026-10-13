"""SwitchboardFREE call metadata -> Bitrix CRM; no recordings or transcripts."""
import asyncio
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request


def phone(value):
    value = str(value or '').strip()
    if not re.fullmatch(r'[+\d ()-]+', value):
        return None
    digits = re.sub(r'\D', '', value)
    if digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('0'):
        digits = '44' + digits[1:]
    return '+' + digits if 8 <= len(digits) <= 15 else None


class Bridge:
    def __init__(self, path=None, rest=None, secret=None, enabled=None, write=None):
        self.path = path or os.getenv('SWITCHBOARD_STATE_DB', '/var/data/switchboard.sqlite3')
        self.rest = rest if rest is not None else os.getenv('SWITCHBOARD_BITRIX_REST_URL', os.getenv('BITRIX_REST_WEBHOOK_URL', ''))
        self.secret = secret if secret is not None else os.getenv('SWITCHBOARD_WEBHOOK_SECRET', '')
        self.enabled = enabled if enabled is not None else os.getenv('SWITCHBOARD_ENABLED') == 'true'
        self.write = write if write is not None else os.getenv('SWITCHBOARD_WRITE_ENABLED') == 'true'
        self.db = None
        self.lock = asyncio.Lock()
        self.ready = False
        self.last_error = None
        self.last_rejection = None

    def connect(self):
        if self.db is None:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(self.path)
            self.db.row_factory = sqlite3.Row
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS calls (
                  ref TEXT PRIMARY KEY, item TEXT NOT NULL, payload TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'waiting', error TEXT,
                  received TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS effects (
                  ref TEXT, kind TEXT, state TEXT, result TEXT,
                  PRIMARY KEY(ref, kind));
            ''')
            self.db.commit()
        return self.db

    async def api(self, method, params):
        parts = urlsplit(self.rest)
        if parts.scheme != 'https' or parts.hostname != 'escs.bitrix24.com' or not re.fullmatch(r'/rest/1/[^/]+/?', parts.path):
            raise RuntimeError('Andy Challoner ESCS webhook required')
        async with httpx.AsyncClient(timeout=15) as client:
            result = await client.post(self.rest.rstrip('/') + '/' + method, json=params)
            result.raise_for_status()
            body = result.json()
        if 'error' in body or 'result' not in body:
            raise RuntimeError('Bitrix request failed')
        return body['result']

    def receive(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid event')
        item, action, ref = (payload.get(k) for k in ('item', 'action', 'ref'))
        if item not in {'inbound_call', 'outbound_call'} or action not in {'call_start', 'call_answered', 'call_complete'} or not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9-]{8,80}', ref):
            raise ValueError('Unsupported event')
        fields = {'item', 'ref', 'startDateTime', 'answeredDateTime', 'completedDateTime', 'calledNumber', 'callersNumber', 'numberDialled', 'cliDisplayed', 'answeredBy', 'secondsToAnswer', 'totalCallDuration', 'talkDuration', 'Result', 'result', 'callOriginator', 'callOriginatorDesc'}
        clean = {k: v for k, v in payload.items() if k in fields and isinstance(v, (str, int)) and len(str(v)) <= 200}
        db = self.connect()
        row = db.execute('SELECT * FROM calls WHERE ref=?', (ref,)).fetchone()
        if row and row['item'] != item:
            raise ValueError('Conflicting call reference')
        # Once complete, duplicate events cannot overwrite the authoritative record.
        if row and row['state'] not in {'waiting', 'pending'}:
            return 'duplicate'
        merged = json.loads(row['payload']) if row else {}
        merged.update(clean)
        state = 'pending' if action == 'call_complete' or (row and row['state'] == 'pending') else 'waiting'
        db.execute('INSERT INTO calls(ref,item,payload,state) VALUES(?,?,?,?) ON CONFLICT(ref) DO UPDATE SET payload=excluded.payload,state=excluded.state', (ref, item, json.dumps(merged), state))
        db.commit()
        return state

    async def effect(self, ref, kind, method, params):
        db = self.connect()
        previous = db.execute('SELECT * FROM effects WHERE ref=? AND kind=?', (ref, kind)).fetchone()
        if previous:
            if previous['state'] == 'done':
                return json.loads(previous['result'])
            raise RuntimeError('Uncertain prior write requires review')
        db.execute('INSERT INTO effects VALUES(?,?,?,?)', (ref, kind, 'started', None))
        db.commit()
        # Commit intent before the remote write. A lost response must never cause
        # an automatic second contact/comment on retry or restart.
        result = await self.api(method, params)
        db.execute('UPDATE effects SET state=?,result=? WHERE ref=? AND kind=?', ('done', json.dumps(result), ref, kind))
        db.commit()
        return result

    async def process(self, row):
        db = self.connect()
        data = json.loads(row['payload'])
        ref = row['ref']
        number = phone(data.get('callersNumber') if row['item'] == 'inbound_call' else data.get('numberDialled'))
        if not number:
            if row['item'] == 'outbound_call' and not data.get('numberDialled'):
                received = datetime.fromisoformat(row['received']).replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - received).total_seconds() < 600:
                    return  # Allow out-of-order start events to supply the number.
            db.execute('UPDATE calls SET state=?,error=? WHERE ref=?', ('review', 'Withheld, invalid number or missing start event', ref))
            db.commit()
            return
        found = await self.api('crm.duplicate.findbycomm', {'type': 'PHONE', 'values': [number]})
        if found == []:
            found = {}
        if not isinstance(found, dict):
            raise RuntimeError('Invalid CRM match response')
        contacts = list(dict.fromkeys(found.get('CONTACT', [])))
        companies = list(dict.fromkeys(found.get('COMPANY', [])))
        if len(contacts) > 1 or len(companies) > 1:
            db.execute('UPDATE calls SET state=?,error=? WHERE ref=?', ('review', 'Ambiguous shared number', ref))
            db.commit()
            return
        targets = []
        if contacts:
            linked = await self.api('crm.contact.company.items.get', {'id': contacts[0]})
            linked_ids = {int(x['COMPANY_ID']) for x in linked}
            if companies and linked_ids and int(companies[0]) not in linked_ids:
                db.execute('UPDATE calls SET state=?,error=? WHERE ref=?', ('review', 'Contact/company match conflict', ref))
                db.commit()
                return
            targets.append(('contact', contacts[0]))
            if not companies and len(linked_ids) == 1:
                companies = list(linked_ids)
        if companies:
            targets.append(('company', companies[0]))
        if not self.write:
            db.execute('UPDATE calls SET state=? WHERE ref=?', ('preview', ref))
            db.commit()
            return
        if not targets:
            created = await self.effect(ref, 'contact', 'crm.contact.add', {'fields': {
                'NAME': 'Telephone caller ' + number, 'OPENED': 'N', 'ASSIGNED_BY_ID': 1,
                'PHONE': [{'VALUE': number, 'VALUE_TYPE': 'WORK'}],
                'ORIGINATOR_ID': 'SwitchboardFREE', 'ORIGIN_ID': number,
                'COMMENTS': 'Provisional telephone contact. Confirm name and organisation before linking a company.'}})
            targets.append(('contact', created))
        outcome = data.get('Result', data.get('result', 'Unknown'))
        direction = 'Inbound' if row['item'] == 'inbound_call' else 'Outbound'
        comment = '\n'.join([
            f'SwitchboardFREE {direction} call: {outcome}', f'Number: {number}',
            f'Start: {data.get("startDateTime", "Not supplied")}',
            f'End: {data.get("completedDateTime", "Not supplied")}',
            f'Duration: {data.get("totalCallDuration", "Not supplied")} seconds',
            f'Call reference: {ref}',
        ])
        for entity, entity_id in targets:
            await self.effect(ref, f'{entity}:{entity_id}', 'crm.timeline.comment.add', {'fields': {'ENTITY_TYPE': entity, 'ENTITY_ID': entity_id, 'COMMENT': comment}})
        db.execute('UPDATE calls SET state=?,error=NULL WHERE ref=?', ('done', ref))
        db.commit()

    async def flush(self):
        async with self.lock:
            if not self.enabled or not self.secret or not self.rest:
                return
            db = self.connect()
            try:
                user = await self.api('profile', {})
                if int(user['ID']) != 1:
                    raise RuntimeError('Andy Challoner account required')
                await self.api('crm.contact.fields', {})
                self.ready = True
                self.last_error = None
            except Exception:
                self.ready = False
                self.last_error = 'CRM connection unavailable or permissions missing'
                return
            rows = db.execute("SELECT * FROM calls WHERE state='pending' ORDER BY received LIMIT 20").fetchall()
            for row in rows:
                try:
                    await self.process(row)
                except Exception:
                    uncertain = db.execute("SELECT 1 FROM effects WHERE ref=? AND state='started'", (row['ref'],)).fetchone()
                    if uncertain:
                        db.execute('UPDATE calls SET state=?,error=? WHERE ref=?', ('review', 'Remote write outcome uncertain; reconcile before retry', row['ref']))
                        db.commit()
                    self.last_error = 'Call processing requires retry or review'

    async def loop(self):
        while True:
            try:
                await self.flush()
            except Exception:
                self.last_error = 'Bridge storage or connection unavailable'
            await asyncio.sleep(15)

    def status(self):
        counts = dict(self.connect().execute('SELECT state,COUNT(*) FROM calls GROUP BY state').fetchall())
        return {'enabled': self.enabled, 'write_enabled': self.write, 'crm_ready': self.ready,
                'secret_configured': bool(self.secret), 'counts': counts, 'last_error': self.last_error,
                'last_rejection': self.last_rejection}


def build_router(bridge, auth):
    router = APIRouter(prefix='/switchboard')

    @router.post('/events')
    async def events(request: Request):
        if not bridge.enabled or not bridge.secret:
            raise HTTPException(503, 'Bridge is not configured')
        supplied = request.headers.get('x-sbf-secret', '')
        if not supplied or not secrets.compare_digest(supplied, bridge.secret):
            bridge.last_rejection = {'secret_header_present': bool(supplied),
                                     'secret_header_length': len(supplied),
                                     'auth_header_names': [key for key in request.headers if 'secret' in key or 'signature' in key or key.startswith('x-sbf')]}
            raise HTTPException(401, 'Unauthorized')
        if request.headers.get('content-length', '').isdigit() and int(request.headers['content-length']) > 16384:
            raise HTTPException(413, 'Event too large')
        body = await request.body()
        if len(body) > 16384:
            raise HTTPException(413, 'Event too large')
        try:
            return {'accepted': True, 'state': bridge.receive(json.loads(body))}
        except (ValueError, TypeError):
            raise HTTPException(400, 'Invalid call event')

    @router.get('/status', dependencies=[Depends(auth)])
    async def status():
        return bridge.status()

    @router.get('/delivery-status')
    async def delivery_status(request: Request):
        supplied = request.headers.get('x-sbf-secret', '')
        if not bridge.secret or not supplied or not secrets.compare_digest(supplied, bridge.secret):
            raise HTTPException(401, 'Unauthorized')
        return bridge.status()

    @router.get('/health')
    async def health():
        return {'configured': bridge.enabled and bool(bridge.secret and bridge.rest),
                'crm_ready': bridge.ready, 'write_enabled': bridge.write}

    return router

