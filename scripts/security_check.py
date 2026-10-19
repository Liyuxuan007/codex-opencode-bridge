"""Scan staged blobs by default, or all tracked files with --all. Never print matches."""
import argparse
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = {
    '.gitignore', '.gitattributes', '.pre-commit-config.yaml', '.env.example',
    'README.md', 'README.zh-CN.md', 'SECURITY.md', 'CONTRIBUTING.md',
    'config.example.json', 'bridge.py', 'deploy.py', 'test_bridge.py',
    'verify_live.py', 'Install.ps1', 'Bridge.ps1',
    'scripts/security_check.py', 'scripts/test_security_check.py', 'scripts/install_hooks.py',
    '.github/workflows/ci.yml',
}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'GitHub token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'provider key': re.compile(r'\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,})\b'),
    'personal absolute path': re.compile(r'(?i)(?:[A-Z]:[\\/][U]sers[\\/][^\s/\\]+|/mnt/[a-z]/[U]sers/[^\s/]+|/(?:home|[U]sers)/[^\s/]+)'),
    'credential URL': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
    'email address': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
    'literal secret': re.compile(r'''(?ix)["']?(?:api[_-]?key|access[_-]?token|password|secret)["']?\s*[:=]\s*["'][A-Za-z0-9_+/=-]{20,}["']'''),
}


def inspect(path, blob):
    errors = []
    if path not in ALLOWED or '..' in PurePosixPath(path).parts:
        errors.append((0, 'file outside publication allowlist'))
    if len(blob) > 1024 * 1024:
        errors.append((0, 'file exceeds 1 MiB source-only limit'))
    try:
        text = blob.decode('utf-8-sig')
    except UnicodeDecodeError:
        return errors + [(0, 'non-UTF-8 or binary file')]
    if '\0' in text:
        errors.append((0, 'binary content'))
    for number, line in enumerate(text.splitlines(), 1):
        for label, expression in PATTERNS.items():
            if expression.search(line):
                errors.append((number, label))
    return errors


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', help='Check tracked working-tree files (CI).')
    args = parser.parse_args()
    names = git('ls-files', '-z') if args.all else git('diff', '--cached', '--name-only', '--diff-filter=ACMR', '-z')
    count = 0
    failures = []
    for raw in names.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8')
        path = ROOT / name
        if path.is_symlink():
            failures.append((name, 0, 'symlink not allowed'))
            continue
        blob = path.read_bytes() if args.all else git('show', ':' + name)
        failures.extend((name, line, label) for line, label in inspect(name, blob))
        count += 1
    for name, line, label in failures:
        print(f'{name}:{line}: {label}', file=sys.stderr)
    print(f'Security check: {count} files; {len(failures)} findings. Secret values are never printed.')
    return bool(failures)


if __name__ == '__main__':
    raise SystemExit(main())
