"""Draft board summary adapter. Not wired into the worker or production image."""
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

import httpx
from reference_chasing import SOURCE_FORM, answer, stamp

BOARD = '424237712'
SOURCE_COLUMN = 'text_mm82vbx9'
SUMMARY_COLUMN = 'long_text_mm8220ks'
ACTIVE = {'Pending Review', 'To be processed by Recruitment', 'Awaiting DBS',
          'DBS awaiting ZC approval', 'Awaiting 3rd ref for DBS', 'Awaiting Training',
          'Awaiting Taster Shift', 'Awaiting 2nd Training', 'Awaiting Start Date',
          'Talent Pool - Pending Position'}
FIELDS = '"text_mm82vbx9","long_text_mm8220ks","last_name","status53"'
QUERIES = {
    'schema': 'query { boards(ids:[424237712]) { id columns(ids:[' + FIELDS + ']) { id type } } }',
    'find': 'query($source:String!) { items_page_by_column_values(board_id:424237712,limit:100,columns:[{column_id:"text_mm82vbx9",column_values:[$source]}]) { cursor items { id name board { id } column_values(ids:[' + FIELDS + ']) { id text } } } }',
    'read': 'query($ids:[ID!]) { items(ids:$ids) { id name board { id } column_values(ids:[' + FIELDS + ']) { id text } } }',
    'write': 'mutation($item:ID!,$values:JSON!) { change_multiple_column_values(board_id:424237712,item_id:$item,column_values:$values) { id } }',
}


