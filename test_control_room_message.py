import unittest
from types import SimpleNamespace
from control_room_message import send_control_room


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, url='https://escs.bitrix24.com/rest/1/test-only/'):
        return SimpleNamespace(rest_url=url,validate=lambda:None)

    async def test_existing_operator_personal_dialog_as_andy(self):
        calls=[]
        async def call(method,params):
            calls.append((method,params))
            return [{'id':'1037','type':'user','title':'Control Room Team','chatId':2039}] if method=='im.recent.list' else 99
        result=await send_control_room(self.settings(),call,'Trial')
        self.assertEqual(result['authorId'],1)
        self.assertEqual(calls[1][0],'im.message.add')
        self.assertEqual(calls[1][1]['DIALOG_ID'],'1037')
        self.assertNotIn('botToken',calls[1][1])

    async def test_wrong_account_portal_and_missing_recipient_block_sending(self):
        calls=[]
        async def call(method,params): calls.append(method); return []
        for url in ['https://escs.bitrix24.com/rest/2/test/', 'https://other.bitrix24.com/rest/1/test/']:
            with self.assertRaises(ValueError): await send_control_room(self.settings(url),call,'Trial')
        self.assertEqual(calls,[])
        with self.assertRaises(ValueError): await send_control_room(self.settings(),call,'Trial')
        self.assertEqual(calls,['im.recent.list'])

    async def test_group_name_cannot_substitute_for_personal_operator(self):
        async def call(method,params): return [{'id':'1037','type':'chat','title':'Control Room Team'}]
        with self.assertRaises(ValueError): await send_control_room(self.settings(),call,'Trial')

    async def test_uncertain_outcome_is_not_success_or_retried(self):
        calls=[]
        async def call(method,params):
            calls.append(method)
            return [{'id':'1037','type':'user','title':'Control Room Team'}] if method=='im.recent.list' else None
        with self.assertRaisesRegex(RuntimeError,'reconciliation'):
            await send_control_room(self.settings(),call,'Trial')
        self.assertEqual(calls,['im.recent.list','im.message.add'])


if __name__=='__main__': unittest.main()
