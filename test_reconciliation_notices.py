import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock
from automatic_followup import AutomaticSelection,names,normalized,values,data,PAUSE
from reconciliation_notices import Notices,contact,STATUS,BODY,WORDING
from recruitment_checklist import message
from recruitment_checklist_access import FORMS
from checklist_workflow import ChecklistWorkflow,REQUEST
from test_automatic_followup import person,change,AutoBoard,Pages
from test_extended_followup import ExtendedSource
from test_checklist_workflow import NOW
class NoticeBoard(AutoBoard):
    async def call(self,q,v=None):
        if 'change_multiple_column_values' in q:
            import json
            updates=json.loads(v['value'])
            if STATUS in updates:
                self.writes.append(updates);return {'change_multiple_column_values':{'id':v['item']}}
        return await super().call(q,v)
def pending():
    r=person();r['group']['id']='new_group';change(r,'status53','Pending Review');change(r,'acceptance','Awaiting Return');return r
def counts(rows):
    emails={};ns={}
    for r in rows:
        key=normalized(data(values(r),'email').get('email',''));emails[key]=emails.get(key,0)+1
        for n in names(r['name']):ns[n]=ns.get(n,0)+1
    return emails,ns
class Tests(unittest.IsolatedAsyncioTestCase):
    def test_wording_and_routing(self):
        self.assertIn(WORDING,message({'history'}));self.assertIn(WORDING,BODY)
        self.assertIn('does not mean you need to submit documents again',BODY)
        r=pending();self.assertEqual(contact(r,*counts([r]))[0],'pre_employment')
        r=person();self.assertEqual(contact(r,*counts([r]))[0],'employed')
    def test_exclusions(self):
        for label in ['No','Sent Closed Email','Sent closing email']:
            r=pending();change(r,'acceptance',label);self.assertIsNone(contact(r,*counts([r])))
        r=pending();change(r,PAUSE,value={'checked':'true'});self.assertIsNone(contact(r,*counts([r])))
        r=pending();self.assertIsNone(contact(r,*counts([r,r])))
        r=pending();r['group']['id']='new_group18301';self.assertIsNone(contact(r,*counts([r])))
    async def test_once_budget_and_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            r=pending();board=NoticeBoard([r]);sender=AsyncMock();worker=ChecklistWorkflow(folder+'/db',board,ExtendedSource(),sender)
            auto=AutomaticSelection(worker);await auto.index.refresh(Pages(),NOW);n=Notices(auto)
            await n.run([r],*counts([r]),len(FORMS),NOW)
            await Notices(auto).run([r],*counts([r]),len(FORMS),NOW)
            self.assertEqual(sender.send.await_count,1)
            other=pending();other['id']='900000002';other['name']='Other Applicant';change(other,'email',value={'email':'other@example.org'})
            board.rows.append(other);await n.run(board.rows,*counts(board.rows),len(FORMS),NOW)
            self.assertEqual(sender.send.await_count,1)
            r['group']['id']='topics';change(r,'status53','Employed with ESCS');change(r,REQUEST,value={'ids':[10]})
            worker.reconcile(r,NOW+timedelta(hours=1),enroll=True)
            self.assertEqual(worker.ledger.db.execute('SELECT sends FROM checklist_candidate').fetchone()[0],1)
            self.assertFalse(worker.ledger.plan(r['id'],NOW+timedelta(hours=71)));worker.ledger.db.close()
    async def test_uncertain_no_retry_future_hold(self):
        with tempfile.TemporaryDirectory() as folder:
            r=pending();sender=AsyncMock();sender.send.side_effect=RuntimeError('uncertain')
            worker=ChecklistWorkflow(folder+'/db',NoticeBoard([r]),ExtendedSource(),sender);auto=AutomaticSelection(worker)
            await auto.index.refresh(Pages(),NOW);n=Notices(auto)
            await n.run([r],*counts([r]),len(FORMS),NOW);await Notices(auto).run([r],*counts([r]),len(FORMS),NOW)
            self.assertEqual(sender.send.await_count,1)
            r['group']['id']='topics';change(r,'status53','Employed with ESCS');change(r,REQUEST,value={'ids':[10]})
            worker.reconcile(r,NOW,enroll=True);self.assertTrue(worker.review(r['id'])['deliveryUncertain']);worker.ledger.db.close()
    async def test_sources_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            r=pending();worker=ChecklistWorkflow(folder+'/db',NoticeBoard([r]),ExtendedSource(),AsyncMock());auto=AutomaticSelection(worker);n=Notices(auto)
            await n.run([r],*counts([r]),0,NOW);self.assertFalse(worker.ledger.db.execute('SELECT * FROM reconciliation_scope').fetchall())
            worker.ledger.db.execute("INSERT INTO reconciliation_notice(item_id,state) VALUES(?,'reserved')",(r['id'],));worker.ledger.db.commit()
            Notices(auto);self.assertEqual(worker.ledger.db.execute('SELECT state FROM reconciliation_notice').fetchone()[0],'unknown');worker.ledger.db.close()
