"""Read-only exact Course ID lookup in the MMVA booking register."""
import json
import re
from datetime import date

import httpx
from fastapi import HTTPException

BOARD = '4727626523'
ACTIVE = {'Booked', 'Confirmed with Client', 'Trainer Allocated', 'Course Ongoing'}
QUERY = '''query AttendanceCourse($ids: [ID!]!) {
 items(ids: $ids) { id name state board { id }
 column_values(ids: ["date", "date2", "timeline", "status", "type_of_training", "dropdown4", "text34", "tags", "address__1"]) { id text value }
 }
}'''


class MondayCourses:
    def __init__(self, token, transport=None):
        self.token, self.transport = token, transport

    async def resolve(self, code, day, enforce_day=True):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', str(code)):
            raise HTTPException(400, 'Enter the numeric Course ID from your Monday.com booking.')
        if not self.token:
            raise HTTPException(503, 'Course verification is unavailable. Please speak to your trainer.')
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False, transport=self.transport) as client:
                r = await client.post('https://api.monday.com/v2', headers={'Authorization': self.token}, json={'query': QUERY, 'variables': {'ids': [str(code)]}})
            j = r.json()
            if r.status_code != 200 or j.get('errors') or not isinstance(j.get('data'), dict):
                raise ValueError()
            items = j['data'].get('items')
            if not isinstance(items, list):
                raise ValueError()
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise HTTPException(503, 'Course verification is unavailable. Please speak to your trainer.')
        if len(items) != 1 or str(items[0].get('id')) != str(code) or str(items[0].get('board', {}).get('id')) != BOARD or items[0].get('state') != 'active':
            raise HTTPException(400, 'Course ID not found in the active MMVA booking register.')
        return parse_course(items[0], str(code), day, enforce_day)



    async def list_courses(self, from_day, until_day):
        try:
            first, last = date.fromisoformat(from_day), date.fromisoformat(until_day)
            if last < first or (last-first).days > 90:
                raise ValueError()
        except (TypeError, ValueError):
            raise HTTPException(400, 'Choose a date range of up to 90 days.')
        if not self.token:
            raise HTTPException(503, 'Course list is unavailable. Please use a known Course ID.')
        fields = 'id name state board { id } column_values(ids: ["date","date2","timeline","status","type_of_training","dropdown4","text34","tags","address__1"]) { id text value }'
        initial = 'query { boards(ids: [' + BOARD + ']) { id items_page(limit: 500) { cursor items { ' + fields + ' } } } }'
        following = 'query($cursor: String!) { next_items_page(limit: 500, cursor: $cursor) { cursor items { ' + fields + ' } } }'
        result, cursor = {}, None
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False, transport=self.transport) as client:
                for page in range(10):
                    r = await client.post('https://api.monday.com/v2', headers={'Authorization': self.token}, json={'query': following if cursor else initial, 'variables': {'cursor': cursor} if cursor else {}})
                    j = r.json()
                    if r.status_code != 200 or j.get('errors') or not isinstance(j.get('data'), dict):
                        raise ValueError()
                    if not cursor:
                        boards = j['data'].get('boards', [])
                        if len(boards) != 1 or str(boards[0].get('id')) != BOARD:
                            raise ValueError()
                        batch = boards[0]['items_page']
                    else:
                        batch = j['data']['next_items_page']
                    for item in batch['items']:
                        if item.get('state') != 'active' or str(item.get('board', {}).get('id')) != BOARD:
                            continue
                        try:
                            c = parse_course(item, str(item['id']), from_day, False)
                        except HTTPException:
                            continue
                        if c['end'] >= from_day and c['start'] <= until_day:
                            result[c['id']] = {k: c[k] for k in ('id', 'title', 'client', 'venue', 'start', 'end')}
                    cursor = batch.get('cursor')
                    if not cursor:
                        break
                if cursor:
                    raise ValueError()
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(503, 'Course list is unavailable. Please use a known Course ID.')
        return sorted(result.values(), key=lambda c: (c['start'], c['venue'].casefold(), c['title'].casefold(), c['id']))


def parse_course(item, code, day, enforce_day=True):
    cols = {c['id']: c for c in item.get('column_values', [])}
    if cols.get('status', {}).get('text') not in ACTIVE:
        raise HTTPException(400, 'This course is not open for attendance. Please ask your trainer.')
    def value(k):
        raw = cols.get(k, {}).get('value')
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    try:
        timeline = value('timeline')
        start = value('date').get('date') or timeline.get('from')
        end = value('date2').get('date') or timeline.get('to') or start
        start, end = date.fromisoformat(start), date.fromisoformat(end)
        today = date.fromisoformat(day)
        if end < start:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, 'Course dates need checking in Monday.com. Please ask your trainer.')
    if enforce_day and not start <= today <= end:
        raise HTTPException(400, 'This course is not scheduled for today. Please check the Course ID with your trainer.')
    title = ' — '.join(str(cols.get(k, {}).get('text') or '').strip() for k in ('text34', 'type_of_training')).strip(' —') or item['name']
    return {'id': str(code), 'title': title, 'trainer': cols.get('dropdown4', {}).get('text') or '',
            'start': start.isoformat(), 'end': end.isoformat(), 'day': day,
            'url': 'https://exclusive-gang.monday.com/boards/' + BOARD + '/pulses/' + str(code),
            'client': str(cols.get('text34', {}).get('text') or '').strip(),
            'venue': ' '.join(str(cols.get('address__1', {}).get('text') or '').split())}
