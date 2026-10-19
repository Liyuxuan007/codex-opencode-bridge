#!/usr/bin/env python3
"""Private, auditable worktree artifacts for the Codex/OpenCode bridge.

This module is deliberately independent of ``bridge.py``.  The coordinator wires
it in and supplies the same ``git(directory, *args, check=True, binary=False,
data=None)`` function used by the bridge.

Security / trust model
----------------------
* Contract path rules (``allowed_paths`` / ``forbidden_paths``) are acceptance
  boundaries used to reject an artifact.  They are **not** an OS sandbox and do
  not confine the agent's process while it works.
* Verification ``checks`` are explicit argv arrays supplied by the trusted
  coordinator, never by the model.  They execute arbitrary commands with the
  coordinator's OS privileges inside the finished worktree.  A shell is never
  added implicitly, but an explicit ``["bash", "-c", ...]`` argv is still an
  arbitrary trusted command.
* No function here commits, pushes, deploys, sends messages, or contacts a
  model/provider.  The only write to a source project is ``git apply``.
* Patches and metadata live under the private state directory.  Worktrees are
  only accepted at ``state/worktrees/<job_id>`` and must be registered Git
  worktrees of the job's project.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import time

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'timed_out'})
MAX_OUTPUT_BYTES = 12000
SCHEMA = 1
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_HEX_COMMIT = re.compile(r'^[0-9a-fA-F]{4,64}$')


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------

def _clean_pattern(value, label):
    if not isinstance(value, str):
        raise ValueError(f'{label} entries must be strings.')
    if not value or value != value.strip():
        raise ValueError(f'{label} entries must be non-empty and have no outer whitespace.')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f'{label} entries must not contain control characters or NUL.')
    while value.startswith('./'):
        value = value[2:]
    if not value:
        raise ValueError(f'{label} entries must not be empty.')
    if value.startswith('/') or value.startswith('\\') or value.startswith('//'):
        raise ValueError(f'{label} entries must be relative: {value!r}')
    if re.match(r'^[A-Za-z]:', value):
        raise ValueError(f'{label} entries must not be drive-absolute: {value!r}')
    if '\\' in value:
        raise ValueError(f'{label} entries must use forward slashes: {value!r}')
    if value.startswith('~'):
        raise ValueError(f'{label} entries must not use home expansion: {value!r}')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'{label} entries must not contain empty, "." or ".." components: {value!r}')
    if any(part.lower() == '.git' for part in parts):
        raise ValueError(f'{label} entries must not target .git: {value!r}')
    return value


def _clean_checks(value):
    if not isinstance(value, list):
        raise ValueError('checks must be a list of argv arrays.')
    result = []
    for argv in value:
        if not isinstance(argv, list) or not argv:
            raise ValueError('Each check must be a non-empty argv array.')
        for index, item in enumerate(argv):
            if not isinstance(item, str) or (index == 0 and not item):
                raise ValueError('Check commands must be non-empty strings.')
            if '\x00' in item:
                raise ValueError('Check arguments must not contain NUL bytes.')
        result.append(list(argv))
    return result


def normalize_contract(value):
    """Validate and normalize a JSON-like artifact contract.

    ``None`` is accepted for compatibility and returned as ``None``.  Otherwise
    the result is ``{allowed_paths, forbidden_paths, checks, max_attempts,
    max_changed_files}``.  Unknown keys are rejected so a typo cannot silently
    weaken acceptance rules.  Path rules are lexical globs, not a sandbox.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('contract must be an object or null.')
    unknown = set(value) - {'allowed_paths', 'forbidden_paths', 'checks',
                            'max_attempts', 'max_changed_files'}
    if unknown:
        raise ValueError('Unknown contract keys: ' + ', '.join(sorted(unknown)))

    def paths(key):
        raw = value.get(key, [])
        if not isinstance(raw, list):
            raise ValueError(f'{key} must be a list of relative glob patterns.')
        return [_clean_pattern(item, key) for item in raw]

    max_attempts = value.get('max_attempts', 2)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 2:
        raise ValueError('max_attempts must be an integer from 1 to 2.')

    max_changed_files = value.get('max_changed_files', 20)
    if (isinstance(max_changed_files, bool) or not isinstance(max_changed_files, int)
            or max_changed_files < 0):
        raise ValueError('max_changed_files must be a non-negative integer.')

    return {
        'allowed_paths': paths('allowed_paths'),
        'forbidden_paths': paths('forbidden_paths'),
        'checks': _clean_checks(value.get('checks', [])),
        'max_attempts': max_attempts,
        'max_changed_files': max_changed_files,
    }


# ---------------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def _glob_components(pattern):
    return tuple(pattern.split('/'))


