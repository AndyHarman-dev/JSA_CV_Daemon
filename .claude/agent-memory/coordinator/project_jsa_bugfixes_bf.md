---
name: project-jsa-bugfixes-bf
description: BF-1 through BF-7 bugfix phases — dismiss, JD tab, timeout, prompts, Gemini rewrite, cancel button, LogEvent publishing
metadata:
  type: project
---

## BF-1 Backend Bugfixes (complete — 2026-05-27)

- **`dismissed` JobState**: Added as terminal state (not approved-terminal — can be re-queued via reset). Transitions: all non-terminal/non-approved → dismissed; dismissed → pending. `mark_failed` guards against both `approved` AND `dismissed` to prevent race when pipeline task outlives the dismiss action.
- **JD in API**: `_job_to_dict` now always includes `jd` field (both summary and full endpoint). `JobDTO.jd: string` in frontend types.
- **Agent timeout**: `agent_timeout: float = 300.0` in `config.py` (env: `JSA_AGENT_TIMEOUT`). `server.py` passes `timeout=settings.agent_timeout` to ALL CLI backends (was missing for claude-cli/gemini-cli; anthropic already had separate `anthropic_timeout`).

**Why:** Claude CLI was timing out at 120s on complex CV tasks. Dismissed state needed because intel brief can tell user a job is unsuitable before pipeline completes.

## BF-2 Frontend Bugfixes (complete — 2026-05-27)

- **Dismiss/Re-queue UI**: Dismiss button (not shown for approved/dismissed); Re-queue for dismissed only. Both have loading spinner + error display. `api.dismiss()` added.
- **JD collapsible**: Toggle section in `JobDetail` using `job.jd`. `▶/▼` indicator, `<pre>` element, max-h-64 scroll.
- **ReviewPane**: `useEffect([jobId])` — do NOT add `state` to dep array. It would cause a "Loading…" flash on approve. State change on revision is handled by unmount/remount of ReviewPane when job goes running→review.

**Why:** Revert from `[jobId, state]` dep — the revision case uses unmount/remount anyway (JobDetail conditionally renders ReviewPane only for review/approved).

## BF-4 `start_session` nudge-retry (complete — 2026-05-27)

- **Root cause**: `ClaudeCliBackend.start_session` called bare `parse_reply(raw)` with no fallback. `send_message` already had a nudge-retry block; `start_session` was missing it.
- **Fix**: Extracted shared `async _parse_with_nudge(session_id, raw) -> AgentReply` helper. Called from both `start_session` (passing the fresh `session_id` UUID) and `send_message` (passing `handle.external_id`). Nudge builds `--resume <session_id>` command.
- **Reviewer note**: `"no sentinel block"` string is coupled to the exact error message wording in `protocol.py:42`. If that wording ever changes, the guard silently inverts. Future cleanup: export a named constant or subclass.
- **Session-expired fix (second round)**: `_run` also now raises `ClaudeSessionExpiredError(ClaudeCliError)` when `returncode != 0 AND not stdout.strip()` + stderr contains "No conversation found". Otherwise raises `ClaudeCliError`. This prevents the misleading "no sentinel block" error when the real cause is a dead session. `context` param added to `_run` and threaded through all callers so session_id appears in the error message.
- **Tests**: `FailingClaudeBackend` test double added; 5 tests covering session-expired, generic CLI error, stdout-present-no-raise, nudge-bypass, and start_session propagation. 486 total tests pass.
- **Tests**: `tests/backend/test_claude_cli_nudge.py` — 29 tests, `ScriptedClaudeBackend` subclass test double (no mocks).

**Why:** First reply from Claude CLI on large CV tasks sometimes omitted the sentinel. Without the fallback in `start_session`, the job went straight to `failed` with no recovery opportunity.

## BF-7 LogEvent/ErrorEvent publishing (complete — 2026-05-27)

