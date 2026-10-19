"""Offline fixtures for usage export and efficiency summaries; no prompts or real IDs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import measure_usage


def codex_record(identifier, turn, **usage):
    return {'type': 'token_usage_record', 'payload': {
        'turn_id': turn, 'response_id': identifier, 'usage': usage}}


class MeasureUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value), encoding='utf-8')
        return path

    def test_codex_deduplicates_responses_and_not_event_counters(self):
        log = self.root / 'events.jsonl'
        records = [
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-a'}},
            codex_record('response-1', 'turn-a', input_tokens=10, cached_input_tokens=4,
                         output_tokens=5, reasoning_output_tokens=2, total_tokens=15),
            codex_record('response-1', 'turn-a', input_tokens=10, cached_input_tokens=4,
                         output_tokens=5, reasoning_output_tokens=2, total_tokens=15),
            codex_record('response-2', 'turn-a', input_tokens=20, cached_input_tokens=10,
                         output_tokens=7, reasoning_output_tokens=3, total_tokens=27),
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 'turn-a',
                                              'usage': {'input_tokens': 9999, 'total_tokens': 9999}}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 'turn-a',
                                              'usage': {'input_tokens': 9999, 'total_tokens': 9999}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-a'}},
        ]
        log.write_text('\n'.join(json.dumps(row) for row in records), encoding='utf-8')
        usage = measure_usage.codex_usage(log, 'turn-a')
        self.assertEqual(usage['recorded_model_responses'], 2)
        self.assertEqual(usage['tokens']['input_tokens'], 30)
        summary = measure_usage.efficiency_summary([usage])
        row = summary['agents'][0]
        self.assertEqual(row['recorded_model_responses'], 2)
        self.assertEqual(row['input_tokens'], 30)
        self.assertEqual(row['cached_input_tokens'], 14)
        self.assertEqual(row['noncached_input_tokens'], 16)
        self.assertEqual(row['output_tokens'], 12)  # reasoning is not added again
        self.assertEqual(row['reasoning_output_tokens'], 5)
        self.assertAlmostEqual(row['cached_input_share'], 14 / 30)
        self.assertNotIn('total_tokens', row)
        self.assertIsNone(summary['wall_clock_seconds'])

    def test_worker_counters_are_separate_and_duration_uses_execution_seconds(self):
        worker = self.write_json('worker.json', {
            'model': 'fixture/model', 'status': 'succeeded',
            'timing': {'queue_seconds': 1.5, 'execution_seconds': 12.5},
            'result': {
                'session_usage': {'assistant_turns': 2, 'tokens': {
                    'input': 100, 'output': 30, 'reasoning': 10,
                    'cache_read': 25, 'cache_write': 5}, 'reported_cost': 0.2},
                'assistant_message_usage': [
                    {'tokens': {'total': 50}}, {'tokens': {'total': 40}}],
                'partial': False,
            },
        })
        usage = measure_usage.worker_usage('worker-a', worker)
        self.assertEqual(usage['recorded_assistant_messages'], 2)
        self.assertEqual(usage['provider_reported_total_tokens'], 90)
        row = measure_usage.efficiency_summary([usage])['agents'][0]
        self.assertEqual((row['input_tokens'], row['output_tokens'], row['reasoning_tokens']),
                         (100, 30, 10))
        self.assertEqual((row['cache_read_tokens'], row['cache_write_tokens']), (25, 5))
        self.assertAlmostEqual(row['cached_input_share'], 25 / 125)
        self.assertEqual(row['recorded_assistant_messages'], 2)
        self.assertEqual(row['execution_seconds'], 12.5)
        self.assertIsNone(measure_usage.efficiency_summary([usage])['wall_clock_seconds'])

    def test_missing_and_zero_are_distinguished(self):
        zeros = self.write_json('zeros.json', {
            'timing': {'execution_seconds': 0},
            'result': {'session_usage': {'assistant_turns': 0, 'tokens': {
                'input': 0, 'output': 0, 'reasoning': 0, 'cache_read': 0, 'cache_write': 0}}},
        })
        missing = self.write_json('missing.json', {
            'timing': {'queue_seconds': 1},
            'result': {'session_usage': {'assistant_turns': 1, 'tokens': {
                'input': 5, 'output': 2, 'reasoning': 1, 'cache_read': 0, 'cache_write': 0}}},
        })
        invalid = self.write_json('invalid.json', {
            'result': {'session_usage': {'tokens': {
                'input': None, 'output': 'unknown', 'reasoning': float('inf'),
                'cache_read': -1, 'cache_write': True}}},
        })
        empty_worker = self.write_json('empty-worker.json', {'result': {}})
        empty_log = self.root / 'empty.jsonl'
        empty_log.write_text('', encoding='utf-8')
        zero_row = measure_usage.efficiency_summary(
            [measure_usage.worker_usage('zero', zeros)])['agents'][0]
        self.assertEqual(zero_row['input_tokens'], 0)
        self.assertEqual(zero_row['recorded_assistant_messages'], 0)
        self.assertEqual(zero_row['execution_seconds'], 0)
        self.assertIsNone(zero_row['cached_input_share'])  # zero denominator is not a rate
        missing_row = measure_usage.efficiency_summary(
            [measure_usage.worker_usage('missing', missing)])['agents'][0]
        self.assertEqual(missing_row['cache_read_tokens'], 0)
        self.assertEqual(missing_row['execution_seconds'], None)  # no timing.execution_seconds
        invalid_row = measure_usage.efficiency_summary(
            [measure_usage.worker_usage('invalid', invalid)])['agents'][0]
        for key in ('input_tokens', 'output_tokens', 'reasoning_tokens',
                    'cache_read_tokens', 'cache_write_tokens', 'cached_input_share'):
            self.assertIsNone(invalid_row[key])
        unavailable = measure_usage.efficiency_summary([
            measure_usage.worker_usage('empty', empty_worker),
            measure_usage.codex_usage(empty_log),
        ])
        worker_row, codex_row = unavailable['agents']
        self.assertEqual(worker_row['availability'], 'missing')
        self.assertIsNone(worker_row['recorded_assistant_messages'])
        self.assertIsNone(worker_row['execution_seconds'])
        self.assertEqual(codex_row['availability'], 'missing')
        self.assertIsNone(codex_row['recorded_model_responses'])
        self.assertIsNone(codex_row['input_tokens'])

    def test_provider_total_is_not_invented_when_missing(self):
        no_total = self.write_json('no-total.json', {
            'result': {'session_usage': {'assistant_turns': 1},
                       'assistant_message_usage': [{'tokens': {}}]}})
        explicit_zero = self.write_json('total-zero.json', {
            'result': {'session_usage': {'assistant_turns': 1},
                       'assistant_message_usage': [{'tokens': {'total': 0}}]}})
        self.assertIsNone(measure_usage.worker_usage('a', no_total)['provider_reported_total_tokens'])
        self.assertEqual(measure_usage.worker_usage('b', explicit_zero)['provider_reported_total_tokens'], 0)

    def test_worker_accepts_compact_and_wrapped_job_results(self):
        compact = self.write_json('compact.json', {
            'timing': {'execution_seconds': 3},
            'session_usage': {'assistant_turns': 1, 'tokens': {'input': 4, 'output': 2}},
        })
        full = {'model': 'fixture/model', 'timing': {'execution_seconds': 7},
                'result': {'session_usage': {'assistant_turns': 2, 'tokens': {'input': 8}}}}
        wrapped = self.write_json('wrapped.json', {
            'content': [{'type': 'text', 'text': json.dumps(full)}]})
        compact_usage = measure_usage.worker_usage('compact', compact)
        wrapped_usage = measure_usage.worker_usage('wrapped', wrapped)
        self.assertEqual(compact_usage['availability'], 'recorded')
        self.assertEqual(compact_usage['timing']['execution_seconds'], 3)
        self.assertEqual(wrapped_usage['availability'], 'recorded')
        self.assertEqual(wrapped_usage['recorded_assistant_messages'], 2)

    def test_assessment_validation_and_rate_rules(self):
        (self.root / 'empty.jsonl').write_text('', encoding='utf-8')
        usage = measure_usage.codex_usage(self.root / 'empty.jsonl')
        no_assessment = measure_usage.efficiency_summary([usage])
        self.assertIsNone(no_assessment['manual_report'])
        self.assertIsNone(no_assessment['first_pass_rate'])
        self.assertIsNone(no_assessment['wall_clock_seconds'])
        assessment = {'tasks_total': 10, 'first_pass_passed': 7, 'repair_rounds': 2,
                      'integration_failures': 1, 'post_acceptance_defects': 1,
                      'wall_clock_seconds': 120.5, 'concurrency': 2}
        summary = measure_usage.efficiency_summary([usage], assessment)
        self.assertAlmostEqual(summary['manual_report']['first_pass_rate'], 0.7)
        self.assertAlmostEqual(summary['first_pass_rate'], 0.7)
        self.assertEqual(summary['manual_report']['source'], 'manual_report')
        self.assertFalse(summary['manual_report']['independent_verification'])
        self.assertEqual(summary['wall_clock_seconds'], 120.5)
        self.assertEqual(summary['wall_clock_source'], 'manual_report')
        zero_tasks = measure_usage.efficiency_summary([usage], {
            'tasks_total': 0, 'first_pass_passed': 0})
        self.assertIsNone(zero_tasks['first_pass_rate'])
        missing_numerator = measure_usage.efficiency_summary([usage], {'tasks_total': 4})
        self.assertIsNone(missing_numerator['first_pass_rate'])
        invalid = [
            {'unknown_key': 1},
            {'tasks_total': True},
            {'tasks_total': -1},
            {'repair_rounds': 1.5},
            {'wall_clock_seconds': float('nan')},
            {'wall_clock_seconds': float('inf')},
            {'concurrency': 0},
            {'concurrency': True},
            {'concurrency': 1.5},
            {'tasks_total': 1, 'first_pass_passed': 2},
        ]
        for value in invalid:
            with self.assertRaises(ValueError):
                measure_usage.efficiency_summary([usage], value)

    def test_cli_prints_minimal_json_without_prompts_ids_or_raw_lists(self):
        log = self.root / 'cli.jsonl'
        rows = [
            {'type': 'turn_context', 'payload': {'model': 'fixture/model',
                                                  'prompt': 'FIXTURE_PROMPT_DO_NOT_EXPORT'}},
            codex_record('response-1', 'turn-cli', input_tokens=6, cached_input_tokens=2,
                         output_tokens=3, reasoning_output_tokens=1),
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-cli'}},
        ]
        log.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
        worker = self.write_json('cli-worker.json', {
            'model': 'fixture/model',
            'result': {'text': 'FIXTURE_PROMPT_DO_NOT_EXPORT', 'session_id': 'FIXTURE_ID_DO_NOT_EXPORT',
                       'session_usage': {'assistant_turns': 1, 'tokens': {'input': 2, 'output': 1}},
                       'assistant_message_usage': [{'tokens': {'total': 3}}]}})
        output = self.root / 'report.json'
        completed = subprocess.run([
            sys.executable, measure_usage.__file__, '--codex-log', str(log),
            '--turn-id', 'turn-cli', '--worker', 'worker-a=' + str(worker),
            '--output', str(output)], capture_output=True, text=True, check=True)
        printed = json.loads(completed.stdout)
        self.assertEqual(printed['schema_version'], 2)
        self.assertIn('efficiency_summary', printed)
        self.assertNotIn('per_response', completed.stdout)
        self.assertNotIn('per_assistant_message', completed.stdout)
        self.assertNotIn('FIXTURE_PROMPT_DO_NOT_EXPORT', completed.stdout)
        self.assertNotIn('FIXTURE_ID_DO_NOT_EXPORT', completed.stdout)
        report = json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(report['schema_version'], 2)
        self.assertIn('efficiency_summary', report)
        self.assertEqual(len(report['agents']), 2)


if __name__ == '__main__':
    unittest.main()
