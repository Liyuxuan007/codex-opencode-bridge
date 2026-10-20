"""Opt-in native permission contract probe; noReply=True, no model inference.

Uses the configured running OpenCode, creates only its own empty probe sessions,
and deletes those sessions afterwards. CI does not need OpenCode or run this file.
"""
import argparse
import json
from pathlib import Path
import tempfile
import bridge
from workflow import assert_native_permissions, worker_permissions


def probe():
    health = bridge.oc('/global/health')
    if not health.get('healthy'):
        raise RuntimeError('OpenCode must be healthy before native validation.')
    result = {'native_version': health.get('version'), 'inference_requested': False, 'cases': []}
    parent = Path.home() / 'projects'
    parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bridge-native-probe-', dir=parent) as directory:
        owned = []
        try:
            for isolation, legacy in (('readonly', True), ('readonly', False), ('worktree', False)):
                expected = worker_permissions(isolation)
                session = bridge.oc('/session', 'POST', {'title': 'Bridge native permission probe',
                                    'permission': expected}, directory)
                sid = session['id']
                owned.append(sid)
                assert_native_permissions(bridge.oc('/session/' + sid, directory=directory), expected)
                provider, model = bridge.CONFIG['default_model'].split('/', 1)
                body = {'noReply': True, 'agent': 'build',
                        'model': {'providerID': provider, 'modelID': model},
                        'parts': [{'type': 'text', 'text': 'Permission contract probe. No reply or tool execution.'}]}
                if legacy:
                    body['tools'] = {'task': False, 'question': False}
                bridge.oc('/session/' + sid + '/message', 'POST', body, directory)
                actual = bridge.oc('/session/' + sid, directory=directory)
                retained = actual.get('permission') == expected
                messages = bridge.oc('/session/' + sid + '/message', directory=directory)
                assistants = sum(message.get('info', {}).get('role') == 'assistant' for message in messages)
                if assistants:
                    raise AssertionError('noReply contract violated: unexpected assistant message.')
                if not legacy:
                    assert_native_permissions(actual, expected)
                acknowledged = bridge.oc('/session/' + sid + '/abort', 'POST', {}, directory) is True
                statuses = bridge.oc('/session/status', directory=directory)
                result['cases'].append({'isolation': isolation, 'legacy_tools': legacy,
                                        'permissions_retained': retained, 'assistant_messages': assistants,
                                        'abort_acknowledged': acknowledged,
                                        'absent_from_active_map': sid not in statuses})
        finally:
            for sid in owned:
                bridge.oc('/session/' + sid, 'DELETE', directory=directory)
    result['passed'] = all(case['permissions_retained'] for case in result['cases'] if not case['legacy_tools'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', help='Optional private report path; not a source artifact.')
    args = parser.parse_args()
    report = probe()
    if args.output:
        path = Path(args.output)
        path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        path.chmod(0o600)
    print(json.dumps(report))
