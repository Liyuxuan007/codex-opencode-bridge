#!/usr/bin/env python3
"""Small, pure-stdlib helpers for bounded coordinator/worker workflows.

This module deliberately does not import the bridge or touch the network.  The
helpers normalise caller input, render the worker instruction, and build a
compact job view.  Compaction is a presentation aid only: it never turns a
missing or uncertain execution result into a successful verification claim.
"""
from __future__ import annotations

import copy
import json

RISK_LEVELS = ('low', 'standard', 'high')
BRIEF_FIELDS = ('goal', 'interfaces', 'acceptance', 'stop_conditions')
BRIEF_LIST_FIELDS = ('interfaces', 'acceptance', 'stop_conditions')
MAX_GOAL_CHARS = 2000
MAX_BRIEF_ITEMS = 12
MAX_BRIEF_ITEM_CHARS = 1000
MAX_ERROR_CHARS = 1000
MAX_GIT_STATUS_CHARS = 1000
MAX_RUNNING_TOOLS = 4
ESSENTIAL_KEYS = ('id', 'status', 'owner', 'model', 'isolation', 'timing',
                  'next_action', 'error', 'error_code', 'retryable', 'queued_reason')
_MISSING = object()


def _validate_risk(value):
    if isinstance(value, bool) or not isinstance(value, str) or value not in RISK_LEVELS:
        raise ValueError('risk must be one of: low, standard, high.')
    return value


def _validate_brief_text(value, name, max_chars):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a nonempty string.')
    if len(value) > max_chars:
        raise ValueError(f'{name} must be at most {max_chars} characters.')
    return value


def _normalize_brief(value):
    """Return a fresh, validated brief without retaining input references."""
    if not isinstance(value, dict):
        raise ValueError('brief must be an object.')
    unknown = set(value) - set(BRIEF_FIELDS)
    if unknown:
        raise ValueError('Unknown brief fields: ' + ', '.join(sorted(map(str, unknown))))

    brief = {}
    if 'goal' in value:
        brief['goal'] = _validate_brief_text(value['goal'], 'brief.goal', MAX_GOAL_CHARS)

    for key in BRIEF_LIST_FIELDS:
        if key not in value:
            continue
        items = value[key]
        if not isinstance(items, list):
            raise ValueError(f'brief.{key} must be a list of nonempty strings.')
        if len(items) > MAX_BRIEF_ITEMS:
            raise ValueError(f'brief.{key} must contain at most {MAX_BRIEF_ITEMS} items.')
        normalized = []
        for index, item in enumerate(items):
            normalized.append(_validate_brief_text(
                item, f'brief.{key}[{index}]', MAX_BRIEF_ITEM_CHARS))
        brief[key] = normalized
    return brief


