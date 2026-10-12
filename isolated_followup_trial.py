"""Isolated synthetic live-form receipt trial. Never sends or writes to Monday."""
import asyncio
import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
import httpx
from recruitment_checklist import Checklist

FORM = '262844476768071'
REQUEST_HASHES = {'history': '333fb2144b47aaf471b26acf1f8ed9afacd4df749856d2f0265770d6f463b59e', 'working_time': 'e033817a4a9caa9e8f6ba840a52af75f83d3d3ecc8fee13ffed51aff24ffcee9'}
TRACKING = 'ESCS follow-up request ID'
COMPLETE = 'Test information supplied?'
ANSWER = 'Yes - synthetic test item supplied'
log = logging.getLogger('escs.checklist.trial')
log.setLevel(logging.INFO)


def test_receipt(record, schema):
    if (not isinstance(record, dict) or str(record.get('form_id')) != FORM
            or record.get('status') != 'ACTIVE' or not str(record.get('id', '')).isdigit()):
        return None
    def select(values, title):
        matches = [v for v in values.values() if isinstance(v, dict) and v.get('text') == title]
        return matches[0] if len(matches) == 1 else None
    answers = record.get('answers')
    if not isinstance(answers, dict):
        return None
    q = select(schema, TRACKING)
    a = select(answers, TRACKING)
    done = select(answers, COMPLETE)
    if not q or not a or not done or a.get('type') != 'control_textbox' or done.get('answer') != ANSWER:
        return None
    token = a.get('answer')
    if not isinstance(token, str) or len(token) != 32:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    kind = next((k for k, expected in REQUEST_HASHES.items() if digest == expected), None)
    return (kind, str(record['id'])) if kind else None


class IsolatedTrial:
    def __init__(self, path='/var/data/recruitment-checklist-test.sqlite', transport=None):
        self.path, self.transport = path, transport
        self.state = {'receipts': 0, 'outstanding': 2, 'boardRead': False,
                      'stopped': False, 'restartStopped': False, 'sends': 0, 'boardWrites': 0}
        self.last_log = None

    async def get(self, path):
        if path not in {'/form/'+FORM+'/questions', '/form/'+FORM+'/submissions'}:
            raise ValueError('Unsupported isolated test read')
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            r = await client.get('https://eu-api.jotform.com'+path,
                headers={'APIKEY': os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY', '')},
                params={'orderby':'id', 'limit':100} if path.endswith('/submissions') else None)
        p = r.json()
        if not r.is_success or not isinstance(p, dict) or p.get('responseCode') != 200:
            raise RuntimeError('Isolated source read rejected')
        return p.get('content')

    async def check_board(self):
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
            r = await client.post('https://api.monday.com/v2',
                headers={'Authorization':os.getenv('MONDAY_RECRUITMENT_API_TOKEN', ''), 'API-Version':'2026-10'},
                json={'query':'query { items(ids:[1005891310]) { id name board { id } } }'})
        p = r.json()
        rows = p.get('data', {}).get('items', [])
        if (not r.is_success or p.get('errors') or len(rows) != 1 or
                str(rows[0].get('id')) != '1005891310' or
                str(rows[0].get('board', {}).get('id')) != '424237712' or
                rows[0].get('name') != 'Andy Challoner'):
            raise RuntimeError('Trial board record did not verify')

    async def tick(self):
        ledger = None
        try:
            await self.check_board()
            self.state['boardRead'] = True
            schema = await self.get('/form/'+FORM+'/questions')
            if not isinstance(schema, dict):
                raise RuntimeError('Test schema unavailable')
            track = [q for q in schema.values() if isinstance(q, dict) and q.get('text') == TRACKING]
            if (len(track) != 1 or track[0].get('type') != 'control_textbox'
                    or track[0].get('hidden') != 'Yes' or track[0].get('required') == 'Yes'):
                raise RuntimeError('Test tracking schema changed')
            rows = await self.get('/form/'+FORM+'/submissions')
            if not isinstance(rows, list) or len(rows) >= 100:
                raise RuntimeError('Incomplete isolated receipt scan')
            ledger = Checklist(self.path)
            now = datetime.now(timezone.utc)
            ledger.request('123', REQUEST_HASHES, now, verified_link=True, staff_requested=True)
            actions = [test_receipt(row, schema) for row in rows]
            for action in actions:
                if action:
                    ledger.receive('123', action[0], action[1], exact_match=True)
            received = ledger.db.execute("SELECT COUNT(*) FROM checklist_requirement WHERE candidate_id='123' AND state='received_review'").fetchone()[0]
            self.state.update(receipts=received, outstanding=2-received, stopped=received == 2)
            ledger.db.close()
            ledger = None
            if received == 2:
                reopened = Checklist(self.path)
                try:
                    # Repeated enrollment and a simulated future due time cannot reopen receipt.
                    reopened.request('123', REQUEST_HASHES, now, verified_link=True, staff_requested=True)
                    self.state['restartStopped'] = reopened.plan('123', now+timedelta(days=30)) == []
                finally:
                    reopened.db.close()
            self.state['error'] = None
            line = 'Follow-up isolated trial: receipts=%d outstanding=%d boardRead=true stopped=%s restartStopped=%s sends=0 boardWrites=0' % (
                received, 2-received, str(self.state['stopped']).lower(), str(self.state['restartStopped']).lower())
        except Exception:
            self.state['error'] = 'trial_requires_review'
            line = 'Follow-up isolated trial requires review; sends=0 boardWrites=0'
        finally:
            if ledger is not None:
                ledger.db.close()
        if line != self.last_log:
            log.info(line)
            self.last_log = line
        return dict(self.state)

    async def loop(self):
        for _ in range(30):  # Bounded trial: stop after completion or 15 minutes.
            result = await self.tick()
            if result['stopped']:
                return
            await asyncio.sleep(30)

