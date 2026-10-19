#!/usr/bin/env python3
"""Offline fault tests for control_plane.py. Python standard library only.

These tests never import ``bridge`` (control_plane must stay import-cycle free) and never touch
the network: a local sqlite store mirrors the broker schema and a fake ``native`` records calls.
"""
import contextlib
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
import unittest

import control_plane
from control_plane import ModelCatalog, QuestionChannel, cancel_job, quota_block_reason, recover_job


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

SCHEMA = '''CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, signature TEXT NOT NULL,
    status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    spec TEXT NOT NULL, session_id TEXT, workspace TEXT, base_commit TEXT,
    result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
    started REAL, progress TEXT)'''


class FakeStore:
    """Bridge-compatible store: same schema, same connect/update signatures."""

    def __init__(self, path):
        self.path = str(path)
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute(SCHEMA)

    @contextlib.contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, job_id):
        with self.connect() as connection:
            row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job ID.')
        return dict(row)

    def update(self, job_id, *, touch=True, **values):
        allowed = {'status', 'session_id', 'workspace', 'base_commit', 'result', 'error',
                   'cancel_requested', 'started', 'progress'}
        if not values.keys() <= allowed:
            raise ValueError('Invalid update field.')
        if touch:
            values['updated'] = time.time()
        with self.connect() as connection:
            connection.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                               [*values.values(), job_id])

    def add(self, job_id, status='running', session_id=None, workspace='/test/ws', model='opencode-go/test'):
        spec = {'project': '/test/project', 'task': 't', 'owner': 'test', 'model': model,
                'isolation': 'worktree', 'timeout_seconds': 60, 'request_id': 'r-' + job_id}
        now = time.time()
        with self.connect() as connection:
            connection.execute('INSERT INTO jobs(id,request_id,signature,status,created,updated,spec,'
                               'session_id,workspace) VALUES(?,?,?,?,?,?,?,?,?)',
                               (job_id, spec['request_id'], 'sig', status, now, now,
                                json.dumps(spec), session_id, workspace))
        return job_id


class FakeNative:
    """Routes native calls to registered handlers and records every invocation."""

    def __init__(self):
        self.calls = []
        self.handlers = []
        self.routes = {}  # convenience: exact path -> handler

    def on(self, predicate, handler):
        self.handlers.append((predicate, handler))
        return self

    def route(self, path, handler):
        self.routes[path] = handler
        return self

    def __call__(self, path, method='GET', body=None, directory=None, timeout=15):
        self.calls.append({'path': path, 'method': method, 'body': body, 'directory': directory})
        handler = self.routes.get(path)
        if handler is not None:
            return handler(path, body, directory)
        for predicate, routed in self.handlers:
            if predicate(path, method):
                return routed(path, body, directory)
        raise RuntimeError(f'Unexpected native call: {method} {path}')

    def count(self, needle):
        return sum(1 for call in self.calls if needle in call['path'])


class FakeRunner:
    def __init__(self, stdout='', returncode=0, error=None):
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.commands = []

    def __call__(self, command, capture_output=True, text=True, timeout=30):
        self.commands.append(command)
        if self.error:
            raise self.error
        return type('Result', (), {'returncode': self.returncode, 'stdout': self.stdout})()


def make_store():
    temp = tempfile.TemporaryDirectory()
    return temp, FakeStore(Path(temp.name) / 'jobs.sqlite3')


# ---------------------------------------------------------------------------
# ModelCatalog
# ---------------------------------------------------------------------------

PROVIDER_PAYLOAD = {'all': [
    {'id': 'opencode-go', 'models': {'m1': {'name': 'M1'}, 'm2': {}}},
    {'id': 'other', 'models': {'x': {}}},
]}


class ModelCatalogTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.process = patch('control_plane.subprocess.run', return_value=type('Result', (), {'returncode': 1, 'stdout': ''})())
        self.process.start()
        self.addCleanup(self.process.stop)

    def test_ttl_caches_and_refreshes(self):
        now = [1000.0]
        native = FakeNative().route('/provider', lambda *a: PROVIDER_PAYLOAD)
        catalog = ModelCatalog(native, {'default_model': 'opencode-go/m1', 'model_cache_seconds': 60},
                               clock=lambda: now[0])
        first = catalog.list()
        self.assertEqual(first['models'], ['opencode-go/m1', 'opencode-go/m2'])
        self.assertFalse(first['stale'])
        self.assertEqual(native.count('/provider'), 1)
        catalog.list()
        self.assertEqual(native.count('/provider'), 1)  # cached within TTL
        now[0] += 61
        catalog.list()
        self.assertEqual(native.count('/provider'), 2)  # TTL expired
        catalog.list(refresh=True)
        self.assertEqual(native.count('/provider'), 3)  # explicit refresh

    def test_allowlist_and_default_provider(self):
        native = FakeNative().route('/provider', lambda *a: PROVIDER_PAYLOAD)
        catalog = ModelCatalog(native, {'allowed_providers': ['opencode-go'],
                                        'allowed_models': ['opencode-go/m1']})
        result = catalog.list()
        self.assertEqual(result['models'], ['opencode-go/m1'])
        self.assertTrue(catalog.validate('opencode-go/m1'))
        self.assertFalse(catalog.validate('opencode-go/m2'))
        self.assertFalse(catalog.validate('other/x'))  # provider filtered out

    def test_cli_fallback(self):
        native = FakeNative()  # no /provider route -> raises
        runner = FakeRunner(stdout='opencode-go/m1\nopencode-go/m2\nother/x\n')
        catalog = ModelCatalog(native, {'opencode_bin': '/bin/opencode'}, runner=runner)
        result = catalog.list()
        self.assertEqual(result['models'], ['opencode-go/m1', 'opencode-go/m2'])
        self.assertEqual(runner.commands, [['/bin/opencode', 'models']])

    def test_stale_catalog_survives_failure_without_authorizing(self):
        now = [0.0]
        state = {'fail': False}
        native = FakeNative()

        def provider(path, body, directory):
            if state['fail']:
                raise RuntimeError('provider offline')
            return PROVIDER_PAYLOAD

        native.route('/provider', provider)
        catalog = ModelCatalog(native, {'default_model': 'opencode-go/m1', 'model_cache_seconds': 5},
                               clock=lambda: now[0])
        catalog.list()
        state['fail'] = True
        now[0] += 10
        stale = catalog.list()
        self.assertTrue(stale['stale'])
        self.assertIn('warning', stale)
        self.assertEqual(stale['models'], ['opencode-go/m1', 'opencode-go/m2'])
        # A new model must NOT be silently authorized while refresh keeps failing.
        self.assertFalse(catalog.validate('opencode-go/brand-new'))
        self.assertNotIn('opencode-go/brand-new', stale['models'])

    def test_concurrent_reads_fetch_once(self):
        native = FakeNative().route('/provider', lambda *a: PROVIDER_PAYLOAD)
        catalog = ModelCatalog(native, {})
        results = []
        threads = [threading.Thread(target=lambda: results.append(catalog.list())) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 8)
        self.assertEqual(native.count('/provider'), 1)


# ---------------------------------------------------------------------------
# cancel_job
# ---------------------------------------------------------------------------

