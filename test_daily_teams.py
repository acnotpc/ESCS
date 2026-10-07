import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from team_sync import Settings, Synchronizer, Binding
from daily_teams import (DailyTeams, Candidate, Plan, Interval, Duty, title_date,
                         service_title, eligible_window, replace_plan_block, build_daily_router)
from daily_teams import post_date, plan_lines, MapProfile, TeamAssignment


class DailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026,10,6,14,0,tzinfo=timezone.utc)
        self.settings = Settings()
        self.settings.enabled = True
        self.settings.live = False
        self.settings.token = 'test'
        self.settings.bot_id = '12'
        self.settings.rest_url = 'https://escs.bitrix24.com/rest/1/test/'
        self.settings.db_path = str(Path(self.tmp.name)/'state.sqlite')
        self.posts, self.calls = [], []
        self.fail_add = False
        async def call(settings, method, params):
            self.calls.append((method,copy.deepcopy(params)))
            if method == 'log.blogpost.get':
                if 'POST_ID' in params:
                    return copy.deepcopy([p for p in self.posts if int(p['ID']) == params['POST_ID']])
                start = params['start']
                return copy.deepcopy(self.posts[start:start+50])
            if method == 'log.blogpost.add':
                pid = max([int(p['ID']) for p in self.posts]+[100])+1
                self.posts.insert(0,{'ID':str(pid),'TITLE':params['POST_TITLE'],
                    'DETAIL_TEXT':params['POST_MESSAGE'],'AUTHOR_ID':str(params['USER_ID'])})
                if self.fail_add:
                    raise RuntimeError('Response lost after successful write')
                return pid
            if method == 'log.blogpost.update':
                post = next(p for p in self.posts if int(p['ID']) == params['POST_ID'])
                post.update(TITLE=params['POST_TITLE'],DETAIL_TEXT=params['POST_MESSAGE'])
                return params['POST_ID']
            raise AssertionError(method)
        self.sync = Synchronizer(self.settings,call)
        self.daily = DailyTeams(self.sync,lambda:self.now)
        self.daily.enabled = self.daily.write = True

    def tearDown(self):
        if self.sync.store:
            self.sync.store.db.close()
        self.tmp.cleanup()

    def candidate(self, **changes):
        values = dict(staff_id='fms_1',name='Alex Example',active=True,marker='blue',
            regular_group=1, availability=Interval(start='2026-10-08T06:00:00+01:00',end='2026-10-08T18:00:00+01:00'),
            commitments_checked=True,duties_checked=True)
        values.update(changes)
        return Candidate(**values)

    def plan(self, **changes):
        data = dict(service_date='2026-10-08',observed_at=self.now.isoformat(),
                    source_reference='FMS visible rota 5–11 Oct',roster_complete=True,candidates=[self.candidate()])
        data.update(changes)
        return Plan(**data)

    def post(self,pid=19792,day='2026-10-08',author='1037',suffix=' - MB'):
        return {'ID':str(pid),'TITLE':service_title(day)+suffix,'AUTHOR_ID':author,
                'DETAIL_TEXT':'Manual crew names\nAVAILABLE: original spares\n[ESCS LIVE TEAM STATUS]\nOriginal live data\n[/ESCS LIVE TEAM STATUS]'}

    def test_exact_dates_and_initials(self):
        self.assertEqual(title_date('2026-10-08 - Teams List - MB'),'2026-10-08')
        for title in ['2026-02-30 - Teams List','2026-10-08 - Teams List copy','2026-10-08 - Teams List - archived']:
            self.assertIsNone(title_date(title))

    def map_plan(self):
        people = [
            self.candidate(staff_id='junior',name='Junior Example',map_profile=MapProfile(order=9,area='East Sheffield',c1=True)),
            self.candidate(staff_id='senior',name='Senior Example',map_profile=MapProfile(order=1,area='Worksop')),
            self.candidate(staff_id='solo',name='Solo Example',regular_group=2,marker='light_blue',
                map_profile=MapProfile(order=5,area='Rochdale'),
                commitments=[Interval(start='2026-10-08T09:30:00+01:00',end='2026-10-08T10:30:00+01:00',purpose='meeting')]),
            self.candidate(staff_id='extra',name='Overtime Example',regular_group=None,overtime=True,
                map_profile=MapProfile(order=12,area='North Sheffield'),availability_note='Overtime until 17:30'),
            self.candidate(staff_id='off',name='Off Example',marker='pink',map_profile=MapProfile(order=7,area='Doncaster')),
            self.candidate(staff_id='bank',name='Bank Example',regular_group=None),
        ]
        return self.plan(candidates=people,map_reference='Verified full-time conveyance map',map_checked_at=self.now.isoformat())

    def test_map_teams_preserve_groups_seniority_c1_commitments_and_separate_overtime(self):
        from daily_teams import proposed_teams
        p=self.map_plan().checked(self.now)
        lines=proposed_teams(p)
        team=next(x for x in lines if x.startswith('Team A'))
        self.assertLess(team.index('Senior Example'),team.index('Junior Example'))
        self.assertIn('(C1)',team)
        self.assertNotIn('East Sheffield',team)
        self.assertNotIn('Worksop',team)
        self.assertNotIn('Overtime Example',team)
        text='\n'.join(lines)
        self.assertIn('Spare - Solo Example',text)
        self.assertNotIn('Required for:',text)
        text='\n'.join(plan_lines(p))
        self.assertIn('Required for: meeting 08 Oct 09:30–08 Oct 10:30',text)
        self.assertIn('Overtime - Overtime Example',text)
        self.assertNotIn('Off Example',text)
        self.assertIn('Other staff - Bank Example',text)
        self.assertIn('Availability, rest and release checks pending',text)

    def test_missing_map_match_never_invents_team_or_c1(self):
        from daily_teams import proposed_teams
        p=self.map_plan()
        p.candidates[0].map_profile=None
        self.assertEqual(proposed_teams(p),['PROPOSED TEAMS: full-time map matching requires verification.'])
        self.assertEqual(proposed_teams(self.plan()),[])

    def test_explicit_team_assignment_moves_c1_and_keeps_single_member_team(self):
        p=self.map_plan()
        p.team_assignments=[TeamAssignment(label='A',staff_ids=['senior']),
                            TeamAssignment(label='B',staff_ids=['solo']),
                            TeamAssignment(label='C',staff_ids=['junior'])]
        p.checked(self.now)
        text='\n'.join(plan_lines(p))
        self.assertIn('Team A - Senior Example\n\nTeam B - Solo Example\n\nTeam C - Junior Example (C1)\n\nOvertime',text)
        self.assertNotIn('Spare - Solo Example',text)
        self.assertIn('Required for: meeting',text)

    def test_assignments_reject_duplicates_omissions_absent_and_overtime_staff(self):
        for ids in [['senior','senior','junior','solo'],['senior','solo'],
                    ['senior','junior','solo','off'],['senior','junior','solo','extra']]:
            p=self.map_plan()
            p.team_assignments=[TeamAssignment(label='A',staff_ids=ids)]
            with self.assertRaises(ValueError): p.checked(self.now)

    def test_only_next_day_pink_is_bold_in_regular_overtime_and_bank_rows(self):
        p=self.map_plan()
        p.next_day_date='2026-10-09'
        p.next_day_observed_at=self.now.isoformat()
        p.next_day_source_reference='Verified FMS Friday 9 Oct colours'
        for c in p.candidates:
            c.next_day_marker='pink' if c.staff_id in ('junior','extra','bank') else 'training'
        p.checked(self.now)
        text='\n'.join(plan_lines(p))
        self.assertIn('[b]Junior Example (C1)[/b]',text)
        self.assertIn('Overtime - [b]Overtime Example[/b]\n\nOther staff - [b]Bank Example[/b]',text)
        self.assertNotIn('[b]Senior Example',text)
        self.assertIn('pink / day off on 2026-10-09',text)

    def test_next_day_colour_requires_correct_date_source_and_fresh_observation(self):
        for issue in ['date','source','stale','future']:
            p=self.map_plan()
            p.candidates[0].next_day_marker='pink'
            p.next_day_date='2026-10-09'
            p.next_day_observed_at=self.now.isoformat()
            p.next_day_source_reference='Verified FMS colours'
            if issue=='date': p.next_day_date='2026-10-08'
            if issue=='source': p.next_day_source_reference=None
            if issue=='stale': p.next_day_observed_at=(self.now-timedelta(days=2)).isoformat()
            if issue=='future': p.next_day_observed_at=(self.now+timedelta(minutes=5)).isoformat()
            with self.assertRaises(ValueError): p.checked(self.now)

    def test_unknown_next_day_marker_does_not_infer_bold_from_current_colour(self):
        p=self.map_plan().checked(self.now)
        self.assertNotIn('[b]', '\n'.join(plan_lines(p)))

    def test_map_metadata_rejects_injection_duplicates_stale_and_mixed_roles(self):
        for change in ['area','order','stale','overtime']:
            p=self.map_plan()
            if change=='area': p.candidates[0].map_profile.area='[b]invalid[/b]'
            if change=='order': p.candidates[0].map_profile.order=1
            if change=='stale': p.map_checked_at=(self.now-timedelta(days=31)).isoformat()
            if change=='overtime': p.candidates[0].overtime=True
            with self.assertRaises(ValueError): p.checked(self.now)

    async def test_map_teams_update_same_dated_post_and_survive_restart(self):
        self.posts=[self.post()]
        self.daily.save_plan(self.map_plan())
        await self.daily.run()
        self.assertIn('Team A - Senior Example, Junior Example (C1)',self.posts[0]['DETAIL_TEXT'])
        self.assertIn('Manual crew names',self.posts[0]['DETAIL_TEXT'])
        self.sync.store.db.close()
        self.sync.store=None
        await self.daily.run()
        self.assertEqual(len([1 for m,p in self.calls if m=='log.blogpost.update']),1)
        self.assertFalse(any(m=='log.blogpost.add' for m,p in self.calls))

    def test_micro_heading_is_date_matched_but_arbitrary_body_is_not(self):
        post=self.post()
        post['MICRO']='Y'
        post['DETAIL_TEXT']='[b]2026-10-08 - Teams List - MB[/b]\nTeam A - original crew'
        post['TITLE']='2026-10-08 - Teams List - MB Team A - original crew'
        self.assertEqual(post_date(post),'2026-10-08')
        post['MICRO']='N'
        self.assertEqual(post_date(post),'2026-10-08')
        post['MICRO']='Y'
        post['TITLE']='Unrelated post'
        self.assertIsNone(post_date(post))
        post['TITLE']='Unrelated post 2026-10-08 - Teams List'
        post['DETAIL_TEXT']='Unrelated heading\n2026-10-08 - Teams List'
        self.assertIsNone(post_date(post))

    async def test_create_exact_title_existing_audience_no_duplicate_on_restart(self):
        self.daily.save_plan(self.plan())
        await self.daily.run()
        self.sync.store.db.close()
        self.sync.store=None
        await self.daily.run()
        adds=[p for m,p in self.calls if m=='log.blogpost.add']
        self.assertEqual(len(adds),1)
        self.assertEqual(adds[0]['DEST'],['SG127'])
        self.assertEqual(adds[0]['POST_TITLE'],'2026-10-08 - Teams List')
        self.assertIn('PROVISIONAL',self.posts[0]['DETAIL_TEXT'])

    async def test_preserve_existing_title_manual_and_live_content(self):
        self.posts=[self.post()]
        self.daily.save_plan(self.plan())
        await self.daily.run()
        text=self.posts[0]['DETAIL_TEXT']
        self.assertTrue(text.startswith('Manual crew names\nAVAILABLE: original spares'))
        self.assertIn('Original live data',text)
        self.assertEqual(self.posts[0]['TITLE'],'2026-10-08 - Teams List - MB')
        self.assertFalse(any(m=='log.blogpost.add' for m,p in self.calls))

    async def test_duplicate_posts_block_all_writes_for_date(self):
        self.posts=[self.post(),self.post(pid=19793,suffix='')]
        self.daily.save_plan(self.plan())
        result=await self.daily.run()
        self.assertEqual(result['days'][2]['state'],'duplicate_requires_review')
        self.assertFalse(any(m in ('log.blogpost.add','log.blogpost.update') for m,p in self.calls))

    async def test_no_evidence_or_preview_never_creates(self):
        await self.daily.run()
        self.daily.save_plan(self.plan())
        self.daily.write=False
        result=await self.daily.run()
        self.assertEqual(result['days'][2]['state'],'preview')
        self.assertFalse(any(m in ('log.blogpost.add','log.blogpost.update') for m,p in self.calls))

    async def test_full_pagination_and_wrong_author_ignored(self):
        self.posts=[{'ID':str(i+1),'TITLE':'Other feed post','AUTHOR_ID':'1037','DETAIL_TEXT':'irrelevant'} for i in range(50)]
        self.posts += [self.post(author='22'),self.post(pid=19793)]
        found=await self.daily.discover()
        self.assertEqual(len(found['2026-10-08']),1)
        self.assertEqual(found['2026-10-08'][0]['ID'],'19793')
        self.assertEqual([p['start'] for m,p in self.calls],[0,50])

    async def test_lost_create_response_rediscovered_without_second_add(self):
        self.daily.save_plan(self.plan())
        self.fail_add=True
        with self.assertRaises(RuntimeError): await self.daily.run()
        self.fail_add=False
        await self.daily.run()
        self.assertEqual(sum(m=='log.blogpost.add' for m,p in self.calls),1)

    async def test_unknown_create_result_blocks_retry_when_post_not_visible(self):
        self.daily.save_plan(self.plan())
        self.fail_add=True
        with self.assertRaises(RuntimeError): await self.daily.run()
        self.posts=[]
        result=await self.daily.run()
        self.assertEqual(result['days'][2]['state'],'creation_requires_reconciliation')
        self.assertEqual(sum(m=='log.blogpost.add' for m,p in self.calls),1)

    def test_missing_stale_duplicate_or_wrong_date_evidence_rejected(self):
        plans=[self.plan(roster_complete=False),self.plan(observed_at=(self.now-timedelta(days=2)).isoformat()),
               self.plan(candidates=[self.candidate(),self.candidate()]),self.plan(service_date='2026-10-09')]
        for plan in plans:
            with self.assertRaises(ValueError): self.daily.save_plan(plan)

    def test_pink_absence_inactive_and_unknown_never_available(self):
        for marker in ['pink','holiday','sick','training','unknown']:
            self.assertIsNone(eligible_window(self.candidate(marker=marker))[0])
        self.assertIsNone(eligible_window(self.candidate(active=False))[0])
        self.assertIsNone(eligible_window(self.candidate(commitments_checked=False))[0])

    def test_rest_and_training_trim_window(self):
        candidate=self.candidate(duties=[Duty(first_meeting='2026-10-07T07:00:00+01:00',final_return='2026-10-07T20:00:00+01:00')],
            commitments=[Interval(start='2026-10-08T09:00:00+01:00',end='2026-10-08T15:00:00+01:00')])
        window,_=eligible_window(candidate)
        self.assertEqual(window[0].hour,15)
        self.assertEqual(window[1].hour,18)

    def test_light_blue_included_with_required_meeting_and_busy_time_removed(self):
        candidate=self.candidate(marker='light_blue',commitments=[Interval(
            start='2026-10-08T09:30:00+01:00',end='2026-10-08T10:30:00+01:00',purpose='meeting')])
        window,_=eligible_window(candidate)
        self.assertEqual((window[0].hour,window[0].minute),(10,30))
        text='\n'.join(plan_lines(self.plan(candidates=[candidate])))
        self.assertIn('Staff Group 1: Alex Example',text)
        self.assertIn('Required for: meeting 08 Oct 09:30–08 Oct 10:30',text)

    def test_light_blue_unknown_commitment_stays_visible_without_clearance(self):
        candidate=self.candidate(marker='light_blue')
        self.assertIsNone(eligible_window(candidate)[0])
        text='\n'.join(plan_lines(self.plan(candidates=[candidate])))
        self.assertIn('Alex Example',text)
        self.assertIn('purpose/time to confirm',text)
        self.assertNotIn('Staff Group 1:',text)

    def test_light_blue_unverified_duty_and_full_day_commitment_block_clearance(self):
        busy=Interval(start='2026-10-08T06:00:00+01:00',end='2026-10-08T18:00:00+01:00',purpose='training')
        self.assertIsNone(eligible_window(self.candidate(marker='light_blue',commitments=[busy]))[0])
        self.assertIsNone(eligible_window(self.candidate(marker='light_blue',commitments=[busy],duties_checked=False))[0])

    def test_pending_roster_names_keep_regular_groups_and_separate_spares(self):
        regular=self.candidate(duties_checked=False)
        spare=self.candidate(staff_id='spare',name='Spare Example',regular_group=None,duties_checked=False)
        text='\n'.join(plan_lines(self.plan(candidates=[regular,spare])))
        self.assertIn('Staff Group 1 (provisional — checks pending): Alex Example',text)
        self.assertIn('SPARE STAFF (provisional — checks pending): Spare Example',text)

    def test_pending_overtime_limit_is_visible_and_plain_text_required(self):
        candidate=self.candidate(regular_group=None,availability=None,duties_checked=False,
                                 availability_note='Overtime: conveyance until 17:30; start time to confirm')
        text='\n'.join(plan_lines(self.plan(candidates=[candidate])))
        self.assertIn('until 17:30',text)
        self.assertIn('Rota evidence expires: 07 Oct 2026 15:00 BST',text)
        with self.assertRaises(ValueError): self.plan(candidates=[self.candidate(availability_note='[url]invalid[/url]')]).checked(self.now)

    def test_config_snapshot_import_is_idempotent_and_preserves_newer_plan(self):
        raw=json.dumps([self.plan().model_dump()])
        self.assertEqual(self.daily.import_config(raw),1)
        self.assertEqual(self.daily.import_config(raw),0)
        newer=self.plan(observed_at=(self.now+timedelta(seconds=30)).isoformat())
        self.daily.save_plan(newer)
        self.assertEqual(self.daily.import_config(raw),0)

    def test_expired_config_snapshot_does_not_become_fresh_after_restart(self):
        raw=json.dumps([self.plan(observed_at=(self.now-timedelta(days=2)).isoformat()).model_dump()])
        self.assertEqual(self.daily.import_config(raw),0)
        self.assertEqual(self.daily.ready().db.execute('SELECT count(*) FROM daily_plans').fetchone()[0],0)

    def test_invalid_or_conflicting_config_is_atomic(self):
        valid=self.plan()
        invalid=self.plan(service_date='2026-10-09')
        with self.assertRaises(ValueError): self.daily.import_config(json.dumps([valid.model_dump(),invalid.model_dump()]))
        self.assertEqual(self.daily.ready().db.execute('SELECT count(*) FROM daily_plans').fetchone()[0],0)
        self.daily.save_plan(valid)
        conflict=self.plan(candidates=[self.candidate(name='Different Example')])
        with self.assertRaises(ValueError): self.daily.import_config(json.dumps([conflict.model_dump()]))
        for raw in ['{}',json.dumps([valid.model_dump(),valid.model_dump()]),' '*65537]:
            with self.assertRaises(ValueError): self.daily.import_config(raw)

    def test_open_duty_blocks_pool_and_long_rest_can_exclude_entire_day(self):
        candidate=self.candidate(duties=[Duty(first_meeting='2026-10-07T07:00:00+01:00')])
        self.assertIsNone(eligible_window(candidate)[0])
        candidate=self.candidate(duties=[Duty(first_meeting='2026-10-07T07:00:00+01:00',final_return='2026-10-08T10:00:00+01:00')])
        self.assertIsNone(eligible_window(candidate)[0])

    def test_broken_block_preserves_original(self):
        with self.assertRaises(ValueError): replace_plan_block('Manual [ESCS PROVISIONAL TEAMS] unclosed',['new'])

    async def test_new_day_routing_does_not_copy_previous_crew(self):
        self.posts=[self.post(day='2026-10-06'),self.post(pid=19793)]
        b=Binding(chat_id=51684,job_number='JOB08448',monday_item_id=13190991750,post_id=123,
            service_date='2026-10-06',team_label='Team A',vehicle='YC71KMG',
            first_meetings={'staff1':self.now.isoformat()},allowed_author_ids=[1037])
        b.daily_route=True
        self.sync.ready().bind(b)
        await self.daily.run()
        self.assertEqual(self.sync.ready().binding(51684).post_id,19792)
        self.assertEqual(self.sync.ready().binding(51684).service_date,'2026-10-06')
        self.assertEqual(self.sync.ready().db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0],1)

    async def test_private_test_binding_cannot_roll_into_live_list(self):
        self.posts=[self.post(day='2026-10-06')]
        b=Binding(chat_id=51962,job_number='JOB00001',monday_item_id=1,post_id=123,
            service_date='2026-10-06',team_label='Team A',vehicle='YC71KMG',
            first_meetings={'staff1':self.now.isoformat()},allowed_author_ids=[1037])
        self.sync.ready().bind(b)
        await self.daily.run()
        self.assertEqual(self.sync.ready().binding(51962).post_id,123)

    def test_management_endpoints_require_existing_authorization(self):
        from fastapi import HTTPException
        def denied(): raise HTTPException(401,'Unauthorized')
        app=FastAPI()
        app.include_router(build_daily_router(self.daily,denied))
        client=TestClient(app)
        self.assertEqual(client.get('/team-sync/daily/status').status_code,401)
        self.assertEqual(client.post('/team-sync/daily/refresh').status_code,401)


if __name__=='__main__': unittest.main()
