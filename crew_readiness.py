"""Read-only ESCS crew continuity preview; never changes allocations or sends."""
from datetime import timedelta, timezone
from statistics import mean
from zoneinfo import ZoneInfo
from conveyance_time_reminders import aware


def preview(crew_ids, working_ids, tomorrow_teams, duty_start, *, release=None,
            comparable_hours=(), source=None):
    """Inputs must be verified per crew/date; start is actual, not availability.

    Comparable durations must be reviewed completed bedwatch duties including
    return/release. Estimated readiness never authorises dispatch.
    """
    crew = tuple(crew_ids)
    if not crew or len(set(crew)) != len(crew):
        raise ValueError('Unique crew IDs required')
    working = set(working_ids)
    active = [person for person in crew if person in working]
    allocations = {person: tomorrow_teams.get(person) for person in active}
    split = len({team for team in allocations.values() if team is not None}) > 1
    result = {'working_crew': active, 'allocations': allocations,
              'split_team_review': split, 'ready_from': None,
              'name_suffix': None, 'provisional': False, 'dispatch_ready': False}
    if not active:
        return result
    if not source or not duty_start:
        result['review'] = 'Verified actual duty start and evidence source required'
        return result
    start = aware(duty_start)
    estimated = release is None
    if estimated:
        durations = list(comparable_hours)
        if not durations or any(not isinstance(h, (int, float)) or not 0 < h <= 72 for h in durations):
            result['review'] = 'Comparable completed duty durations required'
            return result
        end = start + timedelta(hours=mean(durations))
    else:
        end = aware(release)
    if end < start:
        raise ValueError('Release precedes duty start')
    result['duty_hours'] = (end-start).total_seconds()/3600
    result['provisional'] = estimated
    if end-start > timedelta(hours=12):
        ready = (end+timedelta(hours=11)).astimezone(ZoneInfo('Europe/London'))
        result['ready_from'] = ready.isoformat()
        result['name_suffix'] = '('+ready.strftime('%H:%M')+(' TBC' if estimated else '')+')'
    return result


def format_review(result, names):
    lines = ['Tomorrow’s rota — crew review', '', 'Staff working tomorrow:']
    for person in result['working_crew']:
        lines.append('• '+names[person]+(' '+result['name_suffix'] if result['name_suffix'] else ''))
    if result['split_team_review']:
        lines += ['', 'Please review: this crew is split across tomorrow’s teams.',
                  'Keep the ongoing crew together while duty end/rest is uncertain.']
    if result['provisional']:
        lines += ['', 'Readiness is estimated. Confirm actual return/release and update the brackets.']
    if result.get('review'):
        lines += ['', result['review']]
    lines += ['', 'Please confirm once reviewed.']
    return '\n'.join(lines)


def planning_checks(service_date, staff, teams, *, now):
    """Review-only rules using verified daily colours and operator evidence.

    Staff records: id, today_colour, tomorrow_colour, tomorrow_ready_from,
    expected_release, actual_release. Release dates/times must include timezone.
    No demographic or driver eligibility inference is performed.
    """
    from datetime import date, datetime, time
    day = date.fromisoformat(service_date)
    current = aware(now)
    cutoff = datetime.combine(day+timedelta(days=1), time(6), ZoneInfo('Europe/London'))
    records = {s['id']: s for s in staff}
    if len(records) != len(staff):
        raise ValueError('Unique staff IDs required')
    assigned = {}
    for team, members in teams.items():
        for person in members:
            if person not in records or person in assigned:
                raise ValueError('Known staff assigned at most once required')
            assigned[person] = team
    fresh, overnight, finish_reviews, unknown = [], [], [], []
    for person, s in records.items():
        today, tomorrow = s.get('today_colour'), s.get('tomorrow_colour')
        if today not in ('pink', 'blue') or tomorrow not in ('pink', 'blue'):
            unknown.append(person)
        if today == 'blue':
            overnight.append(person)
        if today == 'pink' and tomorrow == 'blue':
            ready = s.get('tomorrow_ready_from')
            if ready and aware(ready) == cutoff.astimezone(timezone.utc):
                fresh.append(person)
        if today == 'blue' and tomorrow == 'pink':
            actual = s.get('actual_release')
            expected = s.get('expected_release')
            value = actual or expected
            if value and aware(value) > cutoff:
                finish_reviews.append({'id': person, 'reason': 'Finish after 06:00 target',
                                       'estimated': not bool(actual), 'release': value})
            elif not value:
                finish_reviews.append({'id': person,
                                       'reason': 'Finish time unknown; plan to finish by 06:00',
                                       'estimated': True})
            if actual and aware(actual) > current:
                raise ValueError('Actual release cannot be in the future')
    mixed = []
    fresh_set, overnight_set = set(fresh), set(overnight)
    for team, members in teams.items():
        new = [p for p in members if p in fresh_set]
        continuing = [p for p in members if p in overnight_set]
        if new and continuing:
            mixed.append({'team': team, 'fresh_starters': new,
                          'potential_overnight_staff': continuing,
                          'reason': 'Consider keeping 06:00 fresh starters together'})
    return {'service_date': service_date, 'finish_target': cutoff.isoformat(),
            'fresh_starters': fresh, 'potential_overnight_staff': overnight,
            'mixed_team_reviews': mixed, 'finish_target_reviews': finish_reviews,
            'unknown_colour_staff': unknown,
            'unassigned_fresh_starters': [p for p in fresh if p not in assigned],
            'allocation_changes': [], 'decision_owner': 'Control Room',
            'dispatch_ready': False}