def status_route(value):
    return lambda *a: value


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.store = make_store()
        self.native = FakeNative()
        self.notified = []

    def tearDown(self):
        self.temp.cleanup()

    def notify(self):
        self.notified.append(True)

    def test_queued_cancel_is_terminal_without_native(self):
        self.store.add('j1', status='queued', session_id=None)
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.store.get('j1')['status'], 'cancelled')
        self.assertEqual(self.native.calls, [])
        self.assertTrue(self.notified)

    def test_abort_failure_returns_attention_required(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({}))
        self.native.on(lambda p, m: m == 'POST' and p.endswith('/abort'),
                       lambda *a: (_ for _ in ()).throw(RuntimeError('abort channel down')))
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        self.assertEqual(result['status'], 'attention_required')
        row = self.store.get('j1')
        self.assertEqual(row['status'], 'attention_required')
        self.assertIn('abort not confirmed', row['error'])
        self.assertFalse(result['abort_confirmed'])

    def test_confirmed_idle_becomes_cancelled(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({'ses_1': {'type': 'idle'}}))
        self.native.on(lambda p, m: p.endswith('/abort'), lambda *a: {})
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(result['outcome'], 'cancelled')
        self.assertTrue(result['abort_confirmed'])

    def test_absent_from_status_map_is_not_proof_of_stopped(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({}))
        self.native.on(lambda p, m: p.endswith('/abort'), lambda *a: {})
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        self.assertEqual(result['status'], 'attention_required')  # absence cannot finalize
        self.assertFalse(result['native_idle'])
        self.assertEqual(self.store.get('j1')['cancel_requested'], 1)

    def test_concurrent_terminal_completion_is_not_overwritten(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({}))

        def abort_and_complete(path, body, directory):
            self.store.update('j1', status='succeeded', result=json.dumps({'text': 'done'}))
            return {}

        self.native.on(lambda p, m: p.endswith('/abort'), abort_and_complete)
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['outcome'], 'terminal_preserved')
        self.assertEqual(self.store.get('j1')['status'], 'succeeded')

    def test_no_session_admission_cannot_be_marked_cancelled(self):
        self.store.add('j1', status='admitting', session_id=None)
        result = cancel_job(self.store, self.native, self.notify, 'j1')
        row = self.store.get('j1')
        self.assertEqual(row['status'], 'cancelling')  # worker may still admit
        self.assertEqual(row['cancel_requested'], 1)
        self.assertEqual(result['status'], 'cancelling')
        self.assertEqual(self.native.calls, [])


# ---------------------------------------------------------------------------
# recover_job
# ---------------------------------------------------------------------------

class RecoverTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.store = make_store()
        self.native = FakeNative()

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_unknown_admission_never_replays(self):
        self.store.add('j1', status='starting', session_id=None)
        result = recover_job(self.store, self.native, None, 'j1', 'inspect')
        self.assertEqual(result['admission'], 'unknown')
        self.assertEqual(result['next_action'], 'inspect_unknown_admission')
        self.assertFalse(result['retryable'])
        self.assertEqual(self.native.calls, [])  # read-only, no prompt

    def test_inspect_detects_final_complete_message(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({'ses_1': {'type': 'running'}}))
        self.native.on(lambda p, m: p.endswith('/message?limit=2'),
                       lambda *a: [{'info': {'role': 'assistant', 'finish': 'stop',
                                             'time': {'completed': 123}}}])
        result = recover_job(self.store, self.native, None, 'j1', 'inspect')
        self.assertTrue(result['final_complete'])
        self.assertEqual(result['next_action'], 'reconcile_terminal')

    def test_inspect_distinguishes_unknown_from_complete(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({'ses_1': {'type': 'running'}}))
        self.native.on(lambda p, m: p.endswith('/message?limit=2'), lambda *a: [])
        result = recover_job(self.store, self.native, None, 'j1', 'inspect')
        self.assertFalse(result['final_complete'])
        self.assertEqual(result['next_action'], 'wait')

    def test_reattach_returns_eligibility_without_prompt(self):
        self.store.add('j1', status='running', session_id='ses_1')
        result = recover_job(self.store, self.native, None, 'j1', 'reattach')
        self.assertTrue(result['eligible'])
        self.assertTrue(result['scheduled_by_parent'])
        self.assertEqual(self.native.calls, [])  # MUST NOT send a prompt
        self.assertEqual(self.native.count('/prompt'), 0)

    def test_reattach_unknown_admission_ineligible(self):
        self.store.add('j1', status='admitting', session_id=None)
        result = recover_job(self.store, self.native, None, 'j1', 'reattach')
        self.assertFalse(result['eligible'])
        self.assertIn('never replay', result['reason'])

    def test_abort_is_idempotent(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.route('/session/status', status_route({'ses_1': {'type': 'idle'}}))
        self.native.on(lambda p, m: p.endswith('/abort'), lambda *a: {})
        first = recover_job(self.store, self.native, None, 'j1', 'abort')
        self.assertTrue(first['aborted'])
        self.assertEqual(self.store.get('j1')['status'], 'cancelled')
        second = recover_job(self.store, self.native, None, 'j1', 'abort')
        self.assertFalse(second['aborted'])
        self.assertEqual(second['reason'], 'already terminal')
        self.assertEqual(self.native.count('/abort'), 1)  # no second native abort


# ---------------------------------------------------------------------------
# QuestionChannel
# ---------------------------------------------------------------------------

def discovery_route(questions):
    return lambda *a: questions


class QuestionChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp, self.store = make_store()
        self.native = FakeNative()

    def tearDown(self):
        self.temp.cleanup()

    def channel(self):
        return QuestionChannel(self.store, self.native, notify=None)

    def test_discovery_is_scoped_and_bounded(self):
        self.store.add('j1', status='running', session_id='ses_1')
        long_text = 'x' * 5000
        questions = [
            {'id': 'q1', 'sessionID': 'ses_1', 'text': long_text,
             'options': [{'id': 'a1', 'label': 'yes'}, {'id': 'a2', 'label': 'no'}]},
            {'id': 'q2', 'sessionID': 'ses_other', 'text': 'other session'},
        ]
        self.native.on(lambda p, m: p.startswith('/question?'), discovery_route(questions))
        result = self.channel().discover(job_id='j1')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['questions'][0]['id'], 'q1')
        self.assertLessEqual(len(result['questions'][0]['text']), control_plane._MAX_TEXT)
        self.assertEqual([o['id'] for o in result['questions'][0]['options']], ['a1', 'a2'])

    def test_ambiguous_answer_requires_reconciliation_and_never_resends(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'),
                       discovery_route([{'id': 'q1', 'sessionID': 'ses_1',
                                         'options': [{'id': 'a1'}]}]))
        self.native.on(lambda p, m: p.endswith('/q1/reply'),
                       lambda *a: (_ for _ in ()).throw(RuntimeError('timeout')))
        channel = self.channel()
        first = channel.answer('j1', 'q1', 'a1', [['yes']])
        self.assertTrue(first['uncertain'])
        self.assertTrue(first['requires_reconciliation'])
        second = channel.answer('j1', 'q1', 'a1', [['yes']])
        self.assertTrue(second['requires_reconciliation'])
        self.assertFalse(second['sent'])
        self.assertEqual(self.native.count('/reply'), 1)  # never blindly resend

    def test_confirmed_answer_is_idempotent(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'),
                       discovery_route([{'id': 'q1', 'sessionID': 'ses_1',
                                         'options': [{'id': 'a1'}]}]))
        self.native.on(lambda p, m: p.endswith('/q1/reply'), lambda *a: {})
        channel = self.channel()
        self.assertTrue(channel.answer('j1', 'q1', 'a1', [['yes']])['confirmed'])
        again = channel.answer('j1', 'q1', 'a1', [['yes']])
        self.assertTrue(again['already_answered'])
        self.assertEqual(self.native.count('/reply'), 1)

    def test_answer_not_pending_is_refused(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'), discovery_route([]))
        result = self.channel().answer('j1', 'q1', 'a1', [['yes']])
        self.assertFalse(result['sent'])
        self.assertEqual(result['reason'], 'not_pending')
        self.assertEqual(self.native.count('/reply'), 0)

    def test_discovery_uncertainty_surfaces_capability_gap(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'),
                       lambda *a: (_ for _ in ()).throw(RuntimeError('404 not implemented')))
        result = self.channel().answer('j1', 'q1', 'a1', [['yes']])
        self.assertEqual(result['reason'], 'discovery_uncertain')
        self.assertTrue(result['requires_reconciliation'])
        self.assertEqual(self.native.count('/reply'), 0)

    def test_reconcile_confirms_resolved_question(self):
        self.store.add('j1', status='running', session_id='ses_1')
        live = {'questions': [{'id': 'q1', 'sessionID': 'ses_1', 'options': [{'id': 'a1'}]}]}
        self.native.on(lambda p, m: p.startswith('/question?'), lambda *a: live['questions'])
        self.native.on(lambda p, m: p.endswith('/q1/reply'),
                       lambda *a: (_ for _ in ()).throw(RuntimeError('timeout')))
        channel = self.channel()
        channel.answer('j1', 'q1', 'a1', [['yes']])
        live['questions'] = []  # human resolved it out-of-band
        result = channel.reconcile('j1', 'q1')
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['resolved_unknown'], ['a1'])
        self.assertEqual(result['confirmed'], [])

    def test_idempotent_answer_survives_pending_question_disappearing(self):
        self.store.add('j1', status='running', session_id='ses_1')
        live = [{'id': 'q1', 'sessionID': 'ses_1'}]
        self.native.on(lambda p, m: p.startswith('/question?'), lambda *a: live)
        self.native.on(lambda p, m: p.endswith('/q1/reply'), lambda *a: True)
        channel = self.channel()
        self.assertTrue(channel.answer('j1', 'q1', 'reply-1', [['yes']])['confirmed'])
        live.clear()
        self.assertTrue(channel.answer('j1', 'q1', 'reply-1', [['yes']])['already_answered'])
        with self.assertRaises(ValueError):
            channel.answer('j1', 'q1', 'reply-1', [['different']])
        self.assertEqual(self.native.count('/reply'), 1)

    def test_new_answer_id_cannot_bypass_uncertain_delivery(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'), discovery_route([{'id': 'q1', 'sessionID': 'ses_1'}]))
        self.native.on(lambda p, m: p.endswith('/q1/reply'), lambda *a: (_ for _ in ()).throw(RuntimeError('timeout')))
        channel = self.channel()
        channel.answer('j1', 'q1', 'reply-1', [['yes']])
        with self.assertRaises(ValueError):
            channel.answer('j1', 'q1', 'reply-2', [['yes']])
        self.assertEqual(self.native.count('/reply'), 1)

    def test_native_nested_question_is_preserved_and_unscoped_question_is_ignored(self):
        self.store.add('j1', status='running', session_id='ses_1')
        self.native.on(lambda p, m: p.startswith('/question?'), discovery_route([
            {'id': 'q1', 'sessionID': 'ses_1', 'questions': [{'question': 'Which file?', 'options': [{'label': 'one'}]}]},
            {'id': 'q2', 'question': 'unscoped'}]))
        result = self.channel().discover('j1')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['questions'][0]['questions'][0]['text'], 'Which file?')


