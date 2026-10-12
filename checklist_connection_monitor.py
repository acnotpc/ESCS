"""Background read-only source test; no employee records, sends or board writes."""
import asyncio
import logging
import os
from datetime import datetime, timezone
from recruitment_checklist_access import ChecklistRead

log = logging.getLogger('escs.checklist.connection')
log.setLevel(logging.INFO)


class ConnectionMonitor:
    def __init__(self, reader=None):
        self.reader = reader
        self.state = {'lastAttempt': None, 'lastSuccess': None, 'connectionOK': False,
                      'schemaReady': False, 'candidateEmailsEnabled': False,
                      'mondayBackgroundConfigured': bool(os.getenv('MONDAY_RECRUITMENT_API_TOKEN'))}
        self._last_log = None

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
        return dict(self.state)

    async def loop(self):
        while True:
            await self.check()
            await asyncio.sleep(300)
