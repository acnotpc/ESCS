"""Individual trainer accounts. Passwords and invitation tokens are never stored in plaintext."""
from contextlib import contextmanager
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from fastapi import HTTPException

ITERATIONS = 600_000

class TrainerAccounts:
    def __init__(self, path, secret):
        self.path, self.secret = str(path), secret
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS trainer_user(email TEXT PRIMARY KEY,name TEXT NOT NULL,password_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,invite_hash TEXT,invite_expires REAL);
            CREATE TABLE IF NOT EXISTS trainer_auth_attempt(key TEXT PRIMARY KEY,started REAL NOT NULL,attempts INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS trainer_reset_attempt(key TEXT PRIMARY KEY,started REAL NOT NULL,attempts INTEGER NOT NULL);
            ''')
            columns={r['name'] for r in db.execute('PRAGMA table_info(trainer_user)')}
            for column,kind in [('reset_hash','TEXT'),('reset_expires','REAL')]:
                if column not in columns:db.execute('ALTER TABLE trainer_user ADD COLUMN '+column+' '+kind)

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def invite(self, email, name, reset=False):
        email=str(email).strip().casefold();name=str(name).strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(email)>254 or not name or len(name)>150:
            raise HTTPException(400,'Enter a valid trainer name and email.')
        token=secrets.token_urlsafe(32);digest=hashlib.sha256(token.encode()).hexdigest();expires=time.time()+86400
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT email FROM trainer_user WHERE email=?',(email,)).fetchone()
            if old and not reset:raise HTTPException(409,'A trainer account already exists. Use the administrator reset option if needed.')
            if old:
                db.execute('UPDATE trainer_user SET name=?,version=version+1,invite_hash=?,invite_expires=?,reset_hash=NULL,reset_expires=NULL WHERE email=?',(name,digest,expires,email))
            else:db.execute('INSERT INTO trainer_user(email,name,invite_hash,invite_expires) VALUES(?,?,?,?)',(email,name,digest,expires))
        return {'email':email,'name':name,'setup_path':'/attendance/trainer/setup#token='+token,'expires_in_hours':24}

    def limit(self, ip, email=''):
        now=time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');db.execute('DELETE FROM trainer_auth_attempt WHERE started<?',(now-300,))
            for kind,value,cap in [('ip',ip,30),('email',email,10)]:
                if not value:continue
                key=hmac.new(self.secret.encode(),(kind+':'+value).encode(),hashlib.sha256).hexdigest()
                row=db.execute('SELECT attempts FROM trainer_auth_attempt WHERE key=?',(key,)).fetchone()
                if row and row['attempts']>=cap:raise HTTPException(429,'Too many attempts. Please wait five minutes.')
                db.execute('INSERT INTO trainer_auth_attempt(key,started,attempts) VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET attempts=attempts+1',(key,now))

    def authenticate(self, email, password, ip):
        email=str(email).strip().casefold();password=password if isinstance(password,str) else ''
        self.limit(ip,email)
        if len(password.encode())>1024:raise HTTPException(401,'Email or password was not recognised.')
        with self.db() as db:row=db.execute('SELECT * FROM trainer_user WHERE email=?',(email,)).fetchone()
        salt,expected=b'\0'*16,b'\0'*32
        if row and row['password_hash']:
            try:
                algorithm,iterations,salt_hex,key_hex=row['password_hash'].split('$')
                if algorithm!='pbkdf2_sha256' or int(iterations)!=ITERATIONS:raise ValueError()
                salt,expected=bytes.fromhex(salt_hex),bytes.fromhex(key_hex)
            except (ValueError,TypeError):raise HTTPException(401,'Email or password was not recognised.')
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,ITERATIONS)
        valid=hmac.compare_digest(actual,expected)
        if not valid or not row or not row['enabled'] or not row['password_hash']:
            raise HTTPException(401,'Email or password was not recognised.')
        return {'email':row['email'],'name':row['name'],'version':row['version']}

    def setup(self, token, password, ip):
        return self._set_password(token,password,ip,'invite')

    def request_reset(self,email,ip):
        email=str(email).strip().casefold()[:255];now=time.time()
        # Independent, durable limits apply equally to known and unknown addresses.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM trainer_reset_attempt WHERE started<?',(now-3600,))
            keys=[('ip:'+str(ip),20,900),('email:'+email,3,3600),('global',50,3600)]
            counters=[]
            for value,cap,window in keys:
                key=hmac.new(self.secret.encode(),('reset:'+value).encode(),hashlib.sha256).hexdigest()
                row=db.execute('SELECT started,attempts FROM trainer_reset_attempt WHERE key=?',(key,)).fetchone()
                count=row['attempts'] if row and row['started']>now-window else 0
                if count>=cap:return None
                counters.append((key,row['started'] if count else now,count+1))
            for key,started,count in counters:
                db.execute('INSERT INTO trainer_reset_attempt VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET started=excluded.started,attempts=excluded.attempts',(key,started,count))
            row=db.execute('SELECT email,name FROM trainer_user WHERE email=? AND enabled=1',(email,)).fetchone()
            if not row:return None
            token=secrets.token_urlsafe(32)
            db.execute('UPDATE trainer_user SET reset_hash=?,reset_expires=? WHERE email=?',(hashlib.sha256(token.encode()).hexdigest(),now+1800,row['email']))
        return {'email':row['email'],'name':row['name'],'reset_path':'/attendance/trainer/reset#token='+token}

    def reset_password(self,token,password,ip):
        return self._set_password(token,password,ip,'reset')

    def _set_password(self, token, password, ip, kind):
        assert kind in {'invite','reset'}
        column=kind+'_hash';expiry=kind+'_expires'
        self.limit(ip)
        if not isinstance(password,str) or len(password)<15 or len(password.encode())>1024:
            raise HTTPException(400,'Choose a password of at least 15 characters (up to 1,024 bytes).')
        if not isinstance(token,str) or len(token)>128:raise HTTPException(400,'This link is invalid or expired. Request a new reset link or contact your administrator.')
        digest=hashlib.sha256(token.encode()).hexdigest();now=time.time()
        with self.db() as db:row=db.execute('SELECT email FROM trainer_user WHERE '+column+'=? AND '+expiry+'>? AND enabled=1',(digest,now)).fetchone()
        if not row:raise HTTPException(400,'This link is invalid or expired. Request a new reset link or contact your administrator.')
        salt=secrets.token_bytes(16);key=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,ITERATIONS)
        encoded='pbkdf2_sha256$'+str(ITERATIONS)+'$'+salt.hex()+'$'+key.hex()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed=db.execute('UPDATE trainer_user SET password_hash=?,invite_hash=NULL,invite_expires=NULL,reset_hash=NULL,reset_expires=NULL,version=version+1 WHERE email=? AND '+column+'=? AND '+expiry+'>? AND enabled=1',(encoded,row['email'],digest,time.time())).rowcount
            if changed!=1:raise HTTPException(409,'This setup link has already been used or expired.')
        return {'ready':True,'email':row['email']}

    def check(self, session):
        if not isinstance(session,dict):raise HTTPException(401,'Please sign in to view course bookings.')
        with self.db() as db:row=db.execute('SELECT email,name,version,enabled,password_hash FROM trainer_user WHERE email=?',(str(session.get('email','')),)).fetchone()
        if not row or not row['enabled'] or not row['password_hash'] or row['version']!=session.get('version'):
            raise HTTPException(401,'Please sign in to view course bookings.')
        return {'email':row['email'],'name':row['name'],'version':row['version']}

    def disable(self,email):
        with self.db() as db:
            changed=db.execute('UPDATE trainer_user SET enabled=0,version=version+1,invite_hash=NULL,invite_expires=NULL,reset_hash=NULL,reset_expires=NULL WHERE email=?',(str(email).strip().casefold(),)).rowcount
        if not changed:raise HTTPException(404,'Trainer account not found.')
        return {'disabled':True}
