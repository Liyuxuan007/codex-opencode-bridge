#!/usr/bin/env python3
"""Tests for the pure workflow helpers."""
from __future__ import annotations

import copy
import json
import unittest

from workflow import compact_job, normalize_workflow, worker_prompt


class NormalizeWorkflowTests(unittest.TestCase):
    def test_defaults_and_risk_validation(self):
        self.assertEqual(normalize_workflow({}), {'risk': 'standard'})
        self.assertEqual(normalize_workflow(None), {'risk': 'standard'})
        for risk in ('low', 'standard', 'high'):
            self.assertEqual(normalize_workflow({'risk': risk}), {'risk': risk})

        for invalid in ('urgent', '', None, True, False, 1, []):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    normalize_workflow({'risk': invalid})

    def test_brief_subset_and_full_copy(self):
        args = {
            'risk': 'high',
            'brief': {
                'goal': 'Fix the parser',
                'interfaces': ['load(data) -> Node'],
                'acceptance': ['Malformed input raises ValueError'],
                'stop_conditions': ['No edits outside parser.py'],
            },
        }
        before = copy.deepcopy(args)
        normalized = normalize_workflow(args)
        self.assertEqual(normalized, args)
        self.assertEqual(args, before)

        # A subset is allowed and the result owns fresh containers.
        subset = {'goal': 'Investigate'}
        normalized = normalize_workflow({'brief': subset})
        self.assertEqual(normalized, {'risk': 'standard', 'brief': {'goal': 'Investigate'}})
        normalized['brief']['goal'] = 'Changed'
        self.assertEqual(subset, {'goal': 'Investigate'})

    def test_absent_or_null_brief_is_omitted(self):
        self.assertEqual(normalize_workflow({'risk': 'low'}), {'risk': 'low'})
        self.assertEqual(normalize_workflow({'brief': None}), {'risk': 'standard'})

    def test_invalid_briefs_are_rejected(self):
        invalid_briefs = [
            [],
            'goal',
            5,
            {'unknown': 'x'},
            {'goal': ''},
            {'goal': '   '},
            {'goal': 3},
            {'goal': True},
            {'goal': 'x' * 2001},
            {'interfaces': 'not a list'},
            {'interfaces': ['ok', '']},
            {'interfaces': ['ok', 4]},
            {'interfaces': ['x' * 1001]},
            {'acceptance': ['x'] * 13},
            {'stop_conditions': [True]},
        ]
        for brief in invalid_briefs:
            with self.subTest(brief=brief):
                with self.assertRaises(ValueError):
                    normalize_workflow({'brief': brief})

    def test_limit_boundaries_are_inclusive(self):
        brief = {
            'goal': 'x' * 2000,
            'interfaces': ['y' * 1000] * 12,
        }
        self.assertEqual(normalize_workflow({'brief': brief})['brief'], brief)

    def test_unknown_top_level_keys_are_ignored(self):
        # The parent may pass a larger argument mapping to this helper.
        self.assertEqual(
            normalize_workflow({'risk': 'low', 'task': 'ignored'}),
            {'risk': 'low'})


class WorkerPromptTests(unittest.TestCase):
    def test_hard_constraints_and_task_once(self):
        task = 'Unique task marker 7fd3c9'
        prompt = worker_prompt('/work/tree', {'task': task})
        self.assertEqual(prompt.count(task), 1)
        self.assertIn('/work/tree', prompt)
        self.assertIn('do not delegate to subagents', prompt)
        self.assertIn('do not git commit/push', prompt)
        self.assertIn('do not deploy or send messages', prompt)
        self.assertIn('Run relevant checks when allowed', prompt)
        self.assertIn('STATUS / CHANGES / CHECKS / RISKS / DECISIONS', prompt)
        self.assertIn('<=2000 characters', prompt)
        self.assertIn('Do not include transcripts', prompt)
        self.assertIn('do not claim tests prove correctness', prompt)
        self.assertIn('not a schema-validated model response', prompt)

    def test_risk_specific_guidance(self):
        high = worker_prompt('/w', {'task': 't', 'risk': 'high'})
        self.assertIn('failure and boundary checks', high)
        self.assertIn('unresolved risks', high)

        low = worker_prompt('/w', {'task': 't', 'risk': 'low'})
        self.assertIn('do not add unrelated tests', low)
        self.assertNotIn('failure and boundary checks', low)

        standard = worker_prompt('/w', {'task': 't', 'risk': 'standard'})
        self.assertNotIn('failure and boundary checks', standard)

    def test_brief_and_contract_are_readable_and_preserved(self):
        spec = {
            'task': 'Do the thing',
            'risk': 'high',
            'brief': {'goal': 'Keep API stable', 'acceptance': ['a', 'b']},
            'contract': {
                'allowed_paths': ['workflow.py'],
                'checks': [['python3', '-m', 'unittest', 'test_workflow', '-v']],
            },
        }
        before = copy.deepcopy(spec)
        prompt = worker_prompt('/w', spec)
        self.assertEqual(spec, before)
        self.assertIn('Task brief (readable JSON', prompt)
        self.assertIn('\n  "goal": "Keep API stable"', prompt)
        self.assertIn('Acceptance contract (scope rules are enforced', prompt)
        self.assertIn('"allowed_paths"', prompt)
        self.assertIn('test_workflow', prompt)
        self.assertIn('\n', prompt)

    def test_invalid_inputs(self):
        for workspace in ('', '   ', None, 3, True):
            with self.subTest(workspace=workspace):
                with self.assertRaises(ValueError):
                    worker_prompt(workspace, {'task': 'x'})
        for spec in (None, [], 'x', {}, {'task': ''}, {'task': 3},
                     {'task': 'x', 'risk': 'urgent'},
                     {'task': 'x', 'brief': {'goal': ''}},
                     {'task': 'x', 'brief': []}):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    worker_prompt('/w', spec)


