#!/usr/bin/env python3
"""Persistent WSL OpenCode worker queue and MCP frontend. Python standard library only."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from workflow import compact_job, normalize_workflow, worker_prompt, worker_permissions, assert_native_permissions
from control_plane import (ModelCatalog, cancel_job, recover_job, QuestionChannel, quota_block_reason,
                           migrate_job_state, update_job, native_failure)
from artifacts import ArtifactManager, normalize_contract

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.json'
if not CONFIG_PATH.is_file():
    CONFIG_PATH = ROOT / 'config.example.json'
CONFIG = json.loads(CONFIG_PATH.read_text(encoding='utf-8-sig'))
CONFIG['opencode_bin'] = os.path.expanduser(CONFIG['opencode_bin'])
STATE = Path(os.environ.get('CODEX_OPENCODE_STATE', str(Path.home() / '.local/state/codex-opencode-bridge'))).expanduser()
if not STATE.is_absolute():
    raise ValueError('CODEX_OPENCODE_STATE must be an absolute private Linux path.')

def validate_config(config):
    if isinstance(config.get('max_concurrency'), bool) or not isinstance(config.get('max_concurrency'), int) or config['max_concurrency'] < 1:
        raise ValueError('max_concurrency must be a positive integer.')
    if config.get('result_view', 'compact') not in ('compact', 'full'):
        raise ValueError('result_view must be compact or full.')
    for key, default in (('result_max_chars', 2500), ('check_output_chars', 2000)):
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 60000:
            raise ValueError(key + ' must be an integer between 0 and 60000.')
    for key in ('provider_caps', 'model_caps'):
        limits = config.get(key, {})
        if not isinstance(limits, dict) or any(not isinstance(k, str) or not k or isinstance(v, bool) or not isinstance(v, int) or v < 1 for k, v in limits.items()):
            raise ValueError(key + ' must map explicit IDs to positive integer limits.')
    for key in ('allowed_providers', 'allowed_models'):
        values = config.get(key)
        if values is not None and (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)):
            raise ValueError(key + ' must be an array of nonempty IDs.')
    for key in ('enable_questions', 'require_verification'):
        if key in config and not isinstance(config[key], bool):
            raise ValueError(key + ' must be boolean.')
    profiles = config.get('worker_profiles', {})
    if not isinstance(profiles, dict) or any(not isinstance(v, dict) or not isinstance(v.get('model'), str) or '/' not in v['model'] or v.get('isolation', 'worktree') not in ('worktree', 'readonly', 'exclusive') for v in profiles.values()):
        raise ValueError('worker_profiles must contain explicit model/isolation objects.')

validate_config(CONFIG)
VERSION = '1.3.1'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
JOB_LOCKS = [threading.RLock() for _ in range(64)]

def job_lock(job_id):
    return JOB_LOCKS[int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % len(JOB_LOCKS)]

ACTIVE = {'starting', 'admitting', 'running', 'cancelling', 'attention_required'}
INSTRUCTIONS = '''Delegate bounded implementation, investigation, or test work to OpenCode in WSL.
Call bridge_health then project_prepare with the intended project path. Prefer Linux projects under
~/projects (expand ~ to the Linux home directory). Use job_submit once per independent task, with unique request_id and owner.
Default to two concurrent workers; use one for coupled work and three only for independent modules or review.
Queue more small tasks than slots rather than enlarging tasks. Define goal, interfaces, acceptance and stop_conditions
in brief; use risk low/standard/high for review depth. Risk is guidance, never a verification bypass.
The shared broker caps ALL clients at the configured max_concurrency; do not start separate OpenCode processes or nested
subagents. Parallel writing tasks use worktree isolation. readonly is a tool policy, not an OS sandbox.
Sessions are never shared between tasks. A succeeded job means the agent finished, NOT that its code
is correct. Read compact job_result first; request view=full only for needed diagnostics. Inspect job_diff,
run relevant verification, then job_apply only after review. Prefer programmatic checks before model review.
Prefer worktree task contracts with allowed paths and coordinator-supplied checks. Call job_verify,
then apply the reviewed patch_sha256. Use job_recover for uncertain execution; never replay a prompt.
Use job_wait in batches of up to eight IDs (at most 30 seconds), retain after_event_id and remove completed IDs.
Do not retrieve unchanged results or full successful logs. Collect concrete failures into one bounded repair brief.
No automatic repair or same-session follow-up exists: a repair submission is explicit and creates a new session.
For multi-Codex orchestration, let specialists submit jobs and return their IDs; the primary agent
centrally batch-waits and resumes specialists only for review, instead of every specialist polling.
If progress reports a stale warning, inspect the tool/session before deciding to cancel; do not
automatically retry a writing task. Timeout results retain partial output and cumulative usage.
Do not delegate communications, purchases, publishing, pushing, or destructive actions without the
human user's authorization. Model/provider credentials remain managed by OpenCode in WSL.'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def init_state():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)


def credential(name):
    init_state()
    path = STATE / name
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    with os.fdopen(fd, 'w') as stream:
        stream.write(secrets.token_urlsafe(36))
    return path.read_text().strip()


def http(url, method='GET', body=None, headers=None, timeout=15):
    request = urllib.request.Request(url, data=None if body is None else encode(body), method=method,
                                     headers={'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode('utf-8', errors='replace')[:1500]
        raise RuntimeError(f'HTTP {error.code}: {detail}') from None


def oc(path, method='GET', body=None, directory=None, timeout=15):
    if directory:
        path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'directory': directory})
    auth = base64.b64encode(('opencode:' + credential('opencode-password')).encode()).decode()
    return http(f"http://127.0.0.1:{CONFIG['opencode_port']}" + path, method, body,
                {'Authorization': 'Basic ' + auth}, timeout)


def ensure_opencode():
    try:
        health = oc('/global/health', timeout=2)
        if health.get('healthy'):
            return health
    except Exception:
        pass
    # Fail rather than replacing an unrelated service occupying our configured port.
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1', CONFIG['opencode_port'])) == 0:
            raise RuntimeError('OpenCode port is occupied by an unrecognized service; no process was stopped.')
    env = dict(os.environ)
    env['OPENCODE_SERVER_PASSWORD'] = credential('opencode-password')
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps({
        'permission': {'*': 'allow', 'task': 'deny', 'question': 'deny', 'external_directory': 'deny'},
        'compaction': {'auto': True, 'prune': True},
    })
    with (STATE / 'opencode.log').open('ab') as log:
        child = subprocess.Popen([CONFIG['opencode_bin'], 'serve', '--pure', '--hostname=127.0.0.1',
                                  f"--port={CONFIG['opencode_port']}"], env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    (STATE / 'opencode-pid').write_text(str(child.pid))
    for _ in range(100):
        if child.poll() is not None:
            raise RuntimeError('OpenCode exited during startup; inspect the private OpenCode log.')
        time.sleep(.2)
        try:
            health = oc('/global/health', timeout=1)
            if health.get('healthy'):
                return health
        except Exception:
            pass
    raise RuntimeError('OpenCode did not become ready within 20 seconds.')


def git(directory, *args, check=True, binary=False, data=None):
    result = subprocess.run(['git', '-C', str(directory), *args], input=data,
                            capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[:1500])
    return result if binary else result.stdout.decode('utf-8', errors='replace').strip()


def project_path(raw, create=False):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError('An explicit project path is required.')
    if re.match(r'^[A-Za-z]:[\\/]', raw):
        raw = '/mnt/' + raw[0].lower() + '/' + raw[3:].replace('\\', '/')
    path = Path(raw).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in CONFIG['allowed_roots']]
    if not any(path.is_relative_to(root) for root in roots):
        raise ValueError('Project is outside allowed_roots. Add its parent to config.json explicitly.')
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Project directory does not exist. Use project_prepare(create=true).')
    return str(path)


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, signature TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                spec TEXT NOT NULL, session_id TEXT, workspace TEXT, base_commit TEXT,
                result TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                started REAL, progress TEXT)''')
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)')}
            if 'started' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN started REAL')
            if 'progress' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN progress TEXT')
            connection.execute('CREATE TABLE IF NOT EXISTS job_events (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, kind TEXT NOT NULL, created REAL NOT NULL)')
            migrate_job_state(connection)

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
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            update_job(connection, job_id, values, touch=touch)

    def record_event(self, job_id, kind):
        with self.connect() as connection:
            connection.execute('INSERT INTO job_events(job_id,kind,created) VALUES(?,?,?)', (job_id, kind, time.time()))

    def event_cursor(self, ids):
        with self.connect() as connection:
            return connection.execute('SELECT COALESCE(MAX(id),0) FROM job_events WHERE job_id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchone()[0]

    def existing_request(self, spec):
        signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        with self.connect() as connection:
            row = connection.execute('SELECT id,signature FROM jobs WHERE request_id=?', (spec['request_id'],)).fetchone()
        if row and row['signature'] != signature:
            raise ValueError('request_id already exists with different inputs.')
        return row['id'] if row else None

    def submit(self, spec, base_commit=None):
        request_id = spec['request_id']
        signature = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        now = time.time()
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute('SELECT * FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            if row:
                if row['signature'] != signature:
                    raise ValueError('request_id already exists with different inputs.')
                return row['id'], True
            job_id = 'job_' + uuid.uuid4().hex[:16]
            connection.execute('INSERT INTO jobs(id,request_id,signature,status,created,updated,spec,base_commit) VALUES(?,?,?,?,?,?,?,?)',
                               (job_id, request_id, signature, 'queued', now, now, json.dumps(spec), base_commit))
            return job_id, False

    def claim(self, limit):
        with self.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active = connection.execute("SELECT spec,workspace FROM jobs WHERE status IN ('starting','admitting','running','cancelling','attention_required')").fetchall()
            if len(active) >= limit:
                return None
            waiting_exclusive = set()
            for row in connection.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created").fetchall():
                spec = json.loads(row['spec'])
                if quota_block_reason(spec, [json.loads(a['spec']) for a in active], CONFIG):
                    if spec['isolation'] == 'exclusive':
                        waiting_exclusive.add(spec['project'])
                    continue
                if spec['project'] in waiting_exclusive:
                    continue
                # Exclusive jobs serialize every job touching that source; worktree jobs are independent.
                blocked = any(json.loads(a['spec'])['project'] == spec['project'] and
                              (spec['isolation'] == 'exclusive' or json.loads(a['spec'])['isolation'] == 'exclusive')
                              for a in active)
                if blocked:
                    if spec['isolation'] == 'exclusive':
                        waiting_exclusive.add(spec['project'])
                    continue
                now = time.time()
                update_job(connection, row['id'], {'status': 'starting', 'started': now}, now=now)
                return row['id']
        return None

    def rows(self, limit=30):
        with self.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,))]


class Broker:
    def __init__(self):
        init_state()
        self.store = Store(STATE / 'jobs.sqlite3')
        self.changed = threading.Condition()
        self.session_conditions = {}
        self.session_conditions_lock = threading.Lock()
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=int(CONFIG['max_concurrency']))
        self.running_jobs = set()
        self.running_jobs_lock = threading.Lock()
        self.health = ensure_opencode()
        self.models = None
        threading.Thread(target=self.watch_events, daemon=True).start()

    def watch_events(self):
        # One SSE connection wakes every client; bounded fallback polling handles dropped events.
        while True:
            try:
                auth = base64.b64encode(('opencode:' + credential('opencode-password')).encode()).decode()
                request = urllib.request.Request(f"http://127.0.0.1:{CONFIG['opencode_port']}/global/event",
                                                 headers={'Authorization': 'Basic ' + auth, 'Accept': 'text/event-stream'})
                with urllib.request.urlopen(request, timeout=45) as response:
                    for line in response:
                        if line.startswith(b'data:'):
                            payload = json.loads(line[5:].strip())
                            event = payload.get('payload', payload)
                            if event.get('type') in ('session.idle', 'session.error', 'session.status', 'message.updated'):
                                properties = event.get('properties', {})
                                sid = properties.get('sessionID') or properties.get('info', {}).get('sessionID')
                                if sid:
                                    with self.session_conditions_lock:
                                        condition = self.session_conditions.get(sid)
                                    if condition:
                                        with condition:
                                            condition.notify_all()
            except Exception:
                time.sleep(2)

    def brief(self, row):
        spec = json.loads(row['spec'])
        progress = json.loads(row['progress']) if row.get('progress') else None
        if progress:
            progress.pop('fingerprint', None)
            observed_end = row.get('execution_ended') or time.time()
            progress['stalled_seconds'] = round(max(0, observed_end - progress['last_progress_at']), 1) if row['status'] in ACTIVE else None
            for tool in progress.get('running_tools', []):
                if tool.get('started_at'):
                    tool['elapsed_seconds'] = round(max(0, time.time() - tool['started_at']), 1)
            if row['status'] in ACTIVE and progress['stalled_seconds'] >= CONFIG.get('stale_warning_seconds', 120):
                progress['warning'] = 'No observable progress. May be tool execution, reasoning, or provider wait; inspect before cancelling.'
        now = time.time()
        observed_end = row.get('execution_ended') or (row['updated'] if row['status'] in TERMINAL | {'attention_required'} else now)
        attention = row.get('attention_seconds') or 0
        if row.get('attention_since') is not None:
            attention += max(0, now - row['attention_since'])
        excluded = (row.get('execution_excluded_seconds') or 0) if row.get('execution_ended') else (row.get('attention_seconds') or 0)
        timing = {'queue_seconds': round(row['started']-row['created'], 3) if row['started'] else None,
                  'execution_seconds': round(max(0, observed_end-row['started']-excluded), 3) if row['started'] else None,
                  'attention_seconds': round(attention, 3),
                  'wall_seconds': round(max(0, ((row.get('finished_at') or row['updated']) if row['status'] in TERMINAL else now)-row['created']), 3),
                  'execution_clock': 'bridge_observed'}
        result = {key: row[key] for key in ('id', 'status', 'created', 'updated', 'session_id', 'workspace', 'base_commit', 'error')} | {
            'owner': spec['owner'], 'project': spec['project'], 'model': spec['model'],
            'isolation': spec['isolation'], 'request_id': spec['request_id'], 'timing': timing, 'progress': progress,
            'risk': spec.get('risk', 'standard')}
        result['failure'] = json.loads(row['failure']) if row.get('failure') else None
        result['next_action'] = ('job_recover' if row['status'] in {'attention_required', 'cancelling'} else
                                 'job_verify' if row['status'] == 'succeeded' and spec.get('contract') else
                                 'job_result' if row['status'] in TERMINAL else 'job_wait')
        if progress and progress.get('pending_questions'):
            result['next_action'] = 'job_answer'
        result['error_code'] = {'timed_out': 'execution_deadline', 'failed': 'execution_failed', 'attention_required': 'execution_uncertain'}.get(row['status'])
        result['retryable'] = False  # Repeated admission uses the original request_id; execution replay is explicit.
        if row['status'] == 'queued':
            with self.store.connect() as connection:
                active = [json.loads(r['spec']) for r in connection.execute("SELECT spec FROM jobs WHERE status IN ('starting','admitting','running','cancelling','attention_required')")]
            reason = quota_block_reason(spec, active, CONFIG)
            if len(active) >= CONFIG['max_concurrency']:
                reason = 'global_limit'
            elif any(a['project'] == spec['project'] and (a['isolation'] == 'exclusive' or spec['isolation'] == 'exclusive') for a in active):
                reason = 'exclusive_lock'
            result['queued_reason'] = reason or 'scheduler_pending'
        return result

    def session_condition(self, sid):
        if not hasattr(self, 'session_conditions'):
            return self.changed
        with self.session_conditions_lock:
            return self.session_conditions.setdefault(sid, threading.Condition())

    def notify(self):
        with self.changed:
            self.changed.notify_all()

    def question_channel(self):
        if not hasattr(self, '_questions'):
            self._questions = QuestionChannel(self.store, oc, self.notify)
        return self._questions

    def catalog(self):
        if not hasattr(self, 'model_catalog'):
            self.model_catalog = ModelCatalog(oc, CONFIG)
        return self.model_catalog.list()['models']

    def submit(self, args):
        project = project_path(args['project'])
        task = args.get('task', '')
        if not isinstance(task, str) or not 1 <= len(task) <= 50000:
            raise ValueError('task must contain 1–50000 characters.')
        profile = args.get('profile')
        profiles = CONFIG.get('worker_profiles', {})
        if profile is not None and profile not in profiles:
            raise ValueError('Unknown worker profile; configure worker_profiles explicitly.')
        model = args.get('model') or (profiles[profile]['model'] if profile else CONFIG['default_model'])
        if model not in self.catalog():
            raise ValueError('Unknown or disallowed model; call bridge_models.')
        isolation = args.get('isolation', profiles[profile].get('isolation', 'worktree') if profile else 'worktree')
        if isolation not in ('worktree', 'readonly', 'exclusive'):
            raise ValueError('Unsupported isolation mode.')
        timeout = args.get('timeout_seconds', CONFIG['default_timeout_seconds'])
        if not isinstance(timeout, int) or not 30 <= timeout <= 14400:
            raise ValueError('timeout_seconds must be between 30 and 14400.')
        request_id = args.get('request_id') or uuid.uuid4().hex
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise ValueError('Invalid request_id.')
        spec = {'project': project, 'task': task, 'model': model, 'owner': args.get('owner', 'main'),
                'isolation': isolation, 'timeout_seconds': timeout, 'request_id': request_id}
        if 'brief' in args and not isinstance(args['brief'], dict):
            raise ValueError('brief must be an object when supplied.')
        workflow = normalize_workflow(args)
        # Preserve old request identities when callers omit new optional fields.
        for key in ('risk', 'brief'):
            if key in args:
                spec[key] = workflow[key]
        if profile is not None:
            spec['profile'] = profile
        if args.get('contract') is not None:
            if isolation != 'worktree':
                raise ValueError('Task contracts require worktree isolation.')
            spec['contract'] = normalize_contract(args['contract'])
        if args.get('questions'):
            if not CONFIG.get('enable_questions', False):
                raise ValueError('Question channel is disabled; enable_questions must be configured explicitly.')
            spec['questions'] = True
        # Lookup before mutable repository preflight: a retry must still find its task after integration.
        existing = self.store.existing_request(spec)
        if existing:
            return self.brief(self.store.get(existing)) | {'deduplicated': True}
        base = None
        if isolation == 'worktree':
            if git(project, 'rev-parse', '--show-toplevel') != project:
                raise ValueError('Use the repository root as project for worktree tasks; project_prepare reports git_root.')
            base = git(project, 'rev-parse', '--verify', 'HEAD')
            if git(project, 'status', '--porcelain'):
                raise ValueError('Worktree uses committed HEAD: commit intended source changes first, or explicitly choose exclusive mode.')
        job_id, deduplicated = self.store.submit(spec, base_commit=base)
        self.notify()
        return self.brief(self.store.get(job_id)) | {'deduplicated': deduplicated}

    def prepare_workspace(self, row, spec):
        if row['workspace']:
            return row['workspace']
        project = spec['project']
        base = row['base_commit'] or git(project, 'rev-parse', 'HEAD', check=False)
        if spec['isolation'] == 'worktree':
            path = STATE / 'worktrees' / row['id']
            path.parent.mkdir(exist_ok=True)
            git(project, 'worktree', 'add', '--detach', str(path), base)
            workspace = str(path)
        else:
            workspace = project
        self.store.update(row['id'], workspace=workspace, base_commit=base or None)
        return workspace

    def update_progress(self, job_id, messages):
        previous_row = self.store.get(job_id)
        previous = json.loads(previous_row['progress']) if previous_row.get('progress') else None
        now = time.time()
        parts, running = [], []
        for message in messages:
            info = message.get('info', {})
            parts.append((info.get('id'), info.get('finish'), info.get('time', {}).get('completed')))
            for part in message.get('parts', []):
                state = part.get('state', {})
                parts.append((part.get('id'), part.get('type'), state.get('status'), len(part.get('text', '')),
                              len(state.get('output', '')) if isinstance(state.get('output'), str) else 0))
                if part.get('type') == 'tool' and state.get('status') in ('running', 'pending'):
                    start = state.get('time', {}).get('start')
                    running.append({'tool': part.get('tool'), 'call_id': part.get('callID'),
                                    'started_at': start / 1000 if start else None})
        fingerprint = hashlib.sha256(encode(parts)).hexdigest()
        if previous and previous.get('fingerprint') == fingerprint:
            return
        reasoning_chars = sum(len(p.get('text', '')) for m in messages for p in m.get('parts', []) if p.get('type') == 'reasoning')
        visible_chars = sum(len(p.get('text', '')) for m in messages for p in m.get('parts', []) if p.get('type') == 'text')
        phase = 'tool_running' if running else ('reasoning' if reasoning_chars else ('receiving_output' if visible_chars else 'provider_wait'))
        progress = {'last_progress_at': now, 'running_tools': running, 'fingerprint': fingerprint, 'phase': phase,
                    'reasoning_chars': reasoning_chars, 'visible_chars': visible_chars,
                    'recent_message_count': len(messages)}
        self.store.update(job_id, progress=json.dumps(progress), touch=False)

    def snapshot_result(self, row, partial=False):
        messages = oc(f"/session/{row['session_id']}/message", directory=row['workspace'], timeout=20)
        if not isinstance(messages, list):
            raise RuntimeError('OpenCode did not return session messages.')
        assistants = [message for message in messages if message.get('info', {}).get('role') == 'assistant']
        unique = {message['info'].get('id', str(index)): message for index, message in enumerate(assistants)}
        totals = {'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0}
        cost = 0
        for message in unique.values():
            info = message['info']; tokens = info.get('tokens', {})
            for key in ('input', 'output', 'reasoning'):
                totals[key] += tokens.get(key, 0)
            for key in ('read', 'write'):
                totals['cache_' + key] += tokens.get('cache', {}).get(key, 0)
            cost += info.get('cost', 0)
        last = assistants[-1]['info'] if assistants else {}
        tool_states = []
        if partial:
            for message in unique.values():
                for part in message.get('parts', []):
                    if part.get('type') == 'tool':
                        state = part.get('state', {}); timing = state.get('time', {})
                        tool_states.append({'tool': part.get('tool'), 'status': state.get('status'),
                                            'elapsed_seconds': round((timing['end']-timing['start'])/1000, 3)
                                            if timing.get('start') and timing.get('end') else None})
        text = next(('\n'.join(part.get('text', '') for part in message.get('parts', []) if part.get('type') == 'text').strip()
                     for message in reversed(assistants)
                     if any(part.get('type') == 'text' and part.get('text', '').strip() for part in message.get('parts', []))), '')
        result = {'text': text, 'partial': partial,
                'last_message_usage': {key: last.get(key) for key in ('tokens', 'cost', 'modelID', 'providerID')},
                'session_usage': {'assistant_turns': len(unique), 'tokens': totals, 'reported_cost': cost,
                                  'scope': 'All retained OpenCode assistant message metadata; reported cost is not subscription billing.'},
                'model_completed_at': last.get('time', {}).get('completed'),
                'workspace_git_status': git(row['workspace'], 'status', '--short', check=False),
                'verification': 'Agent completion only. Parent must independently review and verify.'}
        if partial:
            result['recent_tool_states'] = tool_states[-8:]
        return result

    def retain_partial(self, job_id):
        row = self.store.get(job_id)
        if row['session_id'] and row['workspace']:
            try:
                result = self.snapshot_result(row, partial=True)
                self.store.update(job_id, result=json.dumps(result, ensure_ascii=False), touch=False)
            except Exception:
                pass  # Preserve the primary error; session/worktree handles remain available.

    def run(self, job_id, recovering=False):
        prompt_attempted = False
        sid = None
        workspace = None
        try:
            row = self.store.get(job_id)
            spec = json.loads(row['spec'])
            if row['cancel_requested']:
                if row['session_id'] and row['workspace']:
                    cancel_job(self.store, oc, self.notify, job_id)
                else:
                    self.store.update(job_id, status='cancelled')
                return
            workspace = self.prepare_workspace(row, spec)
            sid = row['session_id']
            with job_lock(job_id):
                if not sid:
                    row = self.store.get(job_id)
                    if row['cancel_requested']:
                        self.store.update(job_id, status='cancelled')
                        return
                    # Each job owns a new session: no idle-session claim races or cross-task context mixing.
                    permissions = worker_permissions(spec['isolation'], bool(spec.get('questions')))
                    provider, model = spec['model'].split('/', 1)
                    session = oc('/session', 'POST', {'title': f"Bridge {job_id} ({spec['owner']})", 'permission': permissions,
                                                     'agent': 'build', 'model': {'providerID': provider, 'id': model}}, workspace)
                    sid = session['id']
                    self.store.update(job_id, session_id=sid)
                    assert_native_permissions(oc(f'/session/{sid}', directory=workspace), permissions)
                    self.store.update(job_id, status='admitting')
                    task = worker_prompt(workspace, spec)
                    prompt_attempted = True  # A lost POST response is ambiguous; never replay it.
                    oc(f'/session/{sid}/prompt_async', 'POST', {
                        'model': {'providerID': provider, 'modelID': model}, 'agent': 'build',
                        'parts': [{'type': 'text', 'text': task}]}, workspace)
                    # Legacy body.tools replaces session permissions in OpenCode 1.19.2.
                    # Never send it, and verify the native service retained the policy.
                    assert_native_permissions(oc(f'/session/{sid}', directory=workspace), permissions)
                    self.store.update(job_id, status='running')
                elif recovering and row['status'] in ('starting', 'admitting'):
                    # A crash in the admit window is ambiguous; never replay a possibly executed instruction.
                    raise RuntimeError('Broker restarted during admission; inspect the retained OpenCode session before resubmitting.')
            deadline = (row['started'] or row['updated']) + spec['timeout_seconds']
            while True:
                current = self.store.get(job_id)
                if current['cancel_requested'] or time.time() >= deadline:
                    cancel_job(self.store, oc, self.notify, job_id,
                               target_status=current.get('termination_status') or ('cancelled' if current['cancel_requested'] else 'timed_out'))
                    confirmed = self.store.get(job_id)
                    if confirmed['status'] == 'timed_out':
                        self.store.update(job_id, error=f"Execution deadline exceeded ({spec['timeout_seconds']}s).")
                    self.retain_partial(job_id)
                    return
                messages = oc(f'/session/{sid}/message?limit=2', directory=workspace)
                self.update_progress(job_id, messages)
                if spec.get('questions'):
                    pending = self.question_channel().discover(job_id)
                    progress = json.loads(self.store.get(job_id)['progress'] or '{}')
                    old_ids = [q['id'] for q in json.loads(current['progress'] or '{}').get('pending_questions', [])]
                    progress['pending_questions'] = pending['questions']
                    if pending.get('capability_uncertain'):
                        progress['question_warning'] = pending.get('error')
                    if pending['questions']:
                        progress['phase'] = 'waiting_parent'
                    elif progress.get('phase') == 'waiting_parent':
                        progress['phase'] = 'provider_wait'
                    self.store.update(job_id, progress=json.dumps(progress), touch=False)
                    if old_ids != [q['id'] for q in pending['questions']]:
                        self.store.record_event(job_id, 'questions_changed')
                        self.notify()
                assistants = [m for m in messages if m.get('info', {}).get('role') == 'assistant']
                if assistants:
                    last = assistants[-1]
                    if last['info'].get('error'):
                        failure = native_failure(last['info']['error'])
                        self.store.update(job_id, failure=json.dumps(failure), touch=False)
                        raise RuntimeError('Native model request failed: ' + failure['category'] + '. See failure diagnostics.')
                    if last['info'].get('finish') not in (None, 'tool-calls') and last['info'].get('time', {}).get('completed'):
                        break
                # v1.19.2 advertises /api/session/{id}/wait but returns 503 (not implemented).
                # Use actual final-message metadata; never infer success from quiet or stable output.
                statuses = oc('/session/status', directory=workspace)
                status = statuses.get(sid, {}).get('type') if isinstance(statuses, dict) else None
                if status == 'idle' and time.time() - (row['started'] or row['updated']) > 15:
                    raise RuntimeError('OpenCode became idle without a completed final answer; inspect its session.')
                condition = self.session_condition(sid)
                with condition:
                    condition.wait(2)
            text = '\n'.join(p.get('text', '') for p in last.get('parts', []) if p.get('type') == 'text').strip()
            if not text or not last['info'].get('time', {}).get('completed'):
                raise RuntimeError('No completed final text was returned. The task is not marked successful.')
            result = self.snapshot_result(self.store.get(job_id))
            if spec.get('contract'):
                result['artifact'] = self.artifacts().freeze(self.store.get(job_id) | {'status': 'succeeded'}, spec['contract'])
            # Cancellation and completion compete under one transaction; cancellation wins if admitted first.
            with self.store.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                cancelled = connection.execute('SELECT cancel_requested FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
                update_job(connection, job_id, {'status': 'cancelled' if cancelled else 'succeeded',
                                               'result': json.dumps(result, ensure_ascii=False)})
                connection.execute('INSERT INTO job_events(job_id,kind,created) VALUES(?,?,?)',
                                   (job_id, 'completed', time.time()))
        except Exception as error:
            current = self.store.get(job_id)
            requested = bool(current['cancel_requested'])
            status = 'cancelled' if requested else 'failed'
            if sid and workspace and (prompt_attempted or recovering):
                try:
                    cancel_job(self.store, oc, self.notify, job_id,
                               target_status=current.get('termination_status') or ('cancelled' if requested else 'failed'))
                    reconciled = self.store.get(job_id)
                    if reconciled['status'] == 'succeeded':
                        return
                    if reconciled['status'] in ('cancelled', 'failed', 'timed_out'):
                        status = reconciled['status']
                    else:
                        status = 'attention_required'
                except Exception:
                    status = 'attention_required'
            self.store.update(job_id, status=status, error=str(error)[:2500])
            self.retain_partial(job_id)
        finally:
            if hasattr(self, 'session_conditions') and sid:
                with self.session_conditions_lock:
                    self.session_conditions.pop(sid, None)
            self.notify()

    def execute_job(self, job_id, recovering=False):
        with self.running_jobs_lock:
            if job_id in self.running_jobs or self.store.get(job_id)['status'] in TERMINAL:
                return
            self.running_jobs.add(job_id)
        try:
            self.run(job_id, recovering)
        finally:
            with self.running_jobs_lock:
                self.running_jobs.discard(job_id)

    def schedule(self):
        # Reattach to already-admitted sessions; do not repeat model prompts after restart.
        with self.store.connect() as connection:
            recovering = [dict(r) for r in connection.execute("SELECT * FROM jobs WHERE status IN ('starting','admitting','running','cancelling') ORDER BY created")]
        for row in recovering:
            self.executor.submit(self.execute_job, row['id'], True)
        while True:
            job_id = self.store.claim(int(CONFIG['max_concurrency']))
            if job_id:
                self.executor.submit(self.execute_job, job_id)
            else:
                with self.changed:
                    self.changed.wait(1)

    def cancel(self, job_id):
        before = self.store.get(job_id)['status']
        with job_lock(job_id):
            cancel_job(self.store, oc, self.notify, job_id)
        if self.store.get(job_id)['status'] != before:
            self.store.record_event(job_id, 'cancel_reconciled')
        return self.brief(self.store.get(job_id))

    def artifacts(self):
        return ArtifactManager(STATE, git)

    def diff(self, job_id):
        row = self.store.get(job_id)
        artifact = self.artifacts().freeze(row, json.loads(row['spec']).get('contract'))
        path = artifact['patch_path']
        return row, Path(path).read_bytes(), path

    def call(self, name, args):
        view = args.get('view', CONFIG.get('result_view', 'compact'))
        if view not in ('compact', 'full'):
            raise ValueError('view must be compact or full.')
        limit = args.get('max_chars', CONFIG.get('result_max_chars', 2500) if view == 'compact' else 12000)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 100000:
            raise ValueError('max_chars must be an integer between 0 and 100000.')
        def present(payload):
            return compact_job(payload, min(limit, 60000)) if view == 'compact' else payload
        if name == 'bridge_health':
            with self.store.connect() as connection:
                counts = {r['status']: r['n'] for r in connection.execute('SELECT status,COUNT(*) n FROM jobs GROUP BY status')}
            return {'version': VERSION, 'opencode': oc('/global/health'), 'max_concurrency': CONFIG['max_concurrency'],
                    'default_model': CONFIG['default_model'], 'counts': counts, 'allowed_roots': CONFIG['allowed_roots'],
                    'result_view': CONFIG.get('result_view', 'compact'),
                    'require_verification': CONFIG.get('require_verification', False)}
        if name == 'bridge_models':
            self.catalog()
            return self.model_catalog.list(refresh=bool(args.get('refresh', False)))
        if name == 'project_prepare':
            path = project_path(args['path'], create=args.get('create', False))
            git_root = git(path, 'rev-parse', '--show-toplevel', check=False)
            exists = bool(git_root)
            if git_root != path and args.get('initialize_git', False):
                git(path, 'init', '-b', 'main')
                # Empty baseline only; never stage or commit the user's existing files automatically.
                git(path, '-c', 'user.name=Codex OpenCode Bridge', '-c', 'user.email=bridge@localhost',
                    'commit', '--allow-empty', '-m', 'Initialize bridge project baseline')
                exists = True
                git_root = path
            return {'project': path, 'git': exists, 'git_root': git_root or None, 'head': git(path, 'rev-parse', 'HEAD', check=False) or None,
                    'changes': git(path, 'status', '--short', check=False),
                    'preferred_mode': 'worktree' if exists else 'exclusive'}
        if name == 'job_submit':
            return self.submit(args)
        if name == 'job_list':
            return {'jobs': [present(self.brief(r)) for r in self.store.rows(min(100, max(1, int(args.get('limit', 20)))))]}
        if name == 'job_status':
            return present(self.brief(self.store.get(args['job_id'])))
        if name == 'job_result':
            row = self.store.get(args['job_id'])
            result = json.loads(row['result']) if row['result'] else None
            if row['status'] in TERMINAL and row['session_id'] and row['workspace'] and (not result or 'session_usage' not in result):
                try:
                    enriched = self.snapshot_result(row, partial=row['status'] != 'succeeded')
                    if result:
                        enriched = {**enriched, **result, 'session_usage': enriched['session_usage'], 'partial': enriched['partial']}
                    result = enriched
                    self.store.update(row['id'], result=json.dumps(result, ensure_ascii=False), touch=False)
                except Exception:
                    pass
            if result:
                if view == 'full':
                    result['text_chars'] = len(result.get('text', ''))
                    result['truncated'] = result['text_chars'] > min(limit, 60000)
                    result['text'] = result.get('text', '')[:min(limit, 60000)]
            return present(self.brief(row) | {'result': result})
        if name == 'job_wait':
            ids = args['job_ids']
            if not isinstance(ids, list) or not 1 <= len(ids) <= 8:
                raise ValueError('Provide 1–8 job IDs.')
            deadline = time.time() + min(30, max(0, float(args.get('seconds', 25))))
            while True:
                rows = [self.store.get(job_id) for job_id in ids]
                cursor = self.store.event_cursor(ids)
                questions = any(json.loads(r['progress'] or '{}').get('pending_questions') for r in rows)
                changed = args.get('after_event_id') is not None and cursor > args['after_event_id']
                if any(row['status'] in TERMINAL | {'attention_required'} for row in rows) or questions or changed or time.time() >= deadline:
                    return {'jobs': [present(self.brief(row)) for row in rows], 'event_id': cursor, 'changed': bool(changed)}
                with self.changed:
                    self.changed.wait(min(1, max(0, deadline - time.time())))
        if name == 'job_cancel':
            return self.cancel(args['job_id'])
        if name in ('job_questions', 'job_answer'):
            row = self.store.get(args['job_id'])
            if not CONFIG.get('enable_questions', False) or not json.loads(row['spec']).get('questions'):
                raise ValueError('This job did not opt into the question channel.')
            if name == 'job_questions':
                if args.get('action') == 'reconcile':
                    return self.question_channel().reconcile(row['id'])
                return self.question_channel().discover(row['id'])
            with job_lock(row['id']):
                result = self.question_channel().answer(row['id'], args['question_id'], args['answer_id'], args['answers'])
            self.store.record_event(row['id'], 'answer_processed')
            self.notify()
            return result
        if name == 'run_check':
            if bool(args.get('project')) == bool(args.get('job_id')):
                raise ValueError('Provide exactly one of project or job_id.')
            if args.get('job_id'):
                row = self.store.get(args['job_id'])
                if row['status'] not in TERMINAL or not row['workspace']:
                    raise ValueError('Wait for the task to finish before independent checks.')
                if json.loads(row['spec'])['isolation'] == 'readonly':
                    raise ValueError('Shell execution is disabled for readonly jobs.')
                directory = row['workspace']
            else:
                directory = project_path(args['project'])
            command = args['argv']
            if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
                raise ValueError('argv must be a non-empty array of strings; no implicit shell is used.')
            timeout = min(900, max(1, int(args.get('timeout_seconds', 120))))
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run(command, cwd=directory, env=env, capture_output=True, timeout=timeout)
            stdout = result.stdout.decode('utf-8', errors='replace')
            stderr = result.stderr.decode('utf-8', errors='replace')
            output_limit = CONFIG.get('check_output_chars', 2000) if view == 'compact' else 12000
            return {'cwd': directory, 'argv': command, 'exit_code': result.returncode,
                    'stdout': stdout[-output_limit:] if output_limit else '',
                    'stderr': stderr[-output_limit:] if output_limit else '',
                    'stdout_chars': len(stdout), 'stderr_chars': len(stderr),
                    'truncated': len(stdout) > output_limit or len(stderr) > output_limit, 'view': view}
        if name == 'job_diff':
            row, patch, path = self.diff(args['job_id'])
            limit = min(100000, max(1000, int(args.get('max_chars', 20000))))
            text = patch.decode('utf-8', errors='replace')
            return {'job_id': row['id'], 'patch_path': path, 'patch': text[:limit], 'truncated': len(text) > limit}
        if name == 'job_verify':
            row = self.store.get(args['job_id'])
            contract = json.loads(row['spec']).get('contract')
            action = args.get('action', 'run')
            if action not in ('run', 'inspect'):
                raise ValueError('Verification action must be run or inspect.')
            if action == 'inspect' and ('checks' in args or 'timeout_seconds' in args):
                raise ValueError('Inspection does not accept check execution arguments.')
            checks = args.get('checks')
            if checks is None:
                checks = (contract or {}).get('checks', [])
            record = (self.artifacts().inspect_verification(row) if action == 'inspect' else
                      self.artifacts().verify(row, checks, contract, timeout=min(900, max(1, int(args.get('timeout_seconds', 120))))))
            if view == 'full':
                return record
            # Full evidence remains on disk; compact responses never imply independent code review.
            summary = {key: record[key] for key in ('job_id', 'verification_state', 'passed', 'reason',
                       'changed_during_checks', 'patch_sha256', 'workspace_sha256', 'attempt_count', 'verification_path')}
            for key in ('evidence_scope', 'freshness_checked'):
                if key in record:
                    summary[key] = record[key]
            summary['checks'] = []
            output_limit = CONFIG.get('check_output_chars', 2000)
            for check in record['checks']:
                item = {key: check.get(key) for key in ('argv', 'exit_code', 'passed', 'timed_out', 'error', 'duration_seconds')}
                for stream in ('stdout', 'stderr'):
                    output = check.get(stream, '')
                    retained = output_limit if not check.get('passed') else min(256, output_limit)
                    item[stream] = output[-retained:] if retained else ''
                    item[stream + '_truncated'] = bool(check.get(stream + '_truncated')) or len(output) > retained
                summary['checks'].append(item)
            return summary | {'view': 'compact', 'detail_hint': 'Use action=inspect, view=full to read retained evidence without rerunning checks.'}
        if name == 'job_cleanup':
            row = self.store.get(args['job_id'])
            return self.artifacts().cleanup(row, json.loads(row['spec'])['project'],
                                            dry_run=args.get('dry_run', True), discard=args.get('discard', False))
        if name == 'job_recover':
            action = args.get('action', 'inspect')
            with job_lock(args['job_id']):
                result = recover_job(self.store, oc, self.notify, args['job_id'], action)
                if action == 'reattach' and result.get('eligible'):
                    row = self.store.get(args['job_id'])
                    if row['status'] in {'starting', 'admitting'}:
                        return result | {'scheduled': False, 'reason': 'Admission is uncertain; inspect or abort.'}
                    if row['status'] == 'attention_required' and not row['cancel_requested']:
                        self.store.update(row['id'], status='running', error=None)
                    self.executor.submit(self.execute_job, row['id'], True)
                    result['scheduled'] = True
                    self.notify()
            return result
        if name == 'job_apply':
            row = self.store.get(args['job_id'])
            if row['status'] != 'succeeded':
                raise ValueError('Only successful jobs may be integrated.')
            spec = json.loads(row['spec'])
            return self.artifacts().apply(row, spec['project'], spec.get('contract'),
                                          require_verification=bool(spec.get('contract')) or CONFIG.get('require_verification', False),
                                          expected_patch_sha256=args.get('patch_sha256'))
        raise ValueError('Unknown tool: ' + name)


def schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False}


S = {'type': 'string'}
I = {'type': 'integer'}
B = {'type': 'boolean'}
V = {'type': 'string', 'enum': ['compact', 'full']}
TOOLS = [
    ('bridge_health', 'Check WSL OpenCode readiness, shared queue, limits and project roots.', schema()),
    ('bridge_models', 'List explicitly allowed provider models; optional refresh bypasses the TTL cache. Visibility is not billing entitlement.', schema({'refresh': B})),
    ('project_prepare', 'Check a Linux/Windows project path; optionally create it and initialize an empty Git baseline.',
     schema({'path': S, 'create': B, 'initialize_git': B}, ['path'])),
    ('job_submit', 'Queue one independent OpenCode task. Use worktree for parallel writes; unique request_id makes retries idempotent.',
     schema({'project': S, 'task': S, 'owner': S, 'model': S, 'profile': S, 'contract': {'type': 'object'}, 'questions': B, 'request_id': S,
             'brief': {'type': 'object'}, 'risk': {'type': 'string', 'enum': ['low', 'standard', 'high']},
             'isolation': {'type': 'string', 'enum': ['worktree', 'readonly', 'exclusive']}, 'timeout_seconds': I}, ['project', 'task', 'request_id'])),
    ('job_list', 'List recent tasks; compact by default, view=full for session/workspace metadata.', schema({'limit': I, 'view': V})),
    ('job_status', 'Read task status; compact by default, view=full for session/workspace metadata.', schema({'job_id': S, 'view': V}, ['job_id'])),
    ('job_result', 'Retrieve task output, cumulative session usage, and partial timeout diagnostics. Success still needs independent verification.',
     schema({'job_id': S, 'max_chars': I, 'view': V}, ['job_id'])),
    ('job_wait', 'Wait up to 30 seconds for any of 1–8 tasks to finish; omit already completed IDs on subsequent waits.',
     schema({'job_ids': {'type': 'array', 'items': S, 'minItems': 1, 'maxItems': 8}, 'seconds': I, 'after_event_id': I, 'view': V}, ['job_ids'])),
    ('job_cancel', 'Cancel the specified queued/running job without cancelling its siblings.', schema({'job_id': S}, ['job_id'])),
    ('job_recover', 'Inspect uncertain execution, request abort, or reattach without replaying a prompt.',
     schema({'job_id': S, 'action': {'type': 'string', 'enum': ['inspect', 'abort', 'reattach']}}, ['job_id'])),
    ('job_verify', 'Run independent checks and persist evidence, or action=inspect to read retained evidence without rerunning. Inspection does not assert freshness.',
     schema({'job_id': S, 'checks': {'type': 'array', 'items': {'type': 'array', 'items': S}}, 'timeout_seconds': I, 'view': V,
             'action': {'type': 'string', 'enum': ['run', 'inspect']}}, ['job_id'])),
    ('job_cleanup', 'Preview or remove an owned finished worktree. Unintegrated artifacts need explicit discard.',
     schema({'job_id': S, 'dry_run': B, 'discard': B}, ['job_id'])),
    ('job_questions', 'Inspect pending native clarification questions, or reconcile an uncertain answer without resending.',
     schema({'job_id': S, 'action': {'type': 'string', 'enum': ['inspect', 'reconcile']}}, ['job_id'])),
    ('job_answer', 'Answer one pending clarification with an idempotency key; no permission escalation.',
     schema({'job_id': S, 'question_id': S, 'answer_id': S, 'answers': {'type': 'array', 'items': {'type': 'array', 'items': S}}}, ['job_id', 'question_id', 'answer_id', 'answers'])),
    ('run_check', 'Run an explicit verification command in a project or finished writing-task workspace. No model call or implicit shell; returns actual exit code.',
     schema({'project': S, 'job_id': S, 'argv': {'type': 'array', 'items': S, 'minItems': 1}, 'timeout_seconds': I, 'view': V}, ['argv'])),
    ('job_diff', 'Inspect the complete patch artifact of a finished worktree task; output is bounded.',
     schema({'job_id': S, 'max_chars': I}, ['job_id'])),
    ('job_apply', 'Apply a reviewed successful worktree patch to its source directory after git apply --check. No commit or push.',
     schema({'job_id': S, 'patch_sha256': S}, ['job_id'])),
]


def broker_url():
    return f"http://127.0.0.1:{CONFIG['broker_port']}"


def broker_call(name, args=None, timeout=40):
    return http(broker_url() + '/call', 'POST', {'name': name, 'arguments': args or {}},
                {'Authorization': 'Bearer ' + credential('broker-token')}, timeout)


def ensure_broker():
    init_state()
    with (STATE / 'startup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            response = broker_call('bridge_health', timeout=3)
            if response.get('ok'):
                return response
            raise RuntimeError(response.get('error', 'Broker health failed.'))
        except Exception:
            pass
        with socket.socket() as probe:
            if probe.connect_ex(('127.0.0.1', CONFIG['broker_port'])) == 0:
                raise RuntimeError('Broker port occupied or unhealthy. No duplicate broker was started.')
        with (STATE / 'broker.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'broker'],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        for _ in range(150):
            time.sleep(.2)
            try:
                response = broker_call('bridge_health', timeout=2)
                if response.get('ok'):
                    return response
            except Exception:
                if child.poll() is not None:
                    raise RuntimeError('Broker failed to start; inspect private broker.log.')
        raise RuntimeError('Broker startup timed out.')


def serve_broker():
    init_state()
    with (STATE / 'broker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        broker = Broker()
        token = credential('broker-token')

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if self.path != '/call' or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    self.send_error(403)
                    return
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 1_000_000:
                        raise ValueError('Invalid request size.')
                    request = json.loads(self.rfile.read(size))
                    result = {'ok': True, 'data': broker.call(request['name'], request.get('arguments', {}))}
                except Exception as error:
                    result = {'ok': False, 'error': str(error)}
                payload = encode(result)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                with contextlib.suppress(BrokenPipeError):
                    self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', CONFIG['broker_port']), Handler)
        server.daemon_threads = True
        (STATE / 'broker-pid').write_text(str(os.getpid()))
        threading.Thread(target=broker.schedule, daemon=True).start()
        server.serve_forever()


def mcp():
    output_lock = threading.Lock()
    startup_lock = threading.Lock()
    ready = threading.Event()

    def handle(request):
        request_id = request.get('id')
        if request_id is None:
            return
        try:
            method = request['method']
            if method == 'initialize':
                result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                          'capabilities': {'tools': {'listChanged': False}},
                          'serverInfo': {'name': 'codex-opencode-wsl-bridge', 'version': VERSION}, 'instructions': INSTRUCTIONS}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': description, 'inputSchema': fields}
                                    for name, description, fields in TOOLS]}
                read_only = {'bridge_models', 'job_list', 'job_status', 'job_result', 'job_wait'}
                for tool in result['tools']:
                    tool['annotations'] = {'readOnlyHint': tool['name'] in read_only,
                                           'destructiveHint': tool['name'] in ('job_cancel', 'job_apply', 'job_cleanup'),
                                           'idempotentHint': tool['name'] in read_only | {'job_cancel', 'job_apply', 'job_answer'},
                                           'openWorldHint': tool['name'] == 'job_submit'}
            elif method == 'tools/call':
                if not ready.is_set():
                    with startup_lock:
                        if not ready.is_set():
                            ensure_broker()
                            ready.set()
                params = request['params']
                arguments = params.get('arguments', {})
                timeout = 40
                if params['name'] in ('job_verify', 'run_check'):
                    checks = arguments.get('checks')
                    if params['name'] == 'job_verify' and checks is None:
                        # The stored contract may contain multiple checks. Keep enough transport
                        # headroom; operation deadlines remain enforced by the broker.
                        count = 20
                    else:
                        count = max(1, len(checks or []))
                    timeout = min(18020, count * min(900, max(1, int(arguments.get('timeout_seconds', 120))))) + 20
                response = broker_call(params['name'], arguments, timeout=timeout)
                data = response.get('data') if response['ok'] else {'error': response['error']}
                result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}],
                          'isError': not response['ok']}
            else:
                raise ValueError('Unsupported MCP method: ' + method)
            message = {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except Exception as error:
            message = {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32603, 'message': str(error)}}
        with output_lock:
            sys.stdout.buffer.write(encode(message) + b'\n')
            sys.stdout.buffer.flush()

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        for line in sys.stdin.buffer:
            try:
                request = json.loads(line)
            except ValueError:
                continue
            executor.submit(handle, request)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['mcp', 'broker', 'doctor', 'call', 'restart', 'stop'])
    parser.add_argument('tool', nargs='?')
    parser.add_argument('arguments', nargs='?', default='{}')
    parser.add_argument('--arguments-file')
    args = parser.parse_args()
    if args.mode == 'mcp':
        mcp()
    elif args.mode == 'broker':
        serve_broker()
    elif args.mode in ('restart', 'stop'):
        health = ensure_broker()
        counts = health['data']['counts']
        if any(counts.get(status, 0) for status in ACTIVE) or counts.get('queued', 0):
            raise RuntimeError('Finish or cancel active/queued jobs before restarting the broker.')
        pid = int((STATE / 'broker-pid').read_text())
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        if str(Path(__file__).resolve()).encode() not in command or b'broker' not in command:
            raise RuntimeError('Recorded PID no longer belongs to this broker; nothing was stopped.')
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(.1)
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', CONFIG['broker_port'])) != 0:
                    break
        print(json.dumps(ensure_broker() if args.mode == 'restart' else {'stopped': True}, ensure_ascii=False, indent=2))
    elif args.mode == 'doctor':
        print(json.dumps(ensure_broker(), ensure_ascii=False, indent=2))
    else:
        ensure_broker()
        raw = Path(args.arguments_file).read_text(encoding='utf-8-sig') if args.arguments_file else args.arguments
        print(json.dumps(broker_call(args.tool, json.loads(raw)), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
