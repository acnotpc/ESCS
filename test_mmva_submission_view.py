import asyncio,copy,json,os,sqlite3,tempfile,unittest
from unittest.mock import patch
from urllib.parse import urlparse,parse_qs
import httpx
from fastapi import HTTPException
from mmva_submission_view import SubmissionViewer,project_details
from mmva_completion import SPECS,report
from test_mmva_completion import SOURCE,CODE,scans,row
import test_mmva_trainer_picker as picker

def raw():return {'id':'55','form_id':SPECS['health'][0],'status':'ACTIVE','created_at':'2026-11-02 08:00:00','ip':'PRIVATE-IP','answers':{'81':{'text':'Course Number','type':'control_textbox','answer':CODE},'80':{'text':'<b>Support required</b>','type':'control_textarea','answer':'Discuss adjustments privately'},'78':{'text':'Private discussion','type':'control_radio','answer':'Yes'},'79':{'text':'Signature','type':'control_signature','answer':'PRIVATE-SIGNATURE'},'90':{'text':'Document','type':'control_fileupload','answer':['https://private-file.invalid/token']},'91':{'text':'Not answered','type':'control_textbox','answer':''},'92':{'text':'Details table','type':'control_widget','answer':{'1':'["Detail","Yes"]'}}}}
class SubmissionTests(unittest.TestCase):
 def test_projection_returns_answers_but_no_metadata_or_private_assets(self):
  r=project_details(raw(),'health',CODE);text=json.dumps(r);self.assertIn('Discuss adjustments privately',text);self.assertIn('Support required',text);self.assertNotIn('<b>',text);self.assertNotIn('PRIVATE-IP',text);self.assertNotIn('PRIVATE-SIGNATURE',text);self.assertNotIn('private-file.invalid',text);self.assertNotIn('Not answered',text);self.assertIn('Detail',text)
 def test_wrong_course_blank_course_wrong_form_and_deleted_fail_closed(self):
  for change in ['wrong_course','blank_course','wrong_form','deleted']:
   r=raw()
   if change.endswith('course'):r['answers']['81']['answer']='OTHER' if change=='wrong_course' else ''
   if change=='wrong_form':r['form_id']=SPECS['fitness'][0]
   if change=='deleted':r['status']='DELETED'
   with self.assertRaises(HTTPException):project_details(r,'health',CODE)
 def test_reader_uses_header_and_rejects_upstream_errors_or_mismatched_id(self):
  def handler(req):
   self.assertEqual(req.method,'GET');self.assertEqual(req.headers['APIKEY'],'test-key');self.assertNotIn('test-key',str(req.url));return httpx.Response(200,json={'responseCode':200,'content':raw()})
  result=asyncio.run(SubmissionViewer('test-key',httpx.MockTransport(handler)).read('health','55',CODE));self.assertEqual(result['course'],CODE)
  for status,payload in [(503,{}),(200,{'responseCode':200,'content':dict(raw(),id='999')})]:
   def failure(req):return httpx.Response(status,json=payload)
   with self.assertRaises(HTTPException):asyncio.run(SubmissionViewer('key',httpx.MockTransport(failure)).read('health','55',CODE))
  with self.assertRaises(HTTPException):asyncio.run(SubmissionViewer('key').read('not-allowed','55',CODE))
 def test_all_review_links_point_to_course_scoped_portal(self):
  data=scans()
  for key in SPECS:data[key]['rows']=[row()]
  r=report(SOURCE,data,[],'2026-11-02')
  entries=[e for p in r['delegates'] for form in p['forms'].values() for e in form['entries']]+[e for form in r['courseForms'].values() for e in form['entries']]
  for e in entries:
   parsed=urlparse(e['url']);self.assertEqual(parsed.path,'/attendance/trainer/submission');self.assertEqual(parse_qs(parsed.query)['course'],[CODE]);self.assertEqual(parsed.netloc,'')
 def test_protected_endpoint_no_cache_and_access_audit(self):
  with tempfile.TemporaryDirectory() as folder:
   env,c=picker.PickerTests().setup_client(folder)
   class Source:
    async def resolve(self,*a,**kw):return SOURCE
   class Viewer:
    def __init__(self,key):pass
    async def read(self,form,sid,course):return project_details(raw(),form,course)
   with patch.dict(os.environ,env),patch('mmva_attendance.MondayCourses',return_value=Source()),patch('mmva_attendance.SubmissionViewer',Viewer):
    url='/attendance/trainer/submission-data';data={'code':CODE,'form':'health','submission':'55'}
    self.assertEqual(c.post(url,json=data).status_code,401)
    c.post('/attendance/trainer/login',json={'email':'trainer@example.invalid','password':'test-only-trainer-password'})
    result=c.post(url,json=data);self.assertEqual(result.status_code,200);self.assertEqual(result.headers['cache-control'],'no-store');self.assertIn('Discuss adjustments privately',result.text);self.assertNotIn('APIKEY',result.text)
    with sqlite3.connect(env['MMVA_ATTENDANCE_DB']) as db:
     rows=db.execute('SELECT course,form,submission,actor FROM mmva_submission_access').fetchall();self.assertEqual(rows,[(CODE,'health','55','trainer@example.invalid')])