class CompactJobTests(unittest.TestCase):
    def brief(self):
        return {
            'id': 'job_1',
            'status': 'attention_required',
            'owner': 'main',
            'model': 'provider/model',
            'isolation': 'worktree',
            'timing': {'queue_seconds': 1.2, 'execution_seconds': 4.5},
            'next_action': 'job_recover',
            'error': 'E' * 1500,
            'error_code': 'execution_uncertain',
            'retryable': False,
            'queued_reason': 'exclusive_lock',
            'request_id': 'request-secret',
            'session_id': 'session-secret',
            'workspace': '/synthetic/private/worktree',
            'base_commit': 'f' * 40,
            'project': '/synthetic/private/project',
            'created': 1,
            'updated': 2,
            'progress': {
                'phase': 'tool_running',
                'last_progress_at': 10.0,
                'stalled_seconds': 42.5,
                'warning': 'No observable progress.',
                'reasoning_chars': 11,
                'visible_chars': 22,
                'fingerprint': 'full-fingerprint',
                'recent_message_count': 7,
                'pending_questions': [{
                    'id': 'q1',
                    'session_id': 'session-secret',
                    'workspace': '/synthetic/private/worktree',
                    'text': 'Continue?',
                    'options': [{'id': 'yes', 'label': 'Yes'}],
                }],
                'running_tools': [
                    {'tool': 'bash', 'call_id': 'c1', 'started_at': 1.0},
                    {'tool': 'read', 'call_id': 'c2', 'started_at': 2.0},
                    {'tool': 'grep', 'call_id': 'c3', 'started_at': 3.0},
                    {'tool': 'edit', 'call_id': 'c4', 'started_at': 4.0},
                    {'tool': 'test', 'call_id': 'c5', 'started_at': 5.0},
                ],
            },
        }

    def result(self):
        return {
            'text': 'T' * 3000,
            'partial': False,
            'last_message_usage': {'tokens': {'input': 99}},
            'session_usage': {
                'assistant_turns': 2,
                'tokens': {'input': 5, 'output': 7, 'reasoning': 1,
                           'cache_read': 2, 'cache_write': 3},
                'reported_cost': 0.01,
                'scope': 'reported metadata only',
            },
            'verification': 'Agent completion only. Parent must verify.',
            'workspace_git_status': 'M ' + ('x' * 1500),
            'model_completed_at': 1234,
            'recent_tool_states': [{'tool': 'bash', 'status': 'completed'}],
            'artifact': {'patch_sha256': 'a' * 64},
        }

    def test_compacts_brief_and_result_without_inventing_success(self):
        payload = self.brief() | {'result': self.result()}
        before = copy.deepcopy(payload)
        compact = compact_job(payload)

        self.assertEqual(payload, before)
        self.assertEqual(compact['view'], 'compact')
        self.assertIn('view=full', compact['detail_hint'])
        for key in ('id', 'status', 'owner', 'model', 'isolation', 'timing',
                    'next_action', 'error_code', 'retryable', 'queued_reason'):
            self.assertIn(key, compact)
        self.assertEqual(compact['id'], 'job_1')
        self.assertEqual(compact['status'], 'attention_required')

        for key in ('request_id', 'session_id', 'workspace', 'base_commit',
                    'project', 'created', 'updated'):
            self.assertNotIn(key, compact)

        self.assertEqual(len(compact['error']), 1000)
        self.assertTrue(compact['error_truncated'])

        progress = compact['progress']
        self.assertEqual(progress['phase'], 'tool_running')
        self.assertEqual(progress['age_seconds'], 42.5)
        self.assertEqual(progress['stale_warning'], 'No observable progress.')
        self.assertNotIn('fingerprint', progress)
        self.assertNotIn('recent_message_count', progress)
        self.assertEqual(progress['running_tools'], ['bash', 'read', 'grep', 'edit'])
        self.assertTrue(progress['running_tools_truncated'])
        self.assertNotIn('session_id', progress['pending_questions'][0])
        self.assertNotIn('workspace', progress['pending_questions'][0])
        self.assertEqual(progress['pending_questions'][0]['text'], 'Continue?')

        result = compact['result']
        self.assertEqual(len(result['text']), 2500)
        self.assertEqual(result['text_chars'], 3000)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['session_usage'], self.result()['session_usage'])
        self.assertEqual(result['verification'], self.result()['verification'])
        self.assertEqual(len(result['workspace_git_status']), 1000)
        self.assertTrue(result['workspace_git_status_truncated'])
        self.assertEqual(result['model_completed_at'], 1234)
        self.assertNotIn('last_message_usage', result)
        self.assertNotIn('recent_tool_states', result)
        self.assertNotIn('artifact', result)
        # No invented aggregate token counter.
        self.assertNotIn('total', result['session_usage'])

    def test_text_limit_and_zero_limit(self):
        payload = {'id': 'j', 'status': 'succeeded', 'result': {'text': 'abcdef'}}
        compact = compact_job(payload, max_chars=3)
        self.assertEqual(compact['result']['text'], 'abc')
        self.assertEqual(compact['result']['text_chars'], 6)
        self.assertTrue(compact['result']['truncated'])

        compact = compact_job(payload, max_chars=0)
        self.assertEqual(compact['result']['text'], '')
        self.assertEqual(compact['result']['text_chars'], 6)
        self.assertTrue(compact['result']['truncated'])

        exact = compact_job(payload, max_chars=6)
        self.assertEqual(exact['result']['text'], 'abcdef')
        self.assertFalse(exact['result']['truncated'])

    def test_null_result_stays_null_and_missing_result_is_absent(self):
        compact = compact_job({'id': 'j', 'status': 'failed', 'result': None})
        self.assertIn('result', compact)
        self.assertIsNone(compact['result'])

        compact = compact_job({'id': 'j', 'status': 'running'})
        self.assertNotIn('result', compact)

    def test_missing_verification_is_not_invented(self):
        compact = compact_job({'id': 'j', 'status': 'succeeded',
                               'result': {'text': 'done'}})
        self.assertNotIn('verification', compact['result'])
        self.assertNotIn('workspace_git_status', compact['result'])

    def test_verification_dict_and_null_error_are_preserved(self):
        payload = {
            'error': None,
            'result': {'verification': {'status': 'unknown', 'evidence': ['x']}},
        }
        before = copy.deepcopy(payload)
        compact = compact_job(payload)
        self.assertIsNone(compact['error'])
        self.assertFalse(compact['error_truncated'])
        self.assertEqual(compact['result']['verification'], payload['result']['verification'])
        self.assertEqual(payload, before)
        compact['result']['verification']['status'] = 'changed'
        self.assertEqual(payload, before)

    def test_no_mutation_and_fresh_nested_objects(self):
        payload = self.brief() | {'result': self.result()}
        before = copy.deepcopy(payload)
        compact = compact_job(payload)
        self.assertEqual(payload, before)
        self.assertIsNot(compact['timing'], payload['timing'])
        self.assertIsNot(compact['progress'], payload['progress'])
        self.assertIsNot(compact['result']['session_usage'], payload['result']['session_usage'])

        compact['timing']['queue_seconds'] = 999
        compact['progress']['running_tools'][0] = 'changed'
        compact['result']['session_usage']['tokens']['input'] = 999
        self.assertEqual(payload, before)
        self.assertEqual(payload['result']['session_usage']['tokens']['input'], 5)

    def test_wrapper_payload_is_supported(self):
        compact = compact_job({'brief': {'id': 'j', 'status': 'failed'}, 'result': None})
        self.assertEqual(compact['id'], 'j')
        self.assertIsNone(compact['result'])

    def test_output_is_json_serializable(self):
        compact = compact_job(self.brief() | {'result': self.result()})
        encoded = json.dumps(compact)
        self.assertIn('"view": "compact"', encoded)

    def test_max_chars_validation(self):
        for invalid in (True, False, -1, 60001, 1.5, '30', None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    compact_job({}, max_chars=invalid)
        self.assertEqual(compact_job({}, max_chars=0)['view'], 'compact')
        self.assertEqual(compact_job({}, max_chars=60000)['view'], 'compact')

    def test_invalid_payload_shapes_are_rejected(self):
        for payload in (None, [], 'x', 3):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    compact_job(payload)
        with self.assertRaises(ValueError):
            compact_job({'result': 'not an object'})
        with self.assertRaises(ValueError):
            compact_job({'progress': 'not an object'})
        with self.assertRaises(ValueError):
            compact_job({'progress': {'running_tools': 'bad'}})
        with self.assertRaises(ValueError):
            compact_job({'result': {'text': 3}})
        with self.assertRaises(ValueError):
            compact_job({'result': {'session_usage': 'bad'}})
        with self.assertRaises(ValueError):
            compact_job({'result': {'verification': 3}})
        with self.assertRaises(ValueError):
            compact_job({'result': {'workspace_git_status': 3}})


if __name__ == '__main__':
    unittest.main()
