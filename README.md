# Codex ↔ OpenCode bridge for WSL

[中文说明](README.zh-CN.md) · [Security policy](SECURITY.md)

A Python standard-library MCP server that delegates bounded coding tasks from
Codex to a persistent OpenCode service in WSL. Multiple Codex callers share one
durable SQLite queue, a configurable global concurrency limit (default two), and
independent OpenCode sessions. Parallel writing tasks use separate Git worktrees.

```text
Windows Codex → stdio MCP in WSL → authenticated loopback broker
                                  → persistent OpenCode HTTP/SSE service
                                  → independent sessions / Git worktrees
```

The bridge does not copy, convert or proxy subscription credentials. Authenticate
Codex and OpenCode separately through their own supported login flows. Account
rate limits still apply. This is not an official OpenAI or OpenCode product.

## Requirements and compatibility

- Windows with WSL2, a Linux distribution, Python 3.10+, Git and OpenCode CLI.
- Codex CLI on Windows for `Install.ps1`; Codex on Linux can register MCP directly.
- PowerShell and `wsl.exe` for the Windows convenience scripts.
- OpenCode provider authentication and access to the configured model.

Bridge version: **1.3.1**. Compatibility baseline: Ubuntu 24.04, OpenCode **1.19.2**, and
Windows Codex CLI **0.163.0-alpha.10.2**. No compatibility with future versions is
implied. The example worker model is **`opencode-go/deepseek-v4.1-flash`**;
the provider controls that identifier's revision. Use `bridge_models` to check
availability and select another supported Go model per task. No model weights,
datasets, vector indexes or external document sources are bundled.

## Install from Windows

1. Clone this repository to a Windows directory accessible from WSL.
2. Copy `config.example.json` to `config.json` and edit it. Set `distro` to the
   result of `wsl --list --quiet`, `opencode_bin` to the WSL executable path, and
   `allowed_roots` to narrowly scoped project directories. `~` is expanded by the
   Linux runtime. The default allows only `~/projects`, not the whole home directory.
3. Check `opencode --version`, provider login and model access inside WSL.
4. From this repository, run:

```powershell
.\Install.ps1
.\Bridge.ps1 -Action doctor
.\Bridge.ps1 -Action models
.\Bridge.ps1 -Action test
```

The installer creates a private local config from the template if needed, resolves
the Linux user's home dynamically, copies the runtime to
`~/.local/share/codex-opencode-bridge`, backs up an existing Windows Codex
configuration, and registers `opencode_bridge`. Open a new Codex chat to load it.
Local config and Codex backups must never be committed. Reinstallation updates the
runtime; finish or cancel all queued/running tasks first. Keep your working local
config when pulling updates; review new template fields manually.

## Install from Linux / WSL

```sh
cp config.example.json config.json
# Edit the config and authenticate OpenCode first.
python3 deploy.py
codex mcp add opencode_bridge -- python3 "$HOME/.local/share/codex-opencode-bridge/bridge.py" mcp
```

The broker starts automatically on the first client connection. State is private
under `~/.local/state/codex-opencode-bridge`. An optional `CODEX_OPENCODE_STATE`
environment variable changes its location; use an absolute private Linux path.
`.env.example` is documentation only: no dotenv loader is used. No keys or tokens
belong in `config.json` or in this repository.

## Use from Codex

For the new contracts, recovery, model routing and usage workflow, see the
[changes](CHANGELOG.md) and [coordinator workflow](WORKFLOW.md). The conservative default remains the existing Go
provider and two global slots. The template also requires independent verification before apply. Model discovery never infers billing entitlement.
Set `allowed_providers`/optional `allowed_models`, `provider_caps`, `model_caps`
and `worker_profiles` in your ignored local config. Providers sharing an account
should use a shared provider cap; model caps apply in addition. Profile selection
is explicit and each job fixes its resolved model. No paid fallback is automatic.

`bridge_models(refresh=true)` bypasses the 60-second catalog cache. A failed refresh
may return a previous snapshot with `stale=true`; it cannot discover new entitlement.
`job_status` includes a queue reason, progress phase and suggested next tool.
Status/result/wait default to compact views; request `view="full"` for legacy metadata.
`result_max_chars` (2500) and `check_output_chars` (2000) bound default output.
Successful verification tails are at most 256 characters per stream. Retained evidence
is unchanged; truncation flags identify omitted output. `result_view="full"` opts out.
`job_wait(after_event_id=...)` can return on semantic changes; keep batch waiting
centralized in the coordinator rather than polling from every specialist.

