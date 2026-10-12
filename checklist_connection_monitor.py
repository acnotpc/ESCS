"""Background read-only source test; no employee records, sends or board writes."""
import asyncio
import logging
import os
import httpx
from datetime import datetime, timezone
from recruitment_checklist_access import ChecklistRead

log = logging.getLogger('escs.checklist.connection')
log.setLevel(logging.INFO)


class ConnectionMonitor:
    def __init__(self, reader=None, monday_transport=None):
        self.reader = reader
        self.monday_transport = monday_transport
        self.state = {'lastAttempt': None, 'lastSuccess': None, 'connectionOK': False,
                      'schemaReady': False, 'candidateEmailsEnabled': False,
                      'mondayBackgroundConfigured': bool(os.getenv('MONDAY_RECRUITMENT_API_TOKEN'))}
        self._last_log = None
        self._last_monday_log = None

    async def check_monday(self):
        token = os.getenv('MONDAY_RECRUITMENT_API_TOKEN', '')
        self.state['mondayBackgroundConfigured'] = bool(token)
        self.state['mondayConnectionOK'] = False
        if not token:
            line = 'Checklist Monday connection not configured; writes=false sending=false'
        else:
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                             transport=self.monday_transport) as client:
                    response = await client.post('https://api.monday.com/v2',
                        headers={'Authorization': token, 'API-Version': '2026-10'},
                        json={'query': 'query { boards(ids:[424237712]) { id columns(ids:["text_mm82vbx9","long_text_mm8220ks"]) { id type } } }'})
                payload = response.json()
                boards = payload.get('data', {}).get('boards', [])
                if not response.is_success or payload.get('errors') or len(boards) != 1:
                    raise RuntimeError('Monday connection rejected')
                columns = {c['id']: c['type'] for c in boards[0].get('columns', [])}
                if str(boards[0].get('id')) != '424237712' or columns != {
                        'text_mm82vbx9': 'text', 'long_text_mm8220ks': 'long_text'}:
                    raise RuntimeError('Recruitment board identity or schema changed')
                self.state.update(mondayConnectionOK=True,
                    mondayLastSuccess=datetime.now(timezone.utc).isoformat())
                line = 'Checklist Monday read connection verified: board=424237712 writes=false sending=false'
            except Exception:
                line = 'Checklist Monday connection unavailable; writes=false sending=false'
        if line != self._last_monday_log:
            log.info(line)
            self._last_monday_log = line

    async def check(self):
        self.state['lastAttempt'] = datetime.now(timezone.utc).isoformat()
        try:
            reader = self.reader or ChecklistRead(os.getenv('JOTFORM_RECRUITMENT_READ_API_KEY', ''))
            forms = await reader.inspect_schemas()
            if set(forms) != {'history', 'medical', 'working_time'}:
                raise RuntimeError('Incomplete pinned scope')
            signatures = sum(v.get('signatureSchemaValid') is True for v in forms.values())
            tracking = sum(v.get('trackingSchemaValid') is True for v in forms.values())
            choice = forms['working_time'].get('choiceSchemaValid') is True
            ready = signatures == 3 and tracking == 3 and choice
            self.state.update(connectionOK=True, schemaReady=ready,
                              lastSuccess=datetime.now(timezone.utc).isoformat(), error=None,
                              signaturesVerified=signatures, trackingVerified=tracking,
                              workingTimeChoiceVerified=choice)
            line = 'Checklist source connection verified: forms=3 signatures=%d tracking=%d choice=%s sending=false' % (
                signatures, tracking, str(choice).lower())
        except Exception:
            # Provider errors may contain credentials or HR content; never log them.
            self.state.update(connectionOK=False, schemaReady=False, error='source_connection_unavailable')
            line = 'Checklist source connection unavailable; sending=false'
        if line != self._last_log:
            log.info(line)
            self._last_log = line
        await self.check_monday()
        return dict(self.state)

    async def loop(self):
        while True:
            await self.check()
            await asyncio.sleep(300)
