"""Fixed-purpose, append-only Monday evidence snapshots with durable send status."""
import hashlib,html,json,sqlite3
from datetime import datetime,timezone
from mmva_monday_management import MondayManagement

def summary_text(course,checks,tasks,certificates,roster):
    lines=['MMVA portal management evidence snapshot','Course ID: '+course,'Review portal: https://escs.onrender.com/attendance/trainer/management','These management records do not by themselves confirm attendance, competence or certification.']
    if checks:
        lines+=['Evidence revision: '+str(checks['version']),'Course leader: '+checks.get('leader',''),'Follow-up: '+checks.get('followup','')]
        for check in checks['checks']:lines += [check['label']+': '+check['state'],check.get('note',''),check.get('reference','')]
    else:lines.append('Management evidence checks: not yet recorded in the portal.')
    lines.append('Expected roster: '+(str(len(roster['people']))+' named delegates; roster revision '+str(roster['version']) if roster else 'not yet recorded in the portal'))
    lines.append('Assigned follow-up actions: '+str(len(tasks)))
    for task in tasks:lines += [task['title']+' | '+task['owner']+' | Due '+task['due']+' | '+task['status'],task.get('note',''),task.get('evidence','')]
    lines.append('Outcome/certificate records in portal: '+str(len(certificates))+'. Original assessment evidence and certificates remain authoritative.')
    return '\n'.join(lines)

class ManagementSync:
    def __init__(self,path):
        self.path=str(path)
        with self.db() as db:db.execute('CREATE TABLE IF NOT EXISTS mmva_management_sync(course TEXT,digest TEXT,payload TEXT,actor TEXT,state TEXT,recorded_at TEXT,updated_at TEXT,update_id TEXT,PRIMARY KEY(course,digest))')
    def db(self):return sqlite3.connect(self.path,timeout=10)
    def queue(self,course,text,actor):
        digest=hashlib.sha256(text.encode()).hexdigest();now=datetime.now(timezone.utc).isoformat()
        with self.db() as db:db.execute('INSERT OR IGNORE INTO mmva_management_sync VALUES(?,?,?,?,?,?,?,?)',(course,digest,text,actor['email'],'pending',now,now,None))
        return self.get(course,digest)
    def get(self,course,digest=None):
        with self.db() as db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT course,digest,state,recorded_at,updated_at,update_id,actor FROM mmva_management_sync WHERE course=?'+(' AND digest=?' if digest else '')+' ORDER BY recorded_at DESC LIMIT 1',(course,digest) if digest else (course,)).fetchone()
        return dict(row) if row else None
    async def send(self,course,digest,token,transport=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');row=db.execute('SELECT payload,actor,state FROM mmva_management_sync WHERE course=? AND digest=?',(course,digest)).fetchone()
            if not row or row[2]!='pending':return self.get(course,digest)
            db.execute('UPDATE mmva_management_sync SET state=? WHERE course=? AND digest=?',('sending',course,digest))
        marker='MMVA snapshot '+digest
        body='<p>'+html.escape(marker)+'</p><p>Saved by '+html.escape(row[1])+'</p><pre>'+html.escape(row[0])+'</pre>'
        state='uncertain';update_id=None
        try:
            result=await MondayManagement(token,transport).call('mutation($item:ID!,$body:String!){create_update(item_id:$item,body:$body){id body}}',{'item':course,'body':body})
            update=result.get('create_update',{})
            if str(update.get('id','')).isdigit() and marker in str(update.get('body','')):state='confirmed';update_id=str(update['id'])
        except Exception:pass
        with self.db() as db:db.execute('UPDATE mmva_management_sync SET state=?,updated_at=?,update_id=? WHERE course=? AND digest=?',(state,datetime.now(timezone.utc).isoformat(),update_id,course,digest))
        return self.get(course,digest)
