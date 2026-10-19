"""Offline artifact tests using temporary, real Git repositories."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from artifacts import ArtifactManager, normalize_contract


def git(directory, *args, check=True, binary=False, data=None):
    """Same contract as bridge.git, without importing bridge."""
    result = subprocess.run(['git', '-C', str(directory), *args], input=data,
                            capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[:1500])
    return result if binary else result.stdout.decode('utf-8', errors='replace').strip()


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / 'state'
        self.project = self.root / 'project'
        self.project.mkdir()
        git(self.project, 'init', '-b', 'main')
        git(self.project, 'config', 'user.name', 'Artifact Tests')
        git(self.project, 'config', 'user.email', 'artifacts@localhost')
        (self.project / 'src').mkdir()
        (self.project / 'src' / 'app.py').write_text("print('hello')\n", encoding='utf-8')
        (self.project / 'README.md').write_text('hello\n', encoding='utf-8')
        (self.project / '.gitignore').write_text('*.log\n', encoding='utf-8')
        git(self.project, 'add', '.')
        git(self.project, 'commit', '-m', 'base')
        self.base = git(self.project, 'rev-parse', 'HEAD')
        self.manager = ArtifactManager(self.state, git)

    def tearDown(self):
        if self.project.is_dir():
            for child in (self.state / 'worktrees').glob('*') if (self.state / 'worktrees').is_dir() else []:
                git(self.project, 'worktree', 'remove', '--force', str(child), check=False)
            git(self.project, 'worktree', 'prune', check=False)
        shutil.rmtree(self.root, ignore_errors=True)
        self.temp.cleanup()

    def job(self, job_id='job_test', status='succeeded', isolation='worktree', base=None):
        worktree = self.state / 'worktrees' / job_id
        worktree.parent.mkdir(parents=True, exist_ok=True)
        git(self.project, 'worktree', 'add', '--detach', str(worktree), base or self.base)
        row = {
            'id': job_id,
            'status': status,
            'workspace': str(worktree),
            'base_commit': base or self.base,
            'session_id': 'ses_test_' + job_id,
            'spec': json.dumps({'project': str(self.project), 'isolation': isolation}),
        }
        return row, worktree

    def check_ok(self):
        return [[sys.executable, '-c', 'print("ok")']]

    # -- contract --------------------------------------------------------------

    def test_normalize_contract_defaults_and_rejections(self):
        self.assertIsNone(normalize_contract(None))
        self.assertEqual(normalize_contract({}), {
            'allowed_paths': [], 'forbidden_paths': [], 'checks': [],
            'max_attempts': 2, 'max_changed_files': 20})
        self.assertEqual(normalize_contract({'allowed_paths': ['./src/**']})['allowed_paths'],
                         ['src/**'])
        bad = [
            {'allowed_paths': ['/etc/passwd']},
            {'allowed_paths': ['../outside']},
            {'allowed_paths': ['src/../../x']},
            {'forbidden_paths': ['a\x00b']},
            {'allowed_paths': ['C:/tmp/x']},
            {'allowed_paths': ['src\\x']},
            {'allowed_paths': ['.git/config']},
            {'checks': ['python', '-c', 'print(1)']},
            {'checks': [['']]},
            {'checks': [['python', '\x00']]},
            {'max_attempts': 0},
            {'max_attempts': 3},
            {'max_attempts': True},
            {'max_changed_files': -1},
            {'unknown': True},
            [],
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    normalize_contract(value)

    # -- freeze ----------------------------------------------------------------

    def test_freeze_binary_new_symlink_modes_and_ignored_files(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('changed')\n", encoding='utf-8')
        (worktree / 'src' / 'tool.bin').write_bytes(b'\x00\x01\x02\xffbinary\x00')
        script = worktree / 'run.sh'
        script.write_text('#!/bin/sh\necho hi\n', encoding='utf-8')
        script.chmod(0o755)
        os.symlink('src/app.py', worktree / 'link')
        (worktree / 'debug.log').write_text('ignored\n', encoding='utf-8')

        frozen = self.manager.freeze(row)
        self.assertTrue(Path(frozen['patch_path']).is_file())
        self.assertEqual(frozen['base_commit'], self.base)
        self.assertIn('src/tool.bin', frozen['changed_files'])
        self.assertIn('run.sh', frozen['changed_files'])
        self.assertIn('link', frozen['changed_files'])
        self.assertNotIn('debug.log', frozen['changed_files'])
        patch = Path(frozen['patch_path']).read_bytes()
        self.assertIn(b'GIT binary patch', patch)
        self.assertIn(b'src/app.py', patch)
        self.assertEqual(len(frozen['patch_sha256']), 64)
        self.assertEqual(len(frozen['workspace_sha256']), 64)

        # Ignored outputs are excluded from both the patch and the input digest.
        (worktree / 'debug.log').write_text('different ignored output\n', encoding='utf-8')
        again = self.manager.freeze(row)
        self.assertEqual(again['workspace_sha256'], frozen['workspace_sha256'])
        self.assertEqual(again['patch_sha256'], frozen['patch_sha256'])

        # Symlink targets participate in the full-workspace digest.
        (worktree / 'link').unlink()
        os.symlink('README.md', worktree / 'link')
        changed_link = self.manager.freeze(row)
        self.assertNotEqual(changed_link['workspace_sha256'], frozen['workspace_sha256'])
        self.assertIn('link', changed_link['changed_files'])

        # Executable mode is part of the patch as well.
        self.assertIn(b'new file mode 100755', Path(changed_link['patch_path']).read_bytes())

    def test_freeze_robust_odd_filename(self):
        row, worktree = self.job()
        name = 'odd name\n"quote.txt'
        (worktree / name).write_text('odd\n', encoding='utf-8')
        frozen = self.manager.freeze(row)
        self.assertIn(name, frozen['changed_files'])
        patch = Path(frozen['patch_path']).read_bytes()
        self.assertIn(b'odd name', patch)
        self.assertTrue(Path(frozen['manifest_path']).is_file())
        manifest = json.loads(Path(frozen['manifest_path']).read_text(encoding='utf-8'))
        self.assertIn(name, manifest['changed_files'])

        raw_name = b'non-utf8-\xff.txt'
        full = os.path.join(os.fsencode(worktree), raw_name)
        descriptor = os.open(full, os.O_CREAT | os.O_WRONLY, 0o644)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(b'raw\n')
        raw_frozen = self.manager.freeze(row)
        self.assertIn(raw_name, [os.fsencode(path) for path in raw_frozen['changed_files']])
        json.loads(Path(raw_frozen['manifest_path']).read_text(encoding='utf-8'))

    def test_freeze_contract_boundaries_and_max_files(self):
        row, worktree = self.job()
        (worktree / 'README.md').write_text('outside\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.manager.freeze(row, {'allowed_paths': ['src/**']})
        with self.assertRaises(ValueError):
            self.manager.freeze(row, {'forbidden_paths': ['README.md']})
        with self.assertRaises(ValueError):
            self.manager.freeze(row, {'max_changed_files': 0})

        (worktree / 'README.md').write_text('hello\n', encoding='utf-8')
        (worktree / 'src' / 'app.py').write_text("print('inside')\n", encoding='utf-8')
        frozen = self.manager.freeze(row, {
            'allowed_paths': ['src/**'], 'forbidden_paths': ['README.md'],
            'max_changed_files': 2})
        self.assertEqual(frozen['changed_files'], ['src/app.py'])
        self.assertIsNone(frozen['contract_error'])

    def test_freeze_rejects_wrong_ownership_and_modes(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('x')\n", encoding='utf-8')
        bad = dict(row, workspace=str(self.project))
        with self.assertRaises(ValueError):
            self.manager.freeze(bad)
        with self.assertRaises(ValueError):
            self.manager.freeze(dict(row, status='running'))
        readonly, _ = self.job('job_readonly', isolation='readonly')
        with self.assertRaises(ValueError):
            self.manager.freeze(readonly)

        # A minimal row without spec still resolves the source project.
        minimal_row, minimal_tree = self.job('job_minimal')
        (minimal_tree / 'src' / 'app.py').write_text("print('minimal')\n", encoding='utf-8')
        minimal = {key: value for key, value in minimal_row.items() if key != 'spec'}
        self.assertEqual(self.manager.freeze(minimal)['changed_files'], ['src/app.py'])

    # -- verify ----------------------------------------------------------------

    def test_verify_pass_and_not_run(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('verified')\n", encoding='utf-8')
        passed = self.manager.verify(row, self.check_ok(), timeout=10)
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['verification_state'], 'passed')
        self.assertEqual(passed['checks'][0]['exit_code'], 0)
        self.assertEqual(passed['before']['workspace_sha256'], passed['after']['workspace_sha256'])
        self.assertTrue(Path(passed['verification_path']).is_file())
        stored = json.loads(Path(passed['verification_path']).read_text(encoding='utf-8'))
        self.assertEqual(stored['patch_sha256'] if 'patch_sha256' in stored else
                         stored['before']['patch_sha256'], passed['before']['patch_sha256'])

        not_run = self.manager.verify(row, [], timeout=10)
        self.assertFalse(not_run['passed'])
        self.assertEqual(not_run['verification_state'], 'not_run')

    def test_verify_real_exit_code_timeout_and_oserror(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('failure')\n", encoding='utf-8')
        failed = self.manager.verify(row, [[sys.executable, '-c', 'import sys; sys.exit(7)']],
                                     timeout=10)
        self.assertFalse(failed['passed'])
        self.assertEqual(failed['checks'][0]['exit_code'], 7)

        timed = self.manager.verify(row, [[sys.executable, '-c',
                                           'import time; time.sleep(5)']], timeout=0.2)
        self.assertFalse(timed['passed'])
        self.assertTrue(timed['checks'][0]['timed_out'])

        # This is a third attempt for the same patch, so use a separate job.
        row2, worktree2 = self.job('job_oserror')
        (worktree2 / 'src' / 'app.py').write_text("print('oserror')\n", encoding='utf-8')
        broken = self.manager.verify(row2, [['/definitely/not/a/real/command']], timeout=2)
        self.assertFalse(broken['passed'])
        self.assertIsNotNone(broken['checks'][0]['error'])

    def test_verify_bounds_captured_output(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('output')\n", encoding='utf-8')
        result = self.manager.verify(row, [[sys.executable, '-c',
                                            'print("x" * 20000)']], timeout=10)
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['checks'][0]['stdout']), 12000)
        self.assertTrue(result['checks'][0]['stdout_truncated'])

    def test_verify_detects_mutation_during_checks(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('mutating')\n", encoding='utf-8')
        result = self.manager.verify(row, [[sys.executable, '-c',
                                            'open("generated.txt","w").write("x")']],
                                     timeout=10)
        self.assertFalse(result['passed'])
        self.assertEqual(result['verification_state'], 'failed')
        self.assertTrue(result['changed_during_checks'])
        self.assertIn('generated.txt', result['after']['changed_files'])

    def test_verify_ignores_ignored_outputs(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('ignored outputs')\n", encoding='utf-8')
        result = self.manager.verify(row, [[sys.executable, '-c',
                                            'open("debug.log","w").write("noise")']],
                                     timeout=10)
        self.assertTrue(result['passed'])
        self.assertFalse(result['changed_during_checks'])
        self.assertNotIn('debug.log', result['after']['changed_files'])

    def test_verify_not_run_does_not_consume_attempts(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('attempt accounting')\n", encoding='utf-8')
        self.assertEqual(self.manager.verify(row, [], timeout=5)['verification_state'], 'not_run')
        passed = self.manager.verify(row, [[sys.executable, '-c', '']],
                                     contract={'max_attempts': 1}, timeout=5)
        self.assertTrue(passed['passed'])
        self.assertEqual(passed['attempt_count'], 1)

    def test_execution_attempt_policy_does_not_limit_verification_repeats(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('attempt')\n", encoding='utf-8')
        command = [[sys.executable, '-c', 'import sys; sys.exit(1)']]
        self.manager.verify(row, command, contract={'max_attempts': 1}, timeout=5)
        repeated = self.manager.verify(row, command, contract={'max_attempts': 1}, timeout=5)
        self.assertEqual(repeated['verification_state'], 'failed')
        self.assertEqual(repeated['attempt_count'], 2)

    # -- apply -----------------------------------------------------------------

    def test_review_hash_checked_under_apply_lock(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('first review')\n")
        reviewed = self.manager.freeze(row)
        (worktree / 'src' / 'app.py').write_text("print('changed later')\n")
        with self.assertRaises(ValueError):
            self.manager.apply(row, self.project, expected_patch_sha256=reviewed['patch_sha256'])
        self.assertEqual((self.project / 'src' / 'app.py').read_text(), "print('hello')\n")

    def test_stronger_contract_cannot_reuse_weaker_verification(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('changed')\n")
        self.manager.verify(row, self.check_ok())
        with self.assertRaises(ValueError):
            self.manager.apply(row, self.project, contract={'allowed_paths': ['src/**']}, require_verification=True)

    def test_contract_checks_cannot_be_replaced_by_trivial_check(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('changed')\n")
        with self.assertRaises(ValueError):
            self.manager.verify(row, self.check_ok(), contract={'checks': [[sys.executable, '-c', 'raise SystemExit(1)']]})

    def test_apply_binary_and_idempotency(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('applied')\n", encoding='utf-8')
        (worktree / 'src' / 'blob.bin').write_bytes(b'\x00\xff\x01')
        self.manager.verify(row, self.check_ok(), timeout=10)
        head_before = git(self.project, 'rev-parse', 'HEAD')
        commits_before = git(self.project, 'rev-list', '--count', 'HEAD')

        applied = self.manager.apply(row, self.project, require_verification=True)
        self.assertTrue(applied['applied'])
        self.assertFalse(applied['already_applied'])
        self.assertEqual((self.project / 'src' / 'app.py').read_text(encoding='utf-8'),
                         "print('applied')\n")
        self.assertEqual((self.project / 'src' / 'blob.bin').read_bytes(), b'\x00\xff\x01')
        self.assertEqual(git(self.project, 'rev-parse', 'HEAD'), head_before)
        self.assertEqual(git(self.project, 'rev-list', '--count', 'HEAD'), commits_before)
        self.assertTrue(Path(applied['applied_record']).is_file())

        second = self.manager.apply(row, self.project, require_verification=True)
        self.assertTrue(second['applied'])
        self.assertTrue(second['already_applied'])
        self.assertEqual(git(self.project, 'rev-parse', 'HEAD'), head_before)

    def test_apply_two_nonoverlapping_jobs_sequentially(self):
        first, first_tree = self.job('job_first')
        second, second_tree = self.job('job_second')
        (first_tree / 'src' / 'app.py').write_text("print('first')\n", encoding='utf-8')
        (second_tree / 'README.md').write_text('second\n', encoding='utf-8')
        self.manager.verify(first, self.check_ok(), timeout=10)
        self.manager.verify(second, self.check_ok(), timeout=10)
        head = git(self.project, 'rev-parse', 'HEAD')
        self.assertTrue(self.manager.apply(first, self.project, require_verification=True)['applied'])
        self.assertTrue(self.manager.apply(second, self.project, require_verification=True)['applied'])
        self.assertEqual((self.project / 'src' / 'app.py').read_text(encoding='utf-8'),
                         "print('first')\n")
        self.assertEqual((self.project / 'README.md').read_text(encoding='utf-8'), 'second\n')
        self.assertEqual(git(self.project, 'rev-parse', 'HEAD'), head)

    def test_apply_refuses_modified_after_verify(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('first')\n", encoding='utf-8')
        self.assertTrue(self.manager.verify(row, self.check_ok(), timeout=10)['passed'])
        (worktree / 'src' / 'app.py').write_text("print('second')\n", encoding='utf-8')
        with self.assertRaises(ValueError):
            self.manager.apply(row, self.project, require_verification=True)
        self.assertEqual((self.project / 'src' / 'app.py').read_text(encoding='utf-8'),
                         "print('hello')\n")

    def test_changed_unchanged_file_input_invalidates_verification(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('patch')\n", encoding='utf-8')
        frozen = self.manager.freeze(row)
        self.assertTrue(self.manager.verify(row, self.check_ok(), timeout=10)['passed'])
        (worktree / 'README.md').write_text('changed after verify\n', encoding='utf-8')
        after = self.manager.freeze(row)
        self.assertNotEqual(frozen['workspace_sha256'], after['workspace_sha256'])
        with self.assertRaises(ValueError):
            self.manager.apply(row, self.project, require_verification=True)

    def test_apply_refuses_head_change(self):
        row, worktree = self.job()
        (worktree / 'src' / 'app.py').write_text("print('head')\n", encoding='utf-8')
        (self.project / 'later.txt').write_text('later\n', encoding='utf-8')
        git(self.project, 'add', 'later.txt')
        git(self.project, 'commit', '-m', 'source moved')
        with self.assertRaises(ValueError):
            self.manager.apply(row, self.project)
        self.assertEqual((self.project / 'src' / 'app.py').read_text(encoding='utf-8'),
                         "print('hello')\n")

    def test_apply_refuses_changed_patch_after_application(self):
        row, worktree = self.job('job_applied_then_changed')
        (worktree / 'src' / 'app.py').write_text("print('applied')\n", encoding='utf-8')
        self.manager.apply(row, self.project)
        (worktree / 'src' / 'app.py').write_text("print('changed later')\n", encoding='utf-8')
        with self.assertRaises(RuntimeError):
            self.manager.freeze(row)
        with self.assertRaises(RuntimeError):
            self.manager.apply(row, self.project)

    # -- cleanup ---------------------------------------------------------------

    def test_cleanup_guards_and_discard(self):
        active, _ = self.job('job_active', status='running')
        plan = self.manager.cleanup(active, self.project, dry_run=True)
        self.assertFalse(plan['eligible'])
        self.assertIn('not terminal', plan['reason'])

        row, worktree = self.job('job_no_apply')
        (worktree / 'src' / 'app.py').write_text("print('discard me')\n", encoding='utf-8')
        frozen = self.manager.freeze(row)
        self.manager.verify(row, self.check_ok(), timeout=10)
        refused = self.manager.cleanup(row, self.project, dry_run=False)
        self.assertFalse(refused['eligible'])
        self.assertTrue(worktree.is_dir())
        self.assertTrue(Path(frozen['patch_path']).is_file())

        discarded = self.manager.cleanup(row, self.project, dry_run=False, discard=True)
        self.assertTrue(discarded['removed'])
        self.assertTrue(discarded['force'])
        self.assertFalse(worktree.is_dir())
        self.assertFalse(Path(frozen['patch_path']).exists())
        self.assertFalse(Path(discarded['private_paths'][1]['path']).exists())
        self.assertFalse(Path(discarded['private_paths'][3]['path']).exists())
        self.assertIn('retained', discarded['session_retention'])

    def test_cleanup_refuses_unverified_applied_force(self):
        row, worktree = self.job('job_applied')
        (worktree / 'src' / 'app.py').write_text("print('cleanup')\n", encoding='utf-8')
        self.manager.apply(row, self.project)
        refused = self.manager.cleanup(row, self.project, dry_run=True)
        self.assertFalse(refused['eligible'])
        self.assertIn('not verified', refused['reason'])
        self.assertTrue(worktree.is_dir())

    def test_cleanup_requires_verified_exact_artifact_for_force(self):
        row, worktree = self.job('job_verified')
        (worktree / 'src' / 'app.py').write_text("print('verified cleanup')\n", encoding='utf-8')
        self.manager.verify(row, self.check_ok(), timeout=10)
        applied = self.manager.apply(row, self.project, require_verification=True)
        frozen = self.manager.freeze(row)
        plan = self.manager.cleanup(row, self.project, dry_run=True)
        self.assertTrue(plan['eligible'])
        self.assertTrue(plan['force'])
        self.assertTrue(plan['verified_exact'])
        self.assertGreater(plan['workspace_bytes'], 0)
        self.assertTrue(Path(frozen['patch_path']).is_file())

        removed = self.manager.cleanup(row, self.project, dry_run=False)
        self.assertTrue(removed['removed'])
        self.assertFalse(worktree.is_dir())
        self.assertFalse(Path(frozen['patch_path']).exists())
        self.assertFalse(Path(removed['private_paths'][1]['path']).exists())
        self.assertFalse(Path(removed['private_paths'][2]['path']).exists())
        self.assertTrue(Path(applied['applied_record']).is_file())
        self.assertTrue(self.project.is_dir())
        self.assertNotIn(str(worktree), git(self.project, 'worktree', 'list', '--porcelain') or '')

        mismatch = self.manager.cleanup(row, self.project, dry_run=True)
        self.assertFalse(mismatch['eligible'])
        self.assertTrue(Path(applied['applied_record']).is_file())

    def test_cleanup_refuses_moved_worktree_head_without_discard(self):
        row, worktree = self.job('job_head_moved')
        (worktree / 'src' / 'app.py').write_text("print('committed')\n", encoding='utf-8')
        git(worktree, 'add', '.')
        git(worktree, 'commit', '-m', 'worktree commit')
        plan = self.manager.cleanup(row, self.project, dry_run=True)
        self.assertFalse(plan['eligible'])
        self.assertIn('HEAD moved', plan['reason'])
        self.assertTrue(worktree.is_dir())

    def test_cleanup_project_mismatch(self):
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init', '-b', 'main')
        row, _ = self.job('job_project')
        plan = self.manager.cleanup(row, other, dry_run=True, discard=True)
        self.assertFalse(plan['eligible'])
        self.assertIn('does not match', plan['reason'])


if __name__ == '__main__':
    unittest.main()
