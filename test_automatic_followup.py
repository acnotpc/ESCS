import copy
import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock
from automatic_followup import ReceiptIndex,AutomaticSelection,propose,PAUSE,ACTION,EXTRA
from recruitment_checklist import Checklist
from checklist_workflow import ChecklistWorkflow,REQUEST,RECEIVED,identity
from recruitment_checklist_access import FORMS
from test_checklist_workflow import record,NOW,Board
from test_extended_followup import ExtendedSource

def person(stage='employed'):
    r=record((),stage=stage);r['id']='900000001';r['name']='Synthetic Employee'
    cols={c['id']:c for c in r['column_values']}
    defaults={'status':'Healthcare Support Worker / Residential Support Worker - Fulltime',
              'acceptance':'Yes','status7':'Full','status72':'In Date/Received','status247':'OUT',
              'status797':'Signed','dup__of_medical_disclosure':'Not Received',
              'dup__of_added_to_maps__conveyance_':'Received No Entries','status64':'Consent Received',
              'update_service_consent6':'Yes','status58':'Received','color_mm2ffyt5':'Yes'}
    for key in EXTRA:
        if key not in cols:r['column_values'].append({'id':key,'type':'checkbox' if key==PAUSE else 'date' if key in {'date_11','date13','date4'} else 'long_text' if key==ACTION else 'status','text':defaults.get(key,''),'value':'{}'})
    c=next(c for c in r['column_values'] if c['id']=='email');c.update(text='synthetic@example.org',value=json.dumps({'email':'synthetic@example.org'}))
    return r

def change(r,col,text='',value=None):
    c=next(c for c in r['column_values'] if c['id']==col);c['text']=text;c['value']=json.dumps(value or {})

class Lookup:
    def __init__(self,value='absent'):self.value=value
    def receipt(self,*args):return self.value

class Pages:
    def __init__(self,rows=None):self.rows=rows or {};self.failed=False;self.repeated=False
    async def page(self,form,offset=0,limit=100):
        if self.failed:raise RuntimeError('provider secret must not leak')
        rows=self.rows.get(form,[])
        return copy.deepcopy(rows[:limit] if self.repeated else rows[offset:offset+limit])

class AutoBoard(Board):
    def __init__(self,rows):super().__init__(rows[0]);self.rows=rows;self.changed=False
    async def call(self,query,variables=None):
        if 'items_page' in query:return {'boards':[{'id':'424237712','items_page':{'cursor':None,'items':copy.deepcopy(self.rows)}}]}
        if 'items(ids' in query:
            result=copy.deepcopy(next(r for r in self.rows if r['id']==variables['ids'][0]))
            if self.changed:change(result,'dup__of_medical_disclosure','Received')
            return {'items':[result]}
        if 'change_multiple_column_values' in query:
            r=next(r for r in self.rows if r['id']==variables['item'])
            updates=json.loads(variables['value']);self.writes.append(updates)
            for col,value in updates.items():change(r,col,value.get('text',''),value)
            return {'change_multiple_column_values':{'id':r['id']}}
        raise AssertionError(query)

