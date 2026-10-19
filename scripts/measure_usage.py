"""Export counters only, never prompts, credentials, tool arguments or responses.

Codex token_usage_record entries are authoritative recorded responses; token_count
events are deliberately not added a second time. Worker inputs are saved job_result
JSON responses. OpenCode assistant-turn counts are not upstream HTTP request counts.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time


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


def worker_usage(label, path):
    item = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if 'content' in item:
        item = json.loads(next(part['text'] for part in item['content'] if part.get('type') == 'text'))
    result = item.get('result') or {}
    usage = result.get('session_usage')
    message_usage = result.get('assistant_message_usage')
    return {'agent': label, 'availability': 'recorded' if usage else 'missing',
            'model': item.get('model'), 'status': item.get('status'), 'timing': item.get('timing'),
            'session_usage': usage, 'upstream_request_count': None,
            'per_assistant_message': message_usage,
            'recorded_assistant_messages': len(message_usage) if message_usage is not None else (usage or {}).get('assistant_turns'),
            'provider_reported_total_tokens': sum(row['tokens'].get('total', 0) for row in message_usage) if message_usage is not None else None,
            'scope': 'Retained unique OpenCode assistant metadata; includes reported usage, not provider billing.',
            'partial': result.get('partial'), 'account_billing': None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-log', required=True)
    parser.add_argument('--turn-id')
    parser.add_argument('--worker', action='append', default=[], help='LABEL=path-to-job-result.json')
    parser.add_argument('--output', required=True)
    parser.add_argument('--wait-complete-seconds', type=int, default=0,
                        help='Optional local finalizer: wait for the selected turn to finish, then export its final response usage.')
    args = parser.parse_args()
    if not 0 <= args.wait_complete_seconds <= 3600:
        parser.error('wait-complete-seconds must be between 0 and 3600')
    if args.wait_complete_seconds and not args.turn_id:
        parser.error('--turn-id is required when waiting for completion')
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
    report = {'schema_version': 1, 'snapshot_at': datetime.now(timezone.utc).isoformat(), 'agents': agents,
              'notes': ['Cached input is a subset of Codex input; do not add it again.',
                        'Codex reasoning_output_tokens is a subset of output; do not add it again.',
                        'OpenCode reasoning/cache semantics are provider-specific; retain separate fields.',
                        'Recorded responses/assistant turns do not include every failed or retried provider request.',
                        'Raw prompts and local session identifiers are intentionally not exported.']}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    output.chmod(0o600)
    print(json.dumps({'agents': [{k: v for k, v in agent.items() if k not in ('per_response', 'per_assistant_message')} for agent in agents]}, ensure_ascii=False))


if __name__ == '__main__':
    main()
