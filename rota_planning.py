"""Connect verified HTML colours to operator-supplied planning evidence."""
from datetime import date, timedelta
from crew_readiness import planning_checks, preview


def review_html_plan(rota, service_date, plan, now):
    following=(date.fromisoformat(service_date)+timedelta(days=1)).isoformat()
    if not rota.get('next_day_covered'):
        raise ValueError('Next-day HTML coverage required')
    days=rota['days']
    current={str(s['staff_id']):s for s in days[service_date]['staff']}
    next_rows={str(s['staff_id']):s for s in days[following]['staff']}
    ids=set(current)|set(next_rows)
    supplied=plan.get('staff',{})
    if set(supplied)-ids:
        raise ValueError('Planning staff must match HTML staff IDs')
    records=[]
    for sid in sorted(ids):
        a,b=current.get(sid,{}),next_rows.get(sid,{})
        extra=supplied.get(sid,{})
        records.append({'id':sid,
            'today_colour':a.get('marker') if a.get('individual_colour_verified') and a.get('active') else None,
            'tomorrow_colour':b.get('marker') if b.get('individual_colour_verified') and b.get('active') else None,
            **{k:extra.get(k) for k in ('tomorrow_ready_from','expected_release','actual_release')}})
    result=planning_checks(service_date,records,plan.get('teams',{}),now=now)
    working=[s['id'] for s in records if s['tomorrow_colour']=='blue']
    allocation={p:t for t,people in plan.get('teams',{}).items() for p in people}
    result['crew_reviews']=[]
    for crew in plan.get('crews',[]):
        if set(crew['staff_ids'])-ids:
            raise ValueError('Crew must match HTML staff IDs')
        result['crew_reviews'].append(preview(crew['staff_ids'],working,allocation,
            crew.get('actual_duty_start'),release=crew.get('actual_release'),
            comparable_hours=crew.get('comparable_completed_duty_hours',()),source=crew.get('source')))
    return result
