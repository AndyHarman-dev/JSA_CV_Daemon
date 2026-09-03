---
status: Done
---

# Agent Chat Upgrade — persisted thread, streaming, composer fix, suggested replies

Source design: `New features for JSA.zip` → `design_handoff_agent_chat_upgrade/`
(`README.md` + `JSA App Shell.dc.html`). The `.dc.html` is a **behavior/visual spec**,
not code to copy — it uses `React.createElement` with inline styles and a proprietary
template/logic split.

---

## Context

The job-detail chat surface today is a dead end. `FollowUpPane.tsx:28` fetches the full
job and keeps exactly **one** record — `job.follow_ups.find(f => f.answered_at === null)`
— discarding every answered turn. `ChatBox.tsx` is write-only and never renders a prior
turn. So a job that asked three clarifying questions across `cv_adjust` and
`cover_letter` shows the user a single question with no memory of what came before, even
though the backend has had a complete, crash-safe transcript the whole time (`Message`
rows, written inside `repo.checkpoint`'s single transaction — `db/models.py:71-83`).

Three consequences the user hits directly:

1. **No history.** Answer a question, the job advances, the question and your answer both
   vanish from the UI. There is no way to see what you already told the agent.
2. **The composer occludes the thread.** `ChatBox.tsx:135-152` is `position: sticky;
   bottom: 0` with `marginBottom: -80`, hard-coupled to `JobDetail.tsx:207`'s
   `padding: "20px 26px 80px"`. While stuck, the composer's border box hangs ~80px below
   its margin box and overlays the content behind it. The comment at `ChatBox.tsx:133`
   claims JobDetail is "the only place ChatBox is rendered" — that is **stale**;
   `ReviewPane.tsx:476` mounts it too, two flex-columns deep, with different padding, so
   the coupling is already wrong there.
3. **No visible progress.** A running stage is a spinner. The pipeline emits `LogEvent`s
   the whole time and `store.ts:207` throws every one of them away (`case "log": break;`).

This plan exposes the existing transcript, rebuilds the chat surface as a real thread,
fixes the composer, adds model-generated suggested replies, and — last — adds token
streaming where a genuine channel exists.

### Locked decisions (from scoping, 2026-09-02)

| Decision | Choice |
|---|---|
| Streaming scope | Stream **content** everywhere available; show a REASONING card **only** where a genuine separate channel exists. Never synthesize one. `google-cli` gets a "working…" indicator. **No SDK dependency.** |
| Chat modes | Bound to existing flows. The composer only writes where the backend already accepts input (open `FollowUp`, or a `RevisionRequest` at `review`/`cv_review`). **No new pipeline entry points, no mid-run interjection.** |
| Prompt files | I do **not** edit `jsa/prompts/*.md`. Phase 5 hands over the exact `<<<SUGGESTIONS>>>` block text for the user to paste. Parser change is backward-compatible: a stale prompt yields no suggestions, never an error. |
| Thread data | Dedicated `GET /api/jobs/{id}/transcript` with server-derived, display-ready turns. **Not** raw `messages` on the shared job DTO. |
| Theme | No restyle needed — verified `theme/tokens.ts` already ships the design's exact palette and fonts. |

### Verified facts that override the handoff README

The README is good but wrong or silent on four points I checked directly. **All source
citations in this plan have now been re-verified independently (2026-09-03)**, including
the two the skeptic flagged as unconfirmable:

- The design handoff is at `~/Downloads/New features for JSA.zip` →
  `design_handoff_agent_chat_upgrade/` (992-line `.dc.html`). It is **not** in the repo —
  extract it before Phase 3. Every `.dc.html` line citation below was spot-checked and is
  accurate (`:719` `stateSurface`, `:729` `avatar(T, letter, color)`, `:733` `thinkCard`,
  `:749` `msgCard`, `:764` `threadInput`, `:778` latest-agent-turn guard, `:802`
  `useQuickReply`, `:815` `renderQuickReplies`, `:828` `renderThread`).

- **`claude` CLI already streams.** Ran it live: `--output-format stream-json
  --include-partial-messages --verbose` emits NDJSON `{"type":"stream_event","event":
  {"type":"content_block_delta","delta":{"type":"text_delta","text":"ban"}}}`, plus a
  final `{"type":"assistant"}` event carrying the assembled content blocks. **Reproduced
  live on 2026-09-03** — the `"text":"ban"` delta above is a verbatim capture. The README's
  claim that CLI backends have "no token-level stdout channel" is false.
  **The stream is not pure model output.** One short reply also emitted six
  `{"type":"system"}` events (`hook_started`/`hook_response` — the local `~/.claude` hooks
  fire on every invocation), a `{"type":"rate_limit_event"}`, and a `{"type":"result"}`.
  A parser must whitelist, not blacklist — see Phase 7 item (7). **No
  `claude-agent-sdk` dependency is warranted** — adopting it would replace the tested
  `run_killable` process-group kill contract (`_subprocess.py:52-67`) to obtain the same
  NDJSON the CLI already prints.
- **`agy` genuinely cannot stream.** `agy --help` has no output-format, stream, or json
  flag. This is the real fidelity gap. (Unrelated but noted: `agy` now **does** have
  `--model` and a `models` subcommand, which contradicts `CLAUDE.md`'s
  `SUPPORTS_MODEL_SELECTION["google-cli"] = False` rationale. Out of scope here.)
- **Structured mode has no reasoning channel by design.** `prompt_assembly.py:126-132`
  instructs the model to put all prose inside `question`, and
  `anthropic_api.py:81-83` discards text blocks preceding the forced `tool_use` block.
  Streamed reasoning is therefore largely a *sentinel-mode* phenomenon.
- **The transcript is not durable across resets.** `repo.soft_reset_job:258-260`,
  `nuclear_reset_job:286-289` and all three `backend_switch_reset` branches
  (`repo.py:359-424`) delete `Message` rows. A BF-19 backend switch or a model-ladder
  hop silently empties the thread. The UI must tolerate the transcript shrinking to zero.

---

> **Phases 1 and 3 should land together.** Phase 1 is separated only because it is the
> smallest, most isolated diff and worth reviewing on its own. But removing sticky
> positioning without Phase 3's scroll container leaves the composer far below the fold
> when a long JD is expanded — better than overlaying content, but a visible regression if
> Phase 1 ships alone. Merge them to `main` as one unit.

## Phase 1 — Composer clipping bugfix (design item 3)

**Current State.** `ChatBox.tsx:127-153` roots the composer in `position: sticky; bottom:
0` with `marginLeft/Right: -26` and `marginBottom: -80`, explicitly documented as coupled
to `JobDetail.tsx:207`'s `padding: "20px 26px 80px"`. `index.css` has **no** height or
overflow rule on `.jta` (only `:77-78`, a transition and a focus ring) — all geometry is
inline. So the stylesheet is not the cause.

**Desired State.** The composer is a normal in-flow block at the end of the thread column
— exactly what the design reference does (`JSA App Shell.dc.html:764`: `threadInput` is
`flex: 'none', paddingTop: 12, borderTop: 1px solid bd`, with no positioning at all). It
knows nothing about its container's padding, so it is correct at both mount sites.

**Problems.** Sticky positioning clamps the *margin* box inside the scrollport. A `-80`
bottom margin puts the margin-box bottom edge 80px above the border-box bottom, so while
stuck the composer's visible box overhangs the bottom of `<main className="flex-1
overflow-y-auto">` (`App.tsx:62`) and is clipped/overlays content. It only cancels
cleanly when scrolled fully to the bottom, which is why it reads as "the messages div is
cut by the hidden input box". At `ReviewPane.tsx:476` the `-26`/`-80` numbers correspond
to nothing at all. The user's "~25%" and the computed ~45% (80px of a ~178px composer)
disagree — resolve empirically during verification rather than tuning the number; the fix
removes the number entirely.

**Solutions.**
- Delete `position/bottom/zIndex/marginLeft/marginRight/marginBottom` from
  `ChatBox.tsx:135-152`. Keep `display: "flex"` (verified present at `:146` — without it
  `flexDirection` is inert and the composer's internal layout breaks), `flexDirection`,
  `gap`, `borderTop`, `paddingTop`. Drop the gradient background and `boxShadow` (both
  exist only to sell the floating dock).
- **`paddingLeft/Right: 26` and `paddingBottom: 20` also go.** No longer an open question:
  the design's `threadInput` (`.dc.html:764`, read verbatim) is exactly
  `flex:'none', paddingTop:12, display:'flex', flexDirection:'column', gap:8,
  borderTop:1px solid bd` — no horizontal padding and no bottom padding at all. The
  horizontal pair only ever existed to cancel the `-26` margins (`:131-133`'s comment);
  once those are gone the container's own `26px` applies, and doubling it would inset the
  composer relative to the thread above it.
- Delete the stale comment at `ChatBox.tsx:129-134`, including the dead reference to
  `.claude/designs/design_handoff_docked_chat_input` — that directory **does not exist**
  (confirmed; `.claude/designs/` holds seven other handoffs, not this one).
- Scrolling moves into the thread container in Phase 3, so the composer stays visible
  without sticky. Until then it simply scrolls with the page — strictly better than
  overlaying.
- **Caution:** `chrome.tsx:21-33`'s `panelBase` returns a `clipPath` under the `hud` skin,
  and `clip-path` clips descendants regardless of `overflow`. Do not nest the Phase 3
  scroll container inside a `panelBase` panel.

**Verify.** With a job in `awaiting_input` and the JD section expanded (so the detail panel
overflows), scroll to the middle of the panel: nothing may be overlaid. The message area
does not exist until Phase 3, so check against what *is* there — the JD `<pre>`
(`JobDetail.tsx:352-401`) and the `PIPELINE_PROGRESS` panel (`:334-350`). Repeat at the
`ReviewPane` revise composer. `frontend/src/__tests__/ChatBox.test.tsx` (296 lines)
must still pass unchanged — it asserts behavior, not geometry.

---

## Phase 2 — Transcript projection + endpoint (design item 1, backend)

**Current State.** `Message` rows exist and are durable, but nothing exposes them.
`_fetch_job_with_relations` (`routes_jobs.py:111-118`) eager-loads only `documents` and
`follow_ups`; `Job.messages` is not loaded, so touching it in `_job_to_dict` raises
`MissingGreenlet` under asyncio. There are no Pydantic response models — `_doc_to_dict`,
`_follow_up_to_dict`, `_job_to_dict` are hand-rolled dict builders.

**Desired State.** One read-only endpoint returns an ordered list of **display-ready**
turns, each tagged with a kind, so the frontend renders a thread without knowing anything
about sentinels, JSON envelopes or prompt plumbing.

**Problems.**
- `Message.content` is **raw model output**. The `system` row is the entire assembled
  system prompt (prompt file + structured contract + language directive —
  `prompt_assembly.py`). The first `user` row embeds the whole JD, tier, research brief
  **and** the base-CV JSON skeleton (`_build_initial_user_msg`, `stages.py:1328-1357`).
  `assistant` rows in structured mode are `{"kind":..., "payload":...}` JSON envelopes.
  Rendering these verbatim ships multi-KB blobs.
- Self-heal and nudge turns (`_self_heal_final:464-465`,
  `_send_message_with_wire_retry:571-574`) are real `user`/`assistant` pairs but are
  machine plumbing, not conversation.
- **The user's answer is not a `Message` row when they submit it.**
  `routes_jobs.py:207-210` writes only `FollowUp.answer`/`answered_at`; it becomes a
  `Message` only at the *next* checkpoint. A Messages-only thread would show the user's
  own answer disappearing until the stage finishes.
- The bus is unbounded and unfiltered (`events/bus.py:11-18`) with no replay, so an event
  can only ever be an invalidation hint — the DB stays the source of truth.

**Solutions.**
- New `jsa/api/transcript.py` with `build_transcript(job, messages, follow_ups,
  documents, revision_requests) -> list[dict]`, pure and unit-testable. Each turn:
  `{seq, kind, role, stage, text, created_at, follow_up_id?, suggested_replies?,
  reasoning?}` where `kind ∈ {"question", "answer", "delivery", "verdict",
  "plumbing"}`.
- Derivation rules:
  - **Drop every `role == "system"` row.** Precedent already exists: `_load_history`
    filters `Message.role.in_(["user","assistant"])` (`stages.py:1386`).
  - **Questions and answers come from `FollowUp` rows, not `Message` rows.** A FollowUp
    is already a clean `{question, answer, asked_at, answered_at}` pair, and it is written
    the instant the user submits — which is exactly what fixes the disappearing-answer
    problem above. Order by `asked_at`, then `id`.
  - **`delivery`**: one compact marker per `Document` (`"Delivered CV v3"`), carrying
    stage + version. Never the document body — `ReviewPane` already renders that.
  - **`verdict`**: `Job.fit_reason`, when set.
  - **Dedupe the user's own words — two rules, and neither may be exact-text match.**
    (a) A `user` `Message` whose stripped content **contains** an answered
    `FollowUp.answer` is not a turn at all: it is that answer re-sent at resume
    (`stages.py:773-782` → `_send_message_with_wire_retry`) and persisted at the next
    checkpoint. Fold it into the `answer` turn, or every answer renders twice.
    (b) A `user` `Message` whose content **contains** a `RevisionRequest.instruction`
    (sent at `stages.py:757-765`) **is** a user turn — classify it as `answer`, never
    `plumbing` — otherwise the user's own revision request hides behind the default-off
    toggle. `build_transcript` therefore also takes `revision_requests` as an input.
    - **Why containment, not equality.** `_send_message_with_wire_retry` overwrites its
      own `sent_text` on a structured-mode wire retry:
      `sent_text = _STRUCTURED_WIRE_CORRECTION.format(original=text)`
      (`stages.py:397-403`, retry at the tail of that function), and it returns
      `{"role": "user", "content": sent_text}` — *the wrapped text*, not the original.
      So after any wire retry the persisted user row is
      `"<correction preamble>\n\n<the user's actual answer/instruction>"`. Under
      exact-match, rule (a) silently stops folding (harmless — it falls to hidden
      `plumbing`) but rule (b) silently **hides the user's revision request**, which is
      the exact bug rule (b) exists to prevent. Match on stripped containment of the
      FollowUp/RevisionRequest text and pin it with a test that feeds a
      `_STRUCTURED_WIRE_CORRECTION`-wrapped instruction.
    - `_self_heal_final`'s own `user` rows (`_CV_CORRECTION*`/`_CL_CORRECTION*`) contain
      neither an answer nor an instruction, so they correctly stay `plumbing`.
  - **`plumbing`**: assistant `Message` rows, plus user `Message` rows not folded by the
    two rules above. Unwrap them for display via the existing
    `unwrap_sentinel_to_canonical` (`turn_models.py:330`) then `json.loads`, falling back
    to the raw string. Truncate to a bounded length. Frontend hides these behind a
    toggle. Carries the turn's persisted reasoning (Phase 6) as
    `reasoning?: str | null`.
- **Merge order must be explicit and total**, because four sources are being interleaved
  and `created_at` provably collides within a single checkpoint (every row written by one
  `checkpoint()` call gets a near-identical `datetime.utcnow()`). Sort by the tuple:
  `(stage_index, timestamp, kind_rank, source_id)` where `timestamp` is `asked_at` /
  `created_at`, `kind_rank` is a **fixed** table `verdict=0, question=1, answer=2,
  plumbing=3, delivery=4`, and `source_id` is the row's own `id`. `seq` is then just the
  index in the sorted list. **Revision turns are the exception**: `_handle_final`
  (`stages.py:1202-1208`) stores a revision's Document under its *anchor* stage
  (`revising_cv → cv_adjust`, `revising_cl → cover_letter`), so a stage-first sort key
  parks `"Delivered CV v2"` before the cover-letter conversation that preceded it. For
  the `delivery` kind, sort by `(timestamp, kind_rank, stage_index, source_id)` instead —
  the checkpoint-collision guarantee needed `stage_index` only as a tiebreak, and
  delivery timestamps across revisions are genuinely distinct. The `verdict` turn's
  timestamp must come from a fixed source (`follow_ups[0].asked_at` or the earliest
  message `created_at`) — `job.updated_at` drifts on every update. Pin ordering with
  tests for both same-checkpoint stability and post-revision chronology (a revised-CV
  delivery must sort after the cover-letter turns).
- New `GET /api/jobs/{job_id}/transcript` in `routes_jobs.py`, 404 on unknown job. Load
  messages with an explicit `select(Message).where(job_id==...).order_by(Message.id.asc())`
  — `Job.messages` has no `order_by` on the relationship (`models.py:65`) and `created_at`
  collides within a checkpoint, so **`id` is the only reliable ordering key**.
  **Do not** add `messages` to `_fetch_job_with_relations`: **seven-plus** mutating
  endpoints share it and would each inflate.
- New `TranscriptChangedEvent {type: "transcript_changed", job_id}` in
  `events/schema.py`. `event_to_dict` is a generic `dataclasses.asdict` (`schema.py:97`),
  so no registry work. Deliberately **id-only, no content** — the bus fans every event out
  to every client with no filtering. Emit it after each `checkpoint()` returns — the
  publish-after-commit sites in `stages.py` are `:986`, `:1117`, `:1139`, `:1242`,
  `:1257`, `:1287`, **plus** `routes_jobs.py:489` (`ignore-fit` clears `fit_reason` →
  verdict turn disappears) — and in the `answer`/`revise` handlers, which publish
  nothing today. Any `datetime` on an event must be `.isoformat()`ed — `ws.py:30` does
  a bare `json.dumps`.
- **Reset paths invalidate the transcript too.** `backend_switch_reset`
  (`repo.py:359-424`) and the model-ladder hop emit only
  `BackendSwitchedEvent`/`ModelSwitchedEvent`; `soft_reset_job`/`nuclear_reset_job`
  (`repo.py:230-303`) emit only `StatusChangedEvent`; `_handle_session_expired`'s
  auto-soft-reset (`orchestrator.py:852-861`) emits only `LogEvent`. Since all of these
  delete Messages, emit `transcript_changed` at those sites as well — or the frontend
  keeps displaying rows that no longer exist. For extra belt, `store.ts` handlers for
  `backend_switched`/`model_switched`/`status_changed` should also refetch the
  transcript for that job, not just `refetchAll()`.

**Verify.** `pytest tests/backend/ -v`. New `tests/backend/test_transcript.py`: system
rows excluded; a structured-mode assistant row never leaks raw JSON into a `question`
turn; an answered FollowUp yields both a question and an answer turn; ordering is stable
across a checkpoint that writes three messages at once. Manually: `curl
localhost:8765/api/jobs/<id>/transcript`.

---

## Phase 3 — Thread UI (design item 1, frontend)

**Current State.** `FollowUpPane.tsx` holds `followUp` in **component-local** state,
refetched on mount / `jobId` change / submit only (`:37-41`, `:102`). `store.ts`'s
`applyEvent` routes `follow_up_needed` straight to `refetchAll()` (`:144-151`), which
calls `GET /api/jobs` returning `JobDTO[]` — a shape that **has no follow-ups at all**.
A second question arriving over WS updates the pane **only sometimes**: the state gate
(`JobDetail.tsx:441-445`) unmounts `FollowUpPane` whenever the client observes the
intervening `running` state, and the fresh mount fetches the new question — but if
`running` is never observed between two `awaiting_input` snapshots (a WS race), the
pane keeps the stale in-component record. The defect is real in that window; relying on
an unmount/remount cycle is not.

**Desired State.** The thread lives in the store, updates live, and renders per the
design's `renderThread`/`msgCard` (`.dc.html:749-838`): agent bubbles left-aligned at 82%
width with an `A` avatar, user bubbles right-aligned with a `Y` avatar, monospace role
label + timestamp, newest at the bottom, auto-scroll on append.

**Problems.**
- Every handled WS event funnels into `refetchAll()` (`store.ts:144-151`). Adding a
  high-frequency event near that switch without care storms `/api/jobs`.
- `theme/Icon.tsx:19-57` has 37 paths but **no** user/bot/brain icon. The design uses
  letter avatars (`A`/`Y`, `.dc.html:729-731`), so no new icons are needed — use letters.
- `frontend/src/__tests__/FollowUpPane.test.tsx` pins three literal English strings and
  the "Submit Answer" button; `ChatBox.test.tsx` uses singular
  `getByRole("textbox")` lookups. A thread plus one composer must keep exactly **one**
  textbox or these break.
- `FollowUpPane.test.tsx:16-32`'s `makeFullJob` **and** `JobDetail.test.tsx:43-59`'s
  `makeJob` / `:10-24`'s inline `getJob` mock omit six required `JobDTO` fields.
  Vitest does not typecheck so it passes, but copying either into a new test fails
  `npm run build` (`tsc && vite build`).
- **Parity gate for the pane rewrite** (per the repo's replace/delete rule): before
  replacing `FollowUpPane` with the `AgentThread` wrapper, pin its observable behavior
  in tests — open question rendered, Submit Answer posts and refetches, exactly one
  textbox. `ChatBox.test.tsx` already does this for the composer and must pass
  unchanged; extend the same discipline to the behaviors `AgentThread` absorbs.

**Solutions.**
- `types.ts`: add `TranscriptTurn` and extend the `WSEvent` union (`:52-74`) with
  `transcript_changed`.
- `store.ts`: add `transcripts: Record<string, TranscriptTurn[]>` and a
  `fetchTranscript(jobId)` action. Handle `transcript_changed` and the
  reset-announcing events (`backend_switched`, `model_switched`, `status_changed`)
  by refetching **only that job's transcript** — explicitly *not* `refetchAll()`.
  **Do not cap the turn list.** The toast `.slice(-5)` pattern (`store.ts:165-175`)
  bounds *ephemeral* notifications; applying it to a transcript would discard the
  history this whole phase exists to surface. If a bound is ever needed, cap at ~500
  with a visible "older turns truncated" affordance — not silently.
- `JobDetail.tsx:441-460`'s pane switch **must be edited** — today `FollowUpPane` mounts
  only on `awaiting_input`, and `state === "running" && viewedStage === "cv"`
  short-circuits to `ReviewPane` ahead of it, so the design's read-only `none` mode is
  currently unreachable. Change: mount the thread for `running` as well, and when
  `running && viewedStage === "cv"` render **both** — `ReviewPane` for the in-progress CV
  and the read-only thread beneath it, since they answer different questions ("what does
  the draft look like" vs "what has the agent been asking").
- New `frontend/src/components/AgentThread.tsx`: renders turns + mounts the existing
  `ChatBox` as its composer. `FollowUpPane.tsx` becomes a thin wrapper that picks the open
  FollowUp for `ChatBox`'s `followUpId` prop and delegates rendering to `AgentThread`.
  Modes per the design: `answer` (`awaiting_input`), `none` (`running`, read-only),
  `revise` (`review`/`cv_review`) — `.dc.html:719-722`, `:828-838`.
- Scroll container on the thread with `overflow-y: auto` and a `max-height`; auto-scroll
  to bottom on append. Must **not** be nested inside a `panelBase` panel (clip-path).
- `plumbing` turns collapsed behind a `SHOW_INTERNALS` toggle, default off.
- Empty/disappeared transcript is a first-class state — a BF-19 switch or model hop
  legitimately empties it (`repo.py:359-424`).
- **i18n:** every new string goes in `strings.en.json` under `agentThread.*` and renders
  via `useT()` (`i18n/useT.ts`) — never a literal. Run `scripts/translate-ui.sh`
  before shipping.
- **Build:** run `npm run build` after the UI work — the `jsa` CLI serves the gitignored
  `jsa/static` bundle, not live source.

**Verify.** `cd frontend && npm test` and `npm run build`. New
`AgentThread.test.tsx` covering: answered + open FollowUps both render; exactly one
textbox in `answer` mode; zero textboxes in `none` mode; plumbing hidden by default;
empty transcript renders the empty state. Manually: run a job to a second question and
confirm the first Q/A is still on screen.

---

## Phase 4 — Suggested replies, backend (design item 4)

**Current State.** No suggested-reply concept exists anywhere — not in
`turn_models.py`'s schemas, not in the sentinel grammar, not in `FollowUp`, not in
`AgentReply` (`base.py:33-38`, `frozen=True`, exactly four fields).

**Desired State.** On a `kind: "question"` turn, a structured backend also returns 2–4
short, distinct, directly-sendable answers, persisted with the FollowUp and served to the
frontend.

**Problems.**
- **Pydantic drops any field with a default from JSON-Schema `required`** — documented
  empirically at `turn_models.py:72-75`. So the field must be `list[str] | None` with
  **no default**, or strict providers will not see it as required and models will omit it.
- `test_mode_parity.py:151-178` asserts `sentinel_fu.question == structured_fu.question`.
  Suggestions must **never** be spliced into `question` text.
- `fit_assessment` has no question branch at all (`turn_models.py:136-138`) — this applies
  only to `cv_adjust`/`cover_letter`/`revising_cv`/`revising_cl`.
- `engine.py` has no Alembic; migration is `create_all` + additive `ALTER TABLE`, each in
  its **own** `try/except OperationalError` block (the reason is documented at
  `engine.py:64-67`).

**Solutions.**
- `turn_models.py`: add `suggested_replies: list[str] | None` (**no default**) to `CvTurn`
  and `ClTurn`. Extend each model's `@model_validator(mode="after")` via
  `_check_payload_iff_final` (`:84-97`) to require null unless `kind == "question"`.
  Thread it through `_route_structured_data` (`:217-240`) into the returned `AgentReply`.
  Leave `wrap_canonical_for_sentinel`/`unwrap_sentinel_to_canonical` (`:305-354`)
  **unchanged** — they intentionally reconstruct only `{kind, question, payload}`, and
  suggestions do not need to survive replay because the `FollowUp` row already holds them.
  This is safe because parsing is `json.loads` + `kind` routing with no `model_validate`
  (module docstring `:27-37`).
- `base.py`: add `suggested_replies: list[str] | None = None` to `AgentReply` (a dataclass,
  so a default is fine here — the no-default rule applies only to the Pydantic schemas).
- `prompt_assembly.py::_structured_contract` (`:107-146`): add a `shape_rules` clause
  instructing the model to populate the field with 2–4 short, distinct, directly-sendable
  answers of *varying* length — not padding to a fixed count. Follow the precedent of the
  existing "`question` is the ONLY field the user will see" clause (`:126-132`), which
  `CLAUDE.md` explicitly warns not to simplify away. The schema itself propagates for free
  via `json_schema_for`. **Sentinel-mode prompt bytes are pinned by
  `tests/backend/fixtures/language_directive_golden.json` — this change must not touch
  that path.**
- `models.py`: `suggested_replies: Mapped[str | None] = mapped_column(Text,
  nullable=True)` on `FollowUp`. **`Text`, JSON-encoded — not SQLAlchemy `JSON`**,
  following the `Document.structured` precedent (`models.py:94`): `create_all` would emit
  `JSON` for new DBs while the ALTER path adds `TEXT` for old ones, and the two deserialize
  differently.
- `engine.py`: one new `try/except OperationalError` block —
  `ALTER TABLE follow_ups ADD COLUMN suggested_replies TEXT`.
- `repo.py`: `checkpoint`'s FollowUp INSERT branch (`:519-523`) accepts and stores it.
  `stages.py::_handle_needs_input`'s `follow_up_data` (`:982-985`) is the **single**
  producer.
- `routes_jobs.py::_follow_up_to_dict` (`:58-67`): `json.loads` it, `None` → `null`.
  Also surface it on the Phase 2 `question` turn. `types.ts::FollowUpDTO` gains
  `suggested_replies: string[] | null`.
- **The sentinel parser lands here too, not in Phase 5.** `test_mode_parity.py` is a
  permanent gate asserting the two modes agree on observable output; extending it to cover
  `suggested_replies` while only the structured side can produce them would fail by
  construction (structured yields a list, sentinel yields `None`). So
  `protocol.py::parse_reply` gains its optional `<<<SUGGESTIONS>>>` handling in this
  phase: on a `NEED_INPUT` block, if a `<<<SUGGESTIONS>>>` marker appears inside the
  content, split the remainder one-per-line into `suggested_replies` and strip it out of
  `question`; absent → `None`, byte-identical to today. Every existing `protocol.py` test
  must pass **unmodified** — this is a pure superset. Phase 5 is then purely prompt
  handover + UI.

**Verify.** `pytest tests/backend/ -v`. Extend `test_mode_parity.py` to assert equal
`suggested_replies` across both modes for the same logical payload — the existing
permanent parity gate, now satisfiable because both parsers exist. New tests: the field is
in `json_schema_for(cv_adjust)`'s `required` list; a `final` turn with non-null
suggestions is a validation error; a legacy `FollowUp` row with a NULL column serializes
as `null`; `parse_reply` with and without the `<<<SUGGESTIONS>>>` block.

---

## Phase 5 — Suggested replies, prompt handover + UI

**Current State.** After Phase 4 both parsers accept suggestions, but no prompt actually
instructs a CLI model to emit them, and nothing renders them.

**Desired State.** Chips above the composer, and the user holds the prompt text needed to
light up the CLI backends.

**Problems.** Prompt files are user-owned (`CLAUDE.md`), and per the locked decision I do
not edit them — so CLI chips stay dark until the user pastes the block. Structured
backends work immediately.

**Solutions.**
- **Hand the user the exact prompt block text** to paste into
  `jsa/prompts/PROMPT_CDADJUST.md` and `CVL_PROMPT.md`. Until pasted, CLI backends simply
  produce no chips.
- Frontend: chip row directly above the textarea, per `.dc.html:815-827` —
  `SUGGESTED REPLIES` label with a `bolt` icon (already in `Icon.tsx`), wrapped ghost
  buttons. Behavior from `useQuickReply` (`.dc.html:802-814`): a short decisive suggestion
  **sends immediately**; a longer "let's fix this — …" one **populates the textarea** with
  the caret at the end for editing. Heuristic for send-vs-populate: send when the text is
  short and terminal (no trailing separator); the design's own `send` flag is a hardcoded
  demo artifact, so derive it rather than expecting the model to supply it.
- Chips belong to a specific FollowUp and clear once it is answered — the design's "only
  show for the latest agent turn" rule (`.dc.html:778`).
- ChatBox has **no `onKeyDown`** today (`:154-175` — it has `onKeyUp` at `:161`), so
  chips need none; but note it if Enter-to-send is wanted later.
- Chips are new user-visible strings — **i18n applies** (`strings.en.json` +
  `useT()`, run `scripts/translate-ui.sh`), same as Phase 3.

**Verify.** `npm test` + `npm run build`. Manually: run a job on
`opencode-zen` or `anthropic` to a question turn and confirm chips appear and both click
behaviors work.

---

## Phase 6 — Streaming: protocol, events, bus batching

**Current State.** Zero streaming anywhere in `jsa/` — every backend method is one
awaited call returning a full `AgentReply`. `bus.publish` (`events/bus.py:11-14`) fans
every event to every subscriber over **unbounded** queues with no filtering, backpressure,
drop policy or replay. `store.ts:207` drops `log` events entirely.

**Desired State.** An additive streaming counterpart alongside the synchronous methods,
gated by a capability flag, with deltas batched before they reach the bus.

**Problems.**
- **Nothing is classifiable until the turn ends.** `_BLOCK_RE` needs the terminating
  `<<<END>>>`; `_extract_structured_text` (`anthropic_api.py:56-87`) needs a complete
  `tool_use` block and rejects `stop_reason == "max_tokens"` outright. Streaming can carry
  *display* text but must never short-circuit parsing.
- **Streamed partials must be retractable, not merely appended.** Six paths replay a
  whole turn and produce a *second* assistant turn superseding the first:
  `_parse_with_nudge` (**five** copies: `_openai_compat.py:314`, `claude_cli.py:114`,
  `google_cli.py:141`, `opencode_go.py:230`, `opencode_zen.py:269`),
  `_parse_structured_with_downgrade`, `_start_session_with_retry`,
  `_send_message_with_wire_retry`, `_self_heal_final`'s up-to-2 corrections — **and**
  the in-backend `retry_transient` loop (`_openai_compat.py:345-377`,
  `opencode_zen.py:330-357`, `opencode_go.py:248-272`), which re-runs the identical
  call up to `_MAX_ATTEMPTS` times *inside* one turn. If attempt 1 streamed chunks
  before failing, attempt 2 streams again into the same accumulator, so a per-attempt
  reset hook is required (see below) — `AgentTurnEndEvent(superseded)` alone cannot
  fix this.
- The bus was sized for coarse status events. Raw per-token events would flood it, and on
  the frontend would storm `/api/jobs` if routed near `applyEvent`'s `refetchAll` block.

**Solutions.**
- `base.py`: `supports_streaming: ClassVar[bool] = False`, mirroring
  `supports_structured_output` — **hard-coded per backend, never runtime-detected**. Add
  an optional `on_chunk: Callable[[AgentChunk], Awaitable[None]] | None = None` callback
  parameter rather than a new `AsyncIterator` method: the callback keeps the existing
  `-> AgentReply` return contract, so all five retry/replay paths, `adapt_history` and
  `run_stage`'s control flow stay untouched. A backend with the flag `False` never
  receives one (same conditional-kwarg rule as `structured_schema`,
  `stages.py:276-285`).
- New events: `AgentChunkEvent {job_id, stage, kind, text}` and
  `AgentTurnEndEvent {job_id, stage, superseded: bool}`. `superseded=True` tells the
  frontend to **discard** the streamed buffer — that is the retraction channel for the
  five replay paths.
  `AgentChunk = {kind: "content" | "reasoning", text: str}`. The callback is **awaitable**
  because `bus.publish` is `async` — a sync callback could not reach the bus without a
  side-channel queue and a separate flush task, which is strictly more machinery for no
  gain. Backends already `await` inside their stream loops, so awaiting the callback there
  is free.
- **Batch in the pipeline, not the bus.** A small accumulator in `stages.py` coalesces
  chunks and flushes on a ~75ms timer or a size threshold, whichever first, so
  `bus.publish` sees roughly the same event rate it does today. **The timer is justified
  by the SSE backends, not by `claude-cli`** — a live capture of a two-word `claude` reply
  produced only *two* `content_block_delta` events, i.e. that channel is already coarse.
  Do not delete the accumulator on the strength of a claude-cli observation; items 1/3/4/5
  emit genuinely fine-grained deltas. Do not change `EventBus`
  itself. The accumulator owns the flush task; the `on_chunk` callback it hands the
  backend just appends to its buffer and returns. It **must force-flush before the
  pipeline publishes the `AgentTurnEndEvent`**, or the final batch races the buffer
  clear. It also exposes a reset hook callable by the in-backend `retry_transient`
  loops, so each new attempt starts an empty buffer instead of concatenating attempts.
- Persist only the finished, joined reasoning text once the turn completes — never the
  per-token buffer. **Decide where it lives before Phase 7 starts.** Options ranked:
  (1) new nullable `reasoning` column on `Message` (additive `ALTER TABLE` — the
  pattern is real and has nine precedents in `jsa/db/engine.py:30-73`; **the replay
  exclusion needs no code**, because `_load_history` projects each row to
  `HistoryTurn(role=m.role, content=m.content)` and `adapt_history` only ever sees that
  two-field projection, so a new column is structurally invisible to replay). Chosen
  here, because the alternative evaporates the REASONING card on page reload and the bus
  has no replay;
  (2) do not persist, live-only. This upholds the "DB stores canonical form, never
  provider wire format" invariant (`turn_models.py` module docstring).
- Streaming is **best-effort**: any failure inside the chunk path must be swallowed and
  the synchronous result returned intact. A streaming bug must never fail a job.

**Verify.** `pytest tests/backend/ -v`. New `test_streaming_protocol.py`: a
`supports_streaming = False` backend never gets `on_chunk`; the accumulator emits at most
N events for M tokens; a `superseded` turn end follows a nudge retry.

---

## Phase 7 — Streaming: per-backend implementations

**Current State.** Seven distinct call sites, because `opencode_zen.py` deliberately keeps
its own private copy of the payload/retry machinery rather than sharing
`_openai_compat.py` (`_openai_compat.py:1-8` — the un-refactoring is intentional and gated
on a 935-line test file).

**Desired State.** Real streaming where a real channel exists; honest silence elsewhere.

**Solutions**, in ascending risk — each independently shippable:

| # | Backend(s) | Change | Channel |
|---|---|---|---|
| 1 | `_openai_compat.py::_call_api_once` (`:379`) | `stream: true` + `client.stream()` SSE parse | content; `reasoning_content` **only** where a routed model actually emits it — never fabricate |
| 2 | | ↳ covers `mistral`, `openrouter`, and `opencode-go`'s `/chat` for free — **not** `gemini` (it overrides `_call_api_once`; item 4 handles it) | |
| 3 | `anthropic_api.py` (`:252-257`) | SDK `messages.stream()` | content; **structured mode streams nothing** — forced tool-use hides it |
| 4 | `gemini_api.py` (`:184-190`) | `:streamGenerateContent` (different URL) | content; `thinkingConfig` thought summaries if enabled |
| 5 | `opencode_zen.py` (`:359-503`) | same as (1), against its private copy | content |
| 6 | `opencode_go.py` `/messages` (`:274`) | hand-built, own path | content |
| 7 | `claude_cli.py` | **`--output-format stream-json --include-partial-messages --verbose`** + a new `run_killable_streaming` in `_subprocess.py` | **content AND reasoning** (`thinking_delta`) — the richest channel of the lot |
| — | `google_cli.py` | **none.** `agy` has no streaming flag. `supports_streaming = False`; "working…" indicator only | — |

**The one genuinely delicate item is (7).** `_subprocess.py:55` uses
`proc.communicate()`, and the process-group kill guarantee (`os.killpg`, `:64`) hangs off
that structure — the module docstring calls this "the one place killability is implemented
and tested". A streaming variant must read `proc.stdout` line-by-line while draining
`stderr` concurrently (or the OS pipe buffer deadlocks) and **re-establish the identical
kill contract**, including the `AgentTimeout` path and `CancelledError` propagation. But
killability is not the whole parity surface — the variant must also reproduce:
`_run`'s non-zero-exit → `ClaudeCliError`, the `"No conversation found"` →
`ClaudeSessionExpiredError` mapping (`claude_cli.py:90-106`), and `_parse_with_nudge`'s
`_LIMIT_KEYWORDS` quota scan of raw text (`claude_cli.py:128-133`). Under
`stream-json`, quota/session failures surface as JSON `result`/`error` events rather
than raw-text keywords, so the detection points differ from the marker strings the
existing tests pin. **A live capture confirms the stream carries far more than model
output:** one short reply emitted six `{"type":"system"}` events
(`hook_started`/`hook_response` — the operator's own `~/.claude` hooks fire on every
`claude` invocation, and their `output` field can contain arbitrary text), one
`{"type":"rate_limit_event"}`, and one `{"type":"result"}`. The parser must therefore
**whitelist**: accept only `type == "stream_event"` → `event.type ==
"content_block_delta"` → `delta.type in {"text_delta", "thinking_delta"}` for chunks, and
the terminal `{"type":"assistant"}` for `raw` reassembly; ignore everything else. A
blacklist would leak hook output into the user's thread. `{"type":"rate_limit_event"}`
and `{"type":"result"}` are the concrete, observed detection points that replace the
raw-text `_LIMIT_KEYWORDS` scan — inspect them rather than guessing at a JSON shape.
Add it as a *new* function; leave `run_killable` untouched so
`google_cli` and `run_research` keep their tested behavior. Reassemble `raw` from the
terminal `{"type":"assistant"}` event's content blocks, which is strictly more robust
than scraping `--output-format text`.

**Verify.** Per-backend unit tests with a fake SSE / fake NDJSON stream. For (7), a fake
executable that dribbles NDJSON lines with a delay, asserting chunks arrive **before**
process exit and that a timeout still kills the whole group. Existing backend tests —
notably `test_opencode_zen.py` (935 lines) — must pass unchanged.

---

## Phase 8 — Streaming UI

**Solutions.** `store.ts`: a per-job streaming buffer, cleared on `AgentTurnEndEvent`
(and discarded outright when `superseded`). `AgentThread` renders an in-progress agent
bubble from the content buffer, and the design's REASONING card
(`.dc.html:733-748` `thinkCard`) — collapsible, spinner while live, `bolt` icon and
chevron when done — **only when reasoning chunks actually arrived**. No chunks → the
plain "working…" indicator, which is the honest ceiling for `google-cli` and for every
structured-mode session.

**Verify.** `npm test`, `npm run build`. Manually: run a `claude-cli` job and watch
reasoning + content stream; run an `anthropic` structured job and confirm no fake
REASONING card appears.

---

## Risks

- **The transcript is destroyed by resets and BF-19 switches** (`repo.py:359-424`). Not a
  bug to fix here, but the UI must never assume monotonic growth — and since
  `transcript_changed` is not emitted on those reset paths today (Phase 2 fixes this),
  the current UI will keep displaying ghost rows until Phase 2 lands.
- **Frontend test churn.** `FollowUpPane.test.tsx`, `ChatBox.test.tsx`,
  `JobDetail.test.tsx` all pin the current single-question shape. Expect to rewrite, not
  patch. Do not copy either test file's `makeFullJob`/`makeJob` fixture — both omit six
  required `JobDTO` fields (`jd`, `model_name`, `effective_model`, `language`,
  `fit_reason`, `retry_count`) and will fail `tsc`.
- **Phase 7 item (7)** is the highest-risk change in the plan: it touches the one tested
  killability seam. Adding a parallel function rather than modifying `run_killable` is the
  mitigation.
- **Prompt-file dependency.** Phase 5's CLI chips do nothing until the user pastes the
  handed-over block.

## Verification (end to end)

```bash
pip install -e .
pytest -v -m "not integration"
cd frontend && npm install && npm test && npm run build
```

Then a live run per `CLAUDE.md` → "How to test a phase":

```bash
jsa --csv jobs.csv --no-browser      # http://localhost:8765
```

Drive a job to a question turn, answer it, confirm the Q/A persists in the thread through
the next stage; confirm no composer overlay while scrolling; confirm chips render on a
structured backend. Playwright is worth using for the scroll/overlay check in Phase 1 —
it is the one defect that is purely visual.

---

## Change Log

2026-09-02: Folded the skeptic review into the plan before implementation. Actions:
corrected two refuted claims (Phase 2's stage-first merge key misordered revision
deliveries — `_handle_final` maps revisions back to their anchor stage; the "system row
contains the CV skeleton" motivation was wrong — that content lives in the first user
row); added the missing reset-path transcript invalidation (BF-19 switch, model hop,
soft/nuclear reset, session-expired auto-reset — none emitted `transcript_changed`);
added the FollowUp-answer/user-Message dedupe and the RevisionRequest-instruction
classification rule (both previously would double-render or hide user input); added the
sixth supersede source (in-backend `retry_transient` loops) plus accumulator
force-flush-before-turn-end ordering; locked a reasoning-persistence decision (new
nullable `Message.reasoning` column, excluded from replay) instead of leaving it
unspecified; corrected overstated claims (the FollowUpPane WS race, five `_parse_with_nudge`
copies not four, seven-plus not five mutating endpoints, both test fixtures broken not
just FollowUpPane's); hardened Phase 1 keep-list (`display:flex` required, decide
`paddingLeft/Right` deliberately); added Phase 7 item (7) parity surface beyond
killability (`ClaudeCliError`/`ClaudeSessionExpiredError`/`_LIMIT_KEYWORDS`, which under
`stream-json` differ from raw-text keyword detection); applied i18n to Phase 5's chip
strings. Verification: awaited — no code written yet.

2026-09-03: Context — the fold-in session above died mid-pass; asked to finish it and
independently re-assess the skeptic's findings rather than take them on trust. Actions:
re-verified every load-bearing finding against the code myself — confirmed the revision
anchor-stage mapping (`stages.py:1202-1208`), the system-row/user-row split
(`stages.py:809-819`), the six `checkpoint` sites (986/1117/1139/1242/1257/1287), the
verbatim answer/instruction resend (`stages.py:757-782`), the in-backend
`retry_transient` loops (`_openai_compat.py:138`, `opencode_zen.py:343`,
`opencode_go.py:257,269`) and `display:"flex"` at `ChatBox.tsx:146`; then resolved the
skeptic's two open "unverifiable" items — extracted the design zip from `~/Downloads`
and spot-checked nine `.dc.html` citations (all accurate), and reproduced the `claude`
NDJSON envelope live (matches the plan verbatim, `"text":"ban"` included). Decisions —
overrode the skeptic on one point (the design citations are verifiable, the zip is just
outside the repo) and found two defects the skeptic missed: (i) the folded-in dedupe
rules used **exact** text match, but `_send_message_with_wire_retry` persists
`_STRUCTURED_WIRE_CORRECTION.format(original=...)` — the wrapper, not the original —
so rule (b) would have hidden the user's revision request, exactly the bug it was
written to prevent; changed both rules to stripped containment. (ii) the `claude`
stream carries hook output, `rate_limit_event` and `result` alongside model deltas, so
Phase 7 item (7) now mandates a whitelist parser and names those events as the quota/
session detection points. Also closed Phase 1's open `paddingLeft/Right` question
against the verified design line (`:764` has no horizontal or bottom padding — both go),
recorded that the `Message.reasoning` replay exclusion needs no code (`_load_history`
projects to `HistoryTurn(role, content)`), noted the accumulator's 75ms timer is
justified by the SSE backends rather than claude-cli's coarse two-delta chunking, and
removed a duplicated `AgentChunk` definition left by the interrupted fold-in.
Verification: verified for all code and design claims listed above; still no
implementation code written — the plan remains `Pending`.

---

2026-09-03 (implementation): Context — began implementing Phase 1 on branch
`fix/agent-chat-composer-clipping` (from `main`). Actions: in `ChatBox.tsx`, removed
`position/bottom/zIndex/marginLeft/marginRight/marginBottom/paddingLeft/paddingRight/
paddingBottom`, the gradient background, `boxShadow`, and the stale comment referencing
the non-existent `.claude/designs/design_handoff_docked_chat_input`; kept
`display:"flex"`, `flexDirection`, `gap`, `borderTop`, `paddingTop:12` (matching
`.dc.html:764` exactly) and added `flex:"none"`. Decisions — none; implementation matched
the plan's Solutions section exactly, no deviations. Verification: `ChatBox.test.tsx`
(24/24) and the full frontend suite (293/293) pass unchanged; `npm run build` (tsc + vite)
clean. **Not verified**: the live visual overlay/clipping check against a real
`awaiting_input` job — no local sqlite DB/job exists in this checkout to drive that state
without a separate setup pass, so this remains open per the plan's own note that Phase 1
alone (without Phase 3's scroll container) is a visible-but-lesser regression risk if
shipped alone; plan already says merge Phases 1+3 to `main` as one unit.

2026-09-03 (implementation): Context — implemented Phase 2 on branch
`fix/agent-chat-composer-clipping` (same branch as Phase 1, per the plan's "merge as
one unit" note). Actions: new `jsa/api/transcript.py::build_transcript` (pure,
unit-tested in `tests/backend/test_transcript.py`, 12 tests); new
`GET /api/jobs/{job_id}/transcript` in `routes_jobs.py` (explicit
`select(Message)...order_by(Message.id.asc())`, not added to
`_fetch_job_with_relations`); new `TranscriptChangedEvent` in `events/schema.py`,
emitted after all six `checkpoint()` call sites in `stages.py`
(`_handle_needs_input`, both fit-assessment checkpoints, and all three `_handle_final`
checkpoints), after `routes_jobs.py`'s `answer`/`revise`/`ignore-fit` handlers, and at
every reset path that deletes Messages (`routes_jobs.py`'s `reset_job` for
soft/nuclear reset, and `orchestrator.py`'s two `backend_switch_reset` call sites —
model-hop and backend-advance — plus `_handle_session_expired`'s auto-soft-reset).
Decisions — deviated from a literal reading of the merge-order spec: implementing
`(stage_index, timestamp, kind_rank, source_id)` for non-delivery turns verbatim
raises `TypeError` when sorted in the same list as delivery turns keyed
`(timestamp, kind_rank, stage_index, source_id)` (comparing a raw `datetime`/epoch-float
against a small int `stage_index` at tuple position 0 across differently-shaped
keys is not just semantically wrong but literally uncomparable/order-breaking in
Python once the two shapes are sorted together). Resolved by keying **every** turn on
epoch-timestamp first, with `stage_index` demoted to a tiebreak position for
non-delivery turns (`(timestamp, stage_index, kind_rank, source_id)`) — this is
consistent with the plan's own stated rationale for the delivery exception ("the
checkpoint-collision guarantee needed stage_index only as a tiebreak"), just applied
uniformly instead of literally per the two written tuples. Pinned with a dedicated
`TestPostRevisionChronology` test (revised-CV delivery sorting after the intervening
cover-letter conversation) plus same-checkpoint stability tests. Verification: `pytest
tests/backend/ -v -m "not integration"` — **1716 passed, 2 skipped, 21
deselected(integration)**, full suite, no regressions. Note: the repo's system
`python3` lacks `pytest-asyncio` — tests must run via `.venv/bin/python -m pytest`,
not a bare `pytest`/`python3 -m pytest` on PATH.

2026-09-03 (implementation): Context — implemented Phase 3 (thread UI) on the same
branch, via a coder subagent. Actions: new `AgentThread.tsx` (turns rendered as
letter-avatar bubbles, `answer`/`none`/`revise` modes, plumbing turns behind a
`SHOW_INTERNALS` toggle default off); `FollowUpPane.tsx` rewritten as a thin wrapper
delegating to `AgentThread(mode="answer")`; `store.ts` gained `transcripts` +
`fetchTranscript`, and WS handlers for `transcript_changed`/`backend_switched`/
`model_switched`/`status_changed` now refetch only that job's transcript instead of a
full `refetchAll()`. Decisions — code review (medium, after this phase) found the new
`AgentThread` dropped the old FollowUpPane's `onSubmitted` callback, relying solely on
WS `transcript_changed` for refresh — a real correctness bug (a dropped WS connection
could leave an answered FollowUp appearing still open); fixed by wiring
`onSubmitted={() => fetchTranscript(jobId)}` on both `ChatBox` mounts. Also caught and
fixed (self, not auto-fixed): a stale sort-order docstring left over from Phase 2's
merge-key correction, and a real containment-dedupe false-positive risk (a short/common
answer like "yes" spuriously matching unrelated plumbing text as a substring) — fixed
with `_MIN_CONTAINMENT_LEN = 12` + exact-equality fallback below that threshold.
Verification: full backend (1716 passed) + frontend (298 passed) suites, `npm run
build` clean.

2026-09-03 (implementation): Context — implemented Phase 4/5 (suggested replies:
backend schema + UI chips) on the same branch. Actions: `CvTurn`/`ClTurn` gained
`suggested_replies: list[str] | None` with no default (so it lands in JSON-Schema
`required`); `FollowUp.suggested_replies` column (JSON-encoded, additive `ALTER TABLE`
migration); `SuggestedReplyChips` in `AgentThread.tsx` with a `shouldSendImmediately`
heuristic (short/decisive suggestions send immediately, longer ones populate the
textarea); `ChatBox` converted to `forwardRef` exposing `sendText`/`populateText`.
Decisions — code review (medium, after this phase) found 6 issues, all auto-fixed:
dedupe-rule ordering (RevisionRequest-instruction match must run before the
answered-answer containment fold, or a real revision instruction containing a prior
short answer as a substring gets silently dropped), the chip immediate-send regex not
properly anchored, test-correctness fixes, `wrap_canonical_for_sentinel` missing a
`<<<SUGGESTIONS>>>` block (a BF-19 backend-switch fidelity gap), the `<<<SUGGESTIONS>>>`
marker regex matching the literal substring anywhere instead of requiring it to start
its own line, and a `build_transcript` session-detachment risk (`DetachedInstanceError`)
from calling it after the DB session closed. These fixes were left uncommitted by the
reviewer; committed separately as `0b00a7f` after discovery via `git status`.
Verification: full backend + frontend suites green, `npm run build` clean.

2026-09-03 (implementation): Context — implemented Phase 6+7 (streaming protocol +
per-backend implementations) on the same branch, the plan's explicitly highest-risk
bundle. Actions: `jsa/pipeline/streaming.py::ChunkAccumulator` (75ms/200-char batching,
`force_flush`/`end_turn`/`reset`); `AgentChunkEvent`/`AgentTurnEndEvent`; `on_chunk`/
`on_retry` threaded conditionally through every backend's `start_session`/
`send_message` (mirrors the existing `structured_schema` conditional-kwarg pattern);
real SSE added to `mistral`/`openrouter`/`opencode-go`(`/chat`)/`opencode-zen`/
`anthropic`(SDK `messages.stream()`)/`gemini`; `claude_cli.py` gained
`run_killable_streaming` (new sibling function in `_subprocess.py` — `run_killable`
itself untouched, confirmed via `git diff --stat` showing zero line changes) plus a
whitelist NDJSON parser (only `stream_event`→`content_block_delta`→`text_delta`|
`thinking_delta` become chunks — a live capture showed the stream also carries hook
system events, `rate_limit_event`, and `result`, which must never leak into the user
thread). Decisions — first pass had a self-identified gap: the turn-supersession
signal (`on_retry` → `end_turn(superseded=True)`) was only wired at 3 of 6 documented
supersede sources; closed in a follow-up pass adding all remaining `_parse_with_nudge`
copies and in-backend `retry_transient` loops (deliberately excluding `google_cli.py`,
since `supports_streaming=False` there means `stages.py` never passes the kwarg).
Code review (medium, run after both passes, first attempt failed with an HTTP 429
session-limit error and was retried successfully) found and fixed 6 real bugs:
`AnthropicAPIBackend` didn't accept `on_retry` despite declaring
`supports_streaming=True` (guaranteed `TypeError` on first retry); four backends
parsed an HTTP-200 JSON error envelope during streaming as an empty SSE stream instead
of routing it through BF-19 classification; `gemini_api.py`'s streaming path ignored
`finishReason == "MAX_TOKENS"`, silently returning truncated replies as complete;
`claude_cli.py`'s streaming handler classified any error-shaped `result` event as
`AgentLimitReached` instead of only genuine quota signals; `end_turn(superseded=True)`
didn't clear `_full_reasoning`, contaminating a retried turn's reasoning with a
discarded attempt's text; `end_turn(superseded=False)` fired before the stale-job
guard/checkpoint, letting a dismissed job announce "turn complete" with nothing
persisted. Fixes committed as `655a99a`. Verification: 1748/1748 backend tests pass
(1 pre-existing unrelated flaky test noted, `test_dev_tunnel.py`); existing
`test_opencode_zen.py` (935 lines) passes unchanged.

2026-09-03 (implementation): Context — implemented Phase 8 (streaming UI) on the same
branch. Actions: `store.ts` gained `streamBuffers: Record<string, {stage, content,
reasoning}>`, populated by `agent_chunk` WS events and cleared unconditionally on
`agent_turn_end` (both the completed and superseded cases end the buffer's life —
completed because the checkpoint already landed and `transcript_changed` will render
the real turn, superseded because the buffer is stale); `AgentThread.tsx` gained
`LiveBubble`, rendering the in-progress agent turn with a collapsible REASONING card
(bolt icon, chevron, open by default) only when reasoning chunks actually arrived,
otherwise a plain "working…" spinner — the honest ceiling for `google-cli` and every
structured-mode session, which never stream reasoning. `WSEvent` extended with
`agent_chunk`/`agent_turn_end`, mirroring the backend event schema exactly; both wire
generically through the existing `applyEvent` dispatch with no changes needed to
`ws.ts`. Decisions — none; implementation matched the plan's Solutions section.
Verification: `tsc --noEmit` clean, 302/302 frontend tests pass, `npm run build`
clean (rebuilt `jsa/static`). Not run: `scripts/translate-ui.sh` (locale catalog
sync) — this environment has no `ANTHROPIC_API_KEY` and its `--backend cli` path
hard-codes `python` instead of `python3`, both pre-existing environment gaps
unrelated to this change. Not verified: the plan's manual live-run check (a
`claude-cli` job streaming reasoning+content, an `anthropic` structured job confirming
no fake REASONING card) — no local sqlite DB/job exists in this checkout to drive
that state, same limitation noted for Phase 1's visual check.

2026-09-03 (bugfix): Context — user ran a real `jsa` job and reported that
"thinking"/"reasoning" never appeared in the UI despite this plan claiming Phases
6-8 (streaming) were implemented and reviewed. Investigation: verified the
streaming plumbing itself is real and correct — live-tested `claude
--output-format stream-json --include-partial-messages --verbose --model
claude-haiku-4-5` and confirmed `thinking_delta` events fire even for a trivial
prompt (extended thinking is on by default for this model via the CLI, no
special flag needed) — then traced `run_stage` and confirmed `on_chunk`/the
`ChunkAccumulator` are correctly wired into all of `cv_adjust`/`cover_letter`/
`revising_cv`/`revising_cl`. Found the actual gap: `_run_fit_assessment` — a
separate function, not part of `run_stage`'s branches — was never touched by
Phase 6/7/8 at all (confirmed via `git log -p` across all three commits: zero
hits). It called `backend.start_session(...)` with no `on_chunk`/`on_retry`
kwarg, so a streaming-capable backend never streamed its fit-assessment turn.
Since `fit_assessment` is the mandatory first stage of every job (see CLAUDE.md
→ "Fit-assessment gate"), this fully explains the report — the user's job(s)
likely never got to see any streamed reasoning because the very first thing
every job does was silently non-streaming. Action: wired the same
`ChunkAccumulator`/`_streaming_kwargs`/`_on_retry_for` pattern `run_stage` uses
into `_run_fit_assessment` (`jsa/pipeline/stages.py`) — accumulator created when
`backend.supports_streaming`, `on_chunk` passed to `start_session`,
`end_turn(superseded=True)` on the `ProtocolError` failure path (discards any
partial stream before the unfit modal), `end_turn(superseded=False)` after the
existing stale-job guard and before checkpoint (mirrors `run_stage`'s own
ordering rationale), and `accumulator.take_reasoning()` persisted onto the
assistant message's `reasoning` field via the existing `Message.reasoning`
column. Added `TestFitAssessmentStreaming` (2 tests) to
`tests/backend/test_fit_assessment.py` pinning both the streaming-backend
reasoning-persisted case and the non-streaming-backend no-reasoning case.
Verification: verified — `tests/backend/test_fit_assessment.py` (50/50) and the
full backend suite (1750/1750, up from 1748 with the 2 new tests) pass. Not
independently re-verified: the live end-to-end UI check (running a real job
through fit_assessment and watching the REASONING card render) — same
no-local-DB limitation noted for every other manual-verification gap in this
plan; the user should confirm this resolves what they observed.

2026-09-03 (bugfix, continued): The user ran a live `jsa` job with their actual
configured backends — `opencode-zen` primary, `opencode-go` fallback — and confirmed
the previous fit_assessment fix did NOT resolve it: no reasoning ever appeared, and
even the final `content` came back as one whole blob, not token-by-token, despite
`supports_streaming = True` on both backends. The user correctly guessed the cause
themselves ("we have a structured output here so he'll be streaming a json... how are
you going to distinct between a json brace `{` and the actual text"). Root cause,
confirmed by reading the code: both `opencode-zen` and `opencode-go` are
structured-capable (`supports_structured_output = True`), and `_structured_schema_for`
(`jsa/pipeline/stages.py`) puts every stage on EVERY structured-capable backend into
structured JSON mode unconditionally — there is no opt-out, so this is not a special
case, it is these backends' only mode in normal operation. Both `_openai_compat.py`
(the shared base `mistral`/`openrouter`/`opencode-go`'s `/chat` protocol build on) and
`opencode_zen.py`'s independent duplicate copy (see its module docstring for why it's
not built on the shared base) had the same line: `stream_cb = on_chunk if
structured_schema is None else None` — i.e. ANY structured call disabled SSE
entirely, content and reasoning alike, unconditionally. Since these backends are
always in structured mode, streaming across this whole family was fully inert in
practice, matching the user's report exactly (`claude-cli`, the one backend that
showed streaming in the earlier fix's live test, is sentinel-only —
`supports_structured_output = False` — so it never hit this gate).

Investigated whether the gate was actually necessary: read `_call_api_once` in both
files and confirmed `response_format` + `stream: true` is sent as a normal, already-
supported combination on this OpenAI-compatible wire shape — nothing in the payload
builder or the 200/4xx/5xx classification logic assumes non-streaming when structured.
The design handoff (`design_handoff_agent_chat_upgrade/README.md`, feature 2) also
states plainly for this backend family: "SSE streaming exists, but a separate
reasoning delta only exists for the specific proxied models that expose one... For
everything else on these backends, stream `content` only — do not fabricate a
reasoning stream" — i.e. the design's intent was real SSE streaming for this family
in general, not a blanket disable under structured mode; the blanket disable was an
implementation shortcut from Phase 6/7, not a locked design decision (not memorialized
in CLAUDE.md as one).

Fix: in both `jsa/agents/_openai_compat.py` and `jsa/agents/opencode_zen.py`, added a
module-level `_reasoning_only(on_chunk)` helper that wraps `on_chunk` to forward only
`kind="reasoning"` chunks, dropping `kind="content"` ones (the raw JSON `content`
delta in structured mode is not human-readable mid-stream, e.g. a stray `{`, and would
corrupt the chat bubble — this is exactly the ambiguity the user flagged, resolved by
never showing it rather than trying to distinguish JSON structure from prose text).
`_call_api` in both files now does `stream_cb = _reasoning_only(on_chunk) if
structured_schema is not None else on_chunk` instead of gating streaming off entirely,
and `retry_cb = on_retry` unconditionally (previously also gated off for structured
calls) — so a retried structured call still correctly discards any reasoning streamed
by the abandoned attempt via `ChunkAccumulator.end_turn(superseded=True)`. The
accumulated `content` (full JSON) is still returned as the raw reply regardless of
what's forwarded to `on_chunk` — `_consume_sse` already accumulated it internally
either way, unchanged. `mistral.py`, `openrouter.py`, and `opencode_go.py`'s
`/chat/completions` protocol all inherit the fix for free via the shared base;
`opencode_go.py`'s `/messages` protocol needed no change — those models are
sentinel-only (`supports_structured_output = False` on that instance), so they never
hit this gate in the first place. `anthropic_api.py`'s equivalent gate
(`use_stream = structured_schema is None and on_chunk is not None`) was NOT touched —
out of scope (the user doesn't use this backend) and a separate, larger gap besides:
that backend never enables extended thinking at all, so even sentinel-mode streaming
there has no reasoning channel to forward regardless of this fix's pattern. Left as a
known gap, not fixed here.

Updated `tests/backend/test_streaming_openai_compat.py`'s
`test_structured_schema_never_streams` (pinned the now-wrong old behavior) into two
tests: `test_structured_schema_streams_reasoning_but_suppresses_content` and
`test_structured_schema_no_on_chunk_uses_non_streaming_path`. Added a new
`TestStreamingBehavior` class (3 tests) to `tests/backend/test_opencode_zen.py`, which
had ZERO prior streaming coverage despite the streaming code (sentinel-mode included)
predating this fix — covers sentinel-mode streams-both-kinds-unfiltered,
structured-mode streams-reasoning-only, and structured-mode-without-on_chunk stays on
the non-streaming `client.post` path.

Verification: verified — targeted files
(`tests/backend/test_streaming_openai_compat.py`,
`tests/backend/test_opencode_zen.py`, `tests/backend/test_opencode_go.py`) and the
full backend suite (1754/1754, up from 1750) pass. One test,
`test_orchestrator.py::TestAwaitingInputResume::test_unanswered_followup_stays_parked`,
failed once in the full-suite run and passed cleanly in isolation — a pre-existing
order-dependent flake unrelated to this change (confirmed by re-running the full suite
a second time with no failures). Not independently re-verified: whether the user's
actual configured opencode-zen/opencode-go model ever emits a `reasoning_content`
delta at all — that is model-specific and this fix cannot manufacture a channel a
given proxied model doesn't expose (the design handoff explicitly anticipates this: "do
not fabricate a reasoning stream"). The user should re-run a live job and report
whether a REASONING card now appears; if their specific model never sends
`reasoning_content`, the honest outcome is unchanged `content` chunking behavior (still
non-streamed, by design, in structured mode) with no reasoning card — not a bug, a
model-capability ceiling.

## Decisions Log

_Reserved for the user. Not to be written by the agent._
