# Security policy

Never publish credentials, local config, prompts, session exports, logs, patches,
databases, acceptance output, provider auth caches, model weights or datasets.
The source-only allowlist is enforced by `.gitignore` and `scripts/security_check.py`.
Adding a new public file requires reviewing and updating both allowlists.

`config.json` is local and ignored; publish only `config.example.json`.
`.env.example` documents an optional environment variable; the runtime does not load
dotenv files. Authenticate OpenCode through its own CLI. Do not store production
credentials in a project the worker can read. An ignored file is still readable by
an agent; Git exclusions are not access controls.

The broker and OpenCode listen on loopback with generated credentials. The state
directory is mode 0700 and credential files are mode 0600 at creation. Keep it on
a private Linux filesystem outside the repository. State contains prompts, results,
diffs and possibly sensitive tool output. Logs are not guaranteed to redact arbitrary
provider/tool error text. Inspect locally; never attach raw logs to public issues.
Delete retained state only after finishing tasks and preserving needed patches.

Allowed project roots restrict admission, not process-level filesystem access.
Git worktrees isolate edits; they are not sandboxes. `readonly` is a tool policy,
not an OS security boundary. Writing workers and `run_check` can execute commands
with the WSL user's permissions. Use a separate user/container for untrusted code.
OpenCode cannot inherit the parent Codex process's sandbox. Outbound network access
and subscription billing remain subject to the configured provider and host policy.

Install the local hook or pre-commit configuration before changes. CI runs both the
publication policy and Gitleaks. Scanners can miss novel secrets; manually review the
staged diff. Never bypass checks to publish a known secret. If a credential leaks,
revoke it first, investigate misuse, then remove it from all reachable Git history.

Enable GitHub secret scanning and push protection and verify both settings after
creation. CI is a second check after upload, not a substitute for scanning locally.
For sensitive vulnerability reports, use GitHub private vulnerability reporting
when enabled; otherwise request a private reporting channel without disclosing the
exploit, credential, prompt or personal data in a public issue.
