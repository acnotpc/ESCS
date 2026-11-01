import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from mmva_trainer_auth import TrainerAccounts

class TrainerAuthTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=str(Path(self.tmp.name)/'auth.sqlite3');self.auth=TrainerAccounts(self.path,'x'*40)
 def tearDown(self):self.tmp.cleanup()
 def enrol(self,email='trainer@example.invalid'):
  invite=self.auth.invite(email,'Test Trainer');token=invite['setup_path'].split('token=')[1]
  self.auth.setup(token,'correct test passphrase 123','setup-ip');return token
 def test_invitation_required_and_single_use(self):
  with self.assertRaises(HTTPException):self.auth.setup('invented','correct test passphrase 123','setup-ip')
  token=self.enrol()
  with self.assertRaises(HTTPException):self.auth.setup(token,'another password test 123','setup-ip')
  user=self.auth.authenticate(' TRAINER@example.invalid ','correct test passphrase 123','login-ip');self.assertEqual(user['name'],'Test Trainer')
  with self.auth.db() as db:
   row=db.execute('SELECT * FROM trainer_user').fetchone();self.assertIsNone(row['invite_hash']);self.assertNotIn('correct test passphrase',row['password_hash'])
 def test_expired_invitation_and_short_password(self):
  invite=self.auth.invite('trainer@example.invalid','Test Trainer');token=invite['setup_path'].split('token=')[1]
  with self.assertRaises(HTTPException):self.auth.setup(token,'short','setup-ip')
  with self.auth.db() as db:db.execute('UPDATE trainer_user SET invite_expires=0')
  with self.assertRaises(HTTPException):self.auth.setup(token,'correct test passphrase 123','setup-ip')
 def test_restart_and_disable(self):
  self.enrol();user=self.auth.authenticate('trainer@example.invalid','correct test passphrase 123','login-ip')
  other=TrainerAccounts(self.path,'x'*40);self.assertEqual(other.check(user)['email'],'trainer@example.invalid')
  other.disable(user['email'])
  with self.assertRaises(HTTPException):self.auth.check(user)
  with self.assertRaises(HTTPException):self.auth.authenticate(user['email'],'correct test passphrase 123','login-ip')
 def test_reset_revokes_sessions_and_requires_explicit_admin_reset(self):
  self.enrol();user=self.auth.authenticate('trainer@example.invalid','correct test passphrase 123','login-ip')
  with self.assertRaises(HTTPException):self.auth.invite(user['email'],'Trainer')
  invite=self.auth.invite(user['email'],'Trainer',reset=True)
  with self.assertRaises(HTTPException):self.auth.check(user)
  self.auth.setup(invite['setup_path'].split('token=')[1],'different test passphrase 456','setup-ip')
  with self.assertRaises(HTTPException):self.auth.authenticate(user['email'],'correct test passphrase 123','login-ip')
  self.assertEqual(self.auth.authenticate(user['email'],'different test passphrase 456','login-ip')['email'],user['email'])
 def test_unknown_and_invalid_password_same_error(self):
  self.enrol()
  errors=[]
  for email in ['trainer@example.invalid','missing@example.invalid']:
   with self.assertRaises(HTTPException) as e:self.auth.authenticate(email,'wrong-password','login-ip')
   errors.append((e.exception.status_code,e.exception.detail))
  self.assertEqual(errors[0],errors[1])
 def test_throttling_persists_across_registry_instances(self):
  for _ in range(10):
   with self.assertRaises(HTTPException):self.auth.authenticate('trainer@example.invalid','wrong-password','login-ip')
  with self.assertRaises(HTTPException) as e:TrainerAccounts(self.path,'x'*40).authenticate('trainer@example.invalid','wrong-password','other-ip')
  self.assertEqual(e.exception.status_code,429)

if __name__=='__main__':unittest.main()