### Bounded work and acceptance

`job_submit` accepts `profile`, optional structured `brief`, `risk` guidance, and a
worktree `contract`. See WORKFLOW.md for task ownership, report format and repair
limits. Risk guidance never relaxes verification. Example contract:

```json
{
  "allowed_paths": ["src/**", "tests/**"],
  "forbidden_paths": ["tests/acceptance/**"],
  "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
  "max_changed_files": 10,
  "max_attempts": 2
}
```

After execution, call `job_verify(job_id)` to run the submitted checks independently.
No checks means `not_run`, never passed. Inspect both verification and the patch,
then pass the reviewed `patch_sha256` to `job_apply`. Contract jobs require a
successful verification matching the current patch and non-ignored workspace input
hashes; changes require a new review/verification. `max_attempts` is a bounded
execution policy field; this release dispatches one attempt and does not automatically
create repair attempts. It does not limit how often independent checks may be rerun.
Ignored files, external services, dependencies and host environment are not made
immutable by the content digest; tests still need a controlled environment.

The project application lock serializes bridge applies; unrelated host processes
can still edit files. Existing non-conflicting batch patches may remain uncommitted
while other patches from the same base are applied. Commit the reviewed batch before
submitting the next worktree wave.

`job_recover(action="inspect")` is read-only. `abort` reconciles cancellation;
`reattach` schedules monitoring of an already admitted session without replaying its
prompt. Unknown admission/termination needs inspection and continues consuming a
slot. Native abort confirmation does not prove all OS descendant processes stopped.

### Clarification questions and retention

Native questions are experimental and disabled by default. Explicitly set
`enable_questions=true` and submit `questions=true` for an opted-in job. Use
`job_questions`, then `job_answer` with the exact question ID, an immutable
idempotency `answer_id`, and native `answers` (array of arrays of strings).
`answer_id` is not an option ID. Question replies are task guidance, not permission
approval. On uncertain delivery, inspect/reconcile; never change the answer ID to
force a resend. Disappearance alone only establishes `resolved_unknown`.
This channel requires version-specific end-to-end validation for your OpenCode build.
Waiting retains a worker thread, project protection and quota slot in this release;
no unsupported slot-release or unlimited-wait claim is made. Original deadlines apply.

`job_cleanup` defaults to a dry run and only targets one owned, terminal Git worktree.
Unapplied or changed work requires explicit discard; applied dirty work must match
verified archived content before force removal. Database/idempotency records are
retained. Native OpenCode sessions and ignored outputs need separate retention
policies. No global pruning or automatic TTL deletion is performed.

### Recording the coordinator and workers

`scripts/measure_usage.py` accepts an explicitly selected local Codex JSONL and
saved worker `job_result` JSON files. It exports counters, not prompts or local IDs:

```sh
python3 scripts/measure_usage.py --codex-log <local-session-jsonl> --turn-id <selected-turn> \
  --worker executor-a=<result-a.json> --worker executor-b=<result-b.json> \
  --assessment <private-assessment.json> --output <private-report.json>
```

When available, Codex `token_usage_record` responses are deduplicated by response ID;
`token_count` events are not added again. Cached input/reasoning are subsets in Codex
usage, not extra totals. Worker assistant-message counts are not exact upstream
request counts, and cost metadata is not a subscription bill. Missing telemetry is
reported as missing. Optional `--assessment` contains restricted manual acceptance
counters (see WORKFLOW.md); schema 2 adds an efficiency summary without inferring
quality from execution success or summing overlapping runtimes. Use `--wait-complete-seconds 600` for a local background exporter
to include the final response after a selected turn finishes; otherwise it is a
point-in-time snapshot. Raw session files and generated reports must remain private.

Replace the project placeholder with an absolute Linux project path under an
allowed root. A Windows drive path also works when its WSL location is allowed.

```text
Use opencode_bridge for this project: <absolute Linux project path>.
Call bridge_health, bridge_models and project_prepare first.
Create and verify a minimal scaffold and commit the intended Git baseline.
Submit independent, bounded worktree tasks with unique request_id and owner.
If using Codex specialists, let them submit tasks and return their job IDs.
The primary agent batch-waits centrally and resumes specialists for review.
Inspect job_result and job_diff, run relevant checks, then apply reviewed patches.
Do not let OpenCode create nested workers or launch another scheduler.
```

