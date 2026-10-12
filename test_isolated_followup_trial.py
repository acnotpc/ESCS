import hashlib
import tempfile
import unittest
from unittest.mock import patch
import httpx
import isolated_followup_trial as trial


class LiveTrialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rows = []
        self.board_ok = True
        self.tokens = {'history':'a'*32, 'working_time':'b'*32}
        self.hashes = {k:hashlib.sha256(v.encode()).hexdigest() for k,v in self.tokens.items()}
        self.patch = patch.dict(trial.REQUEST_HASHES, self.hashes, clear=True)
        self.patch.start()
        self.path = self.tmp.name+'/trial.sqlite'
        self.transport = httpx.MockTransport(self.handle)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def handle(self, request):
        if request.url.host == 'api.monday.com':
            self.assertNotIn('mutation', request.content.decode())
            self.assertIn('1005891310', request.content.decode())
            return httpx.Response(200, json={'data':{'items':[{'id':'1005891310',
                'name':'Andy Challoner' if self.board_ok else 'Different record',
                'board':{'id':'424237712'}}]}})
        self.assertEqual('GET', request.method)
        self.assertTrue(request.url.path.startswith('/form/'+trial.FORM+'/'))
        data = self.rows if request.url.path.endswith('/submissions') else {
            '1':{'text':trial.TRACKING,'type':'control_textbox','hidden':'Yes','required':'No'}}
        return httpx.Response(200, json={'responseCode':200,'content':data})

    def record(self, kind, sid):
        return {'id':sid, 'form_id':trial.FORM, 'status':'ACTIVE', 'answers':{
            '1':{'text':trial.TRACKING,'type':'control_textbox','answer':self.tokens[kind]},
            '2':{'text':trial.COMPLETE,'answer':trial.ANSWER}}}

    async def test_full_cycle_partial_stop_and_reopen(self):
        worker = trial.IsolatedTrial(self.path,self.transport)
        self.assertEqual(2,(await worker.tick())['outstanding'])
        self.rows.append(self.record('history','456'))
        partial = await worker.tick()
        self.assertEqual(1,partial['outstanding'])
        self.assertFalse(partial['stopped'])
        self.rows.append(self.record('working_time','789'))
        complete = await worker.tick()
        self.assertTrue(complete['stopped'])
        self.assertTrue(complete['restartStopped'])
        restarted = await trial.IsolatedTrial(self.path,self.transport).tick()
        self.assertEqual(0,restarted['outstanding'])
        self.assertEqual(0,restarted['sends'])
        self.assertEqual(0,restarted['boardWrites'])

    async def test_wrong_token_and_wrong_form_do_not_stop(self):
        unknown = self.record('history','456')
        unknown['answers']['1']['answer'] = 'c'*32
        wrong_form = self.record('working_time','789')
        wrong_form['form_id'] = '999'
        self.rows = [unknown,wrong_form]
        result = await trial.IsolatedTrial(self.path,self.transport).tick()
        self.assertEqual(2,result['outstanding'])

    async def test_changed_board_record_holds(self):
        self.board_ok = False
        result = await trial.IsolatedTrial(self.path,self.transport).tick()
        self.assertEqual('trial_requires_review',result['error'])
        self.assertEqual(0,result['sends'])

    async def test_trial_logs_no_tokens_or_personal_names(self):
        with self.assertLogs('escs.checklist.trial','INFO') as logs:
            await trial.IsolatedTrial(self.path,self.transport).tick()
        content = ''.join(logs.output)
        self.assertNotIn('Andy Challoner',content)
        self.assertNotIn('a'*32,content)
