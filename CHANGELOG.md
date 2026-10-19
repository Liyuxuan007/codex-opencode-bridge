# Changelog

## 1.2.0

Strong coordinators can define bounded work and retain independently verified evidence.

- Separate control-plane and artifact modules, with offline fault and real-Git tests.
- Recoverable cancellation failures, admission/cancellation serialization, guarded
  reattachment, and structured next-action/error fields. Reattachment never prompts again.
- Provider/model discovery with TTL refresh, explicit provider/model allowlists,
  named execution profiles and provider/model quotas under the existing global cap.
- Optional worktree contracts with path/file-count rules and coordinator-supplied
  checks. Contract jobs require successful digest-bound verification before apply.
- Frozen binary patches, non-ignored workspace input hashes, per-project application
  locks, review hash checks and guarded worktree cleanup with a dry-run default.
- Session-specific SSE wakeups with bounded fallback polling; progress phases and
  event cursors. Raw reasoning is not returned in progress.
- Experimental opt-in native clarification questions with immutable answer
  reservations. Ambiguous answers are never silently resent or declared confirmed
  merely because the question disappeared. Waiting jobs retain their quota slot.
- Counter-only usage exporter: deduplicated Codex per-response records, separate
  worker session usage, and optional local finalization after the turn completes.

Compatibility: existing job IDs, SQLite rows and tool names remain valid. Optional
fields and tables are additive. Existing tasks without a contract keep legacy apply
behavior unless `require_verification` is explicitly enabled. `succeeded` still means
execution completed; it is not an acceptance claim. No automatic repair dispatch,
cross-provider fallback, automatic deletion, or nested worker creation is enabled.

## 1.1.0

Initial public release: shared durable queue, independent sessions/worktrees,
explicit SQLite connection closure, cumulative retained-session usage, partial
timeout diagnostics and stale-progress warnings.
