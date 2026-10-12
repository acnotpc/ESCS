"""Deterministic administrative coverage checks; no authenticity/suitability decisions."""
import hashlib
import json
import re
from datetime import date
from recruitment_checklist import history_gaps

FORM = '211923425771051'
FIELDS = {
 '21': ('10 Year Employment History (Select + sign to add more entries)', 'control_widget'),
 '35': ('Gaps in Employment (Select + sign to add more entries)', 'control_widget'),
 '40': ('10 Year Employment History Employer 2 (Select + sign to add more entries)', 'control_widget'),
 '39': ('Only use if advised by HR','control_radio'),
 '36': ('Do you have any gaps in your employment history?','control_radio'),
 '16': ('Signature','control_signature'),
 '13': ('Name','control_fullname'),
 '15': ('Date','control_datetime'),
}
WORK_KEYS = {'Employer Name','Contact Details Email','Telephone','Position Held','Date From','Date To','Still in business'}
GAP_KEYS = {'Reason For Gap In Employment','Date From','Date To'}

class HistoryReview(ValueError):
    pass


def check_schema(schema):
    if not isinstance(schema,dict):raise HistoryReview('History schema unavailable')
    for qid,(title,typ) in FIELDS.items():
        q=schema.get(qid)
        if not isinstance(q,dict) or q.get('text')!=title or q.get('type')!=typ:
            raise HistoryReview('History fields changed')
    return True


def answer(schema,record,qid):
    q=record.get('answers',{}).get(qid)
    if q is None:return None
    if (not isinstance(q,dict) or q.get('text')!=schema[qid]['text'] or q.get('type')!=schema[qid]['type']
            or str(q.get('qid',qid))!=qid):
        raise HistoryReview('History answer identity changed')
    return q.get('answer')


def rows(value,allowed):
    if value in (None,''):return []
    if isinstance(value,str):
        if len(value)>100000:raise HistoryReview('History table too large')
        try:value=json.loads(value)
        except ValueError:raise HistoryReview('History table format needs review') from None
    if not isinstance(value,list) or len(value)>100:raise HistoryReview('History table format needs review')
    result=[]
    for row in value:
        if not isinstance(row,dict):raise HistoryReview('History row format needs review')
        normalized={}
        for key,v in row.items():
            if not isinstance(key,str) or not isinstance(v,str):raise HistoryReview('History row format needs review')
            key=' '.join(key.split())
            if key not in allowed or key in normalized:raise HistoryReview('History table columns changed')
            normalized[key]=v.strip()
        if any(normalized.values()):result.append(normalized)
    return result


def period(row,label,anchor,issues):
    values=[]
    for key in ('Date From','Date To'):
        raw=row.get(key,'')
        if not raw:
            issues.append(label+': please provide '+key.lower()+'.');values.append(None);continue
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
            raise HistoryReview('Date format or ongoing-period meaning needs HR review')
        try:parsed=date.fromisoformat(raw)
        except ValueError:
            issues.append(label+': please correct the invalid '+key.lower()+'.');values.append(None);continue
        if parsed>anchor:
            issues.append(label+': please confirm '+key.lower()+'; it is after the submission date.');values.append(None)
        else:values.append(parsed)
    first,last=values
    if first and last and first>last:
        issues.append(label+': date from is after date to; please correct the dates.');return None
    return (first,last) if first and last else None


