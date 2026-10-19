#!/usr/bin/env python3
"""Reviewed control-plane helpers for the Codex OpenCode bridge. Python standard library only.

This module is intentionally standalone: it never imports ``bridge`` so the broker can import it
without risking a ``__main__`` import cycle. The coordinator wires the pieces together and passes
the same duck-typed collaborators used by the broker:

* ``store``  - a ``bridge.Store``-compatible object exposing ``get``/``update``/``rows``/``connect``.
* ``native`` - ``oc(path, method='GET', body=None, directory=None, timeout=15)``.
* ``notify`` - a zero-argument callable that wakes waiters.

Catalog visibility is not billing entitlement; this module never touches provider auth.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import urllib.parse

__all__ = [
    'TERMINAL', 'ACTIVE', 'ModelCatalog', 'cancel_job', 'recover_job',
    'QuestionChannel', 'quota_block_reason', '_brief',
]

TERMINAL = {'succeeded', 'failed', 'cancelled', 'timed_out'}
ACTIVE = {'starting', 'admitting', 'running', 'cancelling', 'attention_required'}
_JOB_FIELDS = {'status', 'session_id', 'workspace', 'base_commit', 'result', 'error',
               'cancel_requested', 'started', 'progress'}

_MAX_QUESTIONS = 10
_MAX_OPTIONS = 10
_MAX_TEXT = 500


def _now():
    return time.time()


def _cfg(config, key, default=None):
    """Read a key from a dict-like or attribute-style config object."""
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, 'get', None)
    if callable(getter):
        try:
            return getter(key, default)
        except TypeError:
            return getter(key) or default
    return getattr(config, key, default)


def _call(notify, *args):
    if notify is not None:
        notify(*args)


def _bounded(value, limit=_MAX_TEXT):
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text if len(text) <= limit else text[:limit]


def _brief(row, **extra):
    """Minimal brief that mirrors the broker's row schema without importing bridge."""
    row = dict(row)
    spec = {}
    raw = row.get('spec')
    if raw:
        try:
            spec = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            spec = {}
    brief = {
        'id': row.get('id'), 'status': row.get('status'), 'created': row.get('created'),
        'updated': row.get('updated'), 'session_id': row.get('session_id'),
        'workspace': row.get('workspace'), 'base_commit': row.get('base_commit'),
        'error': row.get('error'), 'cancel_requested': row.get('cancel_requested'),
        'owner': spec.get('owner'), 'project': spec.get('project'),
        'model': spec.get('model'), 'isolation': spec.get('isolation'),
        'request_id': spec.get('request_id'),
    }
    brief.update(extra)
    return brief


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------

def _provider_items(raw):
    """Normalize /provider (or a CLI listing) into a list of (provider_id, [model_id])."""
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ('all', 'connected', 'providers', 'data'):
            value = raw.get(key)
            if isinstance(value, list):
                items.extend(value)
        if not items and ('id' in raw or 'providerID' in raw):
            items = [raw]
    pairs = []
    for provider in items:
        if not isinstance(provider, dict):
            continue
        pid = provider.get('id') or provider.get('providerID') or provider.get('name')
        if not pid:
            continue
        models = provider.get('models')
        mids = []
        if isinstance(models, dict):
            mids = list(models.keys())
        elif isinstance(models, list):
            mids = [m.get('id') if isinstance(m, dict) else m for m in models]
        mids = [m for m in mids if isinstance(m, str) and m]
        pairs.append((pid, mids))
    return pairs


