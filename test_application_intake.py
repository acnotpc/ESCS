import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from application_intake import ApplicationIntake, FORMS
from automatic_followup import AutomaticSelection, values, names, normalized, data
from checklist_workflow import ChecklistWorkflow, REQUEST, RECEIVED, SUMMARY, RESUME
from test_automatic_followup import person, change, AutoBoard

NOW = datetime(2026, 10, 25, 13, tzinfo=timezone.utc)


def submission(sid, form, email=True):
    answers = {'1':{'text':'Name','answer':{'first':'Synthetic','last':'Applicant'}},
               '2':{'text':'Date of Birth','answer':{'year':'1990','month':'1','day':'2'}}}
    if email:
        answers['3'] = {'text':'Email','answer':'synthetic@example.org'}
    return {'id':str(sid),'form_id':form,'status':'ACTIVE','answers':answers}


class Source:
    def __init__(self):
        self.rows = {'260903309464356':[submission(100, '260903309464356')]}
        self.fail = False

    async def _get(self, path):
        if self.fail:
            raise RuntimeError('source unavailable')
        if path.startswith('/submission/'):
            return copy.deepcopy(next(r for rows in self.rows.values() for r in rows if r['id']==path.split('/')[-1]))
        form = path.split('/')[2]
        return copy.deepcopy(self.rows.get(form, []))

    async def schema(self, form):
        from test_history_completeness import schema
        return schema()