def _match_parts(path, pattern):
    """Anchor a component-wise glob to the full relative path.

    ``*``/``?`` never cross ``/``.  A ``**`` component matches zero or more
    components.  A pattern without a slash matches a basename at any depth
    (gitignore-like); a pattern with a slash is anchored at the repository root.
    """
    @functools.lru_cache(maxsize=None)
    def walk(i, j):
        if j == len(pattern):
            return i == len(path)
        if pattern[j] == '**':
            return walk(i, j + 1) or (i < len(path) and walk(i + 1, j))
        return (i < len(path) and fnmatch.fnmatchcase(path[i], pattern[j])
                and walk(i + 1, j + 1))
    return walk(0, 0)


def _path_matches(path, pattern):
    parts = tuple(path.split('/'))
    if '/' not in pattern:
        return bool(parts) and fnmatch.fnmatchcase(parts[-1], pattern)
    return _match_parts(parts, _glob_components(pattern))


def _contract_error(contract, changed_files):
    if contract is None:
        return None
    if len(changed_files) > contract['max_changed_files']:
        return ('changed file count %d exceeds max_changed_files %d'
                % (len(changed_files), contract['max_changed_files']))
    for path in changed_files:
        if any(_path_matches(path, pattern) for pattern in contract['forbidden_paths']):
            return f'changed path is forbidden by contract: {path}'
        if contract['allowed_paths'] and not any(
                _path_matches(path, pattern) for pattern in contract['allowed_paths']):
            return f'changed path is outside allowed_paths: {path}'
    return None


