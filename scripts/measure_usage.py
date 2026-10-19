"""Export counters only, never prompts, credentials, tool arguments or responses.

Codex token_usage_record entries are authoritative recorded responses; token_count
events are deliberately not added a second time. Worker inputs are saved job_result
JSON responses. OpenCode assistant-turn counts are not upstream HTTP request counts.

The efficiency summary reports cached input and reasoning separately because they are
subsets of other counters in some formats and independent counters in others. It does
not add token totals across providers and does not treat recorded cost as billing.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time


COUNT_KEYS = ('tasks_total', 'first_pass_passed', 'repair_rounds',
              'integration_failures', 'post_acceptance_defects')
ASSESSMENT_KEYS = frozenset(COUNT_KEYS + ('wall_clock_seconds', 'concurrency'))


def _finite(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _number(value):
    """Return a finite non-negative number, or None for missing/invalid data."""
    value = _finite(value)
    if value is None or value < 0:
        return None
    return value


def _count(value):
    """Return a non-negative integer counter, or None when unavailable."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate JSON key: ' + str(key))
        value[key] = item
    return value


def validate_assessment(assessment):
    """Validate a manual measurement report and return its restricted counters."""
    if assessment is None:
        return None
    if not isinstance(assessment, dict):
        raise ValueError('Assessment must be a JSON object.')
    unknown = sorted(set(assessment) - ASSESSMENT_KEYS)
    if unknown:
        raise ValueError('Unknown assessment key(s): ' + ', '.join(unknown))
    result = {}
    for key in COUNT_KEYS:
        if key not in assessment:
            continue
        value = assessment[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(key + ' must be a non-negative integer.')
        result[key] = value
    for key in ('wall_clock_seconds', 'concurrency'):
        if key not in assessment:
            continue
        value = _finite(assessment[key])
        if value is None or value < 0:
            raise ValueError(key + ' must be a finite non-negative number.')
        if key == 'concurrency' and (not isinstance(value, int) or value <= 0):
            raise ValueError('concurrency must be a positive integer.')
        result[key] = value
    if ('tasks_total' in result and 'first_pass_passed' in result
            and result['first_pass_passed'] > result['tasks_total']):
        raise ValueError('first_pass_passed cannot exceed tasks_total.')
    return result


def load_assessment(path):
    value = json.loads(Path(path).read_text(encoding='utf-8-sig'), object_pairs_hook=_pairs)
    return validate_assessment(value)


def _codex_summary(agent):
    tokens = agent.get('tokens')
    tokens = tokens if isinstance(tokens, dict) else {}
    input_tokens = _number(tokens.get('input_tokens'))
    cached = _number(tokens.get('cached_input_tokens'))
    output = _number(tokens.get('output_tokens'))
    reasoning = _number(tokens.get('reasoning_output_tokens'))
    if input_tokens is not None and cached is not None and cached <= input_tokens:
        noncached = input_tokens - cached
        share = cached / input_tokens if input_tokens > 0 else None
    else:
        noncached = None
        share = None
    availability = agent.get('availability') or ('recorded' if tokens else 'missing')
    return {'agent': agent.get('agent'), 'model': agent.get('model'), 'kind': 'codex',
            'availability': availability, 'input_tokens': input_tokens,
            'cached_input_tokens': cached, 'noncached_input_tokens': noncached,
            'output_tokens': output, 'reasoning_output_tokens': reasoning,
            'cached_input_share': share,
            'recorded_model_responses': _count(agent.get('recorded_model_responses')),
            'execution_seconds': None}


def _worker_summary(agent):
    usage = agent.get('session_usage')
    tokens = usage.get('tokens') if isinstance(usage, dict) else None
    tokens = tokens if isinstance(tokens, dict) else {}
    input_tokens = _number(tokens.get('input'))
    output = _number(tokens.get('output'))
    reasoning = _number(tokens.get('reasoning'))
    cache_read = _number(tokens.get('cache_read'))
    cache_write = _number(tokens.get('cache_write'))
    if input_tokens is not None and cache_read is not None and input_tokens + cache_read > 0:
        share = cache_read / (input_tokens + cache_read)
    else:
        share = None
    timing = agent.get('timing')
    duration = _number(timing.get('execution_seconds')) if isinstance(timing, dict) else None
    availability = agent.get('availability') or ('recorded' if usage else 'missing')
    return {'agent': agent.get('agent'), 'model': agent.get('model'), 'kind': 'worker',
            'availability': availability, 'input_tokens': input_tokens,
            'output_tokens': output, 'reasoning_tokens': reasoning,
            'cache_read_tokens': cache_read, 'cache_write_tokens': cache_write,
            'cached_input_share': share,
            'cached_input_share_basis': 'cache_read / (input + cache_read); provider-specific counters',
            'recorded_assistant_messages': _count(agent.get('recorded_assistant_messages')),
            'execution_seconds': duration}


def _manual_report(assessment):
    if assessment is None:
        return None, None, None
    report = {key: assessment.get(key) for key in COUNT_KEYS}
    report['wall_clock_seconds'] = assessment.get('wall_clock_seconds')
    report['concurrency'] = assessment.get('concurrency')
    tasks_total = report['tasks_total']
    passed = report['first_pass_passed']
    rate = passed / tasks_total if tasks_total and passed is not None else None
    report['first_pass_rate'] = rate
    report['source'] = 'manual_report'
    report['independent_verification'] = False
    return report, rate, report['wall_clock_seconds']


def efficiency_summary(agents, assessment=None):
    """Build deterministic per-agent counters without prompts, IDs or mixed totals.

    Assessment is optional. Its rate is computed only when both the numerator and a
    positive denominator are supplied; the result is self-reported, not verified.
    """
    assessment = validate_assessment(assessment)
    if agents is None:
        agents = []
    rows = []
    for agent in agents:
        if not isinstance(agent, dict):
            raise ValueError('Each agent entry must be an object.')
        tokens = agent.get('tokens')
        is_codex = (agent.get('agent') == 'codex-coordinator'
                    or 'recorded_model_responses' in agent
                    or (isinstance(tokens, dict) and 'input_tokens' in tokens))
        rows.append(_codex_summary(agent) if is_codex else _worker_summary(agent))
    manual, first_pass_rate, wall_clock = _manual_report(assessment)
    return {'agents': rows, 'manual_report': manual,
            'first_pass_rate': first_pass_rate, 'wall_clock_seconds': wall_clock,
            'wall_clock_source': 'manual_report' if wall_clock is not None else None,
            'notes': ['Per-worker execution_seconds values are not summed.',
                      'wall_clock_seconds is null unless supplied by manual_report.',
                      'Cached and reasoning counters are retained separately and are not added to totals.',
                      'Manual assessment is self-reported, not independent verification.']}


def codex_usage(path, turn_id=None):
    records = []
    current = None
    events = []
    model = None
    completed = set()
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # A live JSONL file may end with an incomplete record.
            payload = item.get('payload', {})
            if item.get('type') == 'event_msg':
                if payload.get('type') == 'task_started':
                    current = payload.get('turn_id')
                elif payload.get('type') == 'task_complete':
                    completed.add(payload.get('turn_id'))
            if item.get('type') == 'token_usage_record':
                records.append((item.get('timestamp'), payload))
            if item.get('type') == 'turn_context':
                events.append((current, 'model', payload.get('model')))
            if item.get('type') == 'response_item' and payload.get('type') == 'function_call':
                events.append((current, 'tool', payload.get('name', 'unknown')))
    selected = turn_id or current
    totals = Counter()
    seen = set()
    calls = []
    for timestamp, payload in records:
        if payload.get('turn_id') != selected:
            continue
        identifier = payload.get('response_id')
        if not identifier:
            raise ValueError('Missing response_id; cannot safely deduplicate Codex usage.')
        if identifier in seen:
            continue
        seen.add(identifier)
        usage = payload.get('usage', {})
        totals.update({key: value for key, value in usage.items() if isinstance(value, (int, float))})
        calls.append({'recorded_at': timestamp, 'usage': usage})
    tools = Counter()
    for event_turn, kind, value in events:
        if event_turn == selected:
            if kind == 'model':
                model = value
            elif kind == 'tool':
                tools[value] += 1
    if not calls:
        return {'agent': 'codex-coordinator', 'availability': 'missing',
                'reason': 'No per-response token_usage_record entries for selected turn.'}
    return {'agent': 'codex-coordinator', 'availability': 'recorded', 'model': model,
            'recorded_model_responses': len(calls), 'tokens': dict(totals),
            'tool_calls_by_name': dict(tools) or None, 'per_response': calls,
            'turn_completed_at_snapshot': selected in completed,
            'scope': 'Selected Codex turn only; snapshot excludes responses not yet recorded.',
            'account_billing': None}


def _provider_total(message_usage):
    """Sum provider-reported per-message totals only when every value is present."""
    if not isinstance(message_usage, list) or not message_usage:
        return None
    total = 0
    for row in message_usage:
        tokens = row.get('tokens') if isinstance(row, dict) else None
        value = _number(tokens.get('total')) if isinstance(tokens, dict) else None
        if value is None:
            return None
        total += value
    return total


def _worker_result(item):
    """Accept MCP content wrappers, full job_result objects and compact result JSON."""
    for _ in range(2):
        if not isinstance(item, dict) or not isinstance(item.get('content'), list):
            break
        text = next((part.get('text') for part in item['content']
                     if isinstance(part, dict) and part.get('type') == 'text'), None)
        if text is None:
            break
        item = json.loads(text)
    if not isinstance(item, dict):
        raise ValueError('Worker input must be a JSON object.')
    result = item.get('result')
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict):
        result = item if any(key in item for key in
                             ('session_usage', 'assistant_message_usage', 'partial', 'text')) else {}
    return item, result