# ---------------------------------------------------------------------------
# quota_block_reason
# ---------------------------------------------------------------------------

class QuotaTests(unittest.TestCase):
    def test_provider_and_model_caps(self):
        config = {'provider_caps': {'opencode-go': 2}, 'model_caps': {'opencode-go/m1': 1}}
        self.assertIsNone(quota_block_reason({'model': 'opencode-go/m1'}, [], config))
        # Provider peers count jointly; one peer is below the cap of two.
        self.assertIsNone(quota_block_reason({'model': 'opencode-go/m1'},
                                             [{'model': 'opencode-go/m2'}], config))
        peers = [{'model': 'opencode-go/m2'}, {'model': 'opencode-go/m3'}]
        self.assertIn('provider quota reached',
                      quota_block_reason({'model': 'opencode-go/m1'}, peers, config))
        # Model cap is enforced independently of the provider cap.
        reason = quota_block_reason({'model': 'opencode-go/m1'},
                                    [{'model': 'opencode-go/m1'}], {'model_caps': {'opencode-go/m1': 1}})
        self.assertIn('model quota reached', reason)

    def test_exact_ids_no_prefix_guessing(self):
        config = {'provider_caps': {'opencode-go': 1}}
        peer = {'model': 'opencode-go-x/m1'}  # different provider prefix
        self.assertIsNone(quota_block_reason({'model': 'opencode-go/m1'}, [peer], config))

    def test_active_including_attention_consume_quota(self):
        # Caller passes all active specs (including attention_required) as active_specs.
        config = {'provider_caps': {'opencode-go': 2}}
        active = [{'model': 'opencode-go/m1', 'status': 'attention_required'},
                  {'model': 'opencode-go/m2', 'status': 'running'}]
        self.assertIn('provider quota reached',
                      quota_block_reason({'model': 'opencode-go/m3'}, active, config))

    def test_unconfigured_caps_allow(self):
        self.assertIsNone(quota_block_reason({'model': 'opencode-go/m1'}, [{'model': 'x/y'}], {}))

    def test_invalid_model_id(self):
        self.assertEqual(quota_block_reason({'model': 'nomodel'}, [], {}), 'invalid or missing model id')


if __name__ == '__main__':
    unittest.main(verbosity=2)