def analyse(schema,record,today):
    """Only fixed history fields are inspected. Return dates/field prompts, no answer text."""
    try:
        check_schema(schema)
        if (not isinstance(record,dict) or str(record.get('form_id'))!=FORM or record.get('status')!='ACTIVE'
                or not str(record.get('id','')).isdigit() or type(today) is not date):
            raise HistoryReview('Canonical history response required')
        submitted=record.get('created_at','')
        if not isinstance(submitted,str) or not re.match(r'^\d{4}-\d{2}-\d{2}(?: |T)',submitted):
            raise HistoryReview('Submission date unavailable')
        anchor=date.fromisoformat(submitted[:10])
        if anchor>today:raise HistoryReview('Submission date needs review')
        values={qid:answer(schema,record,qid) for qid in FIELDS}
        digest=hashlib.sha256(json.dumps(values,sort_keys=True,ensure_ascii=True).encode()).hexdigest()
        if values['39']!='Full 10 year history':
            raise HistoryReview('Declaration-only or exceptional history needs HR review')
        legacy=values['40']
        if isinstance(legacy,str) and legacy.strip()=='[]':legacy=[]
        if legacy not in (None,'',[]):raise HistoryReview('Older employer section needs HR review')
        work=rows(values['21'],WORK_KEYS);gaps=rows(values['35'],GAP_KEYS)
        issues=[];spans=[];work_spans=[];uncertain=False
        name=values['13']
        if not isinstance(name,dict) or not name.get('first') or not name.get('last'):
            issues.append('Please complete your name on the history form.')
        if not isinstance(values['16'],str) or not values['16'].strip():
            issues.append('Please sign the history declaration.')
        dated=values['15']
        if not isinstance(dated,dict) or not all(str(dated.get(k,'')).isdigit() for k in ('year','month','day')):
            issues.append('Please provide the declaration date.')
        else:
            try:declared=date(int(dated['year']),int(dated['month']),int(dated['day']))
            except ValueError:declared=None
            if declared is None or declared>anchor:issues.append('Please correct the declaration date.')
        if values['36'] not in {'Yes','No'}:issues.append('Please answer whether there are gaps in the history.')
        if values['36']=='No' and gaps:raise HistoryReview('Gap declaration conflicts with listed periods')
        if values['36']=='Yes' and not gaps:issues.append('Please add the dates and explanations for the gaps you declared.')
        for number,row in enumerate(work,1):
            label='Employment/education entry '+str(number)
            for key in ('Employer Name','Position Held'):
                if not row.get(key):issues.append(label+': please complete '+('organisation/education provider' if key=='Employer Name' else 'role or course')+'.')
            if row.get('Still in business') not in {'Yes','No'}:
                issues.append(label+': please confirm whether the organisation is still in business.')
            if row.get('Still in business')=='Yes' and not row.get('Contact Details Email') and not row.get('Telephone'):
                issues.append(label+': please provide a contact email or telephone number.')
            span=period(row,label,anchor,issues)
            if span:spans.append(span);work_spans.append(span)
            else:uncertain=True
        for number,row in enumerate(gaps,1):
            label='Period out of employment/education '+str(number)
            if not row.get('Reason For Gap In Employment'):
                issues.append(label+': please provide an explanation for this period.')
            span=period(row,label,anchor,issues)
            if span and row.get('Reason For Gap In Employment'):spans.append(span)
            elif not span:uncertain=True
        if not uncertain:
            for first,last in history_gaps(spans,anchor):
                issues.append('Please account for '+first.isoformat()+' to '+last.isoformat()+' with employment, education or an explanation of time out of work.')
        # Concurrent roles can be valid. Never tell the employee to remove one.
        overlaps=any(b[0]<=a[1] for a,b in zip(sorted(work_spans),sorted(work_spans)[1:]))
        if overlaps:raise HistoryReview('Overlapping employment/education periods need HR review; concurrent roles may be valid')
        if len(issues)>50:raise HistoryReview('Large correction list needs HR review')
        return {'status':'needs_information' if issues else 'complete','issues':issues,
                'digest':digest,'anchor':anchor.isoformat(),'reviewReason':None}
    except (HistoryReview,ValueError,TypeError,KeyError) as exc:
        # Fixed messages only; never expose raw explanation/name/contact content.
        safe=str(exc) if isinstance(exc,HistoryReview) else 'History data needs HR review'
        return {'status':'staff_review','issues':[],'digest':None,'anchor':None,'reviewReason':safe}
