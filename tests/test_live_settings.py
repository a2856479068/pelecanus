"""Real scheduling and persistence with an injected model; no paid requests."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from server import Monitor, PROMPT


class LiveSettingsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        def model(config, prompt):
            self.calls.append((config['model'], prompt))
            self.entered.set()
            self.release.wait(3)
            return '<html><body><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="5"/></svg></body></html>', {}, config['model']
        self.monitor = Monitor(self.folder.name, model_call=model)
        self.monitor.save(dict(protocol='chat', base_url='https://example.com/v1', api_key='fixture', model='fixture'))

    def tearDown(self):
        self.release.set()
        self.wait_worker()
        self.folder.cleanup()

    def wait_worker(self):
        deadline = time.monotonic() + 4
        while self.monitor.run_cancellations and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.monitor.run_cancellations)

    def test_legacy_minutes_are_migrated_once_without_changing_duration(self):
        with self.monitor.db() as db:
            db.execute('UPDATE settings SET value=?', (json.dumps({'interval_minutes':7}),))
        restarted = Monitor(self.folder.name)
        self.assertEqual(restarted.settings()['interval_seconds'], 420)
        self.assertEqual(restarted.settings()['task_prompt'], PROMPT)
        with restarted.db() as db:
            stored = json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
        self.assertNotIn('interval_minutes', stored)
        self.assertEqual(Monitor(self.folder.name).settings()['interval_seconds'], 420)

    def test_seconds_and_prompt_changes_during_request_affect_only_future_work(self):
        original = '  原始提示词\n第二行  '
        self.monitor.save({'interval_seconds':7, 'task_prompt':original, 'enabled':True})
        rid = self.monitor.start_run()
        self.assertTrue(self.entered.wait(2))
        self.monitor.save({'interval_seconds':3, 'task_prompt':'下一轮提示词'})
        self.assertIsNone(self.monitor.settings()['next_run'])
        self.release.set(); self.wait_worker()
        with self.monitor.db() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
        self.assertEqual(row['prompt'], original)
        self.assertEqual(self.calls, [('fixture', original)])
        self.assertEqual(self.monitor.settings()['next_run'], row['finished'] + 3)
        self.assertEqual(self.monitor.state()['task_prompt'], '下一轮提示词')
        self.assertIsNone(self.monitor.schedule_once(row['finished'] + 2.9))
        self.monitor.stop_testing()
        self.monitor.run_once(); self.wait_worker()
        self.assertEqual(self.calls[-1][1], '下一轮提示词')
        self.assertFalse(self.monitor.settings()['enabled'])
        self.assertIsNone(self.monitor.settings()['next_run'])

    def test_start_stop_and_single_run_have_authoritative_states(self):
        self.monitor.start_testing()
        with self.assertRaisesRegex(ValueError, '暂停'):
            self.monitor.run_once()
        self.monitor.schedule_once(time.time() + 1)
        self.assertTrue(self.entered.wait(2))
        running = self.monitor.state()
        self.assertIsNotNone(running['running'])
        self.monitor.stop_testing()
        stopping = self.monitor.state()
        self.assertFalse(stopping['settings']['enabled'])
        self.assertTrue(stopping['stopping'])
        self.assertIsNone(stopping['running'])
        self.release.set(); self.wait_worker()
        self.assertFalse(self.monitor.state()['stopping'])
        self.assertIsNone(self.monitor.schedule_once(time.time() + 100))

    def test_group_single_run_only_uses_first_enabled_member(self):
        with self.monitor.db() as db:
            db.execute("INSERT INTO node_groups(id,name,enabled,created) VALUES (1,'group',1,?)", (time.time(),))
            for position, enabled in ((0, 0), (1, 1), (2, 1)):
                db.execute("INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id,group_position) VALUES (?,?,?,?,?,'chat',?,1,?)",
                           (f'node{position}', 'https://example.com/v1', 'fixture', f'model{position}', 'low', enabled, position))
        self.monitor.save({'schedule_mode':'groups'})
        self.release.set()
        self.monitor.run_once(); self.wait_worker()
        self.assertEqual([call[0] for call in self.calls], ['model1'])
        self.assertFalse(self.monitor.settings()['enabled'])
        self.assertIsNone(self.monitor.schedule_once(time.time() + 100000))
        self.assertFalse(self.monitor.scheduled_queue)

    def test_guest_prompt_is_snapshotted_when_submitted(self):
        self.monitor.save({'guest_enabled':True, 'task_prompt':'submitted prompt'})
        config, published = self.monitor.prepare_guest(dict(protocol='chat', base_url='https://example.com/v1', api_key='fixture', model='fixture'))
        self.monitor.save({'task_prompt':'later prompt'})
        self.release.set()
        self.monitor.execute_guest(config, published)
        self.assertEqual(self.calls[-1][1], 'submitted prompt')
        self.assertEqual(published['prompt'], 'submitted prompt')

    def test_invalid_prompt_and_seconds_do_not_replace_saved_settings(self):
        before = self.monitor.settings()
        for values in ({'task_prompt':'  '}, {'task_prompt':'x'*4001}, {'interval_seconds':0}, {'interval_seconds':True}, {'interval_seconds':1.5}, {'interval_seconds':86401}):
            with self.assertRaises(ValueError):
                self.monitor.save(values)
        self.assertEqual(self.monitor.settings(), before)


if __name__ == '__main__':
    unittest.main()