class AutoTests(unittest.IsolatedAsyncioTestCase):
    def test_completed_fields_never_request_again(self):
        r=person();change(r,'dup__of_medical_disclosure','Received')
        self.assertEqual(propose(r,Lookup(),NOW)[0],set())
    def test_signed_existing_receipt_suppresses_stale_status(self):
        selected,review,_=propose(person(),Lookup('found'),NOW)
        self.assertFalse(selected);self.assertIn('existing submission found',review[0])
    def test_failed_source_does_not_mean_missing(self):
        selected,review,_=propose(person(),Lookup('unverified'),NOW)
        self.assertFalse(selected);self.assertIn('source comparison is incomplete',review[0])
    def test_new_applicant_requires_acceptance_and_initial_checks(self):
        r=person('pre_employment');change(r,'acceptance','Awaiting Return')
        self.assertFalse(propose(r,Lookup(),NOW)[0])
        change(r,'acceptance','Yes');change(r,'offerletter_new_starter_jotform')
        self.assertFalse(propose(r,Lookup(),NOW)[0])
    def test_new_applicant_waits_three_complete_days(self):
        r=person('pre_employment');change(r,'offerletter_new_starter_jotform',value={'date':'2026-10-12'})
        selected,_,due=propose(r,Lookup(),NOW)
        self.assertEqual(selected,{'dress_code'});self.assertGreaterEqual(due,NOW+timedelta(hours=72))
    def test_recent_manual_contact_delays_automatic_chase(self):
        r=person();change(r,'date13',value={'date':'2026-10-12'})
        self.assertGreaterEqual(propose(r,Lookup(),NOW)[2],NOW+timedelta(hours=72))
    def test_pause_and_declined_consent(self):
        r=person();change(r,PAUSE,value={'checked':'true'})
        self.assertFalse(propose(r,Lookup(),NOW)[0])
        r=person();change(r,'dup__of_medical_disclosure','Received');change(r,'status64','Declined')
        self.assertFalse(propose(r,Lookup(),NOW)[0])
    def test_no_automatic_offer_payroll_or_partial_history(self):
        r=person();change(r,'dup__of_medical_disclosure','Received');change(r,'status797','Not Sent')
        change(r,'status58','Not Received');change(r,'status7','Received with some gaps')
        selected,review,_=propose(r,Lookup(),NOW)
        self.assertFalse(selected);self.assertEqual(len(review),3)
    def test_initial_group_only_monitored_and_leavers_excluded(self):
        r=person();r['group']['id']='new_group';change(r,'status53','Pending Review')
        self.assertFalse(propose(r,Lookup(),NOW)[0])
        r['group']['id']='new_group18301';change(r,'status53','Leaver')
        self.assertEqual(propose(r,Lookup(),NOW)[:2],(set(),[]))

    async def test_full_archive_pagination_private_index_restart_and_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Checklist(folder+'/private.sqlite');index=ReceiptIndex(ledger.db)
            form=FORMS['dress_code']
            rows=[{'id':str(1000-n),'form_id':form,'status':'ACTIVE','answers':{'1':{'text':'Name','answer':'Synthetic '+str(n)},'2':{'text':'Email','answer':'person'+str(n)+'@example.org'}}} for n in range(101)]
            pages=Pages({form:rows})
            self.assertEqual(await index.refresh(pages,NOW),len(FORMS))
            self.assertEqual(index.receipt('dress_code','Synthetic 100','person100@example.org',NOW),'found')
            self.assertEqual(index.receipt('dress_code','Absent Person','absent@example.org',NOW),'absent')
            dump=' '.join(str(tuple(row)) for row in ledger.db.execute('SELECT * FROM auto_source_keys'))
            self.assertNotIn('@example.org',dump);self.assertNotIn('Synthetic',dump)
            index=ReceiptIndex(ledger.db);self.assertEqual(index.receipt('dress_code','Synthetic 100','',NOW),'found')
            pages.failed=True;await index.refresh(pages,NOW)
            self.assertEqual(index.receipt('dress_code','Absent Person','',NOW),'unverified');ledger.db.close()

    async def test_index_unknown_identity_and_repeated_page_block_absence(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Checklist(folder+'/private.sqlite');index=ReceiptIndex(ledger.db);form=FORMS['dress_code']
            await index.refresh(Pages({form:[{'id':'100','form_id':form,'status':'ACTIVE','answers':{}}]}),NOW)
            self.assertEqual(index.receipt('dress_code','Missing Person','',NOW),'unverified')
            pages=Pages({form:[{'id':str(n+100),'form_id':form,'status':'ACTIVE','answers':{'1':{'text':'Name','answer':'Synthetic'}}} for n in range(100)]});pages.repeated=True
            await index.refresh(pages,NOW);self.assertEqual(index.receipt('dress_code','Missing Person','',NOW),'unverified');ledger.db.close()

    async def test_selection_once_existing_campaign_and_changed_record(self):
        with tempfile.TemporaryDirectory() as folder:
            board=AutoBoard([person()]);worker=ChecklistWorkflow(folder+'/private.sqlite',board,ExtendedSource(),AsyncMock())
            auto=AutomaticSelection(worker)
            self.assertEqual((await auto.run(Pages(),NOW))['selected'],1)
            self.assertEqual((await auto.run(Pages(),NOW+timedelta(minutes=5)))['selected'],0)
            self.assertEqual(len(board.writes),1);worker.ledger.db.close()
        with tempfile.TemporaryDirectory() as folder:
            board=AutoBoard([person()]);board.changed=True
            worker=ChecklistWorkflow(folder+'/private.sqlite',board,ExtendedSource(),AsyncMock())
            self.assertEqual((await AutomaticSelection(worker).run(Pages(),NOW))['selected'],0)
            self.assertFalse(board.writes);worker.ledger.db.close()

    async def test_duplicate_email_or_name_never_selects(self):
        with tempfile.TemporaryDirectory() as folder:
            first=person();second=person();second['id']='900000002'
            board=AutoBoard([first,second]);worker=ChecklistWorkflow(folder+'/private.sqlite',board,ExtendedSource(),AsyncMock())
            state=await AutomaticSelection(worker).run(Pages(),NOW)
            self.assertEqual(state['selected'],0)
            self.assertTrue(all(REQUEST not in writes for writes in board.writes));worker.ledger.db.close()

    async def test_scheduled_due_is_preserved_after_population(self):
        with tempfile.TemporaryDirectory() as folder:
            r=person('pre_employment');change(r,'offerletter_new_starter_jotform',value={'date':'2026-10-12'})
            board=AutoBoard([r]);worker=ChecklistWorkflow(folder+'/private.sqlite',board,ExtendedSource(),AsyncMock())
            self.assertEqual((await AutomaticSelection(worker).run(Pages(),NOW))['selected'],1)
            self.assertFalse(worker.ledger.plan(r['id'],NOW));worker.ledger.db.close()

    async def test_person_pause_stops_existing_campaign_without_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            r=person();change(r,REQUEST,value={'ids':[10]})
            board=AutoBoard([r]);sender=AsyncMock();source=ExtendedSource()
            worker=ChecklistWorkflow(folder+'/private.sqlite',board,source,sender);worker.probe_done=True
            await worker.run(NOW);self.assertEqual(sender.send.await_count,1)
            change(r,PAUSE,value={'checked':'true'});board.current=r
            await worker.run(NOW+timedelta(days=4));self.assertEqual(sender.send.await_count,1)
            self.assertEqual(worker.review(r['id'])['confirmedEmails'],1);worker.ledger.db.close()
