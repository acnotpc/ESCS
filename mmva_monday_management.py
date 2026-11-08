"""Narrow, manager-authorised updates to verified MMVA booking columns."""
import json,hashlib
import httpx
from fastapi import HTTPException
from mmva_monday_courses import BOARD,TRAINER_EMAIL_COLUMN

def allocation_revision(source):
    return hashlib.sha256(json.dumps({k:source.get(k) for k in ('id','trainer','trainerEmails','status')},sort_keys=True).encode()).hexdigest()

class MondayManagement:
    def __init__(self,token,transport=None):self.token,self.transport=token,transport
    async def call(self,query,variables=None):
        try:
            async with httpx.AsyncClient(timeout=20,follow_redirects=False,transport=self.transport) as client:
                r=await client.post('https://api.monday.com/v2',headers={'Authorization':self.token},json={'query':query,'variables':variables or {}})
            result=r.json()
            if r.status_code!=200 or result.get('errors') or not isinstance(result.get('data'),dict):raise ValueError()
            return result['data']
        except (httpx.HTTPError,ValueError,TypeError):raise HTTPException(503,'Monday update could not be confirmed. Refresh the source booking before retrying.')
    async def columns(self):
        data=await self.call('query { boards(ids: [4727626523]) { id columns { id type settings_str } } }')
        boards=data.get('boards',[])
        if len(boards)!=1 or str(boards[0]['id'])!=BOARD:raise HTTPException(503,'MMVA board verification failed.')
        cols={c['id']:c for c in boards[0]['columns']}
        for key,kind in [('dropdown4','dropdown'),(TRAINER_EMAIL_COLUMN,'long_text'),('status','status')]:
            if cols.get(key,{}).get('type')!=kind:raise HTTPException(503,'Booking column configuration needs checking.')
        try:
            labels=json.loads(cols['dropdown4']['settings_str'])['labels']
            return [dict(id=int(c['id']),name=c.get('name') or c.get('label')) for c in labels if not c.get('is_deactivated')]
        except (ValueError,KeyError,TypeError):raise HTTPException(503,'Trainer choices could not be verified.')
    async def write(self,code,values):
        result=await self.call('mutation($item:ID!,$value:JSON!){change_multiple_column_values(board_id:4727626523,item_id:$item,column_values:$value){id}}',{'item':code,'value':json.dumps(values)})
        if str(result.get('change_multiple_column_values',{}).get('id'))!=code:raise HTTPException(503,'Monday change is unconfirmed. Refresh before retrying.')
    async def allocate(self,code,pairs,accounts):
        choices={c['id']:c['name'] for c in await self.columns()};registry={a['email']:a['name'] for a in accounts};ids=[];emails=[]
        if not isinstance(pairs,list) or not 1<=len(pairs)<=8:raise HTTPException(400,'Select one to eight named trainer accounts.')
        for pair in pairs:
            label=pair.get('label');email=str(pair.get('email','')).strip().casefold();name=registry.get(email,'')
            name={'andy challoner':'andrew challoner'}.get(name.casefold(),name.casefold())
            if type(label) is not int or label not in choices or not name or name!=choices[label].casefold():raise HTTPException(400,'Each selected name must match an enabled trainer login account.')
            if label in ids or email in emails:raise HTTPException(400,'Remove duplicate trainer selections.')
            ids.append(label);emails.append(email)
        await self.write(code,{'dropdown4':{'ids':ids},TRAINER_EMAIL_COLUMN:{'text':'\n'.join(emails)}})
        return dict(emails=emails,names=[choices[i] for i in ids])
