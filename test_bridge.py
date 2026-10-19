"""Offline tests: atomic scheduling, idempotency, path validation and execution failure semantics."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, call
import bridge


def spec(number=0, **overrides):
    return {'project': '/test/project', 'task': 'bounded task', 'owner': 'test', 'model': 'opencode-go/test',
            'isolation': 'worktree', 'timeout_seconds': 60, 'request_id': 'request-' + str(number), **overrides}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = bridge.Store(Path(self.temp.name) / 'jobs.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_parallel_retries_create_one_task(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.store.submit(spec()), range(30)))
        self.assertEqual(len({value[0] for value in results}), 1)
        self.assertEqual(sum(not value[1] for value in results), 1)
        with self.assertRaises(ValueError):
            self.store.submit(spec(task='different'))

    def test_parallel_claims_enforce_global_six(self):
        for number in range(20):
            self.store.submit(spec(number))
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            claims = list(pool.map(lambda _: self.store.claim(6), range(20)))
        active = [value for value in claims if value]
        self.assertEqual(len(active), 6)
        self.assertEqual(len(set(active)), 6)
        self.store.update(active[0], status='succeeded')
        self.assertIsNotNone(self.store.claim(6))

    def test_exclusive_project_lock_and_independent_project(self):
        first, _ = self.store.submit(spec(1, isolation='exclusive'))
        self.store.submit(spec(2))
        third, _ = self.store.submit(spec(3, project='/other'))
        self.assertEqual(self.store.claim(6), first)
        self.assertEqual(self.store.claim(6), third)
        self.assertIsNone(self.store.claim(6))

    def test_state_survives_new_store(self):
        first, _ = self.store.submit(spec())
        self.store.claim(6)
        restored = bridge.Store(self.store.path)
        self.assertEqual(restored.get(first)['status'], 'starting')
        self.assertEqual(restored.submit(spec()), (first, True))

    def test_cancelled_queued_task_is_not_claimed(self):
        first, _ = self.store.submit(spec())
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        broker.notify = lambda: None
        self.assertEqual(broker.cancel(first)['status'], 'cancelled')
        self.assertIsNone(self.store.claim(6))

    def test_idle_without_result_is_failure(self):
        first, _ = self.store.submit(spec())
        self.store.claim(6)
        self.store.update(first, session_id='ses_test', workspace='/test/project', status='running', started=bridge.time.time()-20)
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        broker.notify = lambda: None
        broker.prepare_workspace = lambda row, item: row['workspace']
        with patch.object(bridge, 'oc', side_effect=[[], {'ses_test': {'type': 'idle'}}, None]):
            broker.run(first, recovering=True)
        self.assertEqual(self.store.get(first)['status'], 'failed')

    def test_ambiguous_admission_is_not_replayed(self):
        first, _ = self.store.submit(spec())
        self.store.claim(6)
        self.store.update(first, session_id='ses_test', workspace='/test/project')
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        broker.notify = lambda: None
        broker.prepare_workspace = lambda row, item: row['workspace']
        with patch.object(bridge, 'oc') as api:
            broker.run(first, recovering=True)
        self.assertEqual(api.call_args_list[0], call('/session/ses_test/abort', 'POST', {}, '/test/project'))
        self.assertFalse(any('/prompt_async' in str(call) for call in api.call_args_list))
        self.assertEqual(self.store.get(first)['status'], 'failed')

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(ValueError):
            bridge.project_path('/outside-allowed-project-roots/.ssh')

    def test_independent_check_reports_real_exit_code(self):
        first, _ = self.store.submit(spec())
        self.store.update(first, status='succeeded', workspace=self.temp.name)
        broker = object.__new__(bridge.Broker)
        broker.store = self.store
        result = broker.call('run_check', {'job_id': first, 'argv': [bridge.sys.executable, '-c', 'print("checked"); raise SystemExit(7)']})
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['stdout'].strip(), 'checked')

    def test_database_connections_close_without_gc(self):
        import gc
        first, _ = self.store.submit(spec())
        gc.collect(); was_enabled = gc.isenabled(); gc.disable()
        try:
            before = len(list(Path('/proc/self/fd').iterdir()))
            for _ in range(150):
                self.store.get(first)
            after = len(list(Path('/proc/self/fd').iterdir()))
            self.assertLessEqual(after-before, 2)
        finally:
            if was_enabled:
                gc.enable()

    def test_exclusive_waiter_cannot_be_starved(self):
        first, _ = self.store.submit(spec(1))
        self.assertEqual(self.store.claim(6), first)
        exclusive, _ = self.store.submit(spec(2, isolation='exclusive'))
        self.store.submit(spec(3))
        other, _ = self.store.submit(spec(4, project='/other'))
        self.assertEqual(self.store.claim(6), other)
        self.assertIsNone(self.store.claim(6))
        self.store.update(first, status='succeeded')
        self.assertEqual(self.store.claim(6), exclusive)

    def test_submission_pins_worktree_base(self):
        first, _ = self.store.submit(spec(), base_commit='original-head')
        row = self.store.get(first)
        broker = object.__new__(bridge.Broker); broker.store = self.store
        with patch.object(bridge, 'STATE', Path(self.temp.name)), patch.object(bridge, 'git', return_value='changed-head') as command:
            broker.prepare_workspace(row, spec())
        self.assertEqual(command.call_args.args[-1], 'original-head')
        self.assertEqual(self.store.get(first)['base_commit'], 'original-head')

    def test_retry_finds_existing_job_before_mutable_git_preflight(self):
        broker = object.__new__(bridge.Broker); broker.store = self.store; broker.notify = lambda: None
        broker.catalog = lambda: ['opencode-go/test']
        args = {'project': '/test/project', 'task': 'bounded task', 'model': 'opencode-go/test',
                'request_id': 'retry-after-apply', 'timeout_seconds': 60}
        with patch.object(bridge, 'project_path', return_value='/test/project'), patch.object(bridge, 'git', side_effect=['/test/project', 'head', '']):
            original = broker.submit(args)
        with patch.object(bridge, 'project_path', return_value='/test/project'), patch.object(bridge, 'git', side_effect=RuntimeError('Source changed')):
            retried = broker.submit(args)
        self.assertEqual(original['id'], retried['id'])
        self.assertTrue(retried['deduplicated'])

    def test_usage_counts_all_unique_assistant_turns(self):
        def message(identifier, value):
            return {'info': {'id': identifier, 'role': 'assistant', 'tokens': {'input': value, 'output': 2, 'cache': {'read': 4}},
                             'cost': value/100}, 'parts': [{'type': 'text', 'text': identifier}]}
        broker = object.__new__(bridge.Broker)
        with patch.object(bridge, 'oc', return_value=[message('a', 10), message('b', 20), message('b', 20)]), patch.object(bridge, 'git', return_value=''):
            result = broker.snapshot_result({'session_id': 'ses_test', 'workspace': '/test'})
        self.assertEqual(result['session_usage']['assistant_turns'], 2)
        self.assertEqual(result['session_usage']['tokens']['input'], 30)
        self.assertAlmostEqual(result['session_usage']['reported_cost'], .3)

    def test_result_enrichment_preserves_completion_timestamp(self):
        first, _ = self.store.submit(spec())
        self.store.update(first, status='succeeded', workspace='/test', session_id='ses_test', result=json.dumps({'text': 'original'}))
        before = self.store.get(first)['updated']
        broker = object.__new__(bridge.Broker); broker.store = self.store
        broker.snapshot_result = lambda row, partial=False: {'text': 'new', 'session_usage': {'assistant_turns': 4}, 'partial': partial}
        result = broker.call('job_result', {'job_id': first})
        self.assertEqual(result['result']['text'], 'original')
        self.assertEqual(result['result']['session_usage']['assistant_turns'], 4)
        self.assertEqual(self.store.get(first)['updated'], before)

    def test_timed_out_job_retains_partial_result(self):
        first, _ = self.store.submit(spec(timeout_seconds=30))
        self.store.claim(6)
        self.store.update(first, session_id='ses_test', workspace='/test', status='running', started=bridge.time.time()-60)
        broker = object.__new__(bridge.Broker); broker.store = self.store; broker.notify = lambda: None
        broker.prepare_workspace = lambda row, item: row['workspace']
        with patch.object(bridge, 'oc', side_effect=[None, []]), patch.object(bridge, 'git', return_value=''):
            broker.run(first)
        row = self.store.get(first)
        self.assertEqual(row['status'], 'timed_out')
        self.assertIn('deadline', row['error'])
        self.assertTrue(json.loads(row['result'])['partial'])

    def test_progress_warning_does_not_change_or_cancel_task(self):
        first, _ = self.store.submit(spec())
        self.store.claim(6)
        self.store.update(first, progress=json.dumps({'last_progress_at': bridge.time.time()-200,
                                                     'running_tools': [{'tool': 'bash', 'started_at': bridge.time.time()-200}],
                                                     'fingerprint': 'not-for-client'}))
        broker = object.__new__(bridge.Broker); broker.store = self.store
        result = broker.brief(self.store.get(first))
        self.assertIn('warning', result['progress'])
        self.assertNotIn('fingerprint', result['progress'])
        self.assertEqual(self.store.get(first)['status'], 'starting')


if __name__ == '__main__':
    unittest.main(verbosity=2)