def worker_usage(label, path):
    item = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    item, result = _worker_result(item)
    usage = result.get('session_usage')
    message_usage = result.get('assistant_message_usage')
    assistant_turns = _count(usage.get('assistant_turns')) if isinstance(usage, dict) else None
    recorded = len(message_usage) if isinstance(message_usage, list) else assistant_turns
    return {'agent': label, 'availability': 'recorded' if usage else 'missing',
            'model': item.get('model') or result.get('model'),
            'status': item.get('status') or result.get('status'),
            'timing': item.get('timing') or result.get('timing'),
            'session_usage': usage, 'upstream_request_count': None,
            'per_assistant_message': message_usage, 'recorded_assistant_messages': recorded,
            'provider_reported_total_tokens': _provider_total(message_usage),
            'scope': 'Retained unique OpenCode assistant metadata; includes reported usage, not provider billing.',
            'partial': result.get('partial'), 'account_billing': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-log', required=True)
    parser.add_argument('--turn-id')
    parser.add_argument('--worker', action='append', default=[], help='LABEL=path-to-job-result.json')
    parser.add_argument('--assessment', help='Optional manual measurement report JSON (restricted keys).')
    parser.add_argument('--output', required=True)
    parser.add_argument('--wait-complete-seconds', type=int, default=0,
                        help='Optional local finalizer: wait for the selected turn to finish, then export its final response usage.')
    args = parser.parse_args()
    if not 0 <= args.wait_complete_seconds <= 3600:
        parser.error('wait-complete-seconds must be between 0 and 3600')
    if args.wait_complete_seconds and not args.turn_id:
        parser.error('--turn-id is required when waiting for completion')
    assessment = None
    if args.assessment:
        try:
            assessment = load_assessment(args.assessment)
        except (OSError, ValueError) as exc:
            parser.error('invalid assessment: ' + str(exc))
    deadline = time.monotonic() + args.wait_complete_seconds
    while time.monotonic() < deadline:
        with Path(args.codex_log).open('rb') as stream:
            stream.seek(max(0, Path(args.codex_log).stat().st_size - 262144))
            tail = stream.read().decode('utf-8', errors='replace')
        done = False
        for line in tail.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            payload = entry.get('payload', {})
            if entry.get('type') == 'event_msg' and payload.get('type') == 'task_complete' and payload.get('turn_id') == args.turn_id:
                done = True
                break
        if done:
            break
        time.sleep(1)
    agents = [codex_usage(args.codex_log, args.turn_id)]
    for spec in args.worker:
        label, path = spec.split('=', 1)
        agents.append(worker_usage(label, path))
    summary = efficiency_summary(agents, assessment)
    report = {'schema_version': 2, 'snapshot_at': datetime.now(timezone.utc).isoformat(), 'agents': agents,
              'efficiency_summary': summary,
              'notes': ['Cached input is a subset of Codex input; do not add it again.',
                        'Codex reasoning_output_tokens is a subset of output; do not add it again.',
                        'OpenCode reasoning/cache semantics are provider-specific; retain separate fields.',
                        'Recorded responses/assistant turns do not include every failed or retried provider request.',
                        'Raw prompts and local session identifiers are intentionally not exported.']}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output.chmod(0o600)
    visible = [{key: value for key, value in agent.items()
                if key not in ('per_response', 'per_assistant_message')} for agent in agents]
    print(json.dumps({'schema_version': 2, 'agents': visible, 'efficiency_summary': summary},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