def normalize_workflow(args):
    """Normalise optional workflow controls.

    ``args`` may be ``None`` (all defaults), a mapping containing ``risk`` and
    an optional ``brief``, or a mapping without either key.  Only the returned
    object is new; the caller's object is never modified.  Unknown top-level
    keys are ignored so this helper can be handed a larger argument mapping,
    while the brief itself is strict about unknown fields and types.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError('workflow arguments must be an object.')

    risk = _validate_risk(args.get('risk', 'standard'))
    result = {'risk': risk}
    brief = args.get('brief')
    if brief is not None:
        result['brief'] = _normalize_brief(brief)
    return result


def _readable_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def worker_prompt(workspace, spec):
    """Build the worker instruction for one bounded job.

    The prompt keeps the original hard constraints, includes the task exactly
    once, and appends optional brief/contract context plus non-enforceable
    reporting guidance.  It does not claim the final report can be validated
    as a schema.
    """
    if not isinstance(workspace, str) or not workspace.strip():
        raise ValueError('workspace must be a nonempty string.')
    if not isinstance(spec, dict):
        raise ValueError('spec must be an object.')

    task = spec.get('task')
    if not isinstance(task, str) or not task.strip():
        raise ValueError('spec.task must be a nonempty string.')
    risk = _validate_risk(spec.get('risk', 'standard'))

    brief = spec.get('brief')
    if brief is not None:
        brief = _normalize_brief(brief)
    contract = spec.get('contract')

    sections = [
        f"Work only in {workspace}. Complete this bounded task: do not delegate to subagents, "
        "do not git commit/push, do not deploy or send messages. Run relevant checks when allowed. "
        "Finish with a concise summary, changed files, actual check results, and unresolved issues.",
        "Task:\n" + task,
    ]

    if risk == 'high':
        sections.append(
            "Risk: high. Explicitly perform relevant failure and boundary checks, and report "
            "unresolved risks or uncertainty instead of implying they were verified.")
    elif risk == 'low':
        sections.append(
            "Risk: low. Run checks relevant to this task; do not add unrelated tests.")
    else:
        sections.append(
            "Risk: standard. Run relevant checks and include boundary cases when the task warrants them.")

    if brief is not None:
        sections.append(
            "Task brief (readable JSON; absent fields are not evidence of success):\n"
            + _readable_json(brief))

    if contract is not None:
        # Keep the bridge's compact contract representation and its key order.
        sections.append(
            "Acceptance contract (scope rules are enforced on the resulting patch):\n"
            + json.dumps(contract, ensure_ascii=False))

    sections.append(
        "TARGET report (guidance only, not a schema-validated model response; the parent does not "
        "parse it as a schema): keep it <=2000 characters and use STATUS / CHANGES / CHECKS / "
        "RISKS / DECISIONS. Include actual commands and exit codes (or 'not run' with the reason), "
        "changed files, and unresolved issues. Do not include transcripts, and do not claim tests "
        "prove correctness.")
    return '\n\n'.join(sections)


def validate_max_chars(max_chars):
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise ValueError('max_chars must be an integer from 0 to 60000.')
    if not 0 <= max_chars <= 60000:
        raise ValueError('max_chars must be an integer from 0 to 60000.')
    return max_chars


def _compact_error(value):
    if value is None:
        return None, False
    if isinstance(value, str):
        return value[:MAX_ERROR_CHARS], len(value) > MAX_ERROR_CHARS
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[:MAX_ERROR_CHARS], len(rendered) > MAX_ERROR_CHARS


def _compact_questions(questions):
    if questions is None:
        return None
    if not isinstance(questions, list):
        raise ValueError('progress.pending_questions must be a list or null.')
    compacted = []
    for question in questions:
        if not isinstance(question, dict):
            compacted.append(copy.deepcopy(question))
            continue
        compacted.append({
            key: copy.deepcopy(value)
            for key, value in question.items()
            if key not in ('session_id', 'sessionID', 'workspace')
        })
    return compacted


def _compact_progress(progress):
    if progress is None:
        return None
    if not isinstance(progress, dict):
        raise ValueError('progress must be an object or null.')

    compacted = {}
    for key in ('phase', 'last_progress_at', 'reasoning_chars', 'visible_chars'):
        if key in progress:
            compacted[key] = copy.deepcopy(progress[key])

    if 'age_seconds' in progress:
        compacted['age_seconds'] = copy.deepcopy(progress['age_seconds'])
    elif 'stalled_seconds' in progress:
        compacted['age_seconds'] = copy.deepcopy(progress['stalled_seconds'])

    if 'stale_warning' in progress:
        compacted['stale_warning'] = copy.deepcopy(progress['stale_warning'])
    elif 'warning' in progress:
        compacted['stale_warning'] = copy.deepcopy(progress['warning'])

    if 'pending_questions' in progress:
        compacted['pending_questions'] = _compact_questions(progress['pending_questions'])

    if 'running_tools' in progress:
        tools = progress['running_tools']
        if tools is None:
            compacted['running_tools'] = None
            compacted['running_tools_truncated'] = False
        elif isinstance(tools, list):
            names = []
            for tool in tools:
                if isinstance(tool, dict):
                    name = tool.get('tool')
                    if isinstance(name, str) and name:
                        names.append(name)
                elif isinstance(tool, str) and tool:
                    names.append(tool)
            compacted['running_tools'] = names[:MAX_RUNNING_TOOLS]
            compacted['running_tools_truncated'] = len(tools) > MAX_RUNNING_TOOLS
        else:
            raise ValueError('progress.running_tools must be a list or null.')
    return compacted


def _compact_result(result, max_chars):
    if not isinstance(result, dict):
        raise ValueError('result must be an object or null.')
    compacted = {}

    if 'text' in result:
        text = result['text']
        if text is None:
            compacted['text'] = None
            compacted['text_chars'] = 0
            compacted['truncated'] = False
        elif isinstance(text, str):
            compacted['text'] = text[:max_chars]
            compacted['text_chars'] = len(text)
            compacted['truncated'] = len(text) > max_chars
        else:
            raise ValueError('result.text must be a string or null.')

    if 'partial' in result:
        compacted['partial'] = copy.deepcopy(result['partial'])

    if 'session_usage' in result:
        usage = result['session_usage']
        if usage is not None and not isinstance(usage, dict):
            raise ValueError('result.session_usage must be an object or null.')
        # Preserve the reported counters exactly; never sum or recompute tokens.
        compacted['session_usage'] = copy.deepcopy(usage)

    if 'verification' in result:
        verification = result['verification']
        if verification is not None and not isinstance(verification, (str, dict)):
            raise ValueError('result.verification must be a string, object, or null.')
        compacted['verification'] = copy.deepcopy(verification)

    if 'workspace_git_status' in result:
        status = result['workspace_git_status']
        if status is None:
            compacted['workspace_git_status'] = None
            compacted['workspace_git_status_truncated'] = False
        elif isinstance(status, str):
            compacted['workspace_git_status'] = status[:MAX_GIT_STATUS_CHARS]
            compacted['workspace_git_status_truncated'] = len(status) > MAX_GIT_STATUS_CHARS
        else:
            raise ValueError('result.workspace_git_status must be a string or null.')

    if 'model_completed_at' in result:
        compacted['model_completed_at'] = copy.deepcopy(result['model_completed_at'])
    return compacted


def compact_job(payload, max_chars=2500):
    """Return a fresh compact job view without mutating the caller payload.

    ``payload`` is either a Broker brief (optionally merged with ``result``) or
    a wrapper containing ``brief`` and ``result``.  A null result is preserved
    as null; missing verification is omitted rather than reported as verified.
    """
    validate_max_chars(max_chars)
    if not isinstance(payload, dict):
        raise ValueError('payload must be an object.')

    if isinstance(payload.get('brief'), dict):
        brief = payload['brief']
        result_source = payload['result'] if 'result' in payload else _MISSING
    else:
        brief = payload
        result_source = payload['result'] if 'result' in payload else _MISSING

    compacted = {
        key: copy.deepcopy(brief[key])
        for key in ESSENTIAL_KEYS
        if key in brief
    }

    if 'error' in compacted:
        compacted['error'], truncated = _compact_error(compacted['error'])
        compacted['error_truncated'] = truncated

    if 'progress' in brief:
        compacted['progress'] = _compact_progress(brief['progress'])

    if result_source is not _MISSING:
        if result_source is None:
            compacted['result'] = None
        else:
            compacted['result'] = _compact_result(result_source, max_chars)

    compacted['view'] = 'compact'
    compacted['detail_hint'] = 'Use view=full for the complete job record.'
    return compacted


__all__ = ['normalize_workflow', 'worker_prompt', 'compact_job']