def _safe_relative_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Git returned an empty path.')
    if '\x00' in value:
        raise ValueError('Git path contains NUL.')
    if value.startswith('/') or re.match(r'^[A-Za-z]:', value):
        raise ValueError(f'Git path is absolute: {value!r}')
    if '\\' in value:
        raise ValueError(f'Git path contains a backslash, which is ambiguous on Windows: {value!r}')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'Git path has unsafe components: {value!r}')
    if any(part.lower() == '.git' for part in parts):
        raise ValueError(f'Git path targets .git: {value!r}')
    return value


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ArtifactManager:
    """Freeze, verify, apply, and clean up finished worktree artifacts."""

    def __init__(self, state, git_fn):
        if not callable(git_fn):
            raise ValueError('git_fn must be callable with the bridge.git signature.')
        root = Path(state).expanduser()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = root.resolve()
        if not self.state.is_dir():
            raise ValueError('state must be a directory.')
        self.worktrees = self._private_dir('worktrees')
        self.patches = self._private_dir('patches')
        self.verifications = self._private_dir('verification')
        self.manifests = self._private_dir('artifacts')
        self.applied_dir = self._private_dir('applied')
        self.locks = self._private_dir('locks')
        self._git_fn = git_fn

    # -- private paths and git -------------------------------------------------

    def _private_dir(self, name):
        path = self.state / name
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise RuntimeError(f'Private state directory must not be a symlink: {path}')
        resolved = path.resolve()
        if not resolved.is_relative_to(self.state):
            raise RuntimeError(f'Private state directory escaped state: {path}')
        return resolved

    def _path(self, directory, suffix):
        # Callers only pass validated job IDs; this guard also protects the
        # atomic file writers from an accidental separator.
        if '/' in suffix or '\\' in suffix or suffix in ('.', '..'):
            raise ValueError('Unsafe private artifact name.')
        return directory / suffix

    def _git(self, directory, *args, check=True, data=None):
        return self._git_fn(str(directory), *args, check=check, binary=True, data=data)

    def _git_text(self, directory, *args, check=True, data=None):
        result = self._git(directory, *args, check=check, data=data)
        return result.stdout.decode('utf-8', errors='surrogateescape').rstrip('\n')

    @staticmethod
    def _git_error(result):
        return result.stderr.decode('utf-8', errors='replace')[:1500]

    def _job_id(self, raw):
        if not isinstance(raw, str) or not SAFE_ID.fullmatch(raw):
            raise ValueError('Unsafe or missing job id.')
        return raw

    def _private_json(self, path):
        if path.is_symlink():
            raise RuntimeError(f'Private artifact must not be a symlink: {path}')
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f'Corrupt private artifact {path}: {exc}') from exc
        if not isinstance(data, dict):
            raise RuntimeError(f'Corrupt private artifact {path}: expected an object.')
        return data

    def _atomic_write(self, path, data, mode=0o600):
        path = Path(path)
        fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp',
                                         dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def _atomic_json(self, path, value):
        # ASCII escaping keeps non-UTF-8 Git paths (surrogate escapes) persistable.
        payload = (json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2)
                   + '\n').encode('utf-8')
        self._atomic_write(path, payload)

    @contextlib.contextmanager
    def _project_lock(self, project):
        digest = hashlib.sha256(os.fsencode(str(project))).hexdigest()
        path = self._path(self.locks, digest + '.lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- row validation --------------------------------------------------------

    def _infer_project(self, workspace):
        result = self._git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir',
                           check=False)
        if result.returncode:
            return None
        common = Path(result.stdout.decode('utf-8', errors='surrogateescape').rstrip('\n'))
        return common.parent if common.name == '.git' else common

    def _canonical_project(self, project):
        path = Path(project).expanduser()
        if not path.is_dir():
            raise ValueError('Project directory does not exist.')
        path = path.resolve()
        top = self._git_text(path, 'rev-parse', '--show-toplevel', check=False)
        if not top:
            raise ValueError('Project is not a Git repository.')
        if Path(top).resolve() != path:
            raise ValueError('Project must be the repository root.')
        return path

    def _registered(self, project):
        output = self._git(project, 'worktree', 'list', '--porcelain', '-z').stdout
        result = set()
        for token in output.split(b'\x00'):
            if token.startswith(b'worktree '):
                result.add(Path(os.fsdecode(token[len(b'worktree '):])).resolve())
        return result

    def _context(self, row, require_success=False):
        if not isinstance(row, dict):
            raise ValueError('row must be a job dictionary.')
        job_id = self._job_id(row.get('id'))
        status = row.get('status')
        if status not in TERMINAL:
            raise ValueError('Artifacts are available only for terminal worktree jobs.')
        if require_success and status != 'succeeded':
            raise ValueError('Only succeeded jobs may be applied.')

        spec = row.get('spec')
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except ValueError as exc:
                raise ValueError('row spec is not valid JSON.') from exc
        if spec is None:
            spec = {}
        if not isinstance(spec, dict):
            raise ValueError('row spec must be an object or JSON object string.')
        isolation = spec.get('isolation')
        if isolation in ('readonly', 'exclusive'):
            raise ValueError('Artifacts require worktree isolation.')
        if isolation not in (None, 'worktree'):
            raise ValueError('Unsupported isolation mode.')

        raw_project = spec.get('project') or row.get('project')
        workspace_raw = row.get('workspace')
        if isinstance(workspace_raw, os.PathLike):
            workspace_raw = os.fspath(workspace_raw)
        if not isinstance(workspace_raw, str) or not workspace_raw:
            raise ValueError('Finished job has no workspace.')
        owned = self.worktrees / job_id
        if owned.is_symlink():
            raise ValueError('Owned worktree path must not be a symlink.')
        expected = owned.resolve()
        workspace = Path(workspace_raw).expanduser().resolve()
        if workspace != expected:
            raise ValueError('Workspace is not owned by state/worktrees/<job_id>.')
        if not workspace.is_dir():
            raise ValueError('Worktree workspace no longer exists.')
        if not workspace.is_relative_to(self.worktrees):
            raise ValueError('Workspace escaped the private worktree directory.')

        if raw_project:
            project = self._canonical_project(raw_project)
        else:
            project = self._infer_project(workspace)
            if project is not None:
                project = self._canonical_project(project)
        if project is None:
            raise ValueError('Cannot determine the source project for this worktree.')
        if workspace not in self._registered(project):
            raise ValueError('Workspace is not the registered Git worktree for this project.')

        head = self._git_text(workspace, 'rev-parse', 'HEAD')
        raw_base = row.get('base_commit')
        if raw_base is None or raw_base == '':
            base = head
        else:
            if not isinstance(raw_base, str) or not _HEX_COMMIT.fullmatch(raw_base):
                raise ValueError('base_commit is not a hexadecimal commit id.')
            base = self._git_text(workspace, 'rev-parse', '--verify', raw_base + '^{commit}',
                                  check=False)
            if not base:
                raise ValueError('base_commit does not exist in the worktree.')
        if head != base:
            raise ValueError('Worktree HEAD moved from base_commit; committed work is not a patch artifact.')

        return {'job_id': job_id, 'status': status, 'project': project,
                'workspace': workspace, 'base_commit': base, 'head': head,
                'spec': spec}

    # -- snapshots -------------------------------------------------------------

    def _workspace_digest(self, workspace):
        result = self._git(workspace, 'ls-files', '-z', '--cached', '--others',
                           '--exclude-standard', check=True)
        raw_paths = {item for item in result.stdout.split(b'\x00') if item}
        digest = hashlib.sha256()
        digest.update(b'codex-opencode-workspace-input-v1\x00')
        for raw_path in sorted(raw_paths):
            relative = os.fsdecode(raw_path)
            _safe_relative_path(relative)
            full = os.path.join(os.fsencode(workspace), raw_path)
            try:
                info = os.lstat(full)
            except FileNotFoundError:
                mode, kind, file_digest, size = b'000000', b'missing', hashlib.sha256().digest(), 0
            else:
                if stat.S_ISLNK(info.st_mode):
                    mode, kind = b'120000', b'symlink'
                    payload = os.fsencode(os.readlink(full))
                    file_digest = hashlib.sha256(payload).digest()
                    size = len(payload)
                elif stat.S_ISREG(info.st_mode):
                    mode = b'100755' if info.st_mode & 0o111 else b'100644'
                    kind = b'file'
                    hasher = hashlib.sha256()
                    size = 0
                    with open(full, 'rb') as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                            hasher.update(chunk)
                            size += len(chunk)
                    file_digest = hasher.digest()
                elif stat.S_ISDIR(info.st_mode):
                    mode, kind, file_digest, size = b'040000', b'directory', hashlib.sha256().digest(), 0
                else:
                    mode, kind, file_digest, size = b'000000', b'special', hashlib.sha256().digest(), 0
            digest.update(len(raw_path).to_bytes(8, 'big'))
            digest.update(raw_path)
            digest.update(mode)
            digest.update(kind)
            digest.update(size.to_bytes(8, 'big'))
            digest.update(file_digest)
        return digest.hexdigest()

    def _snapshot(self, row, contract=None, persist=False, enforce_contract=True):
        context = self._context(row)
        workspace = context['workspace']
        contract = normalize_contract(contract)

        listed = self._git(workspace, 'ls-files', '-z', '--cached', '--others',
                           '--exclude-standard').stdout
        if any(listed.split(b'\x00')):
            add = self._git(workspace, 'add', '--intent-to-add', '--', '.', check=False)
            if add.returncode:
                raise RuntimeError(self._git_error(add))

        patch = self._git(workspace, 'diff', '--binary', '--no-ext-diff', '--no-textconv',
                          '--no-color', '--no-renames', context['base_commit'], '--').stdout
        names = self._git(workspace, 'diff', '--name-only', '-z', '--no-ext-diff',
                          '--no-textconv', '--no-renames', context['base_commit'], '--').stdout
        changed = []
        seen = set()
        for item in names.split(b'\x00'):
            if not item:
                continue
            relative = _safe_relative_path(os.fsdecode(item))
            if relative not in seen:
                seen.add(relative)
                changed.append(relative)

        contract_error = _contract_error(contract, changed)
        if enforce_contract and contract_error:
            raise ValueError(contract_error)

        workspace_sha = self._workspace_digest(workspace)
        patch_sha = hashlib.sha256(patch).hexdigest()
        snapshot = {
            'schema': SCHEMA,
            'kind': 'artifact-freeze',
            'job_id': context['job_id'],
            'project': str(context['project']),
            'workspace': str(workspace),
            'base_commit': context['base_commit'],
            'workspace_head': context['head'],
            'patch_path': str(self._path(self.patches, context['job_id'] + '.patch')),
            'manifest_path': str(self._path(self.manifests, context['job_id'] + '.json')),
            'patch_sha256': patch_sha,
            'workspace_sha256': workspace_sha,
            'patch_size': len(patch),
            'changed_files': changed,
            'changed_file_count': len(changed),
            'contract_error': contract_error,
            'contract': contract,
            'frozen_at': time.time(),
            '_patch': patch,
        }

        if persist:
            applied = self._private_json(self._path(self.applied_dir, context['job_id'] + '.json'))
            if applied and applied.get('patch_sha256') != patch_sha:
                raise RuntimeError('Worktree changed after its artifact was applied; refusing to overwrite the archived patch.')
            self._atomic_write(Path(snapshot['patch_path']), patch)
            self._atomic_json(Path(snapshot['manifest_path']),
                              {key: value for key, value in snapshot.items() if not key.startswith('_')})
        return snapshot

    @staticmethod
    def _public(snapshot):
        return {key: value for key, value in snapshot.items() if not key.startswith('_')}

    # -- public freeze ---------------------------------------------------------

    def freeze(self, row, contract=None):
        """Freeze a finished worktree into a private binary patch.

        Untracked, non-ignored files are made visible with ``git add
        --intent-to-add`` (index metadata only).  The digest covers every
        tracked path including unchanged files, all non-ignored untracked files,
        normalized Git modes, and symlink targets.  Ignored outputs are excluded.
        """
        return self._public(self._snapshot(row, contract=contract, persist=True,
                                           enforce_contract=True))

    # -- checks ----------------------------------------------------------------

    def _run_check(self, workspace, argv, timeout):
        started = time.monotonic()
        stdout_file = tempfile.TemporaryFile(mode='w+b')
        stderr_file = tempfile.TemporaryFile(mode='w+b')
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        timed_out = False
        error = None
        code = None
        try:
            process = subprocess.Popen(
                argv, cwd=str(workspace), env=environment,
                stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                start_new_session=True)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
            code = process.returncode
        except OSError as exc:
            error = f'{type(exc).__name__}: {exc}'
        finally:
            stdout, stdout_truncated = self._read_tail(stdout_file)
            stderr, stderr_truncated = self._read_tail(stderr_file)
            stdout_file.close()
            stderr_file.close()
        return {
            'argv': list(argv),
            'exit_code': code,
            'timed_out': timed_out,
            'error': error,
            'stdout': stdout,
            'stderr': stderr,
            'stdout_truncated': stdout_truncated,
            'stderr_truncated': stderr_truncated,
            'duration_seconds': round(time.monotonic() - started, 3),
            'passed': code == 0 and not timed_out and error is None,
        }

    @staticmethod
    def _read_tail(handle):
        handle.flush()
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - MAX_OUTPUT_BYTES))
        data = handle.read(MAX_OUTPUT_BYTES)
        return data.decode('utf-8', errors='replace'), size > MAX_OUTPUT_BYTES

    # -- public verify ---------------------------------------------------------

    def inspect_verification(self, row):
        """Read retained evidence without running checks or asserting freshness."""
        job_id = self._job_id(row['id'])
        path = self._path(self.verifications, job_id + '.json')
        record = self._private_json(path)
        if record is None:
            return {'job_id': job_id, 'verification_state': 'not_run', 'passed': False,
                    'reason': 'No retained verification record.', 'checks': [],
                    'changed_during_checks': None, 'patch_sha256': None, 'workspace_sha256': None,
                    'attempt_count': 0, 'verification_path': str(path),
                    'evidence_scope': 'retained_snapshot', 'freshness_checked': False}
        return {**record, 'evidence_scope': 'retained_snapshot', 'freshness_checked': False}

    def verify(self, row, checks=None, contract=None, timeout=120):
        """Run trusted explicit argv checks against a frozen worktree.

        Checks are **not** model-provided.  They run arbitrary trusted commands
        with the coordinator's privileges and are not sandboxed.  A verification
        passes only when every check exits 0 and the patch and full workspace
        input digests are unchanged between the before/after freezes.  No checks
        means ``verification_state='not_run'`` and ``passed=False``.
        """
        contract = normalize_contract(contract)
        if checks is None:
            checks = contract['checks'] if contract else []
        checks = _clean_checks(checks)
        if contract and contract['checks'] and checks != contract['checks']:
            raise ValueError('Verification checks must match the submitted contract.')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
            raise ValueError('timeout must be a positive number of seconds (<= 3600).')

        before = self._snapshot(row, contract=contract, persist=True, enforce_contract=True)
        job_id = before['job_id']
        previous = self._private_json(self._path(self.verifications, job_id + '.json'))
        attempts = list(previous.get('attempts', [])) if previous else []
        same_patch = [item for item in attempts
                      if item.get('patch_sha256') == before['patch_sha256']
                      and item.get('verification_state') != 'not_run']
        # Verification repeats are distinct from model execution attempts.
        # max_attempts remains an execution policy, not a cap on validation commands.
        # Legacy note:
        # contract=None remains permissive for compatibility and records attempts.
        started = time.time()
        results = [self._run_check(before['workspace'], argv, timeout) for argv in checks]
        after = self._snapshot(row, contract=contract, persist=False, enforce_contract=False)
        changed = (before['patch_sha256'] != after['patch_sha256']
                   or before['workspace_sha256'] != after['workspace_sha256'])
        after_contract_error = after.get('contract_error')
        passed = bool(checks) and all(item['passed'] for item in results) and not changed
        state = 'not_run' if not checks else ('passed' if passed else 'failed')
        reasons = []
        if not checks:
            reasons.append('No checks were supplied; verification is not_run and cannot satisfy require_verification.')
        if any(item['timed_out'] for item in results):
            reasons.append('At least one check timed out.')
        if any(item['error'] for item in results):
            reasons.append('At least one check failed to start.')
        if any(item['exit_code'] not in (0, None) for item in results):
            reasons.append('At least one check returned a non-zero exit code.')
        if changed:
            reasons.append('Patch or workspace input digest changed during checks.')
        if after_contract_error:
            reasons.append('Checks produced an artifact that violates the contract: ' + after_contract_error)

        completed = time.time()
        if checks:
            attempt = {'patch_sha256': before['patch_sha256'],
                       'workspace_sha256': before['workspace_sha256'],
                       'verification_state': state, 'passed': passed,
                       'started_at': started, 'completed_at': completed}
            attempts = (attempts + [attempt])[-20:]
        record = {
            'schema': SCHEMA,
            'kind': 'artifact-verification',
            'job_id': job_id,
            'project': before['project'],
            'workspace': before['workspace'],
            'base_commit': before['base_commit'],
            'contract': contract,
            'verification_state': state,
            'passed': passed,
            'reason': ' '.join(reasons) if reasons else 'All checks exited 0 and digests are unchanged.',
            'changed_during_checks': changed,
            'started_at': started,
            'completed_at': completed,
            'timeout_seconds': timeout,
            'checks': results,
            'results': results,
            'patch_sha256': before['patch_sha256'],
            'workspace_sha256': before['workspace_sha256'],
            'before': {key: before[key] for key in
                       ('patch_sha256', 'workspace_sha256', 'patch_path', 'changed_files',
                        'changed_file_count', 'frozen_at')},
            'after': {key: after[key] for key in
                      ('patch_sha256', 'workspace_sha256', 'changed_files', 'changed_file_count')},
            'attempt_count': len(same_patch) + (1 if checks else 0),
            'max_attempts': contract['max_attempts'] if contract else None,
            'attempts': attempts,
            'verification_path': str(self._path(self.verifications, job_id + '.json')),
        }
        self._atomic_json(Path(record['verification_path']), record)
        return record

    # -- apply -----------------------------------------------------------------

    def _write_applied(self, snapshot, already_applied=False):
        job_id = snapshot['job_id']
        path = self._path(self.applied_dir, job_id + '.json')
        existing = self._private_json(path)
        if existing:
            if existing.get('patch_sha256') != snapshot['patch_sha256']:
                raise RuntimeError('Refusing to overwrite the applied marker for a different patch.')
            return existing
        record = {
            'schema': SCHEMA,
            'kind': 'artifact-applied',
            'job_id': job_id,
            'project': snapshot['project'],
            'base_commit': snapshot['base_commit'],
            'patch_path': snapshot['patch_path'],
            'patch_sha256': snapshot['patch_sha256'],
            'workspace_sha256': snapshot['workspace_sha256'],
            'changed_files': snapshot['changed_files'],
            'applied_at': time.time(),
            'already_applied_on_first_seen': bool(already_applied),
        }
        self._atomic_json(path, record)
        return record

    def apply(self, row, project, contract=None, require_verification=False, expected_patch_sha256=None):
        """Apply a frozen binary patch to the canonical source project.

        A per-project interprocess ``flock`` (keyed by SHA-256 of the canonical
        project path) serializes freeze+verification checks+apply.  The source
        HEAD must still equal the worktree base commit.  Mutable source content
        is rechecked by ``git apply --check``; non-overlapping changes from an
        earlier artifact are allowed so a batch can be integrated without an
        intermediate commit.  Nothing is committed or pushed.
        """
        contract = normalize_contract(contract)
        context = self._context(row, require_success=True)
        project = self._canonical_project(project)
        if project != context['project']:
            raise ValueError('Project does not match the job source project.')

        with self._project_lock(project):
            snapshot = self._snapshot(row, contract=contract, persist=True,
                                      enforce_contract=True)
            if expected_patch_sha256 is not None and expected_patch_sha256 != snapshot['patch_sha256']:
                raise ValueError('Patch differs from the reviewed patch_sha256.')
            job_id = snapshot['job_id']
            applied_path = self._path(self.applied_dir, job_id + '.json')
            applied = self._private_json(applied_path)
            if applied and applied.get('patch_sha256') != snapshot['patch_sha256']:
                raise RuntimeError('Worktree patch changed after application; refusing to apply a different artifact.')

            if require_verification:
                verification = self._private_json(
                    self._path(self.verifications, job_id + '.json'))
                after = (verification or {}).get('after') or {}
                if not verification or not verification.get('passed') or \
                        verification.get('verification_state') != 'passed':
                    raise ValueError('require_verification is set but no successful verification exists.')
                if verification.get('contract') != contract:
                    raise ValueError('Verification was performed under a different contract.')
                if (after.get('patch_sha256') != snapshot['patch_sha256']
                        or after.get('workspace_sha256') != snapshot['workspace_sha256']):
                    raise ValueError('Current worktree does not match the successfully verified artifact.')

            source_head = self._git_text(project, 'rev-parse', 'HEAD')
            if source_head != snapshot['base_commit']:
                raise ValueError('Source HEAD changed; review/rebase the retained patch manually.')

            patch = snapshot['_patch']
            if not patch:
                return {'applied': False, 'already_applied': False, 'reason': 'No changes.',
                        'job_id': job_id, 'project': str(project),
                        'base_commit': snapshot['base_commit'],
                        'patch_path': snapshot['patch_path'],
                        'patch_sha256': snapshot['patch_sha256'],
                        'workspace_sha256': snapshot['workspace_sha256']}

            reverse = self._git(project, 'apply', '--check', '--reverse', '--binary', '-',
                                data=patch, check=False)
            if reverse.returncode == 0:
                marker = self._write_applied(snapshot, already_applied=True)
                return {'applied': True, 'already_applied': True, 'job_id': job_id,
                        'project': str(project), 'base_commit': snapshot['base_commit'],
                        'patch_path': snapshot['patch_path'],
                        'patch_sha256': snapshot['patch_sha256'],
                        'workspace_sha256': snapshot['workspace_sha256'],
                        'applied_record': str(applied_path), 'marker_created_at': marker['applied_at']}

            if applied:
                raise RuntimeError('The previously applied artifact is no longer present in the source; manual review required.')

            # HEAD is unchanged; git apply --check below is the mutable-content
            # recheck.  A non-overlapping change from an earlier artifact is
            # allowed so several worktree jobs can be integrated in one batch.
            check = self._git(project, 'apply', '--check', '--binary', '-', data=patch, check=False)
            if check.returncode:
                raise RuntimeError('Patch does not apply cleanly: ' + self._git_error(check))
            actual = self._git(project, 'apply', '--binary', '-', data=patch, check=False)
            if actual.returncode:
                raise RuntimeError('git apply failed: ' + self._git_error(actual))
            confirm = self._git(project, 'apply', '--check', '--reverse', '--binary', '-',
                                data=patch, check=False)
            if confirm.returncode:
                raise RuntimeError('git apply completed but the reverse check failed; inspect the source immediately.')
            marker = self._write_applied(snapshot)
            return {'applied': True, 'already_applied': False, 'job_id': job_id,
                    'project': str(project), 'base_commit': snapshot['base_commit'],
                    'patch_path': snapshot['patch_path'],
                    'patch_sha256': snapshot['patch_sha256'],
                    'workspace_sha256': snapshot['workspace_sha256'],
                    'applied_record': str(applied_path),
                    'changed_files': snapshot['changed_files'],
                    'marker_created_at': marker['applied_at']}

    # -- cleanup ---------------------------------------------------------------

    @staticmethod
    def _tree_size(path):
        total, count = 0, 0
        if not path.exists():
            return 0, 0
        for root, directories, files in os.walk(path, followlinks=False):
            for name in files + directories:
                full = os.path.join(root, name)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    total += info.st_size
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                if stat.S_ISREG(info.st_mode):
                    count += 1
        return total, count

    @staticmethod
    def _file_size(path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return 0, False
        return (info.st_size if stat.S_ISREG(info.st_mode) else 0), True

    def _cleanup_plan(self, row, project, discard):
        job_id = self._job_id(row.get('id'))
        status = row.get('status')
        spec = row.get('spec')
        if isinstance(spec, str):
            spec = json.loads(spec)
        spec = spec if isinstance(spec, dict) else {}
        workspace_raw = row.get('workspace')
        if isinstance(workspace_raw, os.PathLike):
            workspace_raw = os.fspath(workspace_raw)
        owned = self.worktrees / job_id
        expected = owned.resolve()
        workspace = Path(workspace_raw).expanduser().resolve() if isinstance(workspace_raw, str) else expected

        patch_path = self._path(self.patches, job_id + '.patch')
        verification_path = self._path(self.verifications, job_id + '.json')
        manifest_path = self._path(self.manifests, job_id + '.json')
        applied_path = self._path(self.applied_dir, job_id + '.json')
        private = [
            {'kind': 'patch', 'path': str(patch_path), 'delete': True},
            {'kind': 'verification', 'path': str(verification_path), 'delete': True},
            {'kind': 'manifest', 'path': str(manifest_path), 'delete': True},
            {'kind': 'applied_marker', 'path': str(applied_path), 'delete': False},
        ]
        for item in private:
            size, exists = self._file_size(item['path'])
            item['bytes'], item['exists'] = size, exists

        row_project = None
        if spec.get('project') or row.get('project'):
            with contextlib.suppress(ValueError):
                row_project = self._canonical_project(spec.get('project') or row.get('project'))
        if row_project is None:
            with contextlib.suppress(ValueError, RuntimeError):
                row_project = self._infer_project(workspace)
                if row_project is not None:
                    row_project = self._canonical_project(row_project)
        project_path = None
        project_mismatch = False
        if project is not None:
            try:
                project_path = self._canonical_project(project)
            except (ValueError, RuntimeError):
                project_mismatch = True
            else:
                project_mismatch = bool(row_project and project_path != row_project)
        if project_path is None and not project_mismatch:
            project_path = row_project

        plan = {
            'schema': SCHEMA,
            'kind': 'artifact-cleanup-plan',
            'job_id': job_id,
            'dry_run': True,
            'discard': bool(discard),
            'status': status,
            'project': str(project_path) if project_path else None,
            'workspace': str(workspace),
            'workspace_exists': workspace.is_dir(),
            'workspace_bytes': 0,
            'workspace_file_count': 0,
            'workspace_dirty': None,
            'workspace_head': None,
            'base_commit': None,
            'worktree_registered': False,
            'has_applied_record': bool(self._private_json(applied_path)),
            'verification_state': (self._private_json(verification_path) or {}).get('verification_state'),
            'archived': {},
            'digest_match': None,
            'verified_exact': False,
            'force': False,
            'eligible': False,
            'reason': None,
            'removal_argv': None,
            'private_paths': private,
            'session_id': row.get('session_id'),
            'session_retention': 'OpenCode sessions are retained by OpenCode; this function never bulk-deletes them.',
            'actions': [],
        }

        if owned.is_symlink():
            plan['reason'] = 'Owned worktree path is a symlink.'
            return plan
        if project_mismatch:
            plan['reason'] = 'Supplied project does not match the job source project.'
            return plan
        isolation = spec.get('isolation')
        if isolation in ('readonly', 'exclusive') or isolation not in (None, 'worktree'):
            plan['reason'] = 'Job did not use worktree isolation.'
            return plan
        if not isinstance(workspace_raw, str) or not workspace_raw or workspace != expected:
            plan['reason'] = 'Workspace is not exactly state/worktrees/<job_id>.'
            return plan
        if not workspace.is_dir():
            plan['reason'] = 'Worktree no longer exists; private artifacts are not deleted without a successful removal.'
            return plan
        if workspace == project_path:
            plan['reason'] = 'Refusing to remove the source project.'
            return plan
        plan['workspace_bytes'], plan['workspace_file_count'] = self._tree_size(workspace)

        if status not in TERMINAL:
            plan['reason'] = 'Job is not terminal; active worktrees are never removed.'
            return plan
        if project_path is None:
            plan['reason'] = 'Cannot determine or validate the source project.'
            return plan
        plan['project'] = str(project_path)
        try:
            registered = workspace in self._registered(project_path)
        except RuntimeError:
            registered = False
        plan['worktree_registered'] = registered
        if not registered:
            plan['reason'] = 'Workspace is not a registered worktree of the source project.'
            return plan

        status_result = self._git(workspace, 'status', '--porcelain', '-z',
                                  '--untracked-files=normal', check=False)
        dirty = bool(status_result.stdout)
        plan['workspace_dirty'] = dirty
        plan['workspace_head'] = self._git_text(workspace, 'rev-parse', 'HEAD', check=False)
        raw_base = row.get('base_commit')
        if isinstance(raw_base, str) and _HEX_COMMIT.fullmatch(raw_base):
            plan['base_commit'] = self._git_text(
                workspace, 'rev-parse', '--verify', raw_base + '^{commit}', check=False) or raw_base
            if plan['workspace_head'] != plan['base_commit'] and not discard:
                plan['reason'] = 'Worktree HEAD moved from base_commit; explicit discard is required.'
                return plan

        applied = self._private_json(applied_path)
        manifest = self._private_json(manifest_path)
        verification = self._private_json(verification_path)
        archived_patch = (applied or {}).get('patch_sha256') or (manifest or {}).get('patch_sha256')
        archived_workspace = ((applied or {}).get('workspace_sha256')
                              or (manifest or {}).get('workspace_sha256'))
        plan['archived'] = {'patch_sha256': archived_patch,
                            'workspace_sha256': archived_workspace}
        if archived_workspace:
            current = self._workspace_digest(workspace)
            patch_size, patch_exists = self._file_size(patch_path)
            patch_sha = None
            if patch_exists:
                patch_sha = hashlib.sha256(patch_path.read_bytes()).hexdigest()
            plan['digest_match'] = (current == archived_workspace
                                    and (not patch_exists or patch_sha == archived_patch))
            plan['current_workspace_sha256'] = current
            plan['archived_patch_present'] = patch_exists
        verification_after = (verification or {}).get('after') or {}
        verified_exact = bool(
            plan['digest_match'] and verification
            and verification.get('verification_state') == 'passed' and verification.get('passed')
            and verification_after.get('patch_sha256') == archived_patch
            and verification_after.get('workspace_sha256') == archived_workspace)
        plan['verified_exact'] = verified_exact

        if not applied and not discard:
            plan['reason'] = 'No applied record; pass discard=True to explicitly discard this worktree.'
            return plan

        force = bool(dirty)
        if dirty and not (verified_exact or discard):
            if applied and not plan['digest_match']:
                plan['reason'] = 'Dirty worktree does not match the archived/applied digest; refusing force removal.'
            elif applied:
                plan['reason'] = 'Applied artifact is not verified as the exact archived digest; refusing force removal.'
            else:
                plan['reason'] = 'Dirty worktree; refusing silent force removal.'
            return plan

        plan['force'] = force
        plan['eligible'] = True
        plan['reason'] = ('Eligible for explicit discard.' if not applied and discard else
                          'Eligible; applied verified digest is unchanged.' if verified_exact else
                          'Eligible; worktree is clean.')
        plan['removal_argv'] = [str(project_path), 'worktree', 'remove'] + \
            (['--force'] if force else []) + [str(workspace)]
        plan['actions'] = ['git worktree remove ' + ('--force ' if force else '') + str(workspace),
                           'delete private patch/verification/manifest after successful removal',
                           'retain minimal applied/idempotency marker' if applied else 'no applied marker to retain',
                           'leave source project and database rows untouched',
                           'leave OpenCode session retention to OpenCode']
        return plan

    def cleanup(self, row, project, dry_run=True, discard=False):
        """Plan or perform removal of one finished, state-owned worktree.

        Refuses active jobs and, unless ``discard=True``, jobs without an applied
        record.  A dirty worktree is never force-removed silently: force is used
        only for a verified exact archived artifact or an explicit discard.
        Deletes only the known private patch/verification/manifest files after a
        successful ``git worktree remove`` and keeps the applied marker.
        """
        plan = self._cleanup_plan(row, project, discard)
        plan['dry_run'] = bool(dry_run)
        if dry_run:
            return plan
        if not plan['eligible']:
            return plan
        project_path = Path(plan['project'])
        with self._project_lock(project_path):
            # Recompute under the project lock to avoid a freeze/apply race.
            current = self._cleanup_plan(row, project, discard)
            current['dry_run'] = False
            if not current['eligible']:
                return current
            result = self._git(project_path, 'worktree', 'remove',
                               *(['--force'] if current['force'] else []),
                               current['workspace'], check=False)
            if result.returncode:
                raise RuntimeError('git worktree remove failed: ' + self._git_error(result))
            current['removed'] = True
            current['deleted'] = []
            current['delete_errors'] = []
            for item in current['private_paths']:
                if not item['delete']:
                    continue
                path = Path(item['path'])
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                        current['deleted'].append(str(path))
                except OSError as exc:
                    current['delete_errors'].append(f'{path}: {exc}')
            current['workspace_exists'] = False
            return current