class SyncHold(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def normal(value):
    return ' '.join(str(value or '').split()).casefold()


def monday_sync_reviews(ledger):
    exists = ledger.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='monday_sync'").fetchone()
    if not exists:
        return []
    return [dict(row) for row in ledger.db.execute(
        "SELECT * FROM monday_sync WHERE state IN ('held','connectionErrors') ORDER BY source_id")]


def check_target(item, source_id, candidate):
    if not isinstance(item, dict) or str(item.get('board', {}).get('id')) != BOARD:
        raise SyncHold('wrong_board')
    if not str(item.get('id', '')).isdigit():
        raise SyncHold('invalid_item')
    values = {v['id']: v.get('text') for v in item.get('column_values', [])}
    if str(values.get(SOURCE_COLUMN) or '').strip() != source_id:
        raise SyncHold('source_link_changed')
    names = {normal(item.get('name')), normal(str(item.get('name') or '') + ' ' + str(values.get('last_name') or ''))}
    if normal(candidate) not in names:
        raise SyncHold('candidate_match')
    if values.get('status53') not in ACTIVE:
        raise SyncHold('not_active_recruitment')
    return values


def summary(rows, needs_source_review):
    labels = {'pending': 'Awaiting response', 'received': 'Received; staff verification required',
              'reply_review': 'Reply requires staff review', 'send_unknown': 'Delivery outcome requires review',
              'exhausted': 'Reminder limit reached; staff follow-up required',
              'consent_stopped': 'Chasing stopped after consent or nomination changed',
              'source_review': 'Checklist details require review', 'sending': 'Send outcome pending'}
    if len(rows) > 2 or len({r['slot'] for r in rows}) != len(rows):
        raise SyncHold('request_shape')
    text = ['Reference automation: ' + str(sum(r['state'] == 'received' for r in rows)) +
            ' of ' + str(len(rows)) + ' received. Receipt is not professional verification.']
    if needs_source_review:
        text.append('Checklist details require staff review; automatic requests are held.')
    for row in sorted(rows, key=lambda r: r['slot']):
        if row['state'] not in labels or row['slot'] not in (0, 1):
            raise SyncHold('request_shape')
        text.append('Referee ' + str(row['slot'] + 1) + ': ' + labels[row['state']] +
                    '. Confirmed emails sent: ' + str(row['sends']) + '.')
    return '\n'.join(text)


class MondayReferenceSync:
    def __init__(self, token, write_enabled=False, transport=None):
        if not token:
            raise RuntimeError('Approved stored Monday connection required')
        self._token, self.write_enabled, self.transport = token, write_enabled, transport

    async def _call(self, operation, variables=None):
        if operation not in QUERIES or (operation == 'write' and not self.write_enabled):
            raise RuntimeError('Unsupported or disabled board operation')
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False, transport=self.transport) as client:
                r = await client.post('https://api.monday.com/v2',
                    headers={'Authorization': self._token, 'API-Version': '2026-10'},
                    json={'query': QUERIES[operation], 'variables': variables or {}})
            payload = r.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError('Recruitment board connection failed') from None
        if not r.is_success or not isinstance(payload, dict) or payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise RuntimeError('Recruitment board operation rejected')
        return payload['data']

    async def schema(self):
        boards = (await self._call('schema')).get('boards', [])
        if len(boards) != 1 or str(boards[0].get('id')) != BOARD:
            raise RuntimeError('Recruitment board identity failed')
        fields = {v['id']: v['type'] for v in boards[0].get('columns', [])}
        if fields != {SOURCE_COLUMN: 'text', SUMMARY_COLUMN: 'long_text', 'last_name': 'text', 'status53': 'status'}:
            raise RuntimeError('Recruitment board schema changed; updates held')

    async def source_name(self, ledger, jotform, source_id):
        if not source_id.isdigit() or int(source_id) <= ledger.baseline:
            raise SyncHold('source_identity')
        value = await jotform._get('/submission/' + source_id)
        if (not isinstance(value, dict) or str(value.get('id')) != source_id or
                str(value.get('form_id')) != SOURCE_FORM or value.get('status') != 'ACTIVE'):
            raise SyncHold('source_identity')
        name = answer(value, 'Name')
        if not isinstance(name, dict) or not all(isinstance(name.get(k, ''), str) for k in ('first', 'last')):
            raise SyncHold('source_identity')
        name = (name.get('first', '').strip() + ' ' + name.get('last', '').strip()).strip()
        if not name or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise SyncHold('source_identity')
        return name

    async def run(self, ledger, jotform):
        # Additive audit, distinct from reference sending and receipt state.
        ledger.db.execute('''CREATE TABLE IF NOT EXISTS monday_sync(
            source_id TEXT PRIMARY KEY,item_id TEXT,state TEXT NOT NULL,
            reason TEXT,last_hash TEXT,checked_at TEXT NOT NULL)''')
        ledger.db.commit()
        await self.schema()
        groups = defaultdict(list)
        for row in ledger.rows():
            groups[row['source_id']].append(row)
        reviews = {row['source_id'] for row in ledger.source_reviews()}
        result = {'updated': 0, 'unchanged': 0, 'ready': 0, 'held': 0, 'connectionErrors': 0}
        for source_id in sorted(set(groups) | reviews, key=int):
            item_id, digest, state, reason = None, None, 'held', None
            try:
                candidate = await self.source_name(ledger, jotform, source_id)
                page = (await self._call('find', {'source': source_id})).get('items_page_by_column_values', {})
                items = page.get('items', [])
                if page.get('cursor') or len(items) != 1:
                    raise SyncHold('missing_or_duplicate_source_link')
                check_target(items[0], source_id, candidate)
                item_id = str(items[0]['id'])
                current = (await self._call('read', {'ids': [item_id]})).get('items', [])
                if len(current) != 1 or str(current[0].get('id')) != item_id:
                    raise SyncHold('item_identity')
                values = check_target(current[0], source_id, candidate)
                text = summary(groups[source_id], source_id in reviews)
                digest = hashlib.sha256(text.encode()).hexdigest()
                if values.get(SUMMARY_COLUMN) == text:
                    state = 'unchanged'
                elif not self.write_enabled:
                    state = 'ready'
                else:
                    payload = json.dumps({SUMMARY_COLUMN: {'text': text}})
                    changed = await self._call('write', {'item': item_id, 'values': payload})
                    if str(changed.get('change_multiple_column_values', {}).get('id')) != item_id:
                        raise RuntimeError('Board update outcome unconfirmed')
                    after = (await self._call('read', {'ids': [item_id]})).get('items', [])
                    if len(after) != 1 or str(after[0].get('id')) != item_id or check_target(after[0], source_id, candidate).get(SUMMARY_COLUMN) != text:
                        raise RuntimeError('Board update read-back failed')
                    state = 'updated'
            except SyncHold as error:
                reason = error.reason
            except ValueError:
                reason = 'source_identity'
            except RuntimeError:
                state, reason = 'connectionErrors', 'connection_failure'
            result[state] += 1
            with ledger.db:
                ledger.db.execute('INSERT OR REPLACE INTO monday_sync VALUES(?,?,?,?,?,?)',
                    (source_id, item_id, state, reason, digest, stamp(datetime.now(timezone.utc))))
        return result