class ModelCatalog:
    """Cached, allowlist-filtered view of discoverable OpenCode models."""

    def __init__(self, native, config=None, runner=None, clock=time.monotonic):
        self.native = native
        self.config = config
        self.runner = runner or subprocess.run
        self.clock = clock
        self._lock = threading.RLock()
        self._cache = None

    # -- infrastructure ----------------------------------------------------
    def _allowed(self):
        providers = _cfg(self.config, 'allowed_providers', ['opencode-go']) or []
        return {p for p in providers if p}

    def _allowed_models(self):
        allowed = _cfg(self.config, 'allowed_models', None)
        return set(allowed) if allowed is not None else None

    def _ttl(self):
        try:
            return max(0.0, float(_cfg(self.config, 'model_cache_seconds', 60) or 60))
        except (TypeError, ValueError):
            return 60.0

    def _row_for(self, pid, mid, kind, meta=None):
        full = f'{pid}/{mid}'
        entry = {'id': full, 'provider': pid, 'model': mid, 'kind': kind}
        if isinstance(meta, dict) and meta.get('name'):
            entry['name'] = _bounded(meta.get('name'))
        return full, entry

    def _select(self, pairs, kind):
        models, entries = [], []
        allowed, allowed_models = self._allowed(), self._allowed_models()
        for pid, mids in pairs:
            if pid not in allowed:
                continue
            for mid in mids:
                full = f'{pid}/{mid}'
                if allowed_models is not None and full not in allowed_models:
                    continue
                if full in models:
                    continue
                models.append(full)
                entries.append({'id': full, 'provider': pid, 'model': mid, 'kind': kind})
        return models, entries

    def _fetch_native(self):
        raw = self.native('/provider', 'GET', None, None)
        pairs = []
        known = {}
        for pid, mids in _provider_items(raw):
            known.setdefault(pid, [])
            known[pid].extend(mids)
        # Preserve metadata names where the provider shape exposes them.
        meta = {}
        for provider in (raw.get('all') if isinstance(raw, dict) else None) or []:
            if isinstance(provider, dict) and isinstance(provider.get('models'), dict):
                pid = provider.get('id') or provider.get('providerID') or provider.get('name')
                for mid, value in provider['models'].items():
                    meta[(pid, mid)] = value if isinstance(value, dict) else None
        connected = raw.get('connected') if isinstance(raw, dict) else None
        for pid, mids in known.items():
            if isinstance(connected, list) and _cfg(self.config, 'connected_only', True) and pid not in connected:
                continue
            pairs.append((pid, sorted(set(mids))))
        models, entries = self._select(pairs, 'native')
        for entry in entries:
            value = meta.get((entry['provider'], entry['model']))
            entry['connected'] = entry['provider'] in connected if isinstance(connected, list) else None
            if isinstance(value, dict):
                for field in ('name', 'limit', 'capabilities', 'cost'):
                    if field in value:
                        entry[field] = value[field]
        return models, entries

    def _fetch_cli(self):
        binary = _cfg(self.config, 'opencode_bin') or 'opencode'
        result = self.runner([binary, 'models'], capture_output=True, text=True, timeout=30)
        if getattr(result, 'returncode', 1):
            raise RuntimeError(f'opencode models exited {getattr(result, "returncode", "?")}')
        pairs = {}
        for line in (result.stdout or '').splitlines():
            text = line.strip()
            if '/' not in text:
                continue
            pid, mid = text.split('/', 1)
            if not pid or not mid:
                continue
            pairs.setdefault(pid, set()).add(mid.split()[0])
        normalized = [(pid, sorted(mids)) for pid, mids in pairs.items()]
        return self._select(normalized, 'cli')

    def _fetch(self):
        errors = []
        try:
            models, entries = self._fetch_native()
            if models:
                return models, entries
            errors.append('native /provider returned no allowed models')
        except Exception as error:  # noqa: BLE001 - transient discovery failure
            errors.append('native /provider failed: ' + str(error)[:200])
        try:
            models, entries = self._fetch_cli()
            if models:
                return models, entries
            errors.append('CLI returned no allowed models')
        except Exception as error:  # noqa: BLE001
            errors.append('CLI failed: ' + str(error)[:200])
        raise RuntimeError('; '.join(errors))

    def _view(self, cache, stale, warning=None):
        view = {
            'models': list(cache['models']),
            'default': cache['default'],
            'entries': [dict(entry) for entry in cache['entries']],
            'fetched_at': cache['fetched_at'],
            'stale': stale,
        }
        if warning:
            view['warning'] = warning
        return view

    # -- public API --------------------------------------------------------
    def list(self, refresh=False):  # noqa: A003 - matches bridge naming
        with self._lock:
            cache = self._cache
            now = self.clock()
            if cache and not refresh and (now - cache['fetched_at']) < self._ttl():
                return self._view(cache, stale=False)
            try:
                models, entries = self._fetch()
            except Exception as error:  # noqa: BLE001
                if cache:
                    return self._view(cache, stale=True,
                                      warning='refresh failed; serving previous catalog: ' + str(error)[:300])
                raise RuntimeError(f'Cannot discover OpenCode models: {error}') from None
            self._cache = {
                'models': sorted(models),
                'entries': sorted(entries, key=lambda item: item['id']),
                'default': _cfg(self.config, 'default_model'),
                'fetched_at': now,
            }
            return self._view(self._cache, stale=False)

    def validate(self, model):
        """True when *model* is currently visible in the (allowlisted) catalog."""
        return model in self.list()['models']


