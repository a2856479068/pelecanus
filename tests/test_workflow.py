"""Regression checks use temporary databases and a fake model, never account quota."""
import hashlib
from http.cookiejar import CookieJar
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from server import Handler, Monitor, PROMPT, SESSION_TTL, ThreadingHTTPServer, downloadable_svg
from codex_runner import call_codex

SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 800"><rect width="1200" height="800" fill="skyblue"/></svg>'
HTML = '<!doctype html><html><body>' + SVG + '</body></html>'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.monitor = Monitor(self.folder.name)
        self.monitor.save(dict(protocol="chat", base_url="https://example.com/v1", api_key="test-only", model="fixture"))

    def seed(self, count=1, status="success", protocol="chat"):
        with self.monitor.db() as db:
            ids = []
            for _ in range(count):
                row = db.execute("""INSERT INTO runs
                    (started,finished,status,source,model,base_url,effort,protocol,scene,nonce,prompt,output)
                    VALUES (?, ?, ?, 'manual','fixture','https://example.com/v1','low',?,'','',?,?)""",
                    (time.time(), time.time(), status, protocol, PROMPT, HTML))
                ids.append(row.lastrowid)
                db.execute("INSERT INTO image_library(run_id,svg,created) VALUES (?,?,?)", (row.lastrowid, SVG, time.time()))
        return ids

    def wait_for_worker(self):
        deadline = time.monotonic() + 3
        while self.monitor.run_cancellations and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.monitor.run_cancellations)

    def test_favorites_survive_restart_and_block_every_delete_path(self):
        favorite, ordinary = self.seed(2)
        self.monitor.set_favorite(favorite, True)
        self.monitor.set_favorite(favorite, True)  # Retrying a request is idempotent.
        restarted = Monitor(self.folder.name)
        self.assertEqual(restarted.gallery(favorite='yes')['items'][0]['id'], favorite)
        self.assertIs(restarted.history(favorite='yes')['items'][0]['favorite'], True)
        self.assertEqual(restarted.gallery(selection_only=True)['ids'], [ordinary])
        self.assertEqual(restarted.history(selection_only=True)['ids'], [ordinary])
        self.assertEqual(restarted.gallery(favorite='yes', selection_only=True)['ids'], [])
        before = restarted.stats()
        for ids in ([favorite], [ordinary, favorite]):
            with self.assertRaisesRegex(ValueError, '收藏'):
                restarted.delete_runs(ids)
        with self.assertRaisesRegex(ValueError, '收藏'):
            restarted.reset_generation_data()
        self.assertEqual(restarted.gallery()['total'], 2)
        self.assertEqual(restarted.stats(), before)
        with restarted.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM image_library').fetchone()[0], 2)
        restarted.set_favorite(favorite, False)
        self.assertEqual(restarted.delete_runs([favorite])['deleted'], 1)
        self.assertEqual(restarted.stats(), before)
        self.assertEqual(restarted.reset_generation_data()['deleted_runs'], 1)

    def test_favorite_rejects_invalid_values_and_running_records(self):
        rid = self.seed(status='running')[0]
        with self.assertRaisesRegex(ValueError, '生成完成'):
            self.monitor.set_favorite(rid, True)
        for value in (None, 'false', 1, [], {}):
            with self.assertRaises(ValueError):
                self.monitor.set_favorite(rid, value)
        with self.assertRaisesRegex(ValueError, '不存在'):
            self.monitor.set_favorite(rid + 1, True)
        for fetch in (self.monitor.history, self.monitor.gallery):
            with self.assertRaises(ValueError):
                fetch(favorite='invalid')

    def test_favorite_migration_preserves_existing_artwork_and_statistics(self):
        rid = self.seed()[0]
        with self.monitor.db() as db:
            db.execute('ALTER TABLE runs DROP COLUMN favorite')
        before = self.monitor.stats()
        restarted = Monitor(self.folder.name)
        row = restarted.gallery()['items'][0]
        self.assertEqual(row['id'], rid)
        self.assertIs(row['favorite'], False)
        self.assertTrue(row['has_svg'])
        self.assertEqual(restarted.stats(), before)

    def test_svg_export_embeds_safe_styles_and_ignores_external_resources(self):
        styled = HTML.replace('<body>', '<head><style>rect {fill: tomato}</style></head><body>')
        self.assertIn('rect {fill: tomato}', downloadable_svg(SVG, styled))
        for css in ('@import "https://example.com/style.css";', 'rect{fill:url(https://example.com/image)}'):
            unsafe = HTML.replace('<body>', '<head><style>' + css + '</style></head><body>')
            self.assertEqual(downloadable_svg(SVG, unsafe), SVG)

    def test_svg_export_preserves_inline_animation_scripts_as_valid_xml(self):
        script = "const r=document.querySelector('rect'); if (1 < 2 && r) r.setAttribute('x', '10');"
        output = HTML.replace('</body>', '<script>' + script + '</script><script src="https://example.com/external.js"></script></body>')
        exported = ET.fromstring(downloadable_svg(SVG, output))
        scripts = exported.findall('{http://www.w3.org/2000/svg}script')
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].text, script)
        self.assertEqual(exported.get('viewBox'), '0 0 1200 800')

    def test_codex_invocations_use_fresh_directories_and_clean_up_after_failure(self):
        fixture = Path(self.folder.name) / 'fake_codex.py'
        locations = Path(self.folder.name) / 'locations.jsonl'
        fixture.write_text('''import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == 'exec' and 'resume' not in args
assert '--ephemeral' in args and '--ignore-user-config' in args
assert '--ignore-rules' in args and 'project_doc_max_bytes=0' in args
assert args[args.index('--sandbox') + 1] == 'read-only'
assert 'features.shell_tool=false' in args and 'features.multi_agent=false' in args
with (Path(__file__).parent / 'locations.jsonl').open('a') as log:
    log.write(json.dumps(str(Path.cwd())) + '\\n')
assert not (Path.cwd() / 'previous.txt').exists()
(Path.cwd() / 'previous.txt').write_text('previous invocation')
if args[args.index('--model') + 1] == 'fail':
    sys.exit(1)
Path(args[args.index('--output-last-message') + 1]).write_text(sys.stdin.read(), encoding='utf-8')
print(json.dumps({'type':'turn.completed','usage':{}}))
''', encoding='utf-8')
        config = dict(model='fixture', effort='low', timeout_seconds=10)
        with patch('codex_runner.codex_command', return_value=[sys.executable, str(fixture)]), \
             patch('codex_runner.verify_account', return_value={}), \
             patch('codex_runner.available_models', return_value=[]):
            self.assertEqual(call_codex(config, 'first prompt')[0], 'first prompt')
            self.assertEqual(call_codex(config, 'second prompt')[0], 'second prompt')
            with self.assertRaisesRegex(ValueError, '生成失败'):
                call_codex(dict(config, model='fail'), 'failed prompt')
        paths = [Path(json.loads(line)) for line in locations.read_text().splitlines()]
        self.assertEqual(len(set(paths)), 3)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_account_snapshot_is_private_and_not_reassigned_after_switch(self):
        legacy = self.seed(protocol="codex")[0]
        self.monitor.save(dict(protocol="codex"))
        self.monitor.model_call = lambda config, prompt: (HTML, {}, 'fixture')
        with patch('server.login_status', return_value={'logged_in':True, 'account_fingerprint':'acct-aaaaaaaaaaaa'}):
            first = self.monitor.start_run()
            self.wait_for_worker()
        def failed(config, prompt):
            raise ValueError('fixture failure')
        self.monitor.model_call = failed
        with patch('server.login_status', return_value={'logged_in':True, 'account_fingerprint':'acct-bbbbbbbbbbbb'}):
            second = self.monitor.start_run()
            self.wait_for_worker()
        restarted = Monitor(self.folder.name)
        rows = {row['id']:row for row in restarted.history()['items']}
        self.assertEqual(rows[first]['account_fingerprint'], 'acct-aaaaaaaaaaaa')
        self.assertEqual(rows[second]['account_fingerprint'], 'acct-bbbbbbbbbbbb')
        self.assertEqual(rows[second]['status'], 'error')
        self.assertEqual(rows[legacy]['account_fingerprint'], '')
        self.assertEqual(restarted.history(account='unknown')['total'], 1)
        self.assertEqual(restarted.history(account='acct-aaaaaaaaaaaa')['items'][0]['id'], first)
        public = restarted.runs() + restarted.gallery()['items'] + restarted.state()['timeline']
        for row in public:
            self.assertNotIn('account_fingerprint', row)
            self.assertNotIn('account_label', row)

    def test_history_filters_and_selection_use_all_pages(self):
        wanted = self.seed(25, protocol='codex')
        unrelated = self.seed(protocol='codex')
        api_run = self.seed()[0]
        with self.monitor.db() as db:
            db.executemany("UPDATE runs SET account_fingerprint='acct-aaaaaaaaaaaa',account_label='Account A',group_name='Group A' WHERE id=?", [(rid,) for rid in wanted])
            db.execute("UPDATE runs SET status='error',error='fixture failure' WHERE id=?", (wanted[0],))
            db.execute("UPDATE runs SET status='running' WHERE id=?", (wanted[1],))
        page = self.monitor.history(account='acct-aaaaaaaaaaaa', model='fixture', group='Group A')
        self.assertEqual((page['total'], page['pages'], len(page['items'])), (25, 2, 20))
        self.assertEqual(len(self.monitor.history(page=2, account='acct-aaaaaaaaaaaa')['items']), 5)
        selected = self.monitor.history(account='acct-aaaaaaaaaaaa', status='success', selection_only=True)['ids']
        self.assertEqual(set(selected), set(wanted[2:]))
        self.assertEqual(self.monitor.history(status='running', selection_only=True)['ids'], [])
        self.assertEqual(self.monitor.history(account='api')['items'][0]['id'], api_run)
        self.assertEqual(self.monitor.history(account='unknown')['items'][0]['id'], unrelated[0])
        self.assertEqual(self.monitor.history(search=f'#{wanted[-1]}')['items'][0]['id'], wanted[-1])
        self.assertEqual(self.monitor.history(search="' OR 1=1 --")['total'], 0)

    def test_gallery_account_selection_and_deletion_cannot_cross_accounts(self):
        account_a, account_b = 'acct-aaaaaaaaaaaa', 'acct-bbbbbbbbbbbb'
        wanted = self.seed(25, protocol='codex')
        other = self.seed(2, protocol='codex')
        unknown = self.seed(protocol='codex')[0]
        api_run = self.seed()[0]
        with self.monitor.db() as db:
            db.executemany("UPDATE runs SET account_fingerprint=?,account_label='Account A' WHERE id=?",
                           [(account_a, rid) for rid in wanted])
            db.executemany("UPDATE runs SET account_fingerprint=?,account_label='Account B' WHERE id=?",
                           [(account_b, rid) for rid in other])
            db.execute("UPDATE runs SET status='running' WHERE id=?", (wanted[0],))
        private = self.monitor.gallery(page=3, account=account_a, include_account=True)
        self.assertEqual((private['total'], private['pages'], len(private['items'])), (25, 3, 1))
        self.assertEqual(private['items'][0]['account_fingerprint'], account_a)
        self.assertEqual({a['value'] for a in private['accounts']}, {account_a, account_b})
        public = self.monitor.gallery()
        self.assertNotIn('accounts', public)
        self.assertTrue(all('account_fingerprint' not in item for item in public['items']))
        with self.assertRaises(ValueError):
            self.monitor.gallery(account=account_a)
        selected = self.monitor.gallery(account=account_a, include_account=True, selection_only=True)['ids']
        self.assertEqual(set(selected), set(wanted[1:]))
        with self.assertRaisesRegex(ValueError, '不属于当前账号'):
            self.monitor.delete_runs([wanted[1], other[0]], account=account_a)
        self.assertEqual(self.monitor.gallery()['total'], 29)
        self.monitor.delete_runs(selected, account=account_a)
        remaining = self.monitor.gallery(include_account=True)['items']
        self.assertEqual({item['id'] for item in remaining}, {wanted[0], unknown, api_run, *other})
        self.assertEqual(self.monitor.stats()['total'], 28)

    def test_interval_begins_after_a_long_generation_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def fake_model(config, prompt):
            entered.set()
            release.wait(3)
            return HTML, {}, 'fixture'
        self.monitor.model_call = fake_model
        with patch('server.time.time', return_value=1000) as clock:
            self.monitor.save({'interval_minutes':5})
            self.monitor.start_testing()
            rid = self.monitor.schedule_once()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIsNone(self.monitor.settings()['next_run'])
                self.assertIsNone(self.monitor.schedule_once(now=5000))
                clock.return_value = 1420
            finally:
                release.set()
                self.wait_for_worker()
            self.assertEqual(self.monitor.runs()[0]['id'], rid)
            self.assertEqual(self.monitor.settings()['next_run'], 1720)
            self.assertEqual(self.monitor.start_testing()['next_run'], 1720)
            self.assertIsNone(self.monitor.schedule_once(now=1719.99))
            clock.return_value = 1720
            self.assertIsNotNone(self.monitor.schedule_once())
            self.wait_for_worker()
            self.assertEqual(len(self.monitor.runs()), 2)

    def test_group_members_wait_after_success_and_failure(self):
        with self.monitor.db() as db:
            group = db.execute("INSERT INTO node_groups(name,enabled,created) VALUES ('timing',1,?)", (time.time(),)).lastrowid
            members = []
            for name in ('first', 'second'):
                members.append(db.execute("""INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id)
                    VALUES (?,'https://example.com/v1','test-only','fixture','low','chat',1,?)""", (name, group)).lastrowid)
        calls = []
        def fake_model(config, prompt):
            calls.append(config['active_node_id'])
            if len(calls) == 1:
                raise ValueError('fixture failure')
            return HTML, {}, 'fixture'
        self.monitor.model_call = fake_model
        with patch('server.time.time', return_value=1000) as clock:
            self.monitor.save({'schedule_mode':'groups', 'interval_minutes':1})
            self.monitor.start_testing()
            self.monitor.schedule_once()
            self.wait_for_worker()
            self.assertEqual(self.monitor.runs()[0]['status'], 'error')
            self.assertEqual(self.monitor.settings()['next_run'], 1060)
            self.assertEqual(self.monitor.scheduled_queue, members[1:])
            self.assertIsNone(self.monitor.schedule_once(now=1059))
            clock.return_value = 1060
            self.monitor.schedule_once()
            self.wait_for_worker()
            self.assertEqual(calls, members)
            self.assertEqual(self.monitor.settings()['next_run'], 1120)
            self.assertIsNone(self.monitor.schedule_once(now=1119))
            clock.return_value = 1120
            self.monitor.schedule_once()
            self.wait_for_worker()
            self.assertEqual(calls, [*members, members[0]])

    def test_interval_edits_during_generation_and_restart_keep_finish_anchor(self):
        deadlines_during_generation = []
        def fake_model(config, prompt):
            self.monitor.save({'interval_minutes':7})
            deadlines_during_generation.append(self.monitor.settings()['next_run'])
            return HTML, {}, 'fixture'
        self.monitor.model_call = fake_model
        with patch('server.time.time', return_value=1234):
            self.monitor.start_testing()
            self.monitor.start_run()
            self.wait_for_worker()
            self.assertEqual(deadlines_during_generation, [None])
            self.assertEqual(self.monitor.settings()['next_run'], 1654)
            self.monitor.save({'interval_minutes':5})
            self.assertEqual(self.monitor.settings()['next_run'], 1534)
            restarted = Monitor(self.folder.name)
            self.assertEqual(restarted.settings()['next_run'], 1534)

    def test_request_rate_survives_artwork_deletion_and_excludes_cancellations(self):
        successful = self.seed(2)
        failed = self.seed(status='error')
        interrupted = self.seed(status='running')
        before = self.monitor.stats()
        self.assertEqual((before['total'], before['completed'], before['success'], before['failed'], before['rate']),
                         (3, 3, 2, 1, 66.7))
        self.monitor.delete_runs(successful[:1] + failed)
        self.assertEqual(self.monitor.stats()['rate'], 66.7)
        self.assertEqual(self.monitor.state()['stats']['rate'], 66.7)
        self.monitor.stop_run(interrupted[0])
        after_stop = self.monitor.stats()
        self.assertEqual((after_stop['total'], after_stop['completed'], after_stop['cancelled'], after_stop['failed']),
                         (3, 3, 1, 1))
        self.monitor.delete_runs(successful[1:] + interrupted)
        self.assertEqual(self.monitor.gallery()['total'], 0)
        restarted = Monitor(self.folder.name)
        self.assertEqual(restarted.stats()['total'], 3)
        self.assertEqual(restarted.state()['stats']['total'], 3)
        self.assertEqual(restarted.stats()['rate'], 66.7)
        with restarted.db() as db:
            db.execute('UPDATE run_outcomes SET started=? WHERE run_id=?',
                       (time.time() - 86400 - 5, failed[0]))
        self.assertEqual(restarted.stats()['rate'], 100.0)
        self.assertEqual(restarted.stats()['total'], 2)

    def test_request_counts_only_include_success_and_error_as_results_arrive(self):
        active = self.seed(status='running')[0]
        queued = self.seed(status='queued')[0]
        cancelled = self.seed(status='cancelled')[0]

        def assert_counts(total, success, failed, cancelled_count, running, rate):
            stats = self.monitor.stats()
            self.assertEqual((stats['total'], stats['completed'], stats['success'], stats['failed'],
                              stats['cancelled'], stats['running'], stats['rate']),
                             (total, total, success, failed, cancelled_count, running, rate))
            public = self.monitor.state()['stats']
            self.assertEqual((public['total'], public['completed'], public['success'], public['errors'], public['rate']),
                             (total, total, success, failed, rate))

        assert_counts(0, 0, 0, 1, 2, None)
        with self.monitor.db() as db:
            db.execute("UPDATE runs SET status='success' WHERE id=?", (active,))
        assert_counts(1, 1, 0, 1, 1, 100.0)
        with self.monitor.db() as db:
            db.execute("UPDATE runs SET status='running' WHERE id=?", (queued,))
        assert_counts(1, 1, 0, 1, 1, 100.0)
        with self.monitor.db() as db:
            db.execute("UPDATE runs SET status='error' WHERE id=?", (queued,))
            db.execute("UPDATE runs SET status='error' WHERE id=?", (queued,))
        assert_counts(2, 1, 1, 1, 0, 50.0)
        self.monitor.delete_runs([cancelled])
        assert_counts(2, 1, 1, 1, 0, 50.0)

    def test_request_count_uses_the_same_24_hour_window_as_success_rate(self):
        now = time.time()
        with self.monitor.db() as db:
            db.executemany('INSERT INTO run_outcomes(run_id,started,status) VALUES (?,?,?)',
                           [(1, now - 86400 - .001, 'error'), (2, now - 86400, 'success'),
                            (3, now - 1, 'error'), (4, now - 1, 'cancelled')])
        with patch('server.time.time', return_value=now):
            stats = self.monitor.stats()
            self.assertEqual((stats['total'], stats['completed'], stats['rate']), (2, 2, 50.0))
        with patch('server.time.time', return_value=now + .001):
            stats = self.monitor.stats()
            self.assertEqual((stats['total'], stats['completed'], stats['rate']), (1, 1, 0.0))

    def test_reused_run_number_keeps_both_request_outcomes(self):
        first = self.seed()[0]
        self.monitor.delete_runs([first])
        second = self.seed(status='error')[0]
        self.assertEqual(second, first)
        self.assertEqual(self.monitor.stats()['rate'], 50.0)
        self.assertEqual(self.monitor.stats()['total'], 2)
        self.assertEqual(self.monitor.gallery()['total'], 1)

    def test_database_reset_clears_content_and_outcomes_but_keeps_access_and_config(self):
        self.monitor.setup_password('test-password')
        token = self.monitor.login('test-password')
        removed, kept = self.seed(2)
        self.monitor.delete_runs([removed])
        with self.monitor.db() as db:
            db.execute('INSERT INTO guest_results VALUES (?,?,?)',
                       ('fixture-guest', time.time(), json.dumps({'status':'success','output':HTML,'svg':SVG})))
        self.monitor.start_testing()
        result = self.monitor.reset_generation_data()
        self.assertEqual((result['deleted_runs'], result['deleted_images'], result['deleted_guest_results'],
                          result['deleted_outcomes'], result['compacted']), (1, 1, 1, 2, True))
        restarted = Monitor(self.folder.name)
        self.assertTrue(restarted.authenticated(token))
        self.assertEqual(restarted.gallery()['total'], 0)
        self.assertEqual(restarted.stats()['total'], 0)
        self.assertIsNone(restarted.stats()['rate'])
        self.assertFalse(restarted.settings()['enabled'])
        self.assertIsNone(restarted.settings()['next_run'])
        self.assertEqual(restarted.settings()['model'], 'fixture')
        with restarted.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM guest_results').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_database_reset_waits_for_active_generation_and_guest_requests(self):
        active = self.seed(status='running')[0]
        with self.assertRaisesRegex(ValueError, '正在生成'):
            self.monitor.reset_generation_data()
        with self.monitor.db() as db:
            db.execute("UPDATE runs SET status='success' WHERE id=?", (active,))
            db.execute('INSERT INTO guest_results VALUES (?,?,?)',
                       ('fixture-guest', time.time(), json.dumps({'status':'queued'})))
        with self.assertRaisesRegex(ValueError, '访客请求'):
            self.monitor.reset_generation_data()
        self.assertEqual(self.monitor.gallery()['total'], 1)
        self.assertEqual(self.monitor.stats()['total'], 1)

    def test_remembered_login_survives_restart_and_logout_revokes_it(self):
        self.monitor.setup_password("test-password")
        token = self.monitor.login("test-password")
        self.assertGreaterEqual(SESSION_TTL, 30 * 86400)
        restarted = Monitor(self.folder.name)
        self.assertTrue(restarted.authenticated(token))
        self.assertFalse(restarted.authenticated("wrong-token"))
        with restarted.db() as db:
            saved = db.execute("SELECT token_hash FROM admin_sessions").fetchone()[0]
        self.assertNotEqual(saved, token)
        self.assertEqual(saved, hashlib.sha256(token.encode()).hexdigest())
        restarted.logout(token)
        self.assertFalse(self.monitor.authenticated(token))
        expired = restarted.login("test-password")
        with restarted.db() as db:
            db.execute("UPDATE admin_sessions SET expires=0")
        self.assertFalse(restarted.authenticated(expired))

    def test_select_all_filters_every_page_and_deletes_only_the_snapshot(self):
        wanted = self.seed(1005)
        failed = self.seed(status="error")
        active = self.seed(status="running")
        other_protocol = self.seed(protocol="responses")
        snapshot = self.monitor.gallery(status="success", protocol="chat", selection_only=True)["ids"]
        self.assertEqual(set(snapshot), set(wanted))
        self.assertNotIn(active[0], self.monitor.gallery(selection_only=True)["ids"])
        newer = self.seed()
        for offset in range(0, len(snapshot), 500):
            batch = snapshot[offset:offset + 500]
            self.assertEqual(self.monitor.delete_runs(batch)["deleted"], len(batch))
        with self.monitor.db() as db:
            remaining = {row[0] for row in db.execute("SELECT id FROM runs")}
            images = {row[0] for row in db.execute("SELECT run_id FROM image_library")}
        self.assertEqual(remaining, set(failed + active + other_protocol + newer))
        self.assertEqual(images, remaining)
        with self.assertRaises(ValueError):
            self.monitor.delete_runs(failed + active)
        self.assertEqual(len(self.monitor.gallery(selection_only=True)["ids"]), 3)

    def test_stop_cancels_active_group_clears_queue_and_waits_for_worker(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def fake_model(config, prompt):
            calls.append(prompt)
            entered.set()
            config["_cancel_event"].wait(5)
            release.wait(5)
            return HTML, {}, "fixture"
        self.monitor.model_call = fake_model
        with self.monitor.db() as db:
            group = db.execute("INSERT INTO node_groups(name,enabled,created) VALUES ('fixture',1,?)", (time.time(),)).lastrowid
            db.execute("""INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id)
                VALUES ('first','https://example.com/v1','test-only','fixture','low','chat',1,?)""", (group,))
            db.execute("""INSERT INTO nodes(name,base_url,api_key,model,effort,protocol,enabled,group_id)
                VALUES ('second','https://example.com/v1','test-only','fixture','low','chat',1,?)""", (group,))
        self.monitor.save({"schedule_mode":"groups"})
        started = self.monitor.start_testing()
        self.assertTrue(started["enabled"])
        self.assertLessEqual(started["next_run"], time.time())
        scheduler = threading.Thread(target=self.monitor.scheduler)
        scheduler.start()
        try:
            self.assertTrue(entered.wait(4))
            self.monitor.stop_testing()
            self.assertFalse(self.monitor.settings()["enabled"])
            self.assertIsNone(self.monitor.settings()["next_run"])
            self.assertEqual(self.monitor.scheduled_queue, [])
            self.assertTrue(self.monitor.state()["stopping"])
            with self.assertRaises(ValueError):
                self.monitor.start_testing()
            release.set()
            deadline = time.monotonic() + 3
            while self.monitor.run_cancellations and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(self.monitor.state()["stopping"])
            self.assertIsNone(self.monitor.settings()["next_run"])
            self.assertEqual(self.monitor.runs()[0]["status"], "cancelled")
            self.assertFalse(self.monitor.runs()[0]["has_svg"])
            self.assertEqual(calls, [PROMPT])
            self.assertTrue(self.monitor.start_testing()["enabled"])
            self.monitor.stop_testing()
        finally:
            release.set()
            self.monitor.stopped.set()
            scheduler.join(3)

    def test_http_cookie_and_preview_preserve_the_original(self):
        self.monitor.setup_password("test-password")
        rid = self.seed(protocol='codex')[0]
        account = 'acct-aaaaaaaaaaaa'
        with self.monitor.db() as db:
            db.execute("UPDATE runs SET account_fingerprint=?,account_label='Account A' WHERE id=?", (account, rid))
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        http.monitor = self.monitor
        worker = threading.Thread(target=http.serve_forever)
        worker.start()
        base = f"http://127.0.0.1:{http.server_port}"
        jar = CookieJar()
        client = request.build_opener(request.ProxyHandler({}), request.HTTPCookieProcessor(jar))
        def post(path, data):
            return client.open(request.Request(base + path, json.dumps(data).encode(), {'Content-Type':'application/json'}))
        try:
            with client.open(base + '/api/runs?page=1') as response:
                public = json.load(response)
                self.assertNotIn('accounts', public)
                self.assertNotIn('account_fingerprint', public['items'][0])
            with self.assertRaises(error.HTTPError) as denied:
                post(f'/api/admin/runs/{rid}/favorite', {'favorite':True})
            self.assertEqual(denied.exception.code, 401)
            with self.assertRaises(error.HTTPError) as denied:
                client.open(base + f'/api/runs?page=1&account={account}')
            self.assertEqual(denied.exception.code, 401)
            with self.assertRaises(error.HTTPError) as denied:
                post('/api/admin/testing/stop', {})
            self.assertEqual(denied.exception.code, 401)
            with post('/api/auth/login', {'password':'test-password'}) as response:
                cookie = response.headers['Set-Cookie']
                self.assertIn('HttpOnly', cookie)
                self.assertIn(f'Max-Age={SESSION_TTL}', cookie)
            http.monitor = Monitor(self.folder.name)
            with client.open(base + '/api/auth/status') as response:
                self.assertTrue(json.load(response)['authenticated'])
            with client.open(base + '/api/admin/runs?page=1&selection=1') as response:
                self.assertEqual(json.load(response)['ids'], [rid])
            with client.open(base + f'/api/runs?page=1&account={account}') as response:
                private = json.load(response)
                self.assertEqual(private['accounts'][0]['value'], account)
                self.assertEqual(private['items'][0]['account_label'], 'Account A')
            with client.open(base + f'/api/runs?page=1&account={account}&selection=1') as response:
                self.assertEqual(json.load(response)['ids'], [rid])
            with self.assertRaises(error.HTTPError) as denied:
                post('/api/admin/runs/delete', {'ids':[rid], 'account':'acct-bbbbbbbbbbbb'})
            self.assertEqual(denied.exception.code, 400)
            with client.open(base + f'/api/runs/{rid}/html') as response:
                self.assertEqual(response.read().decode(), HTML)
            with client.open(base + f'/api/runs/{rid}/html?preview=1') as response:
                self.assertIn('pelican:preview-size', response.read().decode())
                self.assertIn('sandbox allow-scripts', response.headers['Content-Security-Policy'])
            with client.open(base + f'/api/runs/{rid}/svg') as response:
                self.assertTrue(response.headers['Content-Disposition'].startswith('inline;'))
            with client.open(base + f'/api/runs/{rid}/svg?download=1') as response:
                self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
                self.assertIn('.svg"', response.headers['Content-Disposition'])
                self.assertEqual(response.headers.get_content_type(), 'image/svg+xml')
                self.assertEqual(response.read().decode(), SVG)
            styled = HTML.replace('<body>', '<head><style>rect {fill: tomato} @keyframes pedal {to {transform: rotate(360deg)}}</style></head><body>')
            with self.monitor.db() as db:
                db.execute('UPDATE runs SET output=? WHERE id=?', (styled, rid))
            with client.open(base + f'/api/runs/{rid}/svg?download=1') as response:
                self.assertIn('rect {fill: tomato}', response.read().decode())
            with client.open(base + f'/api/runs/{rid}/html') as response:
                self.assertEqual(response.read().decode(), styled)
            animated = styled.replace('</body>', '<script>document.querySelector("rect").setAttribute("x", "10");</script></body>')
            with self.monitor.db() as db:
                db.execute('UPDATE runs SET output=? WHERE id=?', (animated, rid))
            with client.open(base + f'/api/runs/{rid}/svg') as response:
                self.assertNotIn('<script', response.read().decode())
                self.assertNotIn('allow-scripts', response.headers['Content-Security-Policy'])
            with client.open(base + f'/api/runs/{rid}/svg?download=1') as response:
                exported = ET.fromstring(response.read())
                self.assertEqual(len(exported.findall('{http://www.w3.org/2000/svg}script')), 1)
                self.assertTrue(response.headers['Content-Disposition'].startswith('attachment;'))
                self.assertIn('sandbox allow-scripts', response.headers['Content-Security-Policy'])
                self.assertIn("default-src 'none'", response.headers['Content-Security-Policy'])
            with client.open(base + f'/api/runs/{rid}/html') as response:
                self.assertEqual(response.read().decode(), animated)
            with post(f'/api/admin/runs/{rid}/favorite', {'favorite':True}) as response:
                self.assertTrue(json.load(response)['favorite'])
            with client.open(base + '/api/runs?favorite=yes') as response:
                self.assertEqual(json.load(response)['items'][0]['id'], rid)
            for path, payload in (('/api/admin/runs/delete', {'ids':[rid]}),
                                  ('/api/admin/database/reset', {'confirm':'RESET_GENERATION_DATA'})):
                with self.assertRaises(error.HTTPError) as denied:
                    post(path, payload)
                self.assertEqual(denied.exception.code, 400)
            with post(f'/api/admin/runs/{rid}/favorite', {'favorite':False}):
                pass
            with post('/api/auth/logout', {}):
                pass
            with client.open(base + '/api/auth/status') as response:
                self.assertFalse(json.load(response)['authenticated'])
        finally:
            http.shutdown()
            worker.join(3)
            http.server_close()

    def test_database_reset_endpoint_requires_login_and_explicit_confirmation(self):
        self.monitor.setup_password('test-password')
        self.seed()
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.monitor = self.monitor
        worker = threading.Thread(target=http.serve_forever)
        worker.start()
        base = f'http://127.0.0.1:{http.server_port}'
        client = request.build_opener(request.ProxyHandler({}), request.HTTPCookieProcessor(CookieJar()))
        def post(path, data):
            return client.open(request.Request(base + path, json.dumps(data).encode(),
                                               {'Content-Type':'application/json'}))
        try:
            with self.assertRaises(error.HTTPError) as denied:
                post('/api/admin/database/reset', {'confirm':'RESET_GENERATION_DATA'})
            self.assertEqual(denied.exception.code, 401)
            with post('/api/auth/login', {'password':'test-password'}):
                pass
            with self.assertRaises(error.HTTPError) as denied:
                post('/api/admin/database/reset', {})
            self.assertEqual(denied.exception.code, 400)
            self.assertEqual(self.monitor.gallery()['total'], 1)
            with post('/api/admin/database/reset', {'confirm':'RESET_GENERATION_DATA'}) as response:
                self.assertEqual(json.load(response)['deleted_runs'], 1)
            self.assertEqual(self.monitor.gallery()['total'], 0)
            with client.open(base + '/api/auth/status') as response:
                self.assertTrue(json.load(response)['authenticated'])
        finally:
            http.shutdown()
            worker.join(3)
            http.server_close()


if __name__ == '__main__':
    unittest.main()