class Board(AutoBoard):
    async def clear_resume(self, item_id):
        change(self.rows[0], RESUME, value={})

    async def call(self, query, variables=None):
        if 'move_item_to_group' in query:
            self.rows[0]['group'] = {'id':'new_group88715'}
            return {'move_item_to_group':{'id':self.rows[0]['id']}}
        if 'change_multiple_column_values' in query:
            result = await super().call(query, variables)
            writes = json.loads(variables['value'])
            if 'status53' in writes:
                change(self.rows[0], 'status53', writes['status53']['label'], writes['status53'])
            return result
        return await super().call(query, variables)


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.item = person('pre_employment')
        self.item.update(name='Synthetic Applicant', created_at=NOW.isoformat())
        self.item['group'] = {'id':'new_group'}
        change(self.item, 'status53', 'Pending Review')
        change(self.item, 'acceptance', 'Pending')
        change(self.item, 'offerletter_new_starter_jotform')
        change(self.item, 'date6', value={'date':'1990-01-02'})
        self.board = Board([self.item])
        self.sender = AsyncMock()
        self.worker = ChecklistWorkflow(self.temp.name+'/test.sqlite', self.board, sender=self.sender)
        self.auto = AutomaticSelection(self.worker)
        self.intake = ApplicationIntake(self.auto)
        self.source = Source()

    async def asyncTearDown(self):
        self.worker.ledger.db.close()
        self.temp.cleanup()

    async def run_cycle(self, now=NOW, count=1):
        return await self.intake.run(self.source, copy.deepcopy(self.board.rows),
                                     {'synthetic@example.org':count},
                                     {n:count for n in names('Synthetic Applicant')}, now)

    async def test_enrol_stage_and_delay_without_acceptance_or_duplicate_pack(self):
        result = await self.run_cycle()
        self.assertIsNone(result['error'])
        self.assertEqual(result['enrolled'], 1)
        self.assertEqual(self.item['group']['id'], 'new_group88715')
        self.assertEqual(values(self.item)['acceptance']['text'], 'Pending')
        await self.run_cycle()
        self.sender.send.assert_not_awaited()
        self.assertEqual(data(values(self.item), REQUEST)['ids'], [1,4,5,11,14])
        self.assertIn('28 October 2026 13:00 GMT', values(self.item)[SUMMARY]['text'])
        self.assertNotIn(2, data(values(self.item), REQUEST)['ids'])  # No pre-offer medical request.

    async def test_untracked_starter_receipt_matches_name_and_dob_stops_chase(self):
        await self.run_cycle()
        for n, (kind, (form, _, _)) in enumerate(FORMS.items()):
            row = submission(200+n, form, email=kind!='dbs')
            if kind == 'history':
                from test_history_completeness import record as history_record
                history = history_record()
                history.update(id=row['id'])
                history['answers']['13']['answer'] = {'first':'Synthetic','last':'Applicant'}
                history['answers']['999'] = {'text':'Email','answer':'synthetic@example.org'}
                row = history
            self.source.rows[form] = [row]
        result = await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(result['received'], 1)
        self.sender.send.assert_not_awaited()
        self.assertEqual(data(values(self.item), RECEIVED)['ids'], [1,4,5,11,14])
        self.assertIn('reminders stopped', values(self.item)[SUMMARY]['text'])

    async def test_reminders_spacing_limit_and_restart(self):
        await self.run_cycle()
        for days in (3,6,9):
            await self.run_cycle(NOW+timedelta(days=days))
            self.intake = ApplicationIntake(self.auto)
        await self.run_cycle(NOW+timedelta(days=12))
        self.assertEqual(self.sender.send.await_count, 3)
        self.assertIn('limit_review', values(self.item)[SUMMARY]['text'])

    async def test_source_failure_and_duplicate_identity_block(self):
        self.assertEqual((await self.run_cycle(count=2))['enrolled'], 0)
        self.source.fail = True
        self.assertIsNotNone((await self.run_cycle())['error'])
        self.assertFalse(self.board.writes)
        self.sender.send.assert_not_awaited()

    async def test_empty_historical_stub_is_retained_without_blocking_real_applicants(self):
        form = FORMS['licence_details'][0]
        self.source.rows[form] = [{'id':'101','form_id':form,'status':'ACTIVE',
                                  'answers':{'1':{'text':'Name','type':'control_fullname'},
                                             '2':{'text':'Driving Licence','answer':[]}}}]
        result = await self.run_cycle()
        self.assertEqual(result['enrolled'],1)
        self.assertEqual(result['emptyArchiveStubs'],1)
        self.assertEqual(data(values(self.item), REQUEST)['ids'],[1,4,5,11,14])
        self.sender.send.assert_not_awaited()

    async def test_uncertain_delivery_never_retries(self):
        await self.run_cycle()
        self.sender.send.side_effect = RuntimeError('unknown delivery')
        await self.run_cycle(NOW+timedelta(days=3))
        self.intake = ApplicationIntake(self.auto)
        await self.run_cycle(NOW+timedelta(days=6))
        self.assertEqual(self.sender.send.await_count, 1)
        self.assertIn('delivery_uncertain', values(self.item)[SUMMARY]['text'])

    async def test_pause_and_old_applications_excluded(self):
        self.item['created_at'] = (NOW-timedelta(days=1)).isoformat()
        self.assertEqual((await self.run_cycle())['enrolled'],0)
        self.item['created_at'] = NOW.isoformat()
        await self.run_cycle()
        change(self.item, 'boolean_mm82qkbk', value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.sender.send.assert_not_awaited()

    async def test_partial_identity_holds_instead_of_duplicate_request(self):
        await self.run_cycle()
        form = FORMS['dbs'][0]
        row = submission(200,form,email=False)
        row['answers'].pop('2')
        self.source.rows[form] = [row]
        await self.run_cycle(NOW+timedelta(days=4))
        self.sender.send.assert_not_awaited()
        self.assertIn('identity_review', values(self.item)[SUMMARY]['text'])

    async def test_cleared_selections_are_preserved_and_pause_campaign(self):
        await self.run_cycle()
        await self.run_cycle()
        change(self.item, REQUEST, value={'ids':[]})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(data(values(self.item), REQUEST)['ids'], [])
        self.sender.send.assert_not_awaited()
        self.assertIn('paused', values(self.item)[SUMMARY]['text'])

    def campaign(self):
        return self.intake.db.execute('SELECT * FROM application_intake WHERE id=?',
                                      (self.item['id'],)).fetchone()

    async def test_staff_eight_selection_preserved_only_eligible_forms_chased(self):
        await self.run_cycle()
        selected = [1,2,3,4,11,12,15,20]
        change(self.item, REQUEST, value={'ids':selected})
        change(self.item, RECEIVED, value={'ids':[20]})
        before = self.campaign()
        await self.run_cycle(NOW+timedelta(days=3))
        self.assertEqual(data(values(self.item), REQUEST)['ids'], selected)
        self.assertEqual(data(values(self.item), RECEIVED)['ids'], [20])
        self.assertEqual(self.sender.send.await_count, 1)
        body = self.sender.send.await_args.args[3]
        for kind in ('history','working_time','driving_declaration'):
            self.assertIn(FORMS[kind][0], body)
        for forbidden in ('90715470243352','92583319416361','73002266184349'):
            self.assertNotIn(forbidden, body)
        self.assertEqual(before['sends'], 0)
        self.assertEqual(self.campaign()['sends'], 1)
        self.assertIn('0/3 forms received', values(self.item)[SUMMARY]['text'])
        self.assertEqual(values(self.item)['acceptance']['text'], 'Pending')

    async def test_existing_pause_requires_explicit_resume_preserving_clock(self):
        await self.run_cycle()
        change(self.item, REQUEST, value={'ids':[1,2,3,4,11,12,15,20]})
        self.intake.db.execute("UPDATE application_intake SET state='paused'")
        self.intake.db.commit()
        before = dict(self.campaign())
        await self.run_cycle()
        self.assertEqual(self.campaign()['state'], 'paused')
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle()
        self.assertEqual(self.campaign()['state'], 'active')
        self.assertEqual(self.campaign()['due'], before['due'])
        self.assertEqual(self.campaign()['sends'], before['sends'])
        self.assertFalse(data(values(self.item), RESUME).get('checked'))
        self.intake = ApplicationIntake(self.auto)
        await self.run_cycle()
        self.sender.send.assert_not_awaited()

    async def test_resume_never_releases_uncertain_attempt_or_manual_pause(self):
        await self.run_cycle()
        self.intake.db.execute("UPDATE application_intake SET state='paused'")
        self.intake.db.execute('INSERT INTO intake_attempt(candidate,attempted,state) VALUES(?,?,?)',
                               (self.item['id'], NOW.isoformat(), 'unknown'))
        self.intake.db.commit()
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(self.campaign()['state'], 'delivery_uncertain')
        self.sender.send.assert_not_awaited()
        self.intake.db.execute('DELETE FROM intake_attempt')
        self.intake.db.execute("UPDATE application_intake SET state='paused'")
        self.intake.db.commit()
        change(self.item, 'boolean_mm82qkbk', value={'checked':'true'})
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(self.campaign()['state'], 'paused')
        self.sender.send.assert_not_awaited()

    async def test_unknown_and_hr_only_requests_block_without_overwrite(self):
        await self.run_cycle()
        for selected in ([999], [2,15,20], []):
            change(self.item, REQUEST, value={'ids':selected})
            change(self.item, RESUME, value={'checked':'true'})
            await self.run_cycle(NOW+timedelta(days=4))
            self.assertEqual(data(values(self.item), REQUEST)['ids'], selected)
            self.assertEqual(self.campaign()['state'], 'paused')
        self.sender.send.assert_not_awaited()

    async def test_resume_cannot_bypass_changed_contact_stage_or_limit(self):
        await self.run_cycle()
        self.intake.db.execute("UPDATE application_intake SET state='paused'")
        self.intake.db.commit()
        change(self.item, 'email', value={'email':'changed@example.org'})
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(self.campaign()['state'], 'paused')
        change(self.item, 'email', value={'email':'synthetic@example.org'})
        change(self.item, 'status53', 'Employed with ESCS')
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(self.campaign()['state'], 'paused')
        change(self.item, 'status53', 'To be processed by Recruitment')
        self.intake.db.execute("UPDATE application_intake SET sends=3")
        self.intake.db.commit()
        change(self.item, RESUME, value={'checked':'true'})
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertNotEqual(self.campaign()['state'], 'active')
        self.assertEqual(self.campaign()['sends'], 3)
        self.sender.send.assert_not_awaited()

    async def test_selected_receipts_stop_chase_preserving_other_hr_receipts(self):
        await self.run_cycle()
        change(self.item, REQUEST, value={'ids':[4,20]})
        change(self.item, RECEIVED, value={'ids':[20]})
        self.source.rows[FORMS['working_time'][0]] = [submission(222, FORMS['working_time'][0])]
        await self.run_cycle(NOW+timedelta(days=4))
        self.assertEqual(self.campaign()['state'], 'received_review')
        self.assertEqual(data(values(self.item), RECEIVED)['ids'], [4,20])
        self.assertEqual(data(values(self.item), REQUEST)['ids'], [4,20])
        self.sender.send.assert_not_awaited()

    async def test_failed_resume_clear_never_releases_hold(self):
        await self.run_cycle()
        self.intake.db.execute("UPDATE application_intake SET state='paused'")
        self.intake.db.commit()
        change(self.item, RESUME, value={'checked':'true'})
        self.board.clear_resume = AsyncMock(side_effect=RuntimeError('unconfirmed'))
        result = await self.run_cycle(NOW+timedelta(days=4))
        self.assertIsNotNone(result['error'])
        self.assertEqual(self.campaign()['state'], 'paused')
        self.sender.send.assert_not_awaited()

    async def test_protected_holds_cannot_be_converted_to_resumable_pause(self):
        await self.run_cycle()
        for protected in ('identity_review','delivery_uncertain','received_review','limit_review'):
            self.intake.db.execute('UPDATE application_intake SET state=?', (protected,))
            self.intake.db.commit()
            change(self.item, 'boolean_mm82qkbk', value={'checked':'true'})
            await self.run_cycle()
            self.assertEqual(self.campaign()['state'], protected)
            change(self.item, 'boolean_mm82qkbk', value={'checked':False})
            change(self.item, RESUME, value={'checked':'true'})
            await self.run_cycle(NOW+timedelta(days=4))
            self.assertNotEqual(self.campaign()['state'], 'active')
        self.sender.send.assert_not_awaited()

    def test_archive_stores_only_hashed_identity_keys(self):
        keys = self.intake.keys(submission(100,'x')['answers'])
        self.assertNotIn('synthetic', repr(keys).lower())
        self.assertNotIn('1990', repr(keys))

    def test_legacy_fullname_label_and_email_only_receipts(self):
        keys = self.intake.keys({'1':{'type':'control_fullname','text':'Your full name',
                                     'answer':{'first':'Synthetic','last':'Applicant'}}})
        self.assertTrue(any(typ=='name' for typ,_ in keys))
        keys = self.intake.keys({'1':{'text':'Email','answer':'synthetic@example.org'}})
        self.assertEqual({typ for typ,_ in keys},{'email'})

    def test_shared_birthday_alone_is_not_an_identity_conflict(self):
        digest = self.auto.index.digest('1990-01-02')
        self.intake.db.execute('INSERT INTO intake_archive VALUES(?,?,?,?)',('dbs','200','dob',digest))
        self.assertEqual(self.intake.matching('dbs',{('dob',digest)}),(None,False))
