"""Pure meeting-time proposal from operator-verified road evidence; no writes."""
import math
from datetime import timedelta, timezone
from team_sync import aware, LONDON
from daily_teams import eligible_window


def meeting_preview(plan, staff_ids, *, point, route, d1_id, required_at, now):
    """Route duration must include reviewed traffic/contingency for departure.

    Evidence is supplied by an operator/routing provider, never derived from
    proximity. This checks the outbound meeting only, not whole-job clearance.
    """
    current = aware(now).astimezone(timezone.utc)
    plan.checked(current)
    if not staff_ids or len(set(staff_ids)) != len(staff_ids):
        raise ValueError('Unique crew IDs required')
    people = {c.staff_id: c for c in plan.candidates}
    if any(s not in people for s in staff_ids):
        raise ValueError('Unknown staff ID')
    if plan.team_assignments and not any(set(t.staff_ids) == set(staff_ids) for t in plan.team_assignments):
        raise ValueError('Preview crew must match an explicit operator team')
    if not point.get('id') or not point.get('label') or point.get('verified') is not True:
        raise ValueError('Verified meeting point required')
    if (route.get('origin_id') != point['id'] or route.get('destination_id') != d1_id
            or route.get('mode') != 'road' or route.get('verified') is not True
            or not route.get('source') or not d1_id):
        raise ValueError('Verified matching road route required')
    observed = aware(route['observed_at']).astimezone(timezone.utc)
    if not -120 <= (current-observed).total_seconds() <= 86400:
        raise ValueError('Road route requires refresh')
    minutes = route.get('duration_minutes')
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
        raise ValueError('Positive finite road duration required')
    required = aware(required_at).astimezone(timezone.utc)
    arrival = required-timedelta(minutes=15)
    departure = arrival-timedelta(minutes=minutes)
    meeting = departure-timedelta(minutes=15)
    if not aware(route['valid_from']).astimezone(timezone.utc) <= departure <= aware(route['valid_until']).astimezone(timezone.utc):
        raise ValueError('Route not valid for proposed departure')
    reviews = []
    if point.get("access_confirmed") is not True:
        reviews.append("Meeting-point access/parking requires confirmation")
    if point.get("vehicle_location_checked") is not True:
        reviews.append("Vehicle location at proposed meeting point requires confirmation")
    if meeting < current:
        reviews.append('Proposed meeting is in the past')
    if required.astimezone(LONDON).date().isoformat() != plan.service_date:
        raise ValueError('D1 must match service date')
    for sid in staff_ids:
        window, reason = eligible_window(people[sid])
        if not window or not (window[0] <= meeting and arrival <= window[1]):
            reviews.append(sid + ': ' + (reason or 'Outbound meeting/travel outside verified availability'))
    crew = [people[sid] for sid in staff_ids]
    fresh = [c.staff_id for c in crew if c.previous_day_marker == 'pink' and c.marker == 'blue'
             and c.availability and aware(c.availability.start).astimezone(LONDON).hour == 6
             and aware(c.availability.start).astimezone(LONDON).minute == 0]
    continuing = [c.staff_id for c in crew if c.previous_day_marker == 'blue']
    if fresh and continuing:
        reviews.append('Mixed fresh 06:00 starters and potential continuing crew; consider keeping fresh starters together')
    if any(c.previous_day_marker is None for c in crew):
        reviews.append('Previous-day colours missing; fresh-start review required')
    return {'fresh_starters': fresh, 'potential_continuing_staff': continuing, 'meeting_point_id': point['id'], 'meeting_point': point['label'],
            'meet_at': meeting.astimezone(LONDON).isoformat(),
            'depart_at': departure.astimezone(LONDON).isoformat(),
            'd1_arrival': arrival.astimezone(LONDON).isoformat(),
            'required_at': required.astimezone(LONDON).isoformat(),
            'road_minutes': minutes, 'reviews': reviews,
            'dispatch_ready': False, 'decision_owner': 'Control Room and allocated crew',
            'remaining_checks': ['Staff travel to meeting point', 'Driver and vehicle eligibility',
                                 'D1 to D2, waiting, return and final release', 'Fresh starts and 06:00 finish target']}


def meeting_lines(result):
    """Render a reviewed proposal for a team-list consumer; never publishes."""
    def clock(value):
        return aware(value).astimezone(LONDON).strftime('%d %b %H:%M %Z')
    lines = ['PROVISIONAL meeting proposal — Control Room and allocated crew to confirm',
             'Meeting point: ' + result['meeting_point'],
             'Meet / start vehicle checks: ' + clock(result['meet_at']),
             'Depart: ' + clock(result['depart_at']),
             'D1 arrival target: ' + clock(result['d1_arrival']),
             'D1 required: ' + clock(result['required_at']),
             'Vehicle checks: 15 minutes; early arrival allowance: 15 minutes']
    lines.extend('Review: ' + review for review in result['reviews'])
    lines.append('Full-job availability, rest, driver/vehicle and release checks remain required.')
    return lines
