"""QuickFile invoice audit adapter. Only fixed-origin GET requests are supported."""
import os
import re
from datetime import date, datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response

BASE_URL = 'https://api-beta.quickfile.co.uk'
ALLOWED_PATH = re.compile(r'/invoices(?:/[1-9][0-9]*)?')

class QuickFileRead:
    def __init__(self, token=None, transport=None):
        self.token = os.getenv('QUICKFILE_API_TOKEN', '') if token is None else token
        self.transport = transport

    async def get(self, path, params=None):
        if not ALLOWED_PATH.fullmatch(path):
            raise HTTPException(400, 'Unsupported QuickFile read operation.')
        if not self.token:
            raise HTTPException(503, 'QuickFile token is not configured.')
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False,
                                         transport=self.transport) as client:
                result = await client.get(BASE_URL + path, params=params,
                    headers={'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'})
        except httpx.HTTPError:
            raise HTTPException(502, 'QuickFile connection failed.') from None
        if result.status_code in (401, 403):
            raise HTTPException(502, 'QuickFile token expired, rejected or lacks invoice access.')
        if result.status_code == 429:
            raise HTTPException(429, 'QuickFile rate limit reached; retry later.')
        if result.status_code == 404:
            raise HTTPException(404, 'QuickFile invoice was not found.')
        if not result.is_success:
            raise HTTPException(502, 'QuickFile returned HTTP_' + str(result.status_code))
        try:
            payload = result.json()
        except ValueError:
            raise HTTPException(502, 'QuickFile returned invalid JSON.') from None
        if not isinstance(payload, dict):
            raise HTTPException(502, 'QuickFile returned an unexpected response.')
        return payload

    async def search(self, *, invoice_number=None, date_from=None, date_to=None,
                     status=None, offset=0, limit=20):
        if not 0 <= offset <= 1000000 or not 1 <= limit <= 100:
            raise HTTPException(422, 'Invalid pagination; limit must be 1-100.')
        for value in (date_from, date_to):
            if value is not None:
                try:
                    date.fromisoformat(str(value))
                except ValueError:
                    raise HTTPException(422, 'Use YYYY-MM-DD dates.') from None
        if date_from and date_to and str(date_from) > str(date_to):
            raise HTTPException(422, 'date_from must not exceed date_to.')
        params = {'type': 'invoice', 'include_deleted': 'false', 'offset': offset,
                  'limit': limit, 'order_column': 'issue_date', 'order_direction': 'desc'}
        params.update({k: str(v) for k,v in {'invoice_number': invoice_number,
            'date_from': date_from, 'date_to': date_to, 'status': status}.items() if v is not None})
        data = await self.get('/invoices', params)
        if not isinstance(data.get('data'), list):
            raise HTTPException(502, 'QuickFile returned an unexpected invoice list.')
        return data

    async def invoice(self, invoice_id):
        if not isinstance(invoice_id, int) or isinstance(invoice_id, bool) or invoice_id < 1:
            raise HTTPException(422, 'Use a positive invoice ID.')
        data = await self.get('/invoices/' + str(invoice_id))
        if data.get('id') != invoice_id:
            raise HTTPException(502, 'QuickFile returned a different invoice ID.')
        # Do not propagate tokenised preview links into audit results.
        data.pop('preview_uri', None)
        return data

    async def check(self):
        result = await self.search(limit=1)
        return {'connected': True, 'mode': 'read-only',
                'verified_at': datetime.now(timezone.utc).isoformat(),
                'invoice_read_verified': True, 'sample_count': len(result['data']),
                'writes_supported': False, 'attachments_verified': False}


def build_quickfile_router(require_key, reader=None):
    reader = reader or QuickFileRead()
    router = APIRouter(prefix='/quickfile', dependencies=[Depends(require_key)])

    @router.get('/status')
    async def status(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return await reader.check()

    @router.get('/invoices')
    async def invoices(response: Response, invoice_number: str | None = Query(None, max_length=100),
                       date_from: date | None = None, date_to: date | None = None,
                       status: str | None = Query(None, max_length=30),
                       offset: int = Query(0, ge=0, le=1000000),
                       limit: int = Query(20, ge=1, le=100)):
        response.headers['Cache-Control'] = 'no-store'
        return await reader.search(invoice_number=invoice_number, date_from=date_from,
            date_to=date_to, status=status, offset=offset, limit=limit)

    @router.get('/invoices/{invoice_id}')
    async def invoice(invoice_id: int, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return await reader.invoice(invoice_id)

    return router
