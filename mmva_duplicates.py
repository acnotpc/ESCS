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

NAME_WARNING='Different names use this email; review identity'
def name_revision(person):
    return hashlib.sha256(json.dumps({'email':person['email'],'names':person.get('nameVariants',[])},sort_keys=True).encode()).hexdigest()

class NameReviews:
    def __init__(self,path):
        self.path=path
        with sqlite3.connect(path,timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS mmva_name_review (course TEXT, email TEXT, version INTEGER, revision TEXT, display_name TEXT, actor TEXT, reason TEXT, recorded_at REAL, PRIMARY KEY(course,email,version))')
    def history(self,course):
        with sqlite3.connect(self.path,timeout=10) as db:
            rows=db.execute('SELECT email,version,revision,display_name,actor,reason,recorded_at FROM mmva_name_review WHERE course=? ORDER BY version',(course,))
            result={}
            for r in rows:result.setdefault(r[0],[]).append(dict(zip(('email','version','revision','name','trainer','reason','time'),r)))
            return result
    def save(self,course,person,actor,name,reason,version):
        if name not in person.get('nameVariants',[]):raise HTTPException(400,'Choose one of the recorded names.')
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise HTTPException(400,'Record how you verified the name variations (up to 1,000 characters).')
        with sqlite3.connect(self.path,timeout=10) as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT MAX(version) FROM mmva_name_review WHERE course=? AND email=?',(course,person['email'])).fetchone()[0] or 0
            if type(version)!=int or version!=current:raise HTTPException(409,'The name verification changed. Refresh before saving.')
            db.execute('INSERT INTO mmva_name_review VALUES (?,?,?,?,?,?,?,?)',(course,person['email'],current+1,name_revision(person),name,actor['email'],reason.strip(),time.time()))

def apply_name_reviews(report,history,today):
    for p in report['delegates']:
        records=history.get(p['email'],[]);latest=records[-1] if records else None
        verified=bool(latest and latest['revision']==name_revision(p))
        p['nameReview']={'verified':verified,'revision':name_revision(p),'version':latest['version'] if latest else 0,'history':records}
        if verified:
            p['name']=latest['name'];p['issues']=[i for i in p['issues'] if i!=NAME_WARNING]
            p['complete']=not p['outstanding'] and not p['issues'] and today>=report['course']['end']
    return report
