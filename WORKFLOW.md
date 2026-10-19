# Coordinator workflow

Start with one strong coordinator and two concurrent OpenCode workers. Use one
worker for tightly coupled changes; consider three for independent modules or an
independent reviewer. Four or more requires workload measurements. These are
starting policies, not a measured universal optimum. The shared queue can contain
more tasks than execution slots; changing concurrency requires an idle broker
restart. No model or reasoning variant changes are implied.

## Dispatch small, independently acceptable tasks

Define interfaces before parallel implementation. Give each writing task disjoint
file ownership and explicit checks. Avoid two workers editing a shared interface
at once. Commit the reviewed integration batch before the next worktree wave.
Estimated 5-15 minute tasks are a planning guide, not a runtime guarantee.

Example `job_submit` arguments (replace the project placeholder):

```json
{
  "project": "<absolute-project-path>",
  "request_id": "feature-parser-v1",
  "owner": "parser",
  "profile": "code_fast",
  "risk": "standard",
  "task": "Implement the bounded parser change described in the brief.",
  "brief": {
    "goal": "Reject invalid input with a stable error code.",
    "interfaces": ["Preserve parse(value) -> Result."],
    "acceptance": ["Valid input remains accepted.", "Malformed input returns invalid_input."],
    "stop_conditions": ["Report a blocker if the public interface needs to change."]
  },
  "contract": {
    "allowed_paths": ["parser.py", "tests/test_parser.py"],
    "checks": [["python3", "-m", "unittest", "discover", "-s", "tests"]],
    "max_changed_files": 2,
    "max_attempts": 2
  }
}
```

`brief` and `risk` guide the worker; they do not enforce semantic correctness or
validate the worker's report. Path/check contracts retain their existing artifact
enforcement. All risk levels require the same verification gate when enabled.
The report target is 2000 characters: STATUS, CHANGES, CHECKS, RISKS, DECISIONS.
Commands and exits must be actual observations; transcripts are unnecessary.

## Wait, verify, review, integrate

1. Centrally `job_wait` on active IDs, carry `after_event_id`, remove finished IDs.
   Avoid reading unchanged results and frequent `job_status` polling.
2. Read compact `job_result` once. Counters and uncertainty are preserved; raw
   session/workspace metadata and duplicate usage fields are omitted. Full text
   is retained privately; use `view="full"` and `max_chars` when needed.
3. Call `job_verify` with the submitted checks before model review. Successful
   output tails are short; failures retain longer tails. To read the existing
   record without rerunning checks, use `action="inspect", view="full"`.
   Inspection reports a retained snapshot, without asserting current freshness;
   apply independently checks the hashes. Full check logs were already bounded
   when captured; this is not an unlimited log archive. Default `action="run"`
   executes checks on every invocation.
4. Inspect `job_diff` and verify behavior, interfaces and error paths. Low risk
   changes need relevant checks and proportionate review. Concurrency, recovery,
   permissions and deletion need independent boundary/fault tests and close review.
   Passing tests written by the implementer alone are insufficient evidence.
5. Apply only a reviewed `patch_sha256`; run relevant integration checks after
   combining patches. `succeeded` is execution status, never acceptance.

The example configuration enables `require_verification`. Existing local configs
are preserved by users: explicitly set it to true to enforce checks for legacy
jobs without contracts. Missing checks give `not_run` and cannot pass the gate.
No automatic model reviewer or additional worker is started by these tools.

## Repair and escalate

Return a single repair brief containing the failing case, expected behavior,
actual evidence and allowed files. After two failed repair rounds, narrow the
task or let the coordinator resolve the difficult section. This is coordinator
policy; automatic repair budgets and dispatch are not implemented.

Current sessions cannot receive a follow-up through this bridge. Repairs require
an explicit new job/session and a new request ID, using a reviewed committed
baseline. Reusing an old request ID retrieves the old task; `job_recover` only
reattaches monitoring and never sends a new prompt. Do not bypass this with raw
native calls. Session reuse needs a separate correctness design.

## Measure before increasing concurrency

Keep a short handoff for a new implementation chat: goal, interface map, current
state, checks and outstanding risks. Avoid carrying unrelated research transcripts.
Do not rotate chats during a tightly coupled task just to improve token statistics.

Use `scripts/measure_usage.py` with explicitly selected coordinator turns and saved
worker results. Compare coordinator cached/new input and output, all worker usage,
end-to-end wall time, first-pass acceptance, repair rounds, integration failures
and defects found after acceptance. Manual assessment counters are labeled as such;
they are not inferred from execution success or presented as independent proof.

Example private assessment (all fields optional, omit unknown measurements):

```json
{
  "tasks_total": 10,
  "first_pass_passed": 8,
  "repair_rounds": 2,
  "integration_failures": 0,
  "post_acceptance_defects": 0,
  "wall_clock_seconds": 1200,
  "concurrency": 2
}
```

Supply `--assessment <private-assessment.json>` alongside the existing exporter
arguments. Never sum overlapping worker execution times as end-to-end wall time,
or treat provider cost metadata as subscription billing. Compare similar tasks at
two and three slots; prefer the lower slot count when quality or total consumption
worsens. No speedup or token-saving percentage has yet been demonstrated.
