import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from checklist_workflow import (ChecklistWorkflow, REQUEST, RECEIVED, SUMMARY, BOARD,
    identity, render, MondayFollowup, WorkflowSource, FollowupSender, LABELS, RESUME)
from recruitment_checklist_access import FORMS, SIGNATURES, CHOICE
from recruitment_tracking import TRACKING
import httpx
from unittest.mock import patch

NOW = datetime(2026,10,12,15,tzinfo=timezone.utc)

def record(ids=(1,2,4), received=(), stage='employed'):
    cols = [{ 'id':k,'type':t,'text':text,'value':json.dumps(v)} for k,t,text,v in [
        (REQUEST,'dropdown','',{'ids':list(ids)}), (RECEIVED,'dropdown','',{'ids':list(received)}),
        (SUMMARY,'long_text','',{'text':''}),(RESUME,'checkbox','',{}),('email','email','andy@securecareservices.co.uk',{'email':'andy@securecareservices.co.uk'}),
        ('status53','status','Director' if stage=='employed' else 'Awaiting Start Date',{'index':12}),
        ('offerletter_new_starter_jotform','date','2026-10-01',{'date':'2026-10-01'})]]
    return {'id':'1005891310','state':'active','board':{'id':BOARD},
            'group':{'id':'topics' if stage=='employed' else 'new_group57757'},'column_values':cols}

def schema(kind):
    if kind=='history':
        from test_history_completeness import schema as history_schema
        rows=history_schema()
        rows['1']={'qid':'1','text':TRACKING,'type':'control_textbox','hidden':'Yes','required':'No','name':'requestID'}
        return rows
    rows = {'1':{'qid':'1','text':TRACKING,'type':'control_textbox','hidden':'Yes','required':'No','name':'requestID'},
            '2':{'qid':'2','text':SIGNATURES[kind],'type':'control_signature'}}
    if kind=='working_time':
        rows['3']={'qid':'3','text':CHOICE,'type':'control_radio','options':'IN|OUT|CANCEL OPT OUT'}
    return rows

class Board:
    def __init__(self, item=None):
        self.current = item or record()
        self.writes=[]
    async def clear_resume(self,item_id):
        next(c for c in self.current['column_values'] if c['id']==RESUME)['value']='{}'
    async def schema(self):return True
    async def requested(self):return [copy.deepcopy(self.current)]
    async def item(self,item_id):return copy.deepcopy(self.current)
    async def summary(self,item_id,text):
        self.writes.append(text)
        next(c for c in self.current['column_values'] if c['id']==SUMMARY)['text']=text

class Source:
    def __init__(self):self.rows={k:[] for k in FORMS};self.fail=False
    async def schema(self,form):return schema(next(k for k,v in FORMS.items() if v==form))
    async def page(self,form,limit=100):return []
    async def after(self,form,baseline):
        if self.fail:raise RuntimeError('failed')
        return copy.deepcopy(self.rows[next(k for k,v in FORMS.items() if v==form)])
    def receipt(self,worker,kind,sid,choice='IN',token=None):
        token = token or worker.ledger.db.execute('SELECT token FROM checklist_links WHERE kind=?',(kind,)).fetchone()[0]
        answers={qid:dict(q,answer=token if qid=='1' else 'signed' if qid=='2' else choice) for qid,q in schema(kind).items()}
        row={'id':str(sid),'form_id':FORMS[kind],'status':'ACTIVE','answers':answers}
        if kind=='history':
            from test_history_completeness import record as history_record
            row=history_record()
            row.update(id=str(sid))
            row['answers']['1']={'qid':'1','text':TRACKING,'type':'control_textbox','answer':token}
        self.rows[kind].append(row)

