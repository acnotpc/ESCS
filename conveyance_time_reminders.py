"""Read-only gap evaluation and durable one-off reminder tracking.

Callers supply verified stage evidence; time-only booking cells and ETAs cannot
establish a date, crew change, release or completed stage on their own.
No network calls, board writes or automatic sending are performed here.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import re


FIELDS = {
    'start': ('hour_mm6w81h0',),
    'arrived_d1': ('hour_mm6w9kt5', 'date1'),
    'departed_d1': ('hour_mm6wmgax',),
    'arrived_d2': ('hour_mm6whxx', 'dup__of_hour'),
    'departed_d2': ('dup__of_dup__of_hour',),
    'end': ('hour9',),
}
LABELS = {'start': 'Depart Office / B2B start', 'arrived_d1': 'Arrive D1',
          'departed_d1': 'Depart D1', 'arrived_d2': 'Arrive D2',
          'departed_d2': 'Depart D2', 'end': 'Office/J22/B2B end'}


def aware(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('Timezone-aware timestamp required')
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Evidence:
    stage: str
    occurred_at: str
    source: str
    estimated: bool = False
    scope: str = 'job'


@dataclass
class Job:
    item_id: int
    job_number: str
    crew_ids: tuple[str, ...]
    values: dict
    evidence: list[Evidence] = field(default_factory=list)
    previous_item_id: int | None = None
    handover_verified: bool = False
    # An explicit handover is an end of this job, not the end of the duty.
    handover_at: str | None = None
    ongoing_bedwatch: bool = False
    pulse_reviewed: bool = False
    bedwatch_source: str | None = None


def present(value):
    return value is not None and str(value).strip() not in ('', 'null')


def evaluate(jobs, now):
    """Return missing fields only for verified stages that have already happened."""
    now = aware(now)
    by_id = {j.item_id: j for j in jobs}
    if len(by_id) != len(jobs):
        raise ValueError('Duplicate booking IDs')
    results = []
    for job in jobs:
        if job.item_id <= 0 or not re.fullmatch(r'JOB\d{5}', job.job_number):
            raise ValueError('Verified booking identity required')
        if not job.crew_ids or len(set(job.crew_ids)) != len(job.crew_ids):
            raise ValueError('Unique crew IDs required')
        missing, review, actual, crew_ends = [], [], {}, []
        if job.ongoing_bedwatch and (not job.pulse_reviewed or not job.bedwatch_source):
            review.append('Review continuing-bedwatch pulse and forms by crew/date')
        for event in job.evidence:
            if event.stage not in FIELDS or not event.source.strip():
                raise ValueError('Explicit stage and source required')
            if event.scope not in ('job', 'crew'):
                raise ValueError('Explicit job or crew scope required')
            at = aware(event.occurred_at)
            if event.estimated or at > now:
                continue
            if event.scope == 'crew':
                if event.stage != 'end':
                    raise ValueError('Crew-scoped evidence must be a crew ending')
                crew_ends.append({'occurred_at': at.isoformat(), 'source': event.source})
                continue
            if event.stage in actual and actual[event.stage] != at:
                review.append('Conflicting ' + LABELS[event.stage] + ' evidence')
            actual[event.stage] = at
        effective_start = actual.get('start')
        if job.previous_item_id is not None:
            previous = by_id.get(job.previous_item_id)
            if not previous or not job.handover_verified or set(previous.crew_ids) != set(job.crew_ids):
                review.append('B2B handover/date/crew requires verification')
            else:
                seen = {job.item_id}
                cursor = previous
                chain_start = None
                while cursor:
                    if cursor.item_id in seen:
                        raise ValueError('Cyclic B2B chain')
                    seen.add(cursor.item_id)
                    starts = [aware(e.occurred_at) for e in cursor.evidence
                              if e.stage == 'start' and not e.estimated and aware(e.occurred_at) <= now]
                    if starts:
                        chain_start = min(starts)
                    if cursor.previous_item_id is None:
                        if not starts:
                            chain_start = None
                            review.append('Original B2B duty start missing')
                        break
                    if not cursor.handover_verified:
                        review.append('Earlier B2B handover requires verification')
                        chain_start = None
                        break
                    parent = by_id.get(cursor.previous_item_id)
                    if not parent or set(parent.crew_ids) != set(cursor.crew_ids):
                        review.append('Earlier B2B crew/date requires verification')
                        chain_start = None
                        break
                    cursor = parent
                effective_start = chain_start
                if previous.handover_at:
                    handover = aware(previous.handover_at)
                    if handover <= now:
                        if 'start' in actual and actual['start'] != handover:
                            review.append('Previous job end differs from next job start')
                        actual['start'] = handover
                    else:
                        review.append('B2B handover is in the future')
                else:
                    review.append('Previous job B2B end time missing')
        for stage in actual:
            if job.ongoing_bedwatch and stage in ('departed_d1','arrived_d2','departed_d2','end'):
                review.append('Actual ' + LABELS[stage] + ' conflicts with ongoing bedwatch; verify crew/job scope')
                continue
            if not any(present(job.values.get(column)) for column in FIELDS[stage]):
                missing.append({'stage': stage, 'field': LABELS[stage]})
        results.append({'item_id': job.item_id, 'job_number': job.job_number,
                        'url': f'https://exclusive-gang.monday.com/boards/376064564/pulses/{job.item_id}',
                        'missing': missing, 'review': list(dict.fromkeys(review)),
                        'continuous_duty_start': effective_start.isoformat() if effective_start else None,
                        'crew_end_evidence': crew_ends,
                        'not_yet_due': ['departed_d1','arrived_d2','departed_d2','end'] if job.ongoing_bedwatch else [],
                        'dispatch_ready': False})
    return results


class ReminderLedger:
    """Persist send intent first; uncertain sends never automatically retry."""
    def __init__(self, db):
        self.db = db
        db.execute('CREATE TABLE IF NOT EXISTS conveyance_reminders (key TEXT PRIMARY KEY, state TEXT NOT NULL, sent_at TEXT, message_id INTEGER)')

    def reserve(self, trial_id, findings):
        payload = [(x['item_id'], sorted(m['stage'] for m in x['missing']), sorted(x['review']))
                   for x in findings if x['missing'] or x['review']]
        if not payload:
            return None
        if not trial_id.strip():
            raise ValueError('Explicit reminder run ID required')
        # One consolidated message per run, even after the findings change.
        key = sha256(trial_id.encode()).hexdigest()
        with self.db:
            added = self.db.execute('INSERT OR IGNORE INTO conveyance_reminders(key,state) VALUES (?,?)', (key, 'sending')).rowcount
        return key if added else None

    def confirm_sent(self, key, sent_at, message_id):
        aware(sent_at)
        if message_id <= 0:
            raise ValueError('Verified sent message ID required')
        with self.db:
            changed = self.db.execute("UPDATE conveyance_reminders SET state='sent',sent_at=?,message_id=? WHERE key=? AND state='sending'", (sent_at,message_id,key)).rowcount
        if changed != 1:
            raise ValueError('Send already reconciled or unknown')

    def elapsed(self, key, confirmed_at):
        row = self.db.execute("SELECT sent_at FROM conveyance_reminders WHERE key=? AND state='sent'", (key,)).fetchone()
        if not row:
            raise ValueError('Verified send required')
        seconds = (aware(confirmed_at) - aware(row[0])).total_seconds()
        if seconds < 0:
            raise ValueError('Confirmation precedes reminder')
        return seconds
