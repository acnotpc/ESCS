"""User-requested, one-off synthetic history email; never changes staff checks."""
import logging
import os
from datetime import date
from history_completeness import analyse,FIELDS,FORM

log=logging.getLogger('escs.history.example');log.setLevel(logging.INFO)

def example_analysis():
    schema={qid:{'text':title,'type':typ} for qid,(title,typ) in FIELDS.items()}
    work=[]
    for n,(first,last) in enumerate([('2016-10-12','2019-06-30'),('2019-09-01','2022-12-31'),('2023-03-01','2026-10-12')],1):
        work.append({'Employer Name':'Example Organisation '+str(n),'Position Held':'Example role',
            'Date From':first,'Date To':last,'Still in business':'Yes',
            'Contact Details Email':'' if n==2 else 'example@example.org','Telephone':''})
    vals={'21':work,'35':[],'40':[],'39':'Full 10 year history','36':'No','16':'synthetic-signature',
          '13':{'first':'Example','last':'Employee'},'15':{'year':'2026','month':'10','day':'12'}}
    record={'id':'900000000001','form_id':FORM,'status':'ACTIVE','created_at':'2026-10-12 12:00:00',
            'answers':{qid:dict(schema[qid],answer=val) for qid,val in vals.items()}}
    result=analyse(schema,record,date(2026,10,12))
    if result['status']!='needs_information' or len(result['issues'])!=3:raise ValueError('Example assessment unexpected')
    return result

def body():
    from checklist_workflow import render
    issues=example_analysis()['issues']
    preview=render({'history'},'employed',{'history':'https://form.jotform.com/'+FORM},issues)
    return ('<p><strong>TEST EXAMPLE ONLY - fictional history, not an assessment of your own record.</strong></p>'
        '<p>This shows the current automated follow-up format for a history submitted on 12 October 2026. '
        'The review period is 12 October 2016 to 12 October 2026. No response or form submission is required for this test.</p>'+
        preview+'<p>To clarify a gap, retain the entries already supplied and add the dates and a brief factual explanation '
        '(for example, studying, unemployed, caring responsibilities or travel) in the gaps section. '
        'If the apparent gap is a date error, correct the relevant dates. Add the missing organisation contact where available; '
        'if unavailable, explain this to HR rather than inventing details.</p>'
        '<p>Please reply to this test email with any changes you would like to the wording. '
        'The form link above is for review; do not submit fictional details into the live form. '
        'Administrative completeness is checked before human review, and does not approve references or recruitment checks.</p>')

async def send_example(automatic,roster,now):
    key=os.getenv('HR_HISTORY_EMAIL_EXAMPLE_ID','');target=os.getenv('HR_HISTORY_EMAIL_EXAMPLE_ITEM','')
    if not key or not target.isdigit():return
    db=automatic.worker.ledger.db
    db.execute('CREATE TABLE IF NOT EXISTS history_email_example(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,state TEXT NOT NULL,attempted_at TEXT NOT NULL)');db.commit()
    old=db.execute('SELECT state FROM history_email_example WHERE id=?',(key,)).fetchone()
    if old:return
    from checklist_workflow import identity
    from automatic_followup import values,data,normalized,names
    rows=[r for r in roster if str(r['id'])==target]
    if len(rows)!=1:raise ValueError('Example recipient requires review')
    item=rows[0];_,stage,email,_,_,_=identity(item)
    if stage!='employed':raise ValueError('Example recipient must be the verified employee')
    if sum(normalized(data(values(r),'email').get('email',''))==normalized(email) for r in roster)!=1:raise ValueError('Example contact ambiguous')
    if any(sum(n in names(r.get('name','')) for r in roster)!=1 for n in names(item.get('name',''))):raise ValueError('Example identity ambiguous')
    fresh=await automatic.fresh(target)
    if fresh.get('name')!=item.get('name') or values(fresh)!=values(item):return
    with db:reserved=db.execute("INSERT OR IGNORE INTO history_email_example VALUES(?,?,'reserved',?)",(key,target,now.isoformat())).rowcount
    if not reserved:return
    try:
        await automatic.worker.sender.send('employed',email,'TEST EXAMPLE - 10-year history gaps requiring clarification',body())
    except Exception:
        with db:db.execute("UPDATE history_email_example SET state='unknown' WHERE id=?",(key,))
        log.info('Synthetic history email example: delivery uncertain; no automatic retry')
    else:
        with db:db.execute("UPDATE history_email_example SET state='accepted' WHERE id=?",(key,))
        log.info('Synthetic history email example: provider accepted; staff records unchanged')