`project_prepare(create=true, initialize_git=true)` can initialize a new repository
with an empty baseline; it does not automatically commit existing files. Existing
projects need a clean, reviewed commit before worktree submissions.

| Tool | Purpose |
| --- | --- |
| `bridge_health`, `bridge_models` | Health, global queue, available models |
| `project_prepare` | Validate/create a project and inspect Git baseline |
| `job_submit` | Idempotent task admission with stable request ID |
| `job_list`, `job_status` | Inspect ownership, status, timing and progress |
| `job_wait` | Wait for 1–8 IDs, at most 30 seconds per call |
| `job_result` | Answer, cumulative retained-session usage, partial diagnostics |
| `job_cancel` | Cancel one job without cancelling siblings |
| `job_diff` | Review a bounded diff and private patch artifact |
| `run_check` | Execute a validation command without a model invocation |
| `job_apply` | Check and apply a reviewed patch; no commit or push |

Writing modes: `worktree` (default, isolated committed baseline) and `exclusive`
(direct project edits, mutually exclusive for that project). `readonly` disables
editing and shell tools but is only a tool policy. It does not permit `run_check`.
Neither worktrees nor allowed roots are OS sandboxes. See [SECURITY.md](SECURITY.md).

Version 1.3.1 checks native session permissions before and after prompt admission.
Deprecated prompt `tools` is omitted because OpenCode 1.19.2 replaces the entire
permission policy when it is present. `python3 verify_native.py` probes this native
contract using temporary sessions and `noReply=True`, without model inference.

Completed provider errors retain structured `failure` diagnostics. An acknowledged
abort plus completed-error evidence, no pending tools and absence from active
status can reconcile termination; absence alone cannot. This does not prove OS
descendants stopped. `upstream_retryable` never authorizes whole-task replay.
Timing separates `execution_seconds`, `attention_seconds` and `wall_seconds`.
Execution is bridge-observed elapsed time; migrated old timestamps are estimates.

`succeeded` means the model returned a completed answer, not that the code is correct.
Require independent validation. A 120-second stale-progress warning does not
automatically cancel or replay a task. Failures and timeouts retain partial output.
Uncertain prompt admission is not blindly retried; uncertain cancellation retains
the slot as `attention_required`. A retained queue does not imply exactly-once tool
side effects across crashes. Inspect uncertain tasks before resubmitting.

`session_usage` sums retained unique assistant messages; `last_message_usage` is
compatibility data only. Provider-reported token/cost values are not actual
subscription billing or the total cost of coordinating Codex callers.

## Operations and testing

```sh
~/.local/bin/codex-opencode doctor
~/.local/bin/codex-opencode call job_list
~/.local/bin/codex-opencode stop
python3 -m unittest test_bridge -v
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

Stopping/redeploying refuses active or queued jobs. Removing MCP registration with
`codex mcp remove opencode_bridge` leaves projects and private state intact.
`verify_live.py` is an opt-in six-model-task acceptance harness; it creates a
private fixture under `~/projects` and writes an ignored report. It queues six
tasks over one to six configured slots and consumes provider quota; use an idle broker. `--resume`
rechecks an existing report without submitting new model tasks. CI uses offline
tests only. No user-specific benchmark data, task traces or prompts are published.

## Publication safeguards

The default-deny `.gitignore` permits only explicitly reviewed source files.
Local config, authentication, logs, prompts, task databases, reports, patches,
weights and datasets remain outside Git. `scripts/security_check.py` also checks
force-added files, personal paths, credential patterns, binary content and size.
Install Gitleaks and run `python3 scripts/install_hooks.py`, or use
`pre-commit install`. Review staged content manually as well. CI reruns scans;
enable and verify GitHub secret scanning and push protection on the repository.
See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor checks.

No open-source license has been selected; public visibility does not grant a
general reuse license. The repository owner can add a license separately.

## References

Built independently using Python standard library and the
[OpenCode server interface](https://opencode.ai/docs/server/) and
[Codex MCP interface](https://developers.openai.com/codex/mcp/).
Design research considered [codex-mcp-opencode](https://github.com/Ichikawashadow/codex-mcp-opencode),
[opencode-mcp](https://github.com/edulelis/opencode-mcp), and
[opencode-mcp-bridge](https://github.com/ManuOtel/opencode-mcp-bridge).
Their runtime code is not included. OpenCode 1.19.2's advertised session-wait API
returned an unimplemented response in validation; this bridge uses SSE wakeups
and final message metadata instead.