class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=self.temp.name+'/test.sqlite'
        self.board=Board();self.source=Source();self.sender=AsyncMock()
        self.worker=ChecklistWorkflow(self.path,self.board,self.source,self.sender)
        # Campaign cases vary their synthetic stage; the fixed Director probe
        # is covered independently in ProgressProbeTests.
        self.worker.probe_done=True
    async def asyncTearDown(self):
        self.worker.ledger.db.close();self.temp.cleanup()
    async def test_combined_partial_full_receipt_stops_after_restart(self):
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,1)
        args=self.sender.send.await_args.args
        self.assertEqual(args[:2],('employed','andy@securecareservices.co.uk'))
        self.assertEqual(args[3].count('Open secure form'),3)
        self.assertNotIn('jobs@',args[3]); self.assertNotIn('1005891310',args[3])
        self.source.receipt(self.worker,'medical',10)
        await self.worker.run(NOW+timedelta(hours=73))
        self.assertEqual(self.sender.send.await_count,2)
        self.assertNotIn('medical disclosure form',self.sender.send.await_args.args[3])
        self.source.receipt(self.worker,'history',11)
        self.source.receipt(self.worker,'working_time',12,'OUT')
        state=await self.worker.run(NOW+timedelta(hours=146))
        self.assertEqual(state['received'],1);self.assertEqual(state['active'],0)
        self.worker.ledger.db.close()
        self.worker=ChecklistWorkflow(self.path,self.board,self.source,self.sender)
        await self.worker.run(NOW+timedelta(days=30))
        self.assertEqual(self.sender.send.await_count,2)
    async def test_in_stops_equally(self):
        self.board.current=record((4,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'working_time',20,'IN')
        self.assertEqual((await self.worker.run(NOW+timedelta(days=4)))['received'],1)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_staff_receipts_stop_rtw_dbs_without_approval_changes(self):
        self.board.current=record((3,5))
        await self.worker.run(NOW)
        self.assertNotIn('92583319416361',self.sender.send.await_args.args[3])
        next(c for c in self.board.current['column_values'] if c['id']==RECEIVED)['value']='{"ids":[3,5]}'
        state=await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(state['received'],1);self.assertEqual(self.sender.send.await_count,1)
        self.assertIn('Staff review remains required',self.board.writes[-1])
    async def test_unknown_delivery_held_no_resend(self):
        self.sender.send.side_effect=RuntimeError('unknown')
        self.assertEqual((await self.worker.run(NOW))['review'],1)
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
    async def test_reserved_restart_held(self):
        self.worker.reconcile(self.board.current,NOW,enroll=True)
        self.worker.ledger.reserve('1005891310',NOW)
        self.worker.ledger.db.close()
        self.worker=ChecklistWorkflow(self.path,self.board,self.source,self.sender)
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,0)
    async def test_receipt_outage_blocks_sending(self):
        await self.worker.run(NOW)
        self.source.fail=True
        self.assertFalse((await self.worker.run(NOW+timedelta(days=4)))['connected'])
        self.assertEqual(self.sender.send.await_count,1)
    async def test_unknown_token_does_not_stop(self):
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'medical',30,token='0'*32)
        state=await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(state['active'],1);self.assertEqual(self.sender.send.await_count,2)
    async def test_known_invalid_signature_holds(self):
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'medical',31)
        self.source.rows['medical'][0]['answers']['2']['answer']=''
        self.assertEqual((await self.worker.run(NOW+timedelta(days=4)))['review'],1)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_removed_request_stops(self):
        await self.worker.run(NOW)
        next(c for c in self.board.current['column_values'] if c['id']==REQUEST)['value']='{"ids":[]}'
        self.assertEqual((await self.worker.run(NOW+timedelta(days=4)))['review'],1)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_email_change_stops(self):
        await self.worker.run(NOW)
        next(c for c in self.board.current['column_values'] if c['id']=='email')['value']='{"email":"different@example.org"}'
        self.assertEqual((await self.worker.run(NOW+timedelta(days=4)))['review'],1)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_leaver_stops(self):
        await self.worker.run(NOW)
        next(c for c in self.board.current['column_values'] if c['id']=='status53')['text']='Leaver'
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
    async def test_reminder_limit(self):
        for days in (0,4,8,12,16,20):await self.worker.run(NOW+timedelta(days=days))
        self.assertEqual(self.sender.send.await_count,4)
        self.assertEqual(self.worker.state['review'],1)
    async def test_empty_selection_no_campaign(self):
        self.board.current=record(())
        state=await self.worker.run(NOW)
        self.assertEqual(state['campaigns'],0);self.assertEqual(self.sender.send.await_count,0)
    async def test_pre_employment_routes_jobs_stage(self):
        self.board.current=record((1,4),stage='pre_employment')
        await self.worker.run(NOW)
        args=self.sender.send.await_args.args
        self.assertEqual(args[0],'pre_employment');self.assertIn('jobs@',args[3]);self.assertNotIn('hr@',args[3])
    async def test_pre_employment_medical_without_stage_evidence_blocked(self):
        self.board.current=record((2,),stage='pre_employment')
        next(c for c in self.board.current['column_values'] if c['id']=='offerletter_new_starter_jotform')['value']='{}'
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,0)
    async def test_second_cycle_unchanged_does_not_write_or_send(self):
        await self.worker.run(NOW)
        count=len(self.board.writes)
        await self.worker.run(NOW+timedelta(minutes=5))
        self.assertEqual(len(self.board.writes),count)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_schema_failure_blocks(self):
        self.source.schema=AsyncMock(side_effect=RuntimeError('bad schema'))
        self.assertFalse((await self.worker.run(NOW))['connected'])
        self.assertEqual(self.sender.send.await_count,0)