# ---------------------------------------------------------------------------
# Cancel / recover
# ---------------------------------------------------------------------------

def _session_evidence(native, sid, workspace):
    """Best-effort native evidence. Absence from the status map proves nothing."""
    evidence = {'session_id': sid, 'native_status': None, 'native_idle': False,
                'status_map_found': False, 'message_count': None, 'last_finish': None,
                'last_completed': None, 'last_error': None, 'final_complete': False,
                'notes': []}
    statuses = None
    try:
        statuses = native('/session/status', 'GET', None, workspace)
    except Exception as error:  # noqa: BLE001
        evidence['notes'].append('session/status unavailable: ' + str(error)[:200])
    if isinstance(statuses, dict):
        evidence['status_map_found'] = True
        entry = statuses.get(sid)
        if isinstance(entry, dict):
            evidence['native_status'] = entry.get('type')
        elif sid in statuses:
            evidence['native_status'] = statuses[sid]
        if evidence['native_status'] == 'idle':
            evidence['native_idle'] = True
        elif not evidence['native_status']:
            evidence['notes'].append('session absent from status map; absence alone does not prove stopped')
    messages = None
    try:
        messages = native(f'/session/{sid}/message?limit=2', 'GET', None, workspace)
    except Exception as error:  # noqa: BLE001
        evidence['notes'].append('messages unavailable: ' + str(error)[:200])
    if isinstance(messages, list):
        evidence['message_count'] = len(messages)
        assistants = [m for m in messages
                      if isinstance(m, dict) and (m.get('info') or {}).get('role') == 'assistant']
        if assistants:
            info = assistants[-1].get('info') or {}
            evidence['last_finish'] = info.get('finish')
            evidence['last_completed'] = (info.get('time') or {}).get('completed')
            if info.get('error'):
                evidence['last_error'] = _bounded(info['error'])
            evidence['final_complete'] = (evidence['last_finish'] not in (None, 'tool-calls')
                                          and bool(evidence['last_completed']))
    return evidence


def _abort_and_reconcile(store, native, job_id, sid, workspace):
    """Abort a native session outside any DB transaction, then reconcile the stored status."""
    abort_error = None
    try:
        native(f'/session/{sid}/abort', 'POST', {}, workspace)
    except Exception as error:  # noqa: BLE001
        abort_error = str(error)
    evidence = _session_evidence(native, sid, workspace)
    idle = evidence.get('native_idle') is True or (evidence.get('final_complete') and evidence.get('native_status') not in ('busy', 'running', 'retry'))
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        fresh = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        now = _now()
        if fresh['status'] in TERMINAL:
            outcome = 'terminal_preserved'  # never overwrite a concurrent completion
        elif abort_error:
            connection.execute("UPDATE jobs SET status='attention_required',error=?,updated=? WHERE id=?",
                               (('abort not confirmed: ' + abort_error)[:2500], now, job_id))
            outcome = 'attention_required'
        elif idle:
            connection.execute("UPDATE jobs SET status='cancelled',updated=? WHERE id=?", (now, job_id))
            outcome = 'cancelled'
        else:
            connection.execute("UPDATE jobs SET status='attention_required',error=?,updated=? WHERE id=?",
                               ('Native termination could not be confirmed; inspect or reattach.', now, job_id))
            outcome = 'attention_required'
    return abort_error, evidence, outcome


