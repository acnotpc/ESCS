"""Shared read-only intake for crew planning; existing connector authentication."""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fms_html import preview_html_rota
from fms_xlsx import MAX_UPLOAD
from rota_planning import review_html_plan


async def preview_rota_with_planning(html, observed_at, service_date, planning_json,
                                     fms_get, now=None):
    # Validate planning before requesting upstream staff data.
    plan = None
    if planning_json:
        if len(planning_json.encode('utf-8')) > 100000:
            raise ValueError('Planning input too large')
        plan = json.loads(planning_json)
        if not isinstance(plan, dict):
            raise ValueError('Planning input must be an object')
        if not isinstance(plan.get('staff', {}), dict) or not isinstance(plan.get('teams', {}), dict):
            raise ValueError('Planning staff and teams must be objects')
        if not isinstance(plan.get('crews', []), list):
            raise ValueError('Planning crews must be a list')
    current = now or datetime.now(timezone.utc)
    result = await preview_html_rota(html, observed_at, service_date, fms_get, current)
    if plan is not None:
        result['planning_review'] = review_html_plan(result, service_date, plan, current.isoformat())
    return result


def build_planning_router(authorize, fms_get):
    router = APIRouter(prefix='/team-sync/daily')

    @router.post('/planning-preview', dependencies=[Depends(authorize)])
    async def preview(request: Request):
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > MAX_UPLOAD * 2 + 110000:
                raise HTTPException(413, 'Planning request exceeds the upload limit')
            data.extend(chunk)
        try:
            body = json.loads(data)
            if not isinstance(body, dict) or not isinstance(body.get('planning'), dict):
                raise ValueError('A planning object is required')
            for key in ('html', 'observed_at', 'service_date'):
                if not isinstance(body.get(key), str):
                    raise ValueError('HTML, capture timestamp and service date are required')
            return await preview_rota_with_planning(body['html'], body['observed_at'],
                body['service_date'], json.dumps(body['planning']), fms_get)
        except (ValueError, TypeError, KeyError, AttributeError):
            # Do not echo staff evidence or malformed JSON into errors/logs.
            raise HTTPException(422, 'Invalid rota or crew-planning evidence') from None

    return router