class IdentityTests(unittest.TestCase):
    def test_wrong_board(self):
        item=record();item['board']['id']='99'
        with self.assertRaises(ValueError):identity(item)
    def test_unknown_selector(self):
        item=record();item['column_values'][0]['value']='{"ids":[99]}'
        with self.assertRaises(ValueError):identity(item)
    def test_email_header_injection(self):
        item=record();next(c for c in item['column_values'] if c['id']=='email')['value']=json.dumps({'email':'andy@example.org\nBCC: other@example.org'})
        with self.assertRaises(ValueError):identity(item)

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_board_scope_pagination_and_summary_write(self):
        calls=[]
        def handler(req):
            body=json.loads(req.content);calls.append(body)
            q=body['query']
            if 'change_multiple_column_values' in q:
                self.assertEqual(json.loads(body['variables']['value']),{SUMMARY:{'text':'received'}})
                data={'change_multiple_column_values':{'id':'1005891310'}}
            elif 'next_items_page' in q:
                self.assertEqual(body['variables']['cursor'],'opaque-cursor')
                data={'next_items_page':{'cursor':None,'items':[]}}
            elif 'items_page' in q:
                self.assertIn('boards(ids:[424237712])',q)
                self.assertIn('operator:is_not_empty',q)
                data={'boards':[{'id':BOARD,'items_page':{'cursor':'opaque-cursor','items':[record()]}}]}
            else:
                data={'items':[record()]}
            return httpx.Response(200,json={'data':data})
        with patch.dict('os.environ',{'MONDAY_RECRUITMENT_API_TOKEN':'test-only'}):
            board=MondayFollowup(httpx.MockTransport(handler))
            self.assertEqual(len(await board.requested()),1)
            await board.summary('1005891310','received')
        self.assertEqual(len(calls),3)
    async def test_schema_matches_live_columns(self):
        cols=[{'id':key,'type':typ,'settings':{'labels':[{'id':i,'label':label} for i,(_,label) in LABELS.items()]}}
              for key,typ in [(REQUEST,'dropdown'),(RECEIVED,'dropdown'),(SUMMARY,'long_text'),(RESUME,'checkbox'),('email','email'),
                              ('status53','status'),('offerletter_new_starter_jotform','date')]]
        board=MondayFollowup();board.call=AsyncMock(return_value={'boards':[{'id':BOARD,'columns':cols}]})
        self.assertTrue(await board.schema())
        cols[0]['settings']={'labels':[{'id':1,'label':'Changed'}]}
        with self.assertRaises(RuntimeError):await board.schema()
    async def test_receipt_scan_wrong_form_and_order_rejected(self):
        source=WorkflowSource('test-only')
        source.page=AsyncMock(return_value=[{'id':'10','form_id':'99'}])
        with self.assertRaises(RuntimeError):await source.after(FORMS['medical'],0)
        source.page=AsyncMock(return_value=[{'id':'10','form_id':FORMS['medical']},{'id':'11','form_id':FORMS['medical']}])
        with self.assertRaises(RuntimeError):await source.after(FORMS['medical'],0)
    async def test_sender_jobs_exact_payload(self):
        calls=[]
        def handler(req):
            body=json.loads(req.content);calls.append((req.url.path,body))
            if req.url.path.endswith('mail.mailbox.get'):
                result={'item':{'id':107,'email':'jobs@securecareservices.co.uk'}}
            else:
                self.assertEqual(body['from'],'jobs@securecareservices.co.uk')
                self.assertEqual(body['to'],['andy@securecareservices.co.uk'])
                self.assertNotIn('cc',body);self.assertNotIn('bcc',body)
                result={'success':True,'to':['andy@securecareservices.co.uk']}
            return httpx.Response(200,json={'result':result})
        with patch.dict('os.environ',{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test-only/',
                                      'BITRIX_RECRUITMENT_MAILBOX_ID':'107'}):
            await FollowupSender(httpx.MockTransport(handler)).send('pre_employment','andy@securecareservices.co.uk','subject','body')
        self.assertEqual(len(calls),2)
    async def test_sender_hr_exact_payload(self):
        sent=[]
        def handler(req):
            body=json.loads(req.content)
            if req.url.path.endswith('mail.mailbox.list'):
                result={'items':[{'id':108,'email':'hr@securecareservices.co.uk'}]}
            elif req.url.path.endswith('mail.mailbox.get'):
                result={'item':{'id':108,'email':'hr@securecareservices.co.uk'}}
            elif req.url.path.endswith('mail.mailbox.senders'):
                result={'items':[{'email':'hr@securecareservices.co.uk'}]}
            else:
                sent.append(body)
                self.assertEqual(body['from'],'hr@securecareservices.co.uk')
                self.assertEqual(body['to'],['andy@securecareservices.co.uk'])
                result={'success':True,'to':['andy@securecareservices.co.uk']}
            return httpx.Response(200,json={'result':result})
        with patch.dict('os.environ',{'BITRIX_RECRUITMENT_MAIL_REST_URL':'https://escs.bitrix24.com/rest/1/test-only/'}):
            await FollowupSender(httpx.MockTransport(handler)).send('employed','andy@securecareservices.co.uk','subject','body')
        self.assertEqual(len(sent),1)

class ProgressProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_andy_email_does_not_block_connection_but_blocks_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            board=Board(record(()));source=Source();sender=AsyncMock()
            next(c for c in board.current['column_values'] if c['id']=='email')['value']='{}'
            worker=ChecklistWorkflow(temp+'/ledger.sqlite',board,source,sender)
            try:
                state=await worker.run(NOW)
                self.assertTrue(state['connected']);self.assertTrue(state['boardProgressVerified'])
                self.assertEqual(sender.send.await_count,0)
                next(c for c in board.current['column_values'] if c['id']==REQUEST)['value']='{"ids":[1]}'
                await worker.run(NOW+timedelta(minutes=5))
                self.assertEqual(sender.send.await_count,0)
                self.assertIn('Email missing or invalid',board.writes[-1])
            finally:worker.ledger.db.close()

class ResumeTests(WorkflowTests):
    def tick(self):
        next(c for c in self.board.current['column_values'] if c['id']==RESUME)['value']='{"checked":"true"}'
    async def test_reviewed_identity_change_preserves_due_count_and_receipts(self):
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'medical',601)
        next(c for c in self.board.current['column_values'] if c['id']=='email')['value']='{"email":"correct@example.org"}'
        await self.worker.run(NOW+timedelta(minutes=5))
        self.assertEqual(self.worker.state['review'],1)
        self.tick()
        await self.worker.run(NOW+timedelta(minutes=10))
        self.assertEqual(self.worker.state['active'],1)
        self.assertEqual(self.sender.send.await_count,1)
        self.assertFalse(self.worker.review('1005891310')['deliveryUncertain'])
        await self.worker.run(NOW+timedelta(hours=73))
        self.assertEqual(self.sender.send.await_count,2)
        self.assertEqual(self.sender.send.await_args.args[1],'correct@example.org')
        self.assertNotIn('medical disclosure form',self.sender.send.await_args.args[3])
    async def test_uncertain_send_cannot_resume(self):
        self.sender.send.side_effect=RuntimeError('timeout')
        await self.worker.run(NOW)
        self.assertTrue(self.worker.review('1005891310')['deliveryUncertain'])
        self.tick()
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
        self.assertEqual(self.worker.state['review'],1)
        self.assertIn('resume blocked',self.board.writes[-1])
    async def test_tick_is_consumed_and_does_not_resume_later_hold(self):
        self.worker.reconcile(self.board.current,NOW,enroll=True)
        self.worker.hold('1005891310','Verify current routing.')
        self.tick()
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,1)
        self.worker.hold('1005891310','Another issue.')
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
    async def test_legacy_no_link_no_attempt_proof(self):
        self.worker.reconcile(self.board.current,NOW,enroll=True)
        self.worker.hold('1005891310','Routing incomplete.')
        self.worker.ledger.db.close()
        self.worker=ChecklistWorkflow(self.path,self.board,self.source,self.sender)
        self.assertFalse(self.worker.review('1005891310')['deliveryUncertain'])
        self.tick()
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,1)
    async def test_legacy_linked_unknown_not_assumed_unsent(self):
        self.worker.reconcile(self.board.current,NOW,enroll=True)
        self.worker.links.issue('1005891310','medical',schema('medical'))
        self.worker.hold('1005891310','Old unresolved hold.')
        self.worker.ledger.db.close()
        self.worker=ChecklistWorkflow(self.path,self.board,self.source,self.sender)
        self.assertTrue(self.worker.review('1005891310')['deliveryUncertain'])
        self.tick()
        await self.worker.run(NOW)
        self.assertEqual(self.sender.send.await_count,0)
    async def test_resume_cannot_reset_exhausted_count(self):
        for days in (0,4,8,12,16):await self.worker.run(NOW+timedelta(days=days))
        self.tick()
        await self.worker.run(NOW+timedelta(days=20))
        self.assertEqual(self.sender.send.await_count,4)
    async def test_resume_cannot_reopen_completed(self):
        await self.worker.run(NOW)
        for i,kind in enumerate(('history','medical','working_time')):self.source.receipt(self.worker,kind,700+i)
        await self.worker.run(NOW+timedelta(days=1))
        self.tick()
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
    async def test_invalid_receipt_cannot_resume(self):
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'medical',801)
        self.source.rows['medical'][0]['answers']['2']['answer']=''
        await self.worker.run(NOW+timedelta(minutes=5))
        self.tick()
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)

