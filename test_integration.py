"""Cross-module regressions with isolated state; no provider credentials or model calls."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import os
import shutil
import signal
import socket
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import unittest
from unittest.mock import Mock, patch
import bridge
from test_bridge import spec

class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.broker = object.__new__(bridge.Broker)
        self.broker.store = bridge.Store(Path(self.temp.name) / 'jobs.db')
        self.broker.changed = threading.Condition()
        self.broker.notify = lambda: None

    def tearDown(self):
        self.temp.cleanup()

    def test_provider_cap_skips_to_independent_provider(self):
        store = self.broker.store
        first, _ = store.submit(spec(1, model='one/a'))
        store.submit(spec(2, model='one/b'))
        other, _ = store.submit(spec(3, model='two/a'))
        with patch.dict(bridge.CONFIG, {'provider_caps': {'one': 1}, 'model_caps': {}}):
            self.assertEqual(store.claim(6), first)
            self.assertEqual(store.claim(6), other)
            self.assertIsNone(store.claim(6))

    def test_event_cursor_is_scoped(self):
        first, _ = self.broker.store.submit(spec(1))
        other, _ = self.broker.store.submit(spec(2))
        self.broker.store.update(first, status='running')
        cursor = self.broker.store.event_cursor([first])
        self.broker.store.update(other, status='running')
        result = self.broker.call('job_wait', {'job_ids': [first], 'seconds': 0, 'after_event_id': cursor})
        self.assertFalse(result['changed'])
        self.broker.store.update(first, status='attention_required')
        result = self.broker.call('job_wait', {'job_ids': [first], 'seconds': 0, 'after_event_id': cursor})
        self.assertTrue(result['changed'])

    def test_quota_blocked_exclusive_keeps_project_fairness(self):
        store = self.broker.store
        first, _ = store.submit(spec(1, model='one/a'))
        with patch.dict(bridge.CONFIG, {'provider_caps': {'one': 1}}):
            self.assertEqual(store.claim(6), first)
            exclusive, _ = store.submit(spec(2, model='one/b', isolation='exclusive'))
            store.submit(spec(3, model='two/a'))
            independent, _ = store.submit(spec(4, model='two/b', project='/other'))
            self.assertEqual(store.claim(6), independent)
            self.assertIsNone(store.claim(6))
            store.update(first, status='succeeded')
            self.assertEqual(store.claim(6), exclusive)

    def test_sse_does_not_allocate_for_unowned_sessions(self):
        self.broker.session_conditions = {}
        self.broker.session_conditions_lock = threading.Lock()
        def stream():
            event = {'type': 'message.updated', 'properties': {'info': {'sessionID': 'unowned'}}}
            yield b'data:' + json.dumps(event).encode()
            raise KeyboardInterrupt
        response = Mock()
        response.__enter__ = Mock(return_value=stream())
        response.__exit__ = Mock(return_value=False)
        with patch.object(bridge, 'credential', return_value='fixture'), patch.object(bridge.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(KeyboardInterrupt):
                self.broker.watch_events()
        self.assertEqual(self.broker.session_conditions, {})

    def test_cancel_failure_has_recoverable_state(self):
        first, _ = self.broker.store.submit(spec())
        self.broker.store.update(first, status='attention_required', session_id='s1', workspace='/synthetic')
        def unavailable(path, *args, **kwargs):
            if path.endswith('/abort'):
                raise RuntimeError('synthetic unavailable channel')
            return {} if path == '/session/status' else []
        with patch.object(bridge, 'oc', unavailable):
            result = self.broker.cancel(first)
        self.assertEqual(result['status'], 'attention_required')
        self.assertEqual(result['next_action'], 'job_recover')

    def test_reattach_only_schedules_existing_session(self):
        first, _ = self.broker.store.submit(spec())
        self.broker.store.update(first, status='attention_required', session_id='s1', workspace='/synthetic')
        self.broker.executor = Mock()
        with patch.object(bridge, 'oc') as native:
            result = self.broker.call('job_recover', {'job_id': first, 'action': 'reattach'})
        self.assertTrue(result['scheduled'])
        self.assertEqual(self.broker.executor.submit.call_args.args[1:], (first, True))
        self.assertFalse(native.called)

    def test_reasoning_progress_is_not_idle(self):
        first, _ = self.broker.store.submit(spec())
        messages = [{'info': {'id': 'm1'}, 'parts': [{'type': 'reasoning', 'text': 'thinking'}]}]
        self.broker.update_progress(first, messages)
        result = json.loads(self.broker.store.get(first)['progress'])
        self.assertEqual(result['phase'], 'reasoning')
        self.assertEqual(result['reasoning_chars'], 8)

    def test_question_requires_both_global_and_per_job_opt_in(self):
        first, _ = self.broker.store.submit(spec())
        with patch.dict(bridge.CONFIG, {'enable_questions': True}):
            with self.assertRaises(ValueError):
                self.broker.call('job_questions', {'job_id': first})

    def test_profile_sets_readonly_default(self):
        self.broker.catalog = lambda: ['opencode-go/test']
        profile = {'read': {'model': 'opencode-go/test', 'isolation': 'readonly'}}
        with patch.dict(bridge.CONFIG, {'worker_profiles': profile}), patch.object(bridge, 'project_path', return_value='/test/project'):
            result = self.broker.submit({'project': '/test/project', 'task': 'Read one file', 'request_id': 'profile', 'profile': 'read'})
        self.assertEqual(result['isolation'], 'readonly')

    def test_usage_deduplicates_responses_and_separates_turns(self):
        module_path = Path(__file__).parent / 'scripts/measure_usage.py'
        description = importlib.util.spec_from_file_location('measure_usage', module_path)
        module = importlib.util.module_from_spec(description)
        description.loader.exec_module(module)
        log = Path(self.temp.name) / 'usage.jsonl'
        def response(identifier, turn, value):
            return {'type': 'token_usage_record', 'payload': {'turn_id': turn, 'response_id': identifier,
                     'usage': {'input_tokens': value, 'cached_input_tokens': value-1, 'output_tokens': 2, 'total_tokens': value+2}}}
        records = [response('r1', 'current', 10), response('r1', 'current', 10), response('r2', 'other', 900)]
        log.write_text('\n'.join(json.dumps(item) for item in records))
        result = module.codex_usage(log, 'current')
        self.assertEqual(result['recorded_model_responses'], 1)
        self.assertEqual(result['tokens']['total_tokens'], 12)
        self.assertEqual(result['tokens']['cached_input_tokens'], 9)

    def test_real_mcp_broker_question_reply_completion_without_provider_calls(self):
        """Exercise actual subprocess + HTTP + SQLite transitions against a fake native API."""
        from verify_live import Client
        root = Path(self.temp.name)
        runtime = root / 'runtime'
        runtime.mkdir()
        state = root / 'isolated-state'
        project = root / 'project'
        project.mkdir()
        for name in ('bridge.py', 'control_plane.py', 'artifacts.py'):
            shutil.copy2(Path(bridge.__file__).parent / name, runtime / name)
        stopped = threading.Event()
        values = {'answered': False, 'reply_count': 0}
        class Native(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def send_json(self, value):
                data = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                route = urlsplit(self.path).path
                if route == '/global/event':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.end_headers()
                    self.wfile.write(b': fixture\n\n')
                    self.wfile.flush()
                    stopped.wait(15)
                elif route == '/global/health':
                    self.send_json({'healthy': True, 'version': 'fixture'})
                elif route == '/provider':
                    self.send_json({'all': [{'id': 'opencode-go', 'models': {'test': {}}}], 'connected': ['opencode-go']})
                elif route == '/session/status':
                    self.send_json({'s1': {'type': 'idle' if values['answered'] else 'busy'}})
                elif route == '/question':
                    self.send_json([] if values['answered'] else [{'id': 'q1', 'sessionID': 's1', 'questions': [{'question': 'Choose fixture?', 'options': [{'label': 'yes'}]}]}])
                elif route == '/session/s1/message':
                    final = values['answered']
                    self.send_json([{'info': {'id': 'm1', 'role': 'assistant', 'finish': 'stop' if final else 'tool-calls',
                                             'time': {'completed': 1}, 'tokens': {}},
                                     'parts': [{'type': 'text', 'text': 'fixture complete'}] if final else []}])
                else:
                    self.send_error(404)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                route = urlsplit(self.path).path
                if route == '/session':
                    values['permissions'] = body['permission']
                    self.send_json({'id': 's1'})
                elif route == '/session/s1/prompt_async':
                    values['tools'] = body['tools']
                    self.send_json(True)
                elif route == '/question/q1/reply':
                    values['reply_count'] += 1
                    values['answered'] = True
                    self.send_json(True)
                else:
                    self.send_error(404)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Native)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            broker_port = probe.getsockname()[1]
        config = dict(bridge.CONFIG, opencode_port=server.server_port, broker_port=broker_port,
                      opencode_bin='unused-fixture', allowed_roots=[str(project)],
                      default_model='opencode-go/test', allowed_providers=['opencode-go'], enable_questions=True)
        (runtime / 'config.json').write_text(json.dumps(config))
        client = None
        try:
            with patch.dict(os.environ, {'CODEX_OPENCODE_STATE': str(state)}), patch.object(bridge, '__file__', str(runtime / 'bridge.py')):
                client = Client()
            self.assertIn('opencode-go/test', client.call('bridge_models')['models'])
            job = client.call('job_submit', {'project': str(project), 'task': 'Ask fixture question then finish.',
                                            'request_id': 'fixture', 'isolation': 'readonly', 'questions': True})
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                result = client.call('job_wait', {'job_ids': [job['id']], 'seconds': 0.5})
                if result['jobs'][0]['next_action'] == 'job_answer':
                    break
            self.assertEqual(result['jobs'][0]['next_action'], 'job_answer')
            answer = {'job_id': job['id'], 'question_id': 'q1', 'answer_id': 'reply-1', 'answers': [['yes']]}
            self.assertTrue(client.call('job_answer', answer)['confirmed'])
            while time.monotonic() < deadline:
                result = client.call('job_wait', {'job_ids': [job['id']], 'seconds': 0.5})
                if result['jobs'][0]['status'] == 'succeeded':
                    break
            self.assertEqual(result['jobs'][0]['status'], 'succeeded')
            self.assertTrue(client.call('job_answer', answer)['already_answered'])
            self.assertEqual(values['reply_count'], 1)
            self.assertFalse(values['tools']['task'])
            self.assertTrue(values['tools']['question'])
        finally:
            if client:
                client.close()
            pid_file = state / 'broker-pid'
            if pid_file.is_file():
                pid = int(pid_file.read_text())
                command = Path(f'/proc/{pid}/cmdline')
                if command.is_file() and str(runtime / 'bridge.py').encode() in command.read_bytes().split(b'\0'):
                    os.kill(pid, signal.SIGTERM)
            stopped.set()
            server.shutdown()
            server.server_close()

if __name__ == '__main__':
    unittest.main()
