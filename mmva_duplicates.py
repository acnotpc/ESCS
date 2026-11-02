"""Course-scoped, trainer-verified identity aliases. Original records are unchanged."""
import copy,hashlib,json,re,sqlite3,time
from fastapi import HTTPException

def normalized_name(name):return ' '.join(re.findall(r'\w+',str(name).casefold()))
def close_email(a,b):
    if a==b:return False
    if len(a)>len(b):a,b=b,a
    if len(b)-len(a)>1:return False
    if len(a)==len(b):return sum(x!=y for x,y in zip(a,b))==1
    for i in range(len(b)):
        if b[:i]+b[i+1:]==a:return True
    return False

def fingerprint(a,b):
    return hashlib.sha256(json.dumps(sorted([a,b],key=lambda p:p['email']),sort_keys=True).encode()).hexdigest()
def candidates(report):
    groups={};result=[]
    for p in report['delegates']:
        name=normalized_name(p['name'])
        if len(name.split())>=2:groups.setdefault(name,[]).append(p)
    for group in groups.values():
        for i,a in enumerate(group):
            for b in group[i+1:]:
                if close_email(a['email'],b['email']):result.append({'emails':[a['email'],b['email']],'revision':fingerprint(a,b),'name':a['name']})
    return result

class DuplicateLinks:
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path,timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_delegate_alias (course TEXT, source TEXT, target TEXT, actor TEXT, reason TEXT, recorded_at REAL, PRIMARY KEY(course,source))')
    def links(self,course):
        with sqlite3.connect(self.path,timeout=10) as db:
            return dict(db.execute('SELECT source,target FROM mmva_delegate_alias WHERE course=?',(course,)))
    def history(self,course):
        with sqlite3.connect(self.path,timeout=10) as db:
            return [dict(zip(('source','target','trainer','reason','time'),r)) for r in db.execute('SELECT source,target,actor,reason,recorded_at FROM mmva_delegate_alias WHERE course=? ORDER BY recorded_at',(course,))]
    def save(self,course,source,target,actor,reason):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise HTTPException(400,'Record how you verified these are the same person (up to 1,000 characters).')
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            links=dict(db.execute('SELECT source,target FROM mmva_delegate_alias WHERE course=?',(course,)))
            if source==target or source in links or target in links or source in links.values():raise HTTPException(409,'Identity links have changed. Refresh before combining.')
            db.execute('INSERT INTO mmva_delegate_alias VALUES (?,?,?,?,?,?)',(course,source,target,actor['email'],reason.strip(),time.time()))

def canonical(email,links):
    seen=set()
    while email in links and email not in seen:seen.add(email);email=links[email]
    return email

def linked_rows(rows,course,links):
    result=copy.deepcopy(rows)
    for r in result:
        if r.get('course',course)==course:r['email']=canonical(r['email'],links)
    return result