def cancel_job(store, native, notify, job_id):
    """Replacement for ``bridge.Broker.cancel`` with transactional, evidence-based semantics."""
    with store.connect() as connection:
        connection.execute('BEGIN IMMEDIATE')
        row = connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            raise ValueError('Unknown job ID.')
        row = dict(row)
        status = row['status']
        if status in TERMINAL:
            pass  # already finished; leave untouched
        elif status == 'queued':
            connection.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,updated=? WHERE id=?",
                               (_now(), job_id))
        else:
            # starting/admitting without a session may still be admitted by a worker, so only
            # request cancellation; the worker observes cancel_requested and finalizes.
            connection.execute("UPDATE jobs SET status='cancelling',cancel_requested=1,updated=? WHERE id=?",
                               (_now(), job_id))

    if status in TERMINAL or status == 'queued' or not row.get('session_id'):
        _call(notify)
        return _brief(store.get(job_id))

    abort_error, evidence, outcome = _abort_and_reconcile(
        store, native, job_id, row['session_id'], row.get('workspace'))
    _call(notify)
    return _brief(store.get(job_id), abort_confirmed=abort_error is None, outcome=outcome,
                  native_idle=bool(evidence.get('native_idle')),
                  note='Native abort is best-effort; OS descendants are not claimed stopped.')


def _inspect_view(row, evidence):
    status = row['status']
    sid = row.get('session_id')
    raw_error = row.get('error')
    final_complete = bool(evidence and evidence.get('final_complete'))
    last_error = evidence.get('last_error') if evidence else None
    if status == 'attention_required':
        error_code = 'attention_required'
    elif last_error:
        error_code = 'model_error'
    elif raw_error:
        error_code = {'failed': 'execution_failed', 'timed_out': 'timeout'}.get(status, 'error')
    else:
        error_code = None
    unknown_admission = status in ('starting', 'admitting') and not sid
    if status == 'succeeded':
        next_action = 'complete'
    elif status in ('failed', 'timed_out', 'cancelled'):
        next_action = 'none'
    elif status == 'attention_required':
        next_action = 'reconcile_or_abort'
    elif unknown_admission:
        next_action = 'inspect_unknown_admission'
    elif final_complete:
        next_action = 'reconcile_terminal'
    elif status in ('running', 'cancelling', 'starting', 'admitting'):
        next_action = 'wait'
    else:
        next_action = 'inspect'
    retryable = status in ('failed', 'timed_out') and not unknown_admission and not final_complete
    error_text = raw_error
    if not error_text and last_error:
        try:
            error_text = json.dumps(last_error, ensure_ascii=False)[:500]
        except (TypeError, ValueError):
            error_text = str(last_error)[:500]
    return _brief(row) | {
        'error_code': error_code,
        'error': error_text,
        'next_action': next_action,
        'retryable': retryable,
        'final_complete': final_complete,
        'admission': 'unknown' if unknown_admission else ('admitted' if sid else 'not_admitted'),
        'evidence': evidence or {'session_id': sid, 'notes': ['no session recorded']},
        'note': 'Absence from the active-session map alone does not prove the session stopped.',
    }


def _plan_reattach(store, job_id):
    row = dict(store.get(job_id))
    status = row['status']
    sid, workspace = row.get('session_id'), row.get('workspace')
    unknown = status in ('starting', 'admitting') and not sid
    eligible = bool(sid and workspace and status not in TERMINAL and not unknown)
    if eligible:
        reason = 'eligible'
    elif unknown:
        reason = 'unknown admission; never replay'
    elif status in TERMINAL:
        reason = 'already terminal'
    else:
        reason = 'no admitted session'
    return {'job_id': job_id, 'eligible': eligible, 'reason': reason, 'status': status,
            'session_id': sid, 'workspace': workspace, 'scheduled_by_parent': True,
            'note': 'Reattach eligibility only: no prompt was sent. The parent schedules recovery.'}


