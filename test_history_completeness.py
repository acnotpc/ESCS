import copy
import json
import unittest
from datetime import date
from history_completeness import analyse, FIELDS
TODAY=date(2026,10,12)

def schema():return {qid:{'qid':qid,'text':text,'type':typ} for qid,(text,typ) in FIELDS.items()}
def work(start='2016-10-12',end='2026-10-12'):
 return {'Employer Name':'Example organisation','Position Held':'Role or education','Date From':start,'Date To':end,
         'Contact Details Email':'contact@example.org','Telephone':'','Still in business':'Yes'}
def record():
 s=schema();answers={qid:dict(q,answer='') for qid,q in s.items()}
 for qid,value in {'21':json.dumps([work()]),'35':'','40':'','39':'Full 10 year history','36':'No',
                   '16':'signature-present','13':{'first':'Synthetic','last':'Person'},'15':{'year':'2026','month':'10','day':'12'}}.items():answers[qid]['answer']=value
 return {'id':'123','form_id':'211923425771051','status':'ACTIVE','created_at':'2026-10-12 15:00:00','answers':answers}

class CompletenessTests(unittest.TestCase):
 def result(self,r):return analyse(schema(),r,TODAY)
 def test_complete_history(self):self.assertEqual(self.result(record())['status'],'complete')
 def test_submission_anchor_does_not_create_gap_tomorrow(self):self.assertEqual(analyse(schema(),record(),date(2026,10,13))['status'],'complete')
 def test_clear_uncovered_period(self):
  r=record();r['answers']['21']['answer']=json.dumps([work('2020-01-01')])
  x=self.result(r);self.assertEqual(x['status'],'needs_information');self.assertIn('2016-10-12 to 2019-12-31',x['issues'][0])
 def test_explained_time_out_counts_equally(self):
  r=record();r['answers']['21']['answer']=json.dumps([work('2020-01-01')]);r['answers']['36']['answer']='Yes'
  r['answers']['35']['answer']=json.dumps([{'Reason For Gap In Employment':'Education or explained time out','Date From':'2016-10-12','Date To':'2019-12-31'}])
  self.assertEqual(self.result(r)['status'],'complete')
 def test_missing_date_does_not_speculate_full_gap(self):
  r=record();r['answers']['21']['answer']=json.dumps([work(end='')]);x=self.result(r)
  self.assertEqual(x['status'],'needs_information');self.assertTrue(any('date to' in i for i in x['issues']))
  self.assertFalse(any('Please account for' in i for i in x['issues']))
 def test_ambiguous_date_held(self):
  r=record();r['answers']['21']['answer']=json.dumps([work(end='12/10/26')]);self.assertEqual(self.result(r)['status'],'staff_review')
 def test_future_date_correction(self):
  r=record();r['answers']['21']['answer']=json.dumps([work(end='2027-01-01')]);self.assertTrue(any('after the submission date' in i for i in self.result(r)['issues']))
 def test_invalid_calendar_date_correction(self):
  r=record();r['answers']['21']['answer']=json.dumps([work(end='2026-02-30')]);self.assertTrue(any('invalid date to' in i for i in self.result(r)['issues']))
 def test_overlaps_go_to_staff_without_employee_correction(self):
  r=record();r['answers']['21']['answer']=json.dumps([work(),work('2020-01-01','2021-01-01')]);x=self.result(r)
  self.assertEqual(x['status'],'staff_review');self.assertEqual(x['issues'],[])
 def test_declaration_only_exception(self):
  r=record();r['answers']['39']['answer']='Declaration Only';self.assertEqual(self.result(r)['status'],'staff_review')
 def test_legacy_nonempty_section_held(self):
  r=record();r['answers']['40']['answer']='legacy data';self.assertEqual(self.result(r)['status'],'staff_review')
 def test_unknown_widget_encoding_held(self):
  r=record();r['answers']['21']['answer']='Employer Name: Example';self.assertEqual(self.result(r)['status'],'staff_review')
 def test_schema_change_held(self):
  s=schema();s['21']['type']='control_textbox';self.assertEqual(analyse(s,record(),TODAY)['status'],'staff_review')
 def test_normalizes_whitespace_in_widget_columns(self):
  r=record();w=work();w['Date   From']=w.pop('Date From');r['answers']['21']['answer']=json.dumps([w]);self.assertEqual(self.result(r)['status'],'complete')
 def test_missing_signature_and_declaration_date(self):
  r=record();r['answers']['16']['answer']='';r['answers']['15']['answer']='';x=self.result(r)
  self.assertTrue(any('sign the history' in i for i in x['issues']));self.assertTrue(any('declaration date' in i for i in x['issues']))
 def test_no_name_contact_reason_or_signature_content_in_output(self):
  r=record();r['answers']['21']['answer']=json.dumps([work('2020-01-01')]);x=json.dumps(self.result(r))
  for value in ('Example organisation','contact@example.org','Synthetic','signature-present'):
   self.assertNotIn(value,x)
 def test_gap_declaration_conflict_held(self):
  r=record();r['answers']['35']['answer']=json.dumps([{'Reason For Gap In Employment':'Reason','Date From':'2016-10-12','Date To':'2017-01-01'}]);self.assertEqual(self.result(r)['status'],'staff_review')
 def test_unknown_columns_held(self):
  r=record();w=work();w['Different Date']='value';r['answers']['21']['answer']=json.dumps([w]);self.assertEqual(self.result(r)['status'],'staff_review')
