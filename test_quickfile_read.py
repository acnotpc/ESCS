import unittest
import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from quickfile_read import QuickFileRead, build_quickfile_router

class QuickFileTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_get_auth_and_pagination(self):
        seen=[]
        def handler(req):
            seen.append(req)
            return httpx.Response(200,json={'count':2,'data':[{'id':7}], 'paging':{'next':1}})
        reader=QuickFileRead('test-secret',httpx.MockTransport(handler))
        result=await reader.search(date_from='2026-09-01',date_to='2026-09-30',offset=100,limit=100)
        req=seen[0]
        self.assertEqual((req.method,req.url.host,req.url.path),('GET','api-beta.quickfile.co.uk','/invoices'))
        self.assertEqual(req.headers['Authorization'],'Bearer test-secret')
        self.assertEqual(req.url.params['offset'],'100')
        self.assertEqual(result['paging'],{'next':1})
        with self.assertRaises(HTTPException):
            await reader.get('/invoices/7/send')
        with self.assertRaises(HTTPException):
            await reader.search(date_from='2026-10-01',date_to='2026-09-01')
        self.assertEqual(len(seen),1)

    async def test_errors_never_include_upstream_body_or_token(self):
        for code in [302,401,403,429,500]:
            reader=QuickFileRead('test-secret',httpx.MockTransport(lambda r:httpx.Response(code,text='test-secret private data')))
            with self.assertRaises(HTTPException) as err:
                await reader.search()
            self.assertNotIn('test-secret',err.exception.detail)
            self.assertNotIn('private data',err.exception.detail)
        with self.assertRaises(HTTPException):
            await QuickFileRead('').search()

    async def test_detail_identity_and_preview_link(self):
        reader=QuickFileRead('secret',httpx.MockTransport(lambda r:httpx.Response(200,json={'id':7,'notes':'audit notes','preview_uri':'private-token-link'})))
        result=await reader.invoice(7)
        self.assertEqual(result['notes'],'audit notes')
        self.assertNotIn('preview_uri',result)
        with self.assertRaises(HTTPException):
            await reader.invoice(8)

    def test_protected_routes_and_no_mutations(self):
        calls=[]
        def handler(req):
            calls.append(req)
            return httpx.Response(200,json={'data':[],'count':0})
        reader=QuickFileRead('secret',httpx.MockTransport(handler))
        def guard(x_connector_key: str | None=Header(None)):
            if x_connector_key!='test-key':
                raise HTTPException(401,'Unauthorized')
        app=FastAPI();app.include_router(build_quickfile_router(guard,reader))
        client=TestClient(app)
        for path in ['/quickfile/status','/quickfile/invoices','/quickfile/invoices/7']:
            self.assertEqual(client.get(path).status_code,401)
        self.assertEqual(calls,[])
        good={'x-connector-key':'test-key'}
        response=client.get('/quickfile/status',headers=good)
        self.assertTrue(response.json()['connected'])
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(client.get('/quickfile/invoices?limit=101',headers=good).status_code,422)
        for method in ['post','put','delete']:
            self.assertEqual(getattr(client,method)('/quickfile/invoices',headers=good).status_code,405)

if __name__=='__main__': unittest.main()
