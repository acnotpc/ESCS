import copy
import json
import tempfile
import unittest
from datetime import date,timedelta
from unittest.mock import AsyncMock
from automatic_followup import AutomaticSelection,values,data,names,normalized
from licence_expiry import LicenceExpiry,EXPIRY,CHECK_STATUS,REMINDER_STATUS,milestone,message
from checklist_workflow import ChecklistWorkflow
from test_automatic_followup import person,change,AutoBoard
from test_extended_followup import ExtendedSource
from test_checklist_workflow import NOW

class LicenceBoard(AutoBoard):
    async def call(self,q,v=None):
        if 'change_multiple_column_values' in q:
            r=next(r for r in self.rows if r['id']==v['item']);updates=json.loads(v['value']);self.writes.append(updates)
            for col,val in updates.items():
                change(r,col,val if isinstance(val,str) else val.get('label',''),val if isinstance(val,dict) else {})
            return {'change_multiple_column_values':{'id':r['id']}}
        return await super().call(q,v)
def counts(rows):
    ec={};nc={}
    for r in rows:
        e=normalized(data(values(r),'email').get('email',''));ec[e]=ec.get(e,0)+1
        for n in names(r['name']):nc[n]=nc.get(n,0)+1
    return ec,nc
class Tests(unittest.IsolatedAsyncioTestCase):
    def test_calendar_thresholds(self):
        today=date(2026,10,12)
        for days,want in [(61,None),(60,60),(31,60),(30,30),(8,30),(7,7),(1,7),(0,None),(-1,None)]:
            self.assertEqual(milestone(today+timedelta(days=days),today),want)
    def test_message_conditional_and_actual_date(self):
        body=message(date(2026,11,10),date(2026,10,12))
        self.assertIn('10 November 2026',body);self.assertIn('29 days remaining',body)
        self.assertIn('requirement of your role',body);self.assertIn('employment at risk',body)
        self.assertIn('not automatic',body);self.assertIn('Do not email identity documents',body)
    async def exercise(self,fail=False):
        r=person();change(r,EXPIRY,value={'date':'2026-12-11'})
        folder=tempfile.TemporaryDirectory();board=LicenceBoard([r]);sender=AsyncMock()
        if fail:sender.send.side_effect=RuntimeError('uncertain')
        worker=ChecklistWorkflow(folder.name+'/db',board,ExtendedSource(),sender);auto=AutomaticSelection(worker);monitor=LicenceExpiry(auto)
        return folder,r,board,sender,worker,auto,monitor
    async def test_60_30_7_once_and_renewal(self):
        folder,r,board,sender,w,auto,m=await self.exercise()
        try:
            await m.run([r],*counts([r]),NOW);await LicenceExpiry(auto).run([r],*counts([r]),NOW)
            self.assertEqual(sender.send.await_count,1);self.assertEqual(sender.send.await_args.args[0],'employed')
            await m.run([r],*counts([r]),NOW+timedelta(days=30));await m.run([r],*counts([r]),NOW+timedelta(days=53))
            self.assertEqual(sender.send.await_count,3)
            change(r,EXPIRY,value={'date':'2030-12-11'});await m.run([r],*counts([r]),NOW+timedelta(days=54))
            self.assertEqual(sender.send.await_count,3)
        finally:w.ledger.db.close();folder.cleanup()
    async def test_unknown_blocks_all_later_reminders(self):
        folder,r,b,s,w,a,m=await self.exercise(True)
        try:
            await m.run([r],*counts([r]),NOW);await LicenceExpiry(a).run([r],*counts([r]),NOW+timedelta(days=30))
            self.assertEqual(s.send.await_count,1)
        finally:w.ledger.db.close();folder.cleanup()
    async def test_expired_flag_once_never_auto_restores(self):
        folder,r,b,s,w,a,m=await self.exercise()
        try:
            change(r,EXPIRY,value={'date':'2026-10-11'});await m.run([r],*counts([r]),NOW)
            self.assertEqual(values(r)[CHECK_STATUS]['text'],'Expired');self.assertEqual(s.send.await_count,0)
            n=len(b.writes);await m.run([r],*counts([r]),NOW);self.assertEqual(len(b.writes),n)
            change(r,EXPIRY,value={'date':'2030-10-11'});await m.run([r],*counts([r]),NOW)
            self.assertEqual(values(r)[CHECK_STATUS]['text'],'Expired')
        finally:w.ledger.db.close();folder.cleanup()
    async def test_preemployment_duplicate_paused_invalid_and_stale_no_send(self):
        folder,r,b,s,w,a,m=await self.exercise()
        try:
            other=copy.deepcopy(r);other['id']='900000002';await m.run([r,other],*counts([r,other]),NOW)
            self.assertEqual(s.send.await_count,0)
            change(r,'boolean_mm82qkbk',value={'checked':'true'});await m.run([r],*counts([r]),NOW);self.assertEqual(s.send.await_count,0)
            change(r,'boolean_mm82qkbk');r['group']['id']='new_group88715';change(r,'status53','To be processed by Recruitment')
            await m.run([r],*counts([r]),NOW);self.assertEqual(s.send.await_count,0)
            r['group']['id']='topics';change(r,'status53','Employed with ESCS');b.changed=True
            await m.run([r],*counts([r]),NOW);self.assertEqual(s.send.await_count,0)
        finally:w.ledger.db.close();folder.cleanup()
