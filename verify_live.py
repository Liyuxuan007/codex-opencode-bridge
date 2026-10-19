"""Real-model acceptance test: three MCP clients, six isolated patches, queue/cancel, integration.

Consumes six short Go model runs. Fixtures are retained under ~/projects for inspection.
"""
import concurrent.futures
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import bridge


class Client:
    def __init__(self):
        self.process = subprocess.Popen([sys.executable, str(Path(bridge.__file__)), 'mcp'],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.counter = 0
        self.lock = threading.Lock()
        self.pending = {}
        threading.Thread(target=self.read, daemon=True).start()
        self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                'clientInfo': {'name': 'bridge-live-verifier', 'version': '1'}})
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
        self.process.stdin.flush()

    def read(self):
        for line in self.process.stdout:
            response = json.loads(line)
            target = self.pending.get(response.get('id'))
            if target:
                target.put(response)

    def rpc(self, method, params):
        with self.lock:
            self.counter += 1
            request_id = self.counter
            target = queue.Queue()
            self.pending[request_id] = target
            self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}) + '\n')
            self.process.stdin.flush()
        response = target.get(timeout=60)
        self.pending.pop(request_id)
        if 'error' in response:
            raise RuntimeError(response['error'])
        return response['result']

    def call(self, name, arguments=None):
        result = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        data = json.loads(result['content'][0]['text'])
        if result.get('isError'):
            raise RuntimeError(data)
        return data

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)


def main():
    started = time.time()
    clients = [Client() for _ in range(3)]
    project = Path.home() / 'projects' / ('bridge-acceptance-' + time.strftime('%Y%m%d-%H%M%S'))
    report = {'project': str(project), 'started': started, 'clients': 3, 'jobs': [], 'peak_active': 0}
    output = Path(__file__).with_name('acceptance-report.json')
    jobs = []
    try:
        tools = clients[0].rpc('tools/list', {})['tools']
        assert any(tool['name'] == 'job_submit' for tool in tools)
        clients[0].call('project_prepare', {'path': str(project), 'create': True, 'initialize_git': True})
        (project / 'tasks').mkdir()
        (project / 'tests').mkdir()
        (project / '.gitignore').write_text('__pycache__/\n*.py[cod]\n')
        (project / 'tasks/__init__.py').write_text('')
        (project / 'tests/__init__.py').write_text('')
        for number in range(1, 7):
            (project / f'tasks/task_{number}.py').write_text('def compute(x):\n    raise NotImplementedError\n')
            (project / f'tests/test_task_{number}.py').write_text(
                f'import unittest\nfrom tasks.task_{number} import compute\n'
                f'class TestTask(unittest.TestCase):\n    def test_compute(self):\n'
                f'        for x in [-3, 0, 2, 17]:\n            self.assertEqual(compute(x), x * {number} + {number})\n')
        bridge.git(project, 'add', '.')
        bridge.git(project, '-c', 'user.name=Bridge Acceptance', '-c', 'user.email=bridge@localhost',
                   'commit', '-m', 'Add six independent acceptance tasks')

        def submit(number):
            client = clients[(number - 1) // 2]
            args = {'project': str(project), 'owner': f'codex-manager-{(number - 1)//2 + 1}',
                    'task': f'Implement tasks/task_{number}.py compute(x) as x * {number} + {number}. '
                            f'Only modify that file. Run python3 -m unittest tests.test_task_{number}. '
                            'Keep the final report short.',
                    'request_id': f'{project.name}-{number}', 'isolation': 'worktree', 'timeout_seconds': 600}
            result = client.call('job_submit', args)
            assert client.call('job_submit', args)['id'] == result['id']
            return result

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            jobs = list(pool.map(submit, range(1, 7)))
        report['jobs'] = jobs
        # All six slots should be occupied before admitting a cancellable seventh job.
        active = [clients[0].call('job_status', {'job_id': job['id']}) for job in jobs]
        report['peak_active'] = sum(job['status'] in bridge.ACTIVE for job in active)
        seventh = clients[0].call('job_submit', {'project': str(project), 'task': 'Reply only QUEUE_PROBE.',
                                                 'owner': 'queue-probe', 'request_id': project.name + '-queued',
                                                 'isolation': 'readonly', 'timeout_seconds': 60})
        report['seventh_initial_status'] = seventh['status']
        assert seventh['status'] == 'queued', seventh
        cancelled = clients[0].call('job_cancel', {'job_id': seventh['id']})
        assert cancelled['status'] == 'cancelled'
        report['queued_cancellation'] = True
        pending = {job['id'] for job in jobs}
        deadline = time.time() + 650
        while pending and time.time() < deadline:
            states = clients[0].call('job_wait', {'job_ids': sorted(pending), 'seconds': 25})['jobs']
            report['peak_active'] = max(report['peak_active'], sum(job['status'] in bridge.ACTIVE for job in states))
            for job in states:
                if job['status'] in bridge.TERMINAL:
                    pending.remove(job['id'])
                    print(json.dumps({'job': job['id'], 'status': job['status'], 'elapsed': round(time.time()-started)}, ensure_ascii=False), flush=True)
        assert not pending, 'Live jobs exceeded acceptance deadline.'
        results = [clients[0].call('job_result', {'job_id': job['id']}) for job in jobs]
        report['jobs'] = results
        assert all(job['status'] == 'succeeded' for job in results), results
        assert len({job['session_id'] for job in results}) == 6
        assert len({job['workspace'] for job in results}) == 6
        independent = []
        for job in results:
            check = subprocess.run([sys.executable, '-m', 'unittest', f"tests.test_task_{results.index(job)+1}"],
                                   cwd=job['workspace'], capture_output=True, text=True)
            independent.append({'job_id': job['id'], 'exit_code': check.returncode, 'output': check.stdout + check.stderr})
            assert check.returncode == 0, check.stderr
            patch = clients[0].call('job_diff', {'job_id': job['id']})
            assert patch['patch']
            applied = clients[0].call('job_apply', {'job_id': job['id']})
            assert applied['applied']
        report['independent_checks'] = independent
        check = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                               cwd=project, capture_output=True, text=True)
        report['integrated_test'] = {'exit_code': check.returncode, 'output': check.stdout + check.stderr}
        assert check.returncode == 0
        report['passed'] = True
    except Exception as error:
        report['passed'] = False
        report['error'] = repr(error)
        for job in jobs:
            try:
                clients[0].call('job_cancel', {'job_id': job['id']})
            except Exception:
                pass
        raise
    finally:
        report['elapsed_seconds'] = round(time.time() - started, 2)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        for client in clients:
            client.close()
        print(json.dumps({'report': str(output), 'passed': report.get('passed'), 'elapsed': report['elapsed_seconds']}), flush=True)