def recover_job(store, native, notify, job_id, action='inspect'):
    """Inspect, abort, or evaluate reattachment for a job without replaying prompts."""
    if action not in ('inspect', 'abort', 'reattach'):
        raise ValueError('Unsupported recovery action.')
    if action == 'reattach':
        return _plan_reattach(store, job_id)

    row = dict(store.get(job_id))
    sid, workspace = row.get('session_id'), row.get('workspace')
    if action == 'abort':
        if row['status'] in TERMINAL:
            evidence = _session_evidence(native, sid, workspace) if sid else None
            return _inspect_view(row, evidence) | {'aborted': False, 'reason': 'already terminal'}
        if not sid:
            with store.connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                fresh = dict(connection.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
                if fresh['status'] not in TERMINAL:
                    connection.execute(
                        "UPDATE jobs SET cancel_requested=1,status='cancelling',updated=? WHERE id=?",
                        (_now(), job_id))
            _call(notify)
            return _inspect_view(dict(store.get(job_id)), None) | {
                'aborted': False, 'reason': 'no admitted session to abort'}
        with store.connect() as connection:
            connection.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job_id,))
        abort_error, evidence, outcome = _abort_and_reconcile(store, native, job_id, sid, workspace)
        _call(notify)
        return _inspect_view(dict(store.get(job_id)), evidence) | {
            'aborted': abort_error is None, 'outcome': outcome, 'abort_error': abort_error}
    evidence = _session_evidence(native, sid, workspace) if sid else None
    return _inspect_view(row, evidence)


# ---------------------------------------------------------------------------
# Question channel (opt-in)
# ---------------------------------------------------------------------------

def _bounded_question(item, qid, session_id):
    nested = item.get('questions')
    if isinstance(nested, list):
        groups = [_bounded_question(q, qid, session_id) for q in nested[:_MAX_QUESTIONS] if isinstance(q, dict)]
        return {'id': qid, 'session_id': session_id, 'questions': groups,
                'text': '\n'.join(q.get('text') or '' for q in groups)[:_MAX_TEXT],
                'options': [], 'multiple': False, 'custom': True}
    options = []
    raw_options = item.get('options')
    if isinstance(raw_options, list):
        for option in raw_options[:_MAX_OPTIONS]:
            if isinstance(option, dict):
                options.append({'id': option.get('id') or option.get('value'),
                                'label': _bounded(option.get('label') or option.get('title')
                                                  or option.get('text'))})
            elif isinstance(option, str):
                options.append({'id': option, 'label': _bounded(option)})
    return {
        'id': qid, 'session_id': session_id,
        'header': _bounded(item.get('header') or item.get('title')),
        'text': _bounded(item.get('text') or item.get('question') or item.get('prompt')),
        'options': options,
        'multiple': bool(item.get('multiple') or item.get('multiSelect')),
        'custom': bool(item.get('custom')),
        'asked_at': item.get('time') or item.get('created'),
    }


