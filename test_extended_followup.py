import copy
import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock
from recruitment_receipts import EXTENDED_ENABLED, EXTENDED_REQUESTS, ReceiptHold
from recruitment_checklist_access import FORMS, SIGNATURES
from checklist_workflow import ChecklistWorkflow, identity, render
from test_checklist_workflow import Board, Source, record, schema as base_schema, NOW
from recruitment_tracking import TRACKING

ACCEPT = 'I accept the terms of this employment contract and acknowledge receipt of a copy of this Contract of Employment which is in accordance with the provisions of the Employment Rights Act 1996 as amended from time to time.'

def schema(kind):
    rows=base_schema(kind)
    if kind not in {v[0] for v in EXTENDED_REQUESTS.values()}:return rows
    fields=[('Name','control_fullname',{'first':'Synthetic','last':'Person','middle':''}),
            ('Date it was signed. This is not the start date' if kind=='contract_bank' else 'Date','control_datetime',{'year':'2026','month':'10','day':'12'})]
    if kind.startswith('contract_'):fields += [('E-mail','control_email','test@example.org'),(ACCEPT,'control_checkbox',[ACCEPT])]
    if kind=='dress_code':fields += [('Email','control_email','test@example.org'),('I have read and understood the policies','control_radio','Yes')]
    if kind=='driving_consent':fields += [('Driver Licence Number','control_textbox','synthetic'),('Current Address','control_address',{'addr_line1':'Synthetic','addr_line2':''}),('Date of Birth','control_datetime',{'year':'1990','month':'1','day':'1'})]
    if kind=='dbs_consent':fields += [('Email','control_email','test@example.org')]
    if kind=='driving_declaration':
        titles=['Have you been involved in any collisions/accidents in the last 5 (five) years, which involve engagement with an insurance company and/or the Police.',
                'Have you been refused, declined or insurance cancelled in the last 5 (five) years or had any special terms imposed?',
                'Does your licence have over 6 points or the following conviction codes:  AC, BA, CD, CU, DD, DG, DR, IN, TT, UT',
                'Been a UK resident for more than 5 years','Have you held a UK Full Driving licence for longer than 12 months?']
        fields += [(title,'control_radio','No') for title in titles]
    for n,(title,typ,value) in enumerate(fields,10):
        rows[str(n)]={'qid':str(n),'text':title,'type':typ,'syntheticValue':value}
        if typ in {'control_radio','control_checkbox'}:rows[str(n)]['options']=ACCEPT if title==ACCEPT else 'Yes|No'
    return rows

class ExtendedSource(Source):
    async def schema(self,form):return schema(next(k for k,v in FORMS.items() if v==form))
    def response(self,worker,kind,sid):
        token=worker.ledger.db.execute('SELECT token FROM checklist_links WHERE kind=?',(kind,)).fetchone()[0]
        return {'id':str(sid),'form_id':FORMS[kind],'status':'ACTIVE','answers':{
            qid:dict(q,answer=token if qid=='1' else 'signed' if qid=='2' else q['syntheticValue'])
            for qid,q in schema(kind).items()}}

@unittest.skipUnless(EXTENDED_ENABLED,'Run with HR_FOLLOWUP_EXTENDED_FORMS=true')
class ExtendedTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_signed_form_stops_only_its_request(self):
        for index,(kind,_,_,signature) in EXTENDED_REQUESTS.items():
            if not signature:continue
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as folder:
                board=Board(record((index,))); board.current['id']='900000001'
                source=ExtendedSource(); sender=AsyncMock()
                worker=ChecklistWorkflow(folder+'/ledger.sqlite',board,source,sender);worker.probe_done=True
                try:
                    state=await worker.run(NOW);self.assertTrue(state['connected']);self.assertEqual(sender.send.await_count,1)
                    self.assertIn('Open secure form',sender.send.await_args.args[3])
                    source.rows[kind].append(source.response(worker,kind,index+1000))
                    self.assertEqual((await worker.run(NOW+timedelta(minutes=5)))['received'],1)
                    await worker.run(NOW+timedelta(days=4));self.assertEqual(sender.send.await_count,1)
                    worker.ledger.db.close()
                    worker=ChecklistWorkflow(folder+'/ledger.sqlite',board,source,sender);worker.probe_done=True
                    await worker.run(NOW+timedelta(days=8));self.assertEqual(sender.send.await_count,1)
                finally:worker.ledger.db.close()

    async def test_incomplete_contract_holds_without_chasing_again(self):
        with tempfile.TemporaryDirectory() as folder:
            board=Board(record((6,)));source=ExtendedSource();sender=AsyncMock()
            worker=ChecklistWorkflow(folder+'/ledger.sqlite',board,source,sender);worker.probe_done=True
            await worker.run(NOW)
            r=source.response(worker,'contract_fulltime',1111);r['answers']['2']['answer']=''
            source.rows['contract_fulltime'].append(r)
            self.assertEqual((await worker.run(NOW+timedelta(days=4)))['review'],1)
            self.assertEqual(sender.send.await_count,1);worker.ledger.db.close()

    async def test_multiple_contracts_blocked(self):
        with self.assertRaises(ValueError):identity(record((6,7)))

    async def test_manual_routes_not_read_or_decrypted(self):
        for kind in ('payroll','licence_details','training','taster','uniform','id_badge','emergency_contacts','sia_evidence'):
            self.assertNotIn(kind,FORMS)
        body=render({'payroll'},'employed',{})
        self.assertIn('<a href="https://form.jotform.com/51518125846861">',body)
        self.assertIn('do not email bank details',body)

    async def test_wrong_contract_token_and_changed_receipt_owner_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            board=Board(record((6,)));source=ExtendedSource();sender=AsyncMock()
            worker=ChecklistWorkflow(folder+'/ledger.sqlite',board,source,sender);worker.probe_done=True
            await worker.run(NOW);r=source.response(worker,'contract_fulltime',1234)
            wrong=copy.deepcopy(r);wrong['form_id']=FORMS['contract_bank']
            with self.assertRaises(ReceiptHold):worker.links.accept(schema('contract_bank'),wrong)
            self.assertTrue(worker.links.accept(schema('contract_fulltime'),r))
            worker.ledger.request('900000002',['contract_fulltime'],NOW,verified_link=True,staff_requested=True)
            worker.links.issue('900000002','contract_fulltime',schema('contract_fulltime'))
            other=worker.ledger.db.execute('SELECT token FROM checklist_links WHERE candidate_id=?',('900000002',)).fetchone()[0]
            r['answers']['1']['answer']=other
            with self.assertRaises(ReceiptHold):worker.links.accept(schema('contract_fulltime'),r)
            worker.ledger.db.close()