- **Root cause**: `LogEvent`/`ErrorEvent` existed in schema but were never published. Frontend LogTail was always empty.
- **Fix**: 6 publish points in `orchestrator.py` (A: job pickup, B: failed transition, C: _run_one exception) and `stages.py` (D: stage entry, E: FINAL received, F: NEED_INPUT before FollowUpNeededEvent).
- **Ordering guarantee**: LogEvent before StatusChangedEvent at A and E; LogEvent before FollowUpNeededEvent at F. Tested with ordering assertions.
- **Exception at C**: `mark_failed` publishes `StatusChangedEvent(running→failed)` internally; then orchestrator publishes `LogEvent(error)` + `ErrorEvent`. Order at C is: StatusChanged → LogEvent → ErrorEvent (asymmetric from A/E by design — mark_failed is internal).
- **Tests**: `test_log_events_bf7.py` — 23 tests covering all 6 points, presence, job_id, level, text content, ordering. 582 total tests pass.

**Why:** LogTail was a working frontend feature with no backend data. Milestones chosen to show meaningful progress without noise.

## BF-6 Cancel button for running jobs (complete — 2026-05-27)

- **Fix**: `POST /api/jobs/{id}/cancel` (running → pending, StatusChangedEvent, orchestrator.kick()); `api.cancel(id)` frontend; Cancel button (amber) in JobDetail visible only when `state === "running"`.
- **Caveat**: Cancel is best-effort for in-flight agent turns. The orchestrator holds in-memory Job for duration of agent call; completing checkpoint can overwrite pending. Documented in route docstring. Hard-stop requires orchestrator-level task cancellation.
- **Tests**: `test_cancel_bf6.py` (12) + `cancel_bf6.test.tsx` (11). 559 backend + 166 frontend.

## BF-5 GeminiCliBackend subprocess rewrite (complete — 2026-05-27)

- **Root cause**: Pty-based approach was broken by (1) directory trust check blocking without `--skip-trust`/`GEMINI_CLI_TRUST_WORKSPACE=true`, (2) ANSI codes corrupting sentinel detection, (3) large stdin blob not reliably triggering pty processing.
- **Fix**: Complete rewrite to subprocess `-p` mode, identical pattern to `ClaudeCliBackend`. Key flags: `--skip-trust` (bypass trust), `-o json` (structured output with `session_id`+`response`), `--session-id <uuid>` (fresh session), `--resume <uuid>` (subsequent messages).
- **Session management**: Gemini CLI v0.41.2 stores sessions on disk by UUID. `restore_session` with `external_id` → no-op like Claude; without → RuntimeError.
- **Deleted**: `jsa/agents/_pty_common.py` (only used by old Gemini pty code). Removed `ptyprocess>=0.7` from `pyproject.toml`.
- **`GeminiSessionHandle`**: `pty` field removed. Only `id` and `external_id`.
- **`_run` stderr logging**: Decode stderr ONCE at top of block; WARNING on nonzero exit, DEBUG on success (not both — reviewer caught a double-log bug where both fired on failure).
- **Session-expired heuristic**: `"session" in stderr_lower and "not found" in stderr_lower` — TBD, exact Gemini CLI string unknown. Falls back to `GeminiCliError` if heuristic misses.
- **Tests**: `tests/backend/test_gemini_cli_bf5.py` — 52 tests. `test_cli_backends.py` Gemini section fully rewritten. 547 total tests pass.

**Why:** Jobs using gemini-cli backend were stuck in "running" forever because the pty never produced a sentinel response.

## BF-3 Prompt Fixes (complete — 2026-05-27)

- **CVL_PROMPT.md**: STEP 4 delivers draft in plain text + ends with `<<<NEED_INPUT>>>` asking for changes or "finalize". STEP 5 sub-case B: on approval, copy full letter into `<<<FINAL>>>`. HARD RULE added: no "see above" in `<<<FINAL>>>`.
- **PROMPT_CDADJUST.md**: Step 3 now instructs Markdown output (not docx/docx.js). Format preservation rules added: `<div align="center">` for centered elements; ATS rules apply to structural elements only (not visual styling). HARD RULE: full Markdown CV must be in `<<<FINAL>>>`, not a file reference.

**Why:** The `.docx`/`docx.js` instruction was impossible for Claude CLI and caused format improvisation. "Override unconditionally" wording caused misalignment of centered names, etc.
