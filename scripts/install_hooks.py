"""Install a local Git hook; gitleaks must be installed separately."""
from pathlib import Path
import shlex
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
hook_dir = subprocess.check_output(['git', '-C', str(root), 'rev-parse', '--git-path', 'hooks'], text=True).strip()
target = Path(hook_dir)
if not target.is_absolute():
    target = root / target
target.mkdir(parents=True, exist_ok=True)
hook = target / 'pre-commit'
if hook.exists():
    raise SystemExit('Existing pre-commit hook preserved; use pre-commit to compose hooks instead.')
hook.write_text('#!/bin/sh\nset -eu\n' + shlex.quote(sys.executable) +
                ' scripts/security_check.py\ncommand -v gitleaks >/dev/null || { echo "Install gitleaks before committing." >&2; exit 1; }\n' +
                'gitleaks git --pre-commit --staged --redact\n', encoding='utf-8')
hook.chmod(0o755)
print('Installed staged-content policy and Gitleaks checks.')
