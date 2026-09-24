"""Account switches use fake CLI responses and temporary databases only."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import codex_runner as runner
from server import Monitor


class AccountIdentityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.state = self.root / 'account.json'
        script = self.root / 'fake_cli.py'
        script.write_text('''import sys,json
from pathlib import Path
assert sys.argv[1]=='app-server'
for line in sys.stdin:
    message=json.loads(line)
    if 'id' not in message: continue
    method=message['method']
    data=json.loads((Path(__file__).parent/'account.json').read_text())
    if method=='initialize': result={}
    elif method=='account/read':
        assert message['params']=={'refreshToken':False}
        result={'account':data.get('account')}
    elif method=='model/list': result={'data':[{'model':data['model'],'supportedReasoningEfforts':[{'reasoningEffort':'low'}]}]}
    else: raise RuntimeError('Unexpected call '+method)
    print(json.dumps({'id':message['id'],'result':result}),flush=True)
''', encoding='utf-8')
        for mocker in (patch.object(runner, 'codex_command', return_value=[sys.executable, str(script)]),
                       patch.object(runner, 'auth_options', return_value=[]),
                       patch.multiple(runner, _catalog=None, _catalog_at=0, _catalog_account='', _catalog_command=None)):
            mocker.start(); self.addCleanup(mocker.stop)

    def select(self, email, model='model-a'):
        self.state.write_text(json.dumps({'account':{'type':'chatgpt', 'email':email, 'planType':'business', 'workspaceId':'same-workspace'}, 'model':model}))

    def test_same_workspace_different_people_change_identity_and_catalog(self):
        self.select('alice@example.com')
        first = runner.login_status()
        self.assertTrue(first['logged_in'])
        self.assertEqual(runner.available_models(first['account_fingerprint'])[0]['model'], 'model-a')
        self.select('bob@example.com', 'model-b')
        second = runner.login_status()
        self.assertNotEqual(first['account_fingerprint'], second['account_fingerprint'])
        self.assertEqual(second['account_email_masked'], 'bo***@example.com')
        self.assertNotIn('bob@example.com', json.dumps(second))
        self.assertEqual(runner.available_models(second['account_fingerprint'])[0]['model'], 'model-b')
        self.assertEqual(runner.account_status({'type':'chatgpt','email':' BOB@EXAMPLE.COM '})['account_fingerprint'], second['account_fingerprint'])

    def test_switch_during_catalog_read_does_not_associate_old_identity(self):
        self.select('alice@example.com')
        first = runner.login_status()
        self.select('bob@example.com')
        with self.assertRaisesRegex(ValueError, '账号已切换'):
            runner.available_models(first['account_fingerprint'], force_refresh=True)

    def test_logout_or_missing_identity_never_reuses_previous_person(self):
        self.select('alice@example.com')
        first = runner.login_status()
        self.assertTrue(first['logged_in'])
        for account in (None, {'type':'apiKey'}, {'type':'chatgpt','email':None}):
            self.state.write_text(json.dumps({'account':account}))
            current = runner.login_status()
            self.assertFalse(current['logged_in'])
            self.assertEqual(current['account_fingerprint'], '')
            with self.assertRaises(ValueError):
                runner.verify_account({'_codex_account_fingerprint':first['account_fingerprint']})

    def test_missing_expected_identity_is_rejected(self):
        with patch.object(runner, 'login_status', return_value={'logged_in':True,'account_fingerprint':''}):
            with self.assertRaisesRegex(ValueError, '账号已切换'):
                runner.verify_account({'_codex_account_fingerprint':'acct-aaaaaaaaaaaa'})

    def test_cli_failure_is_unknown_and_does_not_return_old_account(self):
        self.select('alice@example.com')
        runner.login_status()
        with patch.object(runner, 'account_client', side_effect=ValueError('failure')):
            current = runner.login_status()
        self.assertTrue(current['installed'])
        self.assertFalse(current['logged_in'])
        self.assertEqual(current['account_fingerprint'], '')


class LegacyAccountTests(unittest.TestCase):
    def test_legacy_workspace_label_is_not_reassigned_and_stats_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = Monitor(folder)
            with monitor.db() as db:
                db.execute("INSERT INTO runs(started,finished,status,source,model,base_url,effort,protocol,scene,nonce,prompt,account_fingerprint,account_label) VALUES (?,?,'success','manual','fixture','codex://local','low','codex','','','prompt','acct-aaaaaaaaaaaa','账号 aaaaaaaaaaaa')", (time.time()-1, time.time()))
            before = monitor.stats()
            self.assertEqual(before['success'], 1)
            reloaded = Monitor(folder)
            record = reloaded.history()['items'][0]
            self.assertEqual(record['account_fingerprint'], 'acct-aaaaaaaaaaaa')
            self.assertIn('未区分个人账号', record['account_label'])
            self.assertEqual(reloaded.stats(), before)
            self.assertNotIn('account_label', reloaded.runs()[0])

    def test_exec_auth_override_preserves_only_credential_store(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / 'config.toml').write_text('cli_auth_credentials_store="keyring"\nmodel="ignored"\n')
            with patch.object(runner, '_auth_path', return_value=path / 'auth.json'):
                self.assertEqual(runner.auth_options(), ['-c', 'cli_auth_credentials_store="keyring"'])


if __name__ == '__main__':
    unittest.main()
