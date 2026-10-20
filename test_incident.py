"""Regressions for permission loss, completed native errors and retained clocks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge
import control_plane as cp
from test_bridge import spec
from workflow import assert_native_permissions, worker_permissions


class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge.Store(Path(self.temp.name) / 'jobs.sqlite3')
        self.job, _ = self.store.submit(spec(isolation='readonly'))
        self.store.claim(2)
        self.store.update(self.job, session_id='s', workspace='/synthetic/project', status='running')
        self.messages = [{'info': {'role': 'assistant', 'finish': None,
                         'time': {'completed': 123}, 'error': {'name': 'APIError',
                         'data': {'message': 'Cannot connect to API', 'isRetryable': True}}},
                         'parts': []}]

    def tearDown(self):
        self.temp.cleanup()

    def native(self, status=None, acknowledged=True, abort_error=False):
        def call(path, method='GET', body=None, directory=None, timeout=15):
            if path.endswith('/abort'):
                if abort_error:
                    raise ConnectionError('unavailable')
                return acknowledged
            if path == '/session/status':
                return {} if status is None else {'s': {'type': status}}
            if '/message' in path:
                return self.messages
            raise AssertionError(path)
        return call

    def test_completed_error_abort_ack_releases_slot_as_failed_without_replay(self):
        result = cp.recover_job(self.store, self.native(), None, self.job, 'abort')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['aborted'])
        self.assertFalse(result['final_complete'])
        self.assertFalse(result['retryable'])
        self.assertTrue(result['failure']['upstream_retryable'])
        self.assertEqual(result['failure']['category'], 'connection_error')
        self.store.submit(spec(2))
        self.assertIsNotNone(self.store.claim(1))

    def test_ambiguous_native_evidence_keeps_slot(self):
        for mode in ('unacknowledged', 'busy', 'retry', 'incomplete', 'pending_tool', 'abort_error'):
            with self.subTest(mode=mode):
                self.store.update(self.job, status='running', cancel_requested=0)
                self.messages[0]['info']['time']['completed'] = None if mode == 'incomplete' else 123
                self.messages[0]['parts'] = ([{'type': 'tool', 'state': {'status': 'running'}}]
                                             if mode == 'pending_tool' else [])
                result = cp.recover_job(self.store, self.native(
                    status=mode if mode in ('busy', 'retry') else None,
                    acknowledged=mode != 'unacknowledged', abort_error=mode == 'abort_error'),
                    None, self.job, 'abort')
                self.assertEqual(result['status'], 'attention_required')
                self.assertFalse(result['aborted'])

    def test_absence_without_terminal_error_is_insufficient(self):
        self.messages = []
        result = cp.recover_job(self.store, self.native(), None, self.job, 'abort')
        self.assertEqual(result['status'], 'attention_required')

    def test_cancellation_prevents_reattachment(self):
        self.store.update(self.job, status='attention_required', cancel_requested=1)
        result = cp.recover_job(self.store, self.native(), None, self.job, 'reattach')
        self.assertFalse(result['eligible'])

    def test_diagnostics_exclude_provider_payload_and_url_credentials(self):
        credential_url = 'https://' + 'example:private' + '@' + 'api.example.test/v1?token=private'
        value = cp.native_failure({'name': 'APIError', 'data': {
            'message': 'Cannot connect ' + credential_url, 'isRetryable': True,
            'responseBody': 'private', 'headers': {'authorization': 'private'},
            'metadata': {'url': credential_url, 'code': 'ECONNRESET'}}})
        self.assertNotIn('private', json.dumps(value))
        self.assertEqual(value['endpoint_host'], 'api.example.test')
        self.assertEqual(value['transport_code'], 'ECONNRESET')

    def test_permissions_fail_closed_on_legacy_replacement(self):
        rules = worker_permissions('readonly')
        assert_native_permissions({'permission': rules}, rules)
        with self.assertRaises(PermissionError):
            assert_native_permissions({'permission': rules[1:3]}, rules)

    def admission(self, *, lose_preflight=False, ambiguous_post=False):
        self.store.update(self.job, status='starting', session_id=None)
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        broker.notify = lambda: None
        broker.prepare_workspace = lambda row, item: '/synthetic/project'
        broker.retain_partial = lambda job: None
        calls = []
        rules = worker_permissions('readonly')
        def native(path, method='GET', body=None, directory=None, timeout=15):
            calls.append((path, body))
            if path == '/session':
                self.assertEqual(body['permission'], rules)
                return {'id': 's'}
            if path == '/session/s':
                return {'permission': rules[:2] if lose_preflight else rules}
            if path.endswith('/prompt_async'):
                self.assertNotIn('tools', body)
                if ambiguous_post:
                    raise ConnectionError('lost POST response')
                return None
            if ambiguous_post and '/message' in path:
                return []
            return self.native()(path, method, body, directory, timeout)
        with patch.object(bridge, 'oc', side_effect=native):
            broker.run(self.job)
        return calls

    def test_run_preflight_permission_loss_never_sends_prompt(self):
        calls = self.admission(lose_preflight=True)
        self.assertEqual(self.store.get(self.job)['status'], 'failed')
        self.assertFalse(any('/prompt_async' in path for path, _ in calls))

    def test_run_completed_api_error_is_failed_with_retained_diagnostics(self):
        calls = self.admission()
        row = self.store.get(self.job)
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(json.loads(row['failure'])['category'], 'connection_error')
        self.assertEqual(sum('/prompt_async' in path for path, _ in calls), 1)

    def test_run_ambiguous_post_is_retained_and_never_replayed(self):
        calls = self.admission(ambiguous_post=True)
        self.assertEqual(self.store.get(self.job)['status'], 'attention_required')
        self.assertEqual(sum('/prompt_async' in path for path, _ in calls), 1)

    def test_state_clock_and_event_changes_are_atomic(self):
        with self.store.connect() as db:
            db.execute('UPDATE jobs SET created=90,started=100,updated=100,execution_ended=NULL WHERE id=?', (self.job,))
            cp.update_job(db, self.job, {'status': 'attention_required'}, now=130)
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        with patch('bridge.time.time', return_value=1000):
            timing = broker.brief(self.store.get(self.job))['timing']
        self.assertEqual(timing['execution_seconds'], 30)
        self.assertEqual(timing['attention_seconds'], 870)
        with self.store.connect() as db:
            cp.update_job(db, self.job, {'status': 'running'}, now=160)
            cp.update_job(db, self.job, {'status': 'attention_required'}, now=180)
            cp.update_job(db, self.job, {'status': 'failed'}, now=200)
            cp.update_job(db, self.job, {'error': 'later metadata'}, now=900)
        with patch('bridge.time.time', return_value=1000):
            timing = broker.brief(self.store.get(self.job))['timing']
        self.assertEqual(timing['execution_seconds'], 50)
        self.assertEqual(timing['attention_seconds'], 50)
        self.assertEqual(timing['wall_seconds'], 110)
        with self.assertRaises(RuntimeError):
            with self.store.connect() as db:
                cp.update_job(db, self.job, {'status': 'running'}, now=950)
                raise RuntimeError('rollback')
        self.assertEqual(self.store.get(self.job)['status'], 'failed')
        with self.store.connect() as db:
            self.assertFalse(db.execute('SELECT 1 FROM job_events WHERE job_id=? AND created=950', (self.job,)).fetchone())

    def test_legacy_attention_migration_freezes_observed_execution(self):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='attention_required',updated=130,execution_ended=NULL,attention_since=NULL WHERE id=?", (self.job,))
            cp.migrate_job_state(db)
        row = self.store.get(self.job)
        self.assertEqual(row['execution_ended'], 130)
        self.assertEqual(row['attention_since'], 130)


if __name__ == '__main__':
    unittest.main()