class HistoryWorkflowTests(WorkflowTests):
    async def test_incomplete_history_targeted_correction_then_complete_stops(self):
        from test_history_completeness import work
        self.board.current=record((1,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'history',900)
        self.source.rows['history'][0]['answers']['21']['answer']=json.dumps([work('2020-01-01')])
        await self.worker.run(NOW+timedelta(minutes=5))
        self.assertEqual(self.sender.send.await_count,1)
        self.assertEqual(self.worker.state['active'],1)
        self.assertIn('2016-10-12 to 2019-12-31',self.board.writes[-1])
        await self.worker.run(NOW+timedelta(hours=73))
        self.assertEqual(self.sender.send.await_count,2)
        body=self.sender.send.await_args.args[3]
        self.assertIn('<li>Please account for 2016-10-12 to 2019-12-31',body)
        self.assertIn('Open secure form',body)
        self.assertNotIn('Example organisation',body)
        self.source.receipt(self.worker,'history',901)
        self.assertEqual((await self.worker.run(NOW+timedelta(hours=74)))['received'],1)
        await self.worker.run(NOW+timedelta(days=30))
        self.assertEqual(self.sender.send.await_count,2)
    async def test_history_ambiguity_hold_not_generic_chase_after_resume(self):
        self.board.current=record((1,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'history',902)
        self.source.rows['history'][0]['answers']['39']['answer']='Declaration Only'
        await self.worker.run(NOW+timedelta(minutes=5))
        self.assertEqual(self.worker.state['review'],1)
        next(c for c in self.board.current['column_values'] if c['id']==RESUME)['value']='{"checked":"true"}'
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
    async def test_staff_receipt_cannot_override_known_incomplete_history(self):
        from test_history_completeness import work
        self.board.current=record((1,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'history',903)
        self.source.rows['history'][0]['answers']['21']['answer']=json.dumps([work('2020-01-01')])
        next(c for c in self.board.current['column_values'] if c['id']==RECEIVED)['value']='{"ids":[1]}'
        state=await self.worker.run(NOW+timedelta(minutes=5))
        self.assertEqual(state['active'],1);self.assertEqual(state['received'],0)
    async def test_changed_latest_receipt_does_not_restore_an_older_version(self):
        self.board.current=record((1,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'history',905)
        await self.worker.run(NOW+timedelta(minutes=5))
        self.source.rows['history']=[]
        self.source.receipt(self.worker,'history',904)
        await self.worker.run(NOW+timedelta(days=4))
        self.assertEqual(self.sender.send.await_count,1)
        self.assertEqual(self.worker.state['review'],1)
    async def test_old_history_receipt_cannot_move_to_another_person_after_correction(self):
        self.board.current=record((1,))
        await self.worker.run(NOW)
        self.source.receipt(self.worker,'history',906)
        first=copy.deepcopy(self.source.rows['history'][0])
        await self.worker.run(NOW+timedelta(minutes=5))
        self.source.receipt(self.worker,'history',907)
        await self.worker.run(NOW+timedelta(minutes=10))
        self.worker.ledger.request('999',['history'],NOW,verified_link=True,staff_requested=True)
        self.worker.history_result('999',schema('history'),first)
        self.assertEqual(self.worker.review('999')['state'],'staff_review')
        owner=self.worker.ledger.db.execute('SELECT candidate_id FROM history_receipt_owner WHERE submission_id=?',('906',)).fetchone()[0]
        self.assertEqual(owner,'1005891310')