def resume_verification():
    """Recheck existing completed tasks without making additional model calls."""
    output = Path(__file__).with_name('acceptance-report.json')
    report = json.loads(output.read_text())
    client = Client()
    project = Path(report['project'])
    try:
        with (project / '.git/info/exclude').open('a') as exclude:
            exclude.write('\n__pycache__/\n*.py[cod]\n')
        results = [client.call('job_result', {'job_id': job['id']}) for job in report['jobs']]
        assert len(results) == 6 and all(job['status'] == 'succeeded' for job in results)
        checks = []
        for number, job in enumerate(results, 1):
            # Earlier fixture lacked .gitignore. Drop only intent-to-add cache entries from these test worktrees.
            bridge.git(job['workspace'], 'reset', '--', 'tasks/__pycache__', 'tests/__pycache__', check=False)
            check = subprocess.run([sys.executable, '-B', '-m', 'unittest', f'tests.test_task_{number}'],
                                   cwd=job['workspace'], capture_output=True, text=True)
            checks.append({'job_id': job['id'], 'exit_code': check.returncode, 'output': check.stdout + check.stderr})
            assert check.returncode == 0
            assert client.call('job_diff', {'job_id': job['id']})['patch']
            assert client.call('job_apply', {'job_id': job['id']})['applied']
        check = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                               cwd=project, capture_output=True, text=True)
        assert check.returncode == 0
        report.update(passed=True, error=None, jobs=results, independent_checks=checks,
                      integrated_test={'exit_code': check.returncode, 'output': check.stdout + check.stderr},
                      integration_reverified_without_model_calls=True)
        print(json.dumps({'passed': True, 'integrated_test': report['integrated_test']}), flush=True)
    finally:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        client.close()


if __name__ == '__main__':
    if '--resume' in sys.argv:
        resume_verification()
    else:
        main()
