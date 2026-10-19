# Contributing

Run offline tests on Linux/WSL (Python 3.10+):

```sh
python3 -m unittest test_bridge -v
python3 -m unittest discover -s scripts -p 'test_*.py' -v
python3 scripts/security_check.py --all
```

Install Gitleaks, then `python3 scripts/install_hooks.py` (no runtime dependencies),
or use `pre-commit install` with the supplied configuration. The checks inspect staged
content, including files force-added against `.gitignore`. Review `git diff --cached`
and `git ls-files` before every push. Do not upload local acceptance reports or logs.

The live verifier consumes six model tasks and creates retained fixtures; it is
opt-in, not part of CI. Set up a private config and provider authentication first.
Do not start it against a busy shared broker. The configured concurrency must be six
for its six-slot assertions. No model credentials or live data are needed for offline
tests. Include relevant test results in changes without copying private runtime data.

No license has been selected yet; public visibility alone does not grant a general
license to reuse or redistribute this code. Ask the repository owner before reuse.