class QuestionChannel:
    """Opt-in pending-question discovery/answering with per-answer idempotency.

    Stored records keep only a digest of the answer payload and a status; question content is
    never persisted here. The parent owns scheduler slots and human interaction.
    """

    def __init__(self, store, native, notify=None):
        self.store = store
        self.native = native
        self.notify = notify
        self._lock = threading.RLock()
        self._ensure_schema()

    def _ensure_schema(self):
        with self.store.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS question_answers (
                job_id TEXT NOT NULL, question_id TEXT NOT NULL, answer_id TEXT NOT NULL,
                answer_sha TEXT NOT NULL, status TEXT NOT NULL, error TEXT,
                created REAL NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY (job_id, question_id, answer_id))''')

    def _question_items(self, session_id, workspace):
        path = '/question?sessionID=' + urllib.parse.quote(session_id, safe='')
        raw = self.native(path, 'GET', None, workspace)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ('questions', 'data', 'items'):
                if isinstance(raw.get(key), list):
                    return raw[key]
        raise RuntimeError('Unrecognized native question response; capability is uncertain.')

    def discover(self, job_id=None, session_id=None, workspace=None):
        if job_id is not None:
            row = dict(self.store.get(job_id))
            session_id = session_id or row.get('session_id')
            workspace = workspace or row.get('workspace')
        if not session_id:
            return {'session_id': None, 'workspace': workspace, 'questions': [], 'count': 0,
                    'note': 'No session recorded; nothing to discover.'}
        try:
            items = self._question_items(session_id, workspace)
        except Exception as error:  # noqa: BLE001
            return {'session_id': session_id, 'workspace': workspace, 'questions': [], 'count': 0,
                    'capability_uncertain': True,
                    'error': 'question discovery failed: ' + str(error)[:300]}
        questions = []
        for item in items[:_MAX_QUESTIONS]:
            if not isinstance(item, dict):
                continue
            qid = item.get('id') or item.get('questionID') or item.get('question_id')
            if not qid:
                continue
            qsid = item.get('sessionID') or item.get('session_id')
            if qsid != session_id:
                continue
            questions.append(_bounded_question(item, qid, session_id))
        return {'session_id': session_id, 'workspace': workspace,
                'questions': questions, 'count': len(questions)}

    def _set_status(self, key, status, error=None):
        with self.store.connect() as connection:
            connection.execute('UPDATE question_answers SET status=?,error=?,updated=? '
                               'WHERE job_id=? AND question_id=? AND answer_id=?',
                               (status, error, _now(), *key))

    def answer(self, job_id, question_id, answer_id='', answers=None):
        if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in (question_id, answer_id)):
            raise ValueError('Bounded question_id and idempotency answer_id are required.')
        if (not isinstance(answers, list) or not answers or len(answers) > 10
                or any(not isinstance(group, list) or len(group) > 20
                       or any(not isinstance(value, str) or len(value) > 4000 for value in group)
                       for group in answers)):
            raise ValueError('answers must be a bounded list of lists of strings.')
        row = dict(self.store.get(job_id))
        session_id, workspace = row.get('session_id'), row.get('workspace')
        if not session_id:
            return {'sent': False, 'confirmed': False, 'reason': 'no_session'}
        digest = hashlib.sha256(json.dumps(answers, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # Serialize discovery + reservation. A different answer_id cannot evade a prior reservation.
        with self._lock, self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            previous = connection.execute('SELECT * FROM question_answers WHERE job_id=? AND question_id=?',
                                          (job_id, question_id)).fetchone()
            if previous:
                if previous['answer_id'] != answer_id or previous['answer_sha'] != digest:
                    raise ValueError('Question already has a different immutable answer reservation.')
                confirmed = previous['status'] == 'confirmed'
                return {'sent': False, 'confirmed': confirmed, 'already_answered': confirmed,
                        'requires_reconciliation': not confirmed, 'answer_id': answer_id,
                        'reason': 'idempotent' if confirmed else 'requires_reconciliation'}
        if row['status'] in TERMINAL | {'attention_required', 'cancelling'}:
            raise ValueError('Cannot send a new answer to a terminal or uncertain job.')
        pending = self.discover(session_id=session_id, workspace=workspace)
        if pending.get('capability_uncertain'):
            return {'sent': False, 'confirmed': False, 'reason': 'discovery_uncertain',
                    'requires_reconciliation': True, 'error': pending.get('error')}
        if question_id not in {q['id'] for q in pending['questions']}:
            return {'sent': False, 'confirmed': False, 'reason': 'not_pending'}
        with self._lock, self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            prior = connection.execute('SELECT 1 FROM question_answers WHERE job_id=? AND question_id=?',
                                       (job_id, question_id)).fetchone()
            if prior:
                return {'sent': False, 'confirmed': False, 'reason': 'requires_reconciliation',
                        'requires_reconciliation': True}
            now = _now()
            connection.execute('INSERT INTO question_answers(job_id,question_id,answer_id,answer_sha,status,error,created,updated) '
                               'VALUES(?,?,?,?,?,?,?,?)', (job_id, question_id, answer_id, digest, 'pending', None, now, now))
        key = (job_id, question_id, answer_id)
        try:
            response = self.native('/question/' + urllib.parse.quote(question_id, safe='') + '/reply',
                                   'POST', {'answers': answers}, workspace)
            if response is False:
                raise RuntimeError('Native reply was not accepted.')
        except Exception as error:
            self._set_status(key, 'uncertain', str(error)[:500])
            _call(self.notify)
            return {'sent': False, 'confirmed': False, 'reason': 'uncertain', 'uncertain': True,
                    'requires_reconciliation': True, 'answer_id': answer_id, 'error': str(error)[:500]}
        self._set_status(key, 'confirmed')
        _call(self.notify)
        return {'sent': True, 'confirmed': True, 'answer_id': answer_id, 'idempotent': False}

    def reconcile(self, job_id, question_id=None):
        row = dict(self.store.get(job_id))
        session_id, workspace = row.get('session_id'), row.get('workspace')
        pending = self.discover(session_id=session_id, workspace=workspace) if session_id else {
            'questions': []}
        if pending.get('capability_uncertain'):
            return {'reconciled': False, 'requires_reconciliation': True, 'error': pending.get('error')}
        live = {question['id'] for question in pending['questions']}
        confirmed, still_pending = [], []
        with self.store.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            query = "SELECT * FROM question_answers WHERE job_id=? AND status IN ('pending','uncertain')"
            params = [job_id]
            if question_id:
                query += ' AND question_id=?'
                params.append(question_id)
            for record in [dict(r) for r in connection.execute(query, params).fetchall()]:
                if record['question_id'] not in live:
                    connection.execute('UPDATE question_answers SET status=?,updated=? WHERE job_id=? '
                                       'AND question_id=? AND answer_id=?',
                                       ('resolved_unknown', _now(), record['job_id'], record['question_id'],
                                        record['answer_id']))
                    confirmed.append(record['answer_id'])
                else:
                    still_pending.append(record['answer_id'])
        return {'reconciled': not still_pending, 'resolved_unknown': confirmed, 'confirmed': [],
                'still_pending': still_pending, 'requires_reconciliation': bool(still_pending)}


# ---------------------------------------------------------------------------
# Quota eligibility (pure)
# ---------------------------------------------------------------------------

def quota_block_reason(spec, active_specs, config):
    """Return a human-readable block reason, or None when the spec may run.

    Global concurrency is handled elsewhere. Only per-provider and per-model caps are checked;
    every active spec (including attention_required) consumes quota and provider peers are counted
    jointly. Exact full IDs only - no prefix guessing.
    """
    model = spec.get('model') if isinstance(spec, dict) else None
    if not isinstance(model, str) or '/' not in model:
        return 'invalid or missing model id'
    provider = model.split('/', 1)[0]
    active = [item for item in (active_specs or [])
              if isinstance(item, dict) and isinstance(item.get('model'), str)]
    provider_caps = _cfg(config, 'provider_caps', {}) or {}
    cap = provider_caps.get(provider)
    if cap is not None:
        used = sum(1 for item in active if item['model'].split('/', 1)[0] == provider)
        if used >= int(cap):
            return f'provider quota reached: {provider} at {used}/{int(cap)}'
    model_caps = _cfg(config, 'model_caps', {}) or {}
    cap = model_caps.get(model)
    if cap is not None:
        used = sum(1 for item in active if item['model'] == model)
        if used >= int(cap):
            return f'model quota reached: {model} at {used}/{int(cap)}'
    return None
