---
status: InProgress
---

# Structured Output for API Backends — Sentinel as Fallback

## Context

The JSA pipeline parses every model reply through `<<<NEED_INPUT>>>`/
`<<<FINAL>>>` sentinel blocks (`jsa/agents/protocol.py::parse_reply`). For CLI
backends (`claude-cli`, `google-cli`) this is the only available channel — but for
HTTP API backends (`anthropic`, `opencode-zen`) the sentinel contract is pure
prompt etiquette, and a stubborn model that never emits sentinels raises
`ProtocolError`, which is deliberately NOT a BF-19 signal and therefore hard-fails
the job (the "stuck pipeline" problem).

The CV and cover-letter stages already mandate a JSON payload *inside* the
FINAL block, validated post-hoc by Pydantic (`jsa/schema/cv.py`,
`jsa/schema/cover_letter.py`). This plan moves those stages — plus the free-text
fit verdict — onto provider-enforced JSON-schema output for the two HTTP API
backends, keeping the sentinel grammar as the CLI fallback path.

### Locked decisions (from user)

1. **Capability detection**: hard-coded per backend (`supports_structured_output`
   ClassVar on `AgentBackend`; anthropic + opencode-zen = True, CLIs = False).
2. **opencode-zen**: attempt structured, per-session downgrade to sentinel on
   unparseable reply (per-session handle flag, not persisted; restarts flip back
   to structured since the downgrade was that session's flakiness).
3. **Reply union**: single flat schema per stage — `{kind: "question"|"final",
   question?, payload?}` (single Pydantic model with nullable fields + cross-field
   validator; NOT an `anyOf` discriminated union — strict json-schema providers
   reject top-level `anyOf` and require `additionalProperties: false` +
   all-fields-required).
4. **Fit verdict**: structured `{verdict: enum[FIT,UNFIT], reason: str}` —
   `reason` is REQUIRED for both verdicts (a bare FIT/UNFIT without a why is
   rejected; e.g. "role requires 15+ years of UI experience, candidate has only
   4" for UNFIT, or "JD and candidate profile align on X" for FIT). Replaces
   substring-tolerant parsing for structured-capable backends.
   **Enforcement mechanism (fixed 2026-08-30 per skeptic gate — see Change
   Log): `reason` must be `str` with NO default and NOT `Optional`, so the
   provider's own strict-schema validation rejects a bare verdict at
   generation time — this is what makes "REQUIRED" real rather than aspirational
   prose. This is a validation-time gate only: it does not change
   `job.fit_reason` storage, which today discards the reason on a FIT verdict
   (`stages.py::_parse_fit_verdict` returns `(True, None)`;
   `stages.py`'s `job.fit_reason = None if is_fit else reason` nulls it again).
   That behavior is unchanged by this plan — decision #4 forces the model to
   justify itself, it does not require persisting a FIT reason anywhere.**
5. **Scope**: all five stages (fit_assessment, cv_adjust, cover_letter,
   revising_cv, revising_cl).
6. **Mixed-mode history** (amended per advisor): no translation upgrading to
   structured mode; mechanical sentinel-wrap applied when replaying canonical
   structured turns into a sentinel-mode session. Never persist provider wire
   format — DB stores canonical normalized JSON text only.

### Advisor amendments (locked)

- Backend parse layer is minimal: `json.loads` + `kind` routing only; all
  semantic/cross-field validation stays stage-side in `_validate_final_content`
  so `_self_heal_final` (2-correction budget) keeps working. Downgrade trigger
  (zen) narrowed to *unparseable JSON / missing·invalid `kind`* only. Semantic
  failures never downgrade.
- Structured-mode `ProtocolError` in `run_stage` spends the existing self-heal
  correction budget (wording: re-emit a valid schema-conforming object) before
  propagating to hard fail; sentinel-mode behavior byte-for-byte unchanged
  (backend-internal CLI nudges already cover it); fit stage stays one-shot to
  modal.
- Anthropic: forced tool-use (`tools=[tool]`, `tool_choice` forced, extract
  `tool_use.input`) as primary mechanism behind a thin dict-extraction adapter;
  `response_format` (SDK 0.104 surface unknown) is a non-blocking discovery
  follow-up inside the same adapter seam. Handle `stop_reason == "max_tokens"`
  under forced tool as a ProtocolError variant that routes into the new self-heal
  budget ("output truncated; re-emit more concisely").
- Prompt assembly lives in ONE composition root:
  `assemble_system_prompt(prompt_text, *, language, structured_model=None,
  fit_verdict=False)` next to the language directive. Structured sessions get an
  appended "structured-output contract" section (assembled at runtime, never
  written into user-edited prompt files) with precedence wording over the
  sentinel-format section + per-stage union schema embedded + "never embed
  sentinel markers in payload values" line + language directive variant ("keys
  stay English; sentinel instructions don't apply this session").
- Parity gate framed as an **equivalence invariant** (sentinel path is retained
  forever for CLIs — nothing gets deleted): sentinel-mode fake vs structured-mode
  fake must produce identical `Document.structured` + `Document.markdown`,
  identical `Job.fit_reason` + state transition, and identical `FollowUp.question`
  text for the same logical payload.
- Observability: `LogEvent` at session start naming active mode (`structured` /
  `sentinel` / `sentinel (downgraded)`).

### Named invariant

**The DB stores canonical form; provider wire format never round-trips.**
`Message` rows persist the normalized union-JSON plain text (never `tool_use`
blocks, never provider envelopes). On replay, history is reconstructed from
canonical text + **content-based format detection of each row** (fixed
2026-08-30, skeptic gate — see Phase 2: a `<<<`-prefixed row is sentinel form,
a `{`-prefixed row is canonical form; there is no persisted or reliably
reconstructible "session active mode" to key off, since Phase 4's downgrade
flag is explicitly never persisted). Sentinel-wrap only when replaying a
canonical row into a sentinel-mode destination backend; unwrap only when
replaying a sentinel row into a structured-mode destination. Anthropic replay
pairs nothing — plain-text assistant turns are valid input even when the
current turn is tool-forced.

---

## Phase 1 — Turn models + canonical normalization

**Current State**
`parse_reply(raw) -> AgentReply` is the only reply parser
(`jsa/agents/protocol.py`). Stage→schema mapping lives implicitly in
`_validate_final_content` (`CVDocument` for cv/revising_cv, `CoverLetter` for
cl/revising_cl, `None` for fit). No backend declares capability.

**Desired State**
`jsa/schema/turn_models.py` holds: flat union models
`FitVerdict{verdict: Literal["FIT","UNFIT"], reason: str}` (reason NON-nullable,
NO default — see decision #4's enforcement-mechanism note above; a nullable/
defaulted field is what caused the strict-schema failure below),
`CvTurn{kind: Literal["question","final"], question: str|None,
payload: CVDocument|None}`, `ClTurn{... payload: CoverLetter|None}` — the two
genuinely-optional fields (`question`, `payload`) stay `X|None` with no default
either (the iff-validator enforces presence, a default would just re-introduce
the missing-`required`-entry bug) — each model also carries
`model_config = ConfigDict(extra="forbid")` (required for
`additionalProperties: false`, see Problems/Bugs below) plus a
`model_validator(mode="after")` enforcing payload-iff-final and
question-iff-question; `STAGE_TURN_MODELS: dict[Stage, type[BaseModel]]`;
`json_schema_for(stage) -> dict` (via `model_json_schema()`, must emit
`additionalProperties: false` AND `required` containing every field —
`ConfigDict(extra="forbid")` handles the first, no-default fields handle the
second); `parse_structured_reply(raw) -> AgentReply` doing
`json.loads` + `kind` routing ONLY — `kind="question"` →
`AgentReply(kind="needs_input", question=…)`; `kind="final"` →
`AgentReply(kind="final", content=json.dumps(payload))` (payload re-serialized so
`_validate_final_content` receives the byte-identical shape as the sentinel path);
fit model replies map to `content=f"{verdict}\n{reason}"` so `_parse_fit_verdict`
is reused unchanged. Raises `ProtocolError("structured reply unparseable…")` on
JSON/missing-kind failure only. `AgentBackend` gains
`supports_structured_output: ClassVar[bool] = False`.

**Problems/Bugs**
None existing — new leaf module. Risks: Pydantic emitting top-level `anyOf`
(avoids via flat nullable fields), schema not strict-clean (assert
`additionalProperties: false` in tests), or re-serialization drift between
parse_structured_reply and the sentinel path's raw content string (round-trip
tests cover).

**CONFIRMED empirically 2026-08-30 (skeptic gate) — the naive version of this
module fails its own strict-clean tripwire, for a subtler reason than
"missing `additionalProperties`":** `FitVerdict.model_json_schema()` with
`reason: str | None = None` (the field shape originally drafted here) produces
NO `additionalProperties` key at all, AND `required` contains only `verdict`
— Pydantic drops any field with a default out of `required` entirely, which
is worse than nullable-but-required for strict providers. Both problems are
now fixed at the Desired-State level above (`ConfigDict(extra="forbid")` +
no defaults on any field, `Optional` types enforced by the iff-validator
instead). Verify in Phase 1's tests with a literal
`assert schema["additionalProperties"] is False` and
`assert set(schema["required"]) == set(schema["properties"])` for every
model in `STAGE_TURN_MODELS` plus `FitVerdict` — not just one spot-check.

**Solutions**
- New `jsa/schema/turn_models.py` (~120 lines), imports only
  `jsa/schema/cv.py`, `jsa/schema/cover_letter.py`, `jsa/db/models.Stage`,
  `jsa/agents/base.py`, `jsa/agents/protocol.py` — schema package remains a leaf;
  zero new edges into backends.
- `jsa/agents/base.py`: one-line ClassVar default.
- Tests: `tests/backend/test_turn_models.py` — round-trip through json_schema →
  validate ⇄ parse_structured_reply ⇄ `_parse_structured`; `additionalProperties:
  false` assertion; iff-rule validators; ProtocolError cases.
- Recommended model: Sonnet (small, isolated module). Tripwire: if Pydantic
  schema shape fights strict-provider requirements in tests → escalate to Opus.

## Phase 2 — Prompt assembly root + replay adapter

**Current State**
Language directive appended ad-hoc inside `run_stage`/`_run_fit_assessment`
(`_with_language_directive`, no-op for `"en"`); sentinel grammar is the only
output-format section. History replays verbatim (`_load_history` → backends).

**Desired State**
One helper `assemble_system_prompt(prompt_text, *, language,
structured_model=None, fit_verdict=False) -> str` owns ALL runtime prompt
mutation: language directive (two variants — sentinel mode: keys + sentinels stay
ASCII; structured mode: keys stay English and the sentinel section is superseded)
and the structured-output contract section (defines `kind` semantics, embeds the
per-stage union JSON schema, explicit precedence over the file's sentinel-format
section, "never embed sentinel markers inside payload string values"). New
`wrap_canonical_for_sentinel(canonical_text) -> str` (~10 lines) converting a
canonical union-JSON turn into sentinel-wrapped text (`kind=final` → wrap payload
in `<<<FINAL>>>...<<<END>>>`; `kind=question` → `<<<NEED_INPUT>>>` wrap) for
replay into sentinel-mode sessions.

**Replay-mode detection FIXED 2026-08-30 (skeptic gate) — content-based, not
session-flag-based:** there is no persisted `mode` column anywhere (Phase 4's
`structured_enabled` flag is explicitly per-session, never persisted — see
Locked decision #2), so a "session ACTIVE MODE" is not actually a thing that
exists at replay time for a freshly-reconstructed job (resume after restart,
BF-19 backend switch, revision resume all rebuild a session from scratch).
Every `Message.content` row already unambiguously self-identifies its own
origin format today — a sentinel row starts with `<<<NEED_INPUT>>>` or
`<<<FINAL>>>` (it is `reply.raw`, per `stages.py`'s existing `Message`
writes); a canonical structured row is bare JSON (starts with `{`). Replace
the session-mode-keyed design with a pure content-sniffing pair:
`_is_sentinel_wrapped(text) -> bool` (checks the `<<<` prefix) plus
`wrap_canonical_for_sentinel`/`unwrap_sentinel_to_canonical` (the new inverse
direction) applied per-row, per-destination, at EVERY `restore_session` call
site — enumerated explicitly so none is silently skipped:
1. fresh/resume `cv_adjust` and `cover_letter` (`stages.py`'s existing
   resume branches),
2. `revising_cv`/`revising_cl` resume (loads the ORIGINAL stage's Messages,
   which `backend_switch_reset` does NOT delete on a revision-stage BF-19
   switch — only the failed revision stage's own Messages are deleted, per
   `jsa/db/repo.py::backend_switch_reset`'s docstring),
3. any BF-19 rewind resume in general (`cv_adjust`/`cover_letter` failures
   rewind to `pending`/`cv_done`, which are *fresh*-session states, so no
   stale-mode replay risk there — but the revision case in (2) is a real,
   reachable gap without this fix: an anthropic structured-mode `cv_adjust`
   session's canonical-JSON Messages, replayed unmodified into a
   `claude-cli` sentinel-only backend after a revision-stage BF-19 switch,
   is exactly the failure this phase exists to prevent).
Legacy/pre-feature rows (written before this feature ships) are indistinguishable
from live sentinel rows under this scheme by construction — they already start
with `<<<`, so they get wrapped/passed-through correctly with zero special-casing.

**Problems/Bugs**
Downgraded zen sessions leave the structured contract in the prompt while
flipping to sentinel mode (accepted — the sentinel nudge wording re-asserts the
format; restart re-assembles the prompt per current mode). Without the sentinel
wrap at replay, BF-19 switching anthropic→claude-cli mid-conversation hands the
CLI grammar-violating assistant turns of bare JSON.

**Solutions**
- New `jsa/pipeline/prompt_assembly.py` (~80 lines) — imports only
  `jsa/store/preferences` + `jsa/schema/turn_models`; `stages.py` switches its
  two `_with_language_directive` call sites to `assemble_system_prompt`.
- Sentinel-wrap/unwrap helper pair colocated in `jsa/schema/turn_models.py`
  (see content-based detection above — replaces the single one-directional
  helper originally scoped here).
- Tests: assembly composition for both modes × languages; wrap/unwrap
  round-trip through `parse_reply`; replay adapter exercised at each of the
  three enumerated call sites above, including the anthropic(structured)→
  claude-cli(sentinel) revision-resume case specifically (not just zen's
  internal downgrade, which the original test list under-covered).
- **PARITY GATE (added 2026-08-30, skeptic gate — required by the project's
  own CLAUDE.md "Parity gate for replace / delete refactors" rule since this
  phase replaces `_with_language_directive` at both its call sites):** before
  swapping either call site, add a characterization test that captures
  `_with_language_directive`'s CURRENT output verbatim — for every language in
  the catalog, for a fixed representative `prompt_text` — as a golden fixture.
  Then assert `assemble_system_prompt(prompt_text, language=lang,
  structured_model=None)` (the sentinel-mode / CLI-backend path) is
  byte-identical to that golden fixture for every language, BEFORE the two
  call sites are switched over. This is the only thing standing between "CLI
  sentinel-mode behavior is byte-identical to today" (this plan's own "Done"
  criterion) and a silent prompt-wording drift for claude-cli/google-cli that
  no other planned test would catch.
- Recommended model: Sonnet.

## Phase 3 — Anthropic backend structured mode

**Current State**
`AnthropicAPIBackend` sends `messages.create(model, max_tokens=8192, system,
messages)` and parses with raw `parse_reply` — the ONLY backend with no nudge; any
sentinel-less reply = ProtocolError → hard fail.

**Desired State**
`supports_structured_output = True`. When a `structured_schema: dict` kwarg is
passed, forced tool call via a thin adapter: `tools=[{"name":"respond",
"input_schema": schema}]` + `tool_choice={"type":"tool","name":"respond"}`;
extract dict from `tool_use.input` (adapter shape keeps a future `response_format`
swap contained inside one function); normalize via `parse_structured_reply`. A
`stop_reason == "max_tokens"` tool reply raises
`ProtocolError("structured reply truncated")` — routed into the new self-heal
budget from Phase 5 ("re-emit more concisely"). When `structured_schema is None`:
exactly today's sentinel behavior (unchanged; no per-session downgrade for
anthropic — structured is reliable there).

**Problems/Bugs**
SDK 0.104.1 surface for `response_format` unknown → discovery item, non-blocking.
Tool-forced truncation (8192 tokens mid-tool-input) must not be a silent
ProtocolError with no corrective signal.

**Solutions**
- `jsa/agents/anthropic_api.py`: `start_session`/`send_message`/`restore_session`
  gain `structured_schema: dict | None = None`; internals branch to the adapter.
  Rate-limit/timeout exception mapping untouched.
- Tests with stub Anthropic SDK responses (via the `tests/backend/fakes` pattern —
  do NOT mock internals): forced-tool happy path, truncation → ProtocolError
  variant, None-schema → sentinel parity.
- Recommended model: Opus. Tripwire: SDK surface mismatch → decide tool-only vs
  response_format; return to advisor if forced-tool extraction is awkward.

## Phase 4 — OpenCode Zen structured mode + per-session downgrade

**Current State**
Payload `{"model","messages","max_tokens":8192}`; replay-style sentinel nudge
(`_parse_with_nudge`); 3-way error classification already drives BF-19
(`AgentLimitReached` / `_TransientOpenCodeError` retry×3 → `AgentBackendUnavailable`
/ immediate `AgentBackendUnavailable`); free models frequently ignore format
instructions.

**Desired State**
`supports_structured_output = True`. Session handle gains
`structured_enabled: bool = True` (per-session, NEVER persisted — a restart
re-attempts structured). When enabled, POST gains
`response_format={"type":"json_schema","json_schema":{"name":<stage>,
"strict":True,"schema":<STAGE_TURN_MODELS schema>}}`. Replies normalize via
`parse_structured_reply`; on `ProtocolError` from THAT parse → set
`handle.structured_enabled=False` and behave exactly as today's sentinel path:
run the existing `_parse_with_nudge` on the same raw text. Downgrade trigger is
unparseable/missing-kind only — semantic failures never downgrade. Nudge text
gets a mode-conditional variant re-asserting sentinel grammar ("disregard the
earlier output-format contract; wrap your reply in
`<<<NEED_INPUT>>>`/`<<<FINAL>>>`..."). Subsequent calls omit `response_format`,
and restart re-assembles the prompt per current mode so the contradiction
self-heals.

**Problems/Bugs**
Proxied free models may treat `response_format` as advisory → mid-session
downgrade contradictions (accepted). Must not interfere with existing
transient-retry / `_MAX_ATTEMPTS=3` classification — the downgrade must not
consume retry budget and vice versa. Strict-schema rejection at the wire level is
possible per proxied model.

**Solutions**
- `jsa/agents/opencode_zen.py`: same `structured_schema` kwarg;
  `OpenCodeZenSessionHandle.structured_enabled`;
  `_parse_structured_with_downgrade` wrapper replacing `_parse_with_nudge` at both
  call sites.
- Tests: downgrade on malformed JSON (sentinel nudge engages, flag sticks,
  subsequent POSTs omit `response_format`); no-downgrade on semantic violation;
  retry-loop independence; mode LogEvents.
- Recommended model: Opus. Tripwire: zen proxy rejecting strict schemas at wire
  level → relax `strict`/name, re-verify with a marked integration test.

## Phase 5 — Pipeline wiring, mode-aware self-heal, structured ProtocolError budget

**Current State**
`run_stage`/`_run_fit_assessment` dispatch on `reply.kind`; `ProtocolError` is
hard-fail outside fit; self-heal corrections mention sentinels explicitly;
backend capability never consulted; backend methods accept no schema kwarg; the
fit-capability trap (fit stage may run on a DIFFERENT backend instance, e.g.
fit_model="google-cli" while the pipeline runs anthropic) is unhandled.

**Desired State**
- Stage→schema map (`STAGE_TURN_MODELS`; fit → `FitVerdict`) consulted per stage;
  when `backend.supports_structured_output` and the running instance's session
  mode is structured, pass `structured_schema=json_schema_for(stage)` into
  start_session / send_message / restore_session. Fit uses the actual fit-backend
  instance (a `google-cli` fit override keeps sentinel mode).
- `assemble_system_prompt` replaces `_with_language_directive` at both call
  sites, arming `structured_model` from the stage schema.
- Self-heal corrections become mode-aware (structured variant: "re-emit a
  corrected object conforming to the schema — a `kind` field plus `question` or
  `payload`"; no sentinel mention).
- Structured-mode `ProtocolError` from the backend in `run_stage` (NOT fit) is
  routed through the SAME `MAX_FINAL_CORRECTIONS=2` budget (as a form of
  self-heal) before propagating. Sentinel-mode semantics unchanged
  (backend-internal CLI nudges remain the only response). Fit stays
  one-shot → modal.
- Mode observability: LogEvent on session start naming active mode.
- `tests/backend/fakes/fake_backend.py` grows a structured-mode simulation knob:
  accepts the schema kwarg, can serve canned replies in both modes, used by the
  parity gate.

**Problems/Bugs**
Fit-capability trap (see above), self-heal double-application between wire-level
vs semantic violations, and keeping sentinel CLI behavior byte-identical — all
addressed explicitly.

**Carry-forwards from Phase 3's advisor verification (2026-08-30, locked before
Phase 5 writes its first line):**
1. **Fresh-session ProtocolError has no handle.** `start_session` constructs the
   handle only AFTER a successful parse, so a structured-mode ProtocolError from
   the fresh paths (cv_adjust start, fit start) leaves nothing to `send_message`
   a correction to — the naive `try/ProtocolError → budget` wrap falls through to
   the orchestrator's generic except and hard-fails on the first backend (the
   exact failure BF-19 exists to prevent). Budget attempts for fresh-session
   failures must re-issue the WHOLE `start_session` (idempotent, same args);
   budget attempts for `send_message` failures (resume/revision, handle exists)
   are corrective re-sends. Two different recovery shapes — design both, and add
   an end-to-end test for the fresh case, not just the send case.
2. **One boolean must drive both the kwarg and the adapter.** At every
   `restore_session` call site, the `structured_schema=` kwarg and
   `adapt_history(structured=...)` must be computed from ONE destination-mode
   decision — passing one without the other replays cross-format rows unadapted
   and silently breaks the named invariant (highest risk on the BF-19
   CLI→anthropic switch path). Pin with a test: sentinel rows restored into a
   structured anthropic session arrive unwrapped.
3. **Correction texts are sentinel-worded.** `_CV_CORRECTION`/`_CL_CORRECTION`/
   `_CV_SUMMARY_NUDGE` (stages.py:250–269) all say "inside one
   `<<<FINAL>>>…<<<END>>>` block" — in a structured session this contradicts the
   prompt's structured contract and invites literal sentinel markers inside JSON
   string values. Structured sessions need mode-conditional variants ("re-emit a
   corrected object conforming to the schema"). `_reprompt` itself needs no
   change — it already relies on the handle-carried schema.

Also noted there (advisor, Phase 5 awareness): `_run_fit_assessment`'s existing
`ProtocolError → unfit-modal` catch now also absorbs truncation/refusal-shaped
structured errors — fail-closed to the modal is correct per the locked doctrine,
and Phase 5 must NOT widen that except toward `AgentTimeout`/`AgentLimitReached`
(CLAUDE.md documents that exact historical bug and its two regression tests).

**Solutions**
- Edits localized to `jsa/pipeline/stages.py` (plus new imports),
  `tests/backend/fakes/fake_backend.py`, and accept-and-ignore kwarg signatures on
  base + CLI backends.
- Tests: run_stage structured end-to-end with fake; BF-19 cross-mode switch
  (structured start → zen downgrade → claude-cli fallback replay uses
  sentinel-wrap; job completes); fit-on-google-cli isolation; ProtocolError
  budget consumption then `mark_failed` unchanged.
- Recommended model: Opus.

## Phase 6 — Parity gate (equivalence invariant) + integration

**Current State**
No cross-mode equivalence asserted anywhere.

**Desired State**
Characterization module `tests/backend/test_mode_parity.py`: the same canned
logical payload through (a) sentinel-mode fake reply `<<<FINAL>>>{json}<<<END>>>`
vs (b) structured-mode fake reply `{kind:"final", payload:...}` → assert identical
`Document.structured`, `Document.markdown`, identical fit verdict (reason+state),
and identical `FollowUp.question` text. Module docstring + commit message frame
it as a PERMANENT equivalence invariant (not a deletion gate — the sentinel path
lives forever for CLIs). Marked integration tests for real anthropic + zen
(opt-in `pytest -m integration`).

**Phase 3 advisor addendum (2026-08-30): the anthropic integration tests are
non-negotiable before structured mode is trusted as a default.** Every Phase-3
test stubs the SDK boundary, so nothing has yet verified that the REAL API (a)
accepts pydantic's `model_json_schema()` output as a tool `input_schema` — it
carries `"title"`, nested `$defs`, and `anyOf: [string, null]` nullables (a 400
here means the whole mechanism is wrong), (b) that forced `tool_choice` actually
yields `stop_reason="tool_use"` with these schemas, and (c) that the fit schema
behaves. All three must be asserted by the marked integration tests in this
phase.

**Problems/Bugs**
None new.

**Solutions**
- Tests + integration markers only; run `pytest -v -m "not integration"`;
  frontend unaffected.
- Recommended model: Sonnet.

## Phase 7 — Docs

**Current State**
CLAUDE.md's Sentinel-protocol section presents sentinels as THE protocol; ARCH.md
architecture narrative likewise.

**Desired State**
CLAUDE.md gains a "Structured output (API backends)" section — capability flag,
session mode, canonical-form invariant, downgrade semantics, replay-wrap rule,
amended BF-19 note (structured-mode ProtocolError spends the self-heal budget).
The sentinel section is reframed as "mandatory for CLI backends and as the
downgrade target". ARCH.md's pipeline section updated. **Prompt files are NOT
edited** — the runtime contract section covers structured sessions (editing rule
honored).

**Problems/Bugs**
None new.

**Solutions**
Minimal diffs following existing doc style. Recommended model: Sonnet.

---

## Decisions Log

(Reserved for the user — agents do not write here.)

## Change Log

**2026-08-30**: context — ran the skeptic gate (`skeptic` subagent, Sonnet)
against this plan before any phase implementation started, per a pre-written
dossier covering claims C1–C14 and pre-synthesis assertions A1–A10. actions —
verified two findings directly against live code/Pydantic before accepting
them (FitVerdict schema emission, `_parse_fit_verdict`'s FIT-reason discard),
trusted two more on the skeptic's specific file:line citations (BF-19 replay
gap, missing prompt-wording characterization test); folded all four into the
plan: (1) decision #4 now specifies its enforcement mechanism (non-nullable
`reason`, no default) and clarifies it doesn't change `job.fit_reason`
storage; (2) Phase 1's turn models now require `ConfigDict(extra="forbid")`
+ no-default fields on every model, with an explicit strict-schema assertion
in tests; (3) Phase 2's replay design switched from session-mode-keyed to
content-based (`<<<` vs `{` prefix) wrap/unwrap, with every `restore_session`
call site enumerated, especially the revision-resume path that
`backend_switch_reset` leaves exposed to a cross-mode BF-19 switch; (4) Phase
2 gained a characterization/golden test pinning `_with_language_directive`'s
current output before it's replaced by `assemble_system_prompt`, per this
project's own CLAUDE.md parity-gate rule. decisions — no skeptic findings
were rejected; all four held up under direct verification or citation-level
trust. Six other claims (C1–C5, C7, C9) were confirmed to already hold as
described and needed no plan changes. Two items remain genuinely open and
unresolved by this pass: Anthropic SDK 0.104.1's actual `response_format`/
forced-tool-use surface (Phase 3 still carries this as a discovery item), and
OpenCode Zen's wire-level acceptance of `response_format` for free models
(deferred to Phase 6's integration tests, as originally planned).
verification — unverified; no phase has been implemented yet, this is a
plan-only revision. Full skeptic report and dossier:
`/Users/wiam/VSCodeProjects/JSA/skeptoc-dossier-on-plan-structured-output-copy.md`.

**2026-08-30**: context — implemented Phase 1 (turn models + canonical
normalization) and Phase 2 (prompt-assembly composition root + replay
adapter) per this plan. actions — added `jsa/schema/turn_models.py`
(`FitVerdict`/`CvTurn`/`ClTurn`, `STAGE_TURN_MODELS`, `json_schema_for`,
`parse_structured_reply`, plus the mixed-mode replay adapter
`wrap_canonical_for_sentinel`/`unwrap_sentinel_to_canonical`/
`adapt_history`); added `supports_structured_output: ClassVar[bool] = False`
to `AgentBackend` (`jsa/agents/base.py`); added
`jsa/pipeline/prompt_assembly.py::assemble_system_prompt` as the single
composition root, and switched `stages.py`'s two `_with_language_directive`
call sites to it, deleting the old functions; wired `adapt_history` (hardcoded
`structured=False`) into all three `restore_session` call sites in
`stages.py` (fresh/resume cv_adjust & cover_letter; revising_cv/revising_cl
fresh-revision; revising_cv/revising_cl mid-revision resume). Captured
`tests/backend/fixtures/language_directive_golden.json` (84 cases: 19
languages + one unknown code, fit_verdict × 2, with/without a trailing
newline on `prompt_text`) from the ORIGINAL `_with_language_directive` before
deleting it, per CLAUDE.md's parity-gate rule — `test_prompt_assembly.py`'s
`TestSentinelModeParityGate` asserts byte-for-byte equality against it.
New test files: `test_turn_models.py` (33 tests), `test_prompt_assembly.py`
(90 tests, mostly the golden-fixture parametrization), `test_replay_adapter.py`
(23 tests: primitives + all three restore_session call sites, including the
plan's specifically-flagged revising_cv mid-revision-resume gap). decisions —
four deliberate deviations from the plan's literal sketch, all surfaced by an
advisor consult mid-session: (1) `parse_structured_reply` takes `(raw, stage)`,
not the plan's one-arg sketch — fit-verdict routing needs the stage to know
which shape to expect; (2) `adapt_history(history, *, structured: bool)` takes
an explicit destination-mode flag rather than consulting
`backend.supports_structured_output` — capability ≠ active session mode (the
OpenCode Zen per-session downgrade means a structured-capable backend can be
mid-downgrade), so Phase 2 hardcodes `structured=False` at all three call
sites, making Phase 5's eventual wiring a literal-swap; (3) added
`unwrap_sentinel_to_canonical` as the new inverse direction (the plan's Phase
2 text only named the one-way `wrap_canonical_for_sentinel`) — needed because
the replay-mode-detection fix (2026-08-30, skeptic gate) requires adapting in
BOTH directions depending on destination mode, not just canonical→sentinel;
it is fail-soft by construction (never raises — a parse failure passes the
row through unchanged), since a fit_assessment-shaped or otherwise-foreign
FINAL body is legacy content this adapter doesn't own; (4) confirmed and left
as-is (not a deviation, a deliberate limitation carried into code comments):
`turn_models.py`'s nested `$defs` (`CVDocument`/`Contact`/`Section`/`Entry`/
`CoverLetter`) are NOT recursively strict (those models are `extra="ignore"`
with defaulted optional fields) — acceptable for Anthropic's `input_schema`
(Phase 3), a live open risk for an OpenCode-Zen-style `strict: true` mode
(Phase 4/6, already flagged in this plan's own Change Log as deferred to
integration tests). Also note for Phase 6's parity gate:
`wrap_canonical_for_sentinel` emits compact (non-indented) JSON inside the
`<<<FINAL>>>` block, unlike a real model's usually pretty-printed sentinel
output — semantically identical after `_strip_code_fence` + `json.loads`, but
the two paths' FINAL-block whitespace differs by construction, not by bug.
verification — verified: `pytest -q -m "not integration"` → 1259 passed, 2
skipped, 2 deselected, 0 failed (full suite, not just the new files); the
pre-existing `tests/backend/test_language_directive.py` (end-to-end
`run_stage` coverage of the language directive) passed unchanged, serving as
a second, independent regression signal beyond the new golden-fixture test.

**2026-08-30**: context — implemented Phase 3 (Anthropic backend structured
mode via forced tool-use), continuing the prior session that had consulted the
advisor and applied the schema-keyed-parser half of `turn_models.py` before
hitting its session limit (intake + locked guidance preserved in
`phase 3 antropic backend.txt`). actions — completed
`parse_structured_reply_for_schema` in `jsa/schema/turn_models.py` (schema-keyed
sibling sharing the private `_route_structured_data` core with the stage-keyed
`parse_structured_reply`; `is_fit` derived from the fixed schema's own
properties — `"kind" not in schema["properties"]` — never from the reply
payload); rewrote `jsa/agents/anthropic_api.py`: `supports_structured_output =
True`, forced tool-use behind a two-function adapter (`_forced_tool_kwargs` +
`_extract_structured_text`: `tools=[{"name":"respond","input_schema":schema}]`
+ `tool_choice` forced; `stop_reason == "max_tokens"` checked BEFORE extraction
→ `ProtocolError("structured reply truncated")`; content blocks scanned for
the tool_use block, never `content[0]`; canonical JSON via
`json.dumps(input, ensure_ascii=False)`); `AnthropicSessionHandle` carries
`structured_schema` (mode established at start/restore; `send_message` defaults
to the handle's schema, an explicit non-None kwarg overrides for that call);
all three session methods gain the additive `structured_schema=None` kwarg;
sentinel path byte-identical to pre-phase (no tools kwargs, `content[0].text`,
pinned by a wire-level parity test). Tests: `TestSchemaKeyedParityWithStageKeyed`
(35: equivalence over all 5 stages, error parity, routing-derivation
independence) + 10 new backend classes (29: request shape, final/question
routing, canonical-raw invariant, scan-not-index, truncation incl.
before-extraction ordering + handle coherence on error, no-tool-block,
parse-level tool-input errors, fit-verdict routing via the schema, handle-
carried + override semantics, exception mapping in structured mode, capability
flag). decisions — advisor verification (done-gate) returned GO with zero
deviations from the prior session's locked guidance; two LOW findings applied
immediately (the explicit-override test; `ensure_ascii=False` so DB Message
rows — which persist `reply.raw` — stay readable UTF-8; every consumer
json.loads's it, so semantically irrelevant); any-tool_use-block matching (not
`name == "respond"`) kept per advisor — with one tool offered and tool_choice
forced, any tool_use IS respond, and name-matching adds rename fragility;
truncation/no-tool-block ProtocolErrors propagate raw from the backend
(recovery policy stays in `run_stage`, matching this backend's existing
no-nudge philosophy). Three carry-forwards locked into Phase 5's
Problems/Bugs above (fresh-session ProtocolError has no handle → budget must
re-issue the whole start_session; one destination-mode boolean must drive both
the schema kwarg and `adapt_history`; sentinel-worded correction texts need
structured variants), and Phase 6 gained the non-negotiable real-API assertions
(schema acceptance, forced-tool completion shape, fit schema). One pre-existing
out-of-scope gap flagged as a follow-up (persisted to agent memory): anthropic
maps only `RateLimitError` into the BF-19 family — `InternalServerError`/
`APIConnectionError` propagate raw and hard-fail on the first backend. Also
re-confirmed against the live SDK 0.104.1 during implementation: the native
`output_config` structured-outputs mechanism exists but mandates recursive
`additionalProperties: false` (unsatisfiable by the nested `$defs`), validating
the locked forced-tool-use choice; `ToolParam` requires only `name` +
`input_schema`. verification — verified: `pytest -q -m "not integration"` →
1323 passed, 2 skipped, 2 deselected, 0 failed (full suite; baseline after
Phase 1-2 was 1259 — +64 = 35 parity + 29 backend tests; ruff is configured in
pyproject but not installed in either environment, so `py_compile` + the suite
stand in for it). Real-API integration coverage remains deferred to Phase 6
per plan (see the Phase 6 addendum).

**2026-08-30**: context — verified Phase 3 landed clean (the system `python3`
gave 379 false failures for lacking `pytest-asyncio`; the repo's `.venv` is the
correct environment and reproduced Phase 3's own recorded 1323-passed
baseline exactly; code re-read against this plan's Desired State found no
discrepancies), then implemented Phase 4 (OpenCode Zen structured mode +
per-session downgrade). actions — per an advisor consult, implemented inline
(this file carries CLAUDE.md's most invariant-dense classification logic; a
cold subagent risked re-breaking it) with the retry/downgrade boundary decided
up front: `_parse_structured_with_downgrade` sits strictly outside `_call_api`'s
`_MAX_ATTEMPTS` retry loop, so a malformed structured reply downgrades after
exactly one API call and a transient/quota/timeout failure never touches the
downgrade flag. `OpenCodeZenSessionHandle` gains `structured_schema` +
`structured_enabled` (established at start/restore from whether a schema was
actually supplied, never unconditional `True`); `_active_schema` collapses to
`None` once downgraded regardless of what the caller keeps passing.
`parse_structured_reply_for_schema` failures (unparseable JSON / missing-
invalid `kind` only — semantic payload validation stays out of scope, per the
plan) fall through to the existing `_parse_with_nudge` with a new mode-
conditional nudge (`_DOWNGRADE_NUDGE_TEXT`). Tests: 22 new in
`test_opencode_zen.py` (74 total), including the two crossed-case tests
advisor asked for by name (transient-then-success leaves `structured_enabled`
True; a retry keeps sending `response_format`), a mid-conversation downgrade
test (advisor: the constructor-only test missed the `send_message` mutation
path — the more likely real-world shape), and a pinned fit-verdict-downgrade
test. decisions — two left open by the plan's prose, decided and documented
in-code: (1) `strict: false` on `response_format` (the turn models' nested
`$defs` aren't recursively strict; the downgrade path already covers a model
that ignores the schema, so `false` costs nothing); (2) a 4xx
`response_format` rejection is classified identically to any other 4xx
(`AgentBackendUnavailable`, unretried) rather than downgrading — indistinguish-
able from a bad model/config at the error-body level, and the existing 3-way
classification already refuses to guess at upstream error-type strings;
flagged in-code for Phase 6's integration tests. Mode `LogEvent` is explicitly
NOT implemented here (backends have no DB session; it belongs in `stages.py`,
Phase 5's territory — Phase 4 only produces the flag Phase 5 will read).
Two more advisor findings resolved: an unterminated-sentinel edge case inside
the downgrade nudge (documented in-code, left as-is — it still propagates to
Phase 5's self-heal budget correctly, just doesn't also downgrade) and the
fit-verdict stage's nudge cost (this backend is stage-agnostic and has no
one-shot-fit awareness, so a malformed fit reply nudges like any other stage —
pinned by a test, flagged for Phase 5 to decide whether that needs upstream
handling). verification — verified: `pytest -q -m "not integration"` (via
`.venv/bin/python`, the repo's correct venv — `venv` also works) → 1345
passed, 2 skipped, 2 deselected, 0 failed (baseline 1323 + 22 new, no
regressions); `py_compile` clean on both changed files (ruff still not
installed in either venv, per Phase 3's own note).

**2026-08-30**: context — verified Phase 4 landed clean (re-ran the suite against
the repo's `.venv`, matched its recorded 1345 baseline), then implemented Phase 5
(pipeline wiring, mode-aware self-heal, structured ProtocolError budget) per two
advisor consults up front — one on the overall shape, one specifically to check
whether "always pass `structured_schema` explicitly at every call site" (my first
reading of the plan's own Solutions bullet) was safe, which surfaced that ~20
test-local `FakeAgentBackend` subclasses across BF-19/dismiss-race/orchestrator
tests have nothing to do with structured output and would all need a signature
change to tolerate an unconditionally-passed kwarg. actions — computed one
`schema = _structured_schema_for(general_purpose_backend, stage)` /
`structured = schema is not None` pair once per `run_stage` invocation (fit
computes its own inside `_run_fit_assessment`, from the resolved `fit_backend`
instance — the "fit-capability trap" the plan's Problems/Bugs section named) and
threaded it through every affected call site: `assemble_system_prompt`'s
`structured_model` kwarg on both fresh-session branches; `adapt_history`'s
`structured` flag at all three `restore_session` call sites (previously
hardcoded `False`); a **conditional** `{"structured_schema": schema} if schema is
not None else {}` kwargs dict at every `start_session`/`restore_session` call
(never at `send_message` — both structured backends resolve it from the handle
when omitted); `_validate_final_content`'s new `structured: bool` param, threaded
into `_parse_structured`'s trailing re-emit sentence only (JSON-decode and
schema-violation branches each got a mode-conditional last sentence, sentinel
branch byte-identical — pinned by `TestParseStructuredModeAwareWording`'s two
byte-identity tests); `_self_heal_final`'s three correction texts got structured-
mode siblings (`_CV_CORRECTION_STRUCTURED`/`_CL_CORRECTION_STRUCTURED`/
`_CV_SUMMARY_NUDGE_STRUCTURED`, no `<<<...>>>` mentions). Added two new recovery
helpers for the "two different recovery shapes" the Phase 3 carry-forwards
flagged: `_start_session_with_retry` (fresh-session shape — no handle exists yet,
so a failed attempt is retried with the IDENTICAL `start_session` args, literally,
up to `MAX_FINAL_CORRECTIONS` times — never amended, since the caller persists
this exact `initial_user_msg` into `accumulated_messages` on success) and
`_send_message_with_wire_retry` (resume/revision shape — a handle exists, so the
retry is a real corrective follow-up turn, `_STRUCTURED_WIRE_CORRECTION`,
embedding the original text; returns exactly one `{user, assistant}` pair for
whichever attempt succeeded, since neither backend persists a failed attempt to
its own `handle.messages` either). Both budgets are a no-op (immediate re-raise,
zero retries) whenever `schema is None` — sentinel-mode behavior is untouched.
Added `_log_session_mode` (LogEvent naming `structured` / `sentinel` / `sentinel
(downgraded)`, read from the handle's `structured_enabled` with a `True` default
so Anthropic/CLI handles — which have no such attribute — never mislabel as
downgraded), called once per stage invocation right after the handle exists (not
from `schema` alone, since OpenCode Zen's downgrade isn't knowable until the
parse runs). Added a base.py comment documenting the
`supports_structured_output=True` ⇒ must-accept-`structured_schema` contract in
place of a Solutions-bullet literal reading. New test file
`tests/backend/test_structured_pipeline_phase5.py` (27 tests): helper unit tests,
the two retry helpers' three cases each, and integration tests through
`run_stage` — fresh cv_adjust and cover_letter on a structured fake (schema
received, contract present in the system prompt, sentinel-mode fake has no
contract, Document written correctly for both `CvTurn` and `ClTurn`), the
fit-capability-trap test (fit backend's OWN capability decides its mode,
independent of a structured general-purpose backend, both directions), the
newly-unlocked sentinel→structured replay direction at both the `cv_adjust`
resume and the `revising_cv` mid-revision-resume call sites (previously
impossible when `adapt_history`'s flag was hardcoded `False` — this complements,
doesn't replace, Phase 2's existing structured→sentinel coverage in
`test_replay_adapter.py`), fresh-revision structured wiring, the fresh-session
retry-then-succeed and budget-exhausted paths end-to-end (job never checkpoints
past an exhausted budget), and the structured-worded self-heal correction text
actually reaching the model (asserted no `<<<` substring). decisions — three
deviations from the plan's literal Phase 5 Solutions bullet, all advisor-driven
and documented in the code: (1) `structured_schema` is passed CONDITIONALLY
(omitted entirely when `None`), not unconditionally at every call site — the
"always pass explicitly" reading would have forced signature changes onto ~20
unrelated test doubles for a legibility gain that a `grep` for the kwarg already
provides just as well; (2) `base.py`'s abstract methods, `claude_cli.py`, and
`google_cli.py` were deliberately NOT given an accept-and-ignore
`structured_schema` parameter — under the conditional-kwarg design no caller
ever supplies it to a backend with `supports_structured_output=False`, so the
parameter would be genuinely dead code the project's own CLAUDE.md tells us not
to add; the real contract is now a code comment on `supports_structured_output`
itself, covering the two backends that actually need it (anthropic, opencode-zen)
plus `fake_backend.py`, which — being the one shared test double structured
tests actually flip to `True` — DOES accept-and-record the kwarg on all three
methods; (3) the wire-level correction resend (`_send_message_with_wire_retry`)
discards the failed attempt from `accumulated_messages` entirely rather than
logging it alongside the correction — matches both backends' own "mutate
handle.messages only on success" contract, so the persisted history never
carries a user turn with no assistant reply after it. A first draft of the
fresh-cv_adjust test asserted `"Structured output contract" in
backend.captured_initial_msg or True` — an advisor pass on the finished phase
caught that the trailing `or True` made the assertion unconditionally pass AND
that the contract lives in the system prompt, not the captured user message;
fixed by adding a `_SystemPromptCapturingBackend` (mirroring
`test_language_directive.py`'s pattern) and a paired negative test (sentinel-mode
fresh session has no contract section). Two more coverage gaps the same advisor
pass named were filled before considering Phase 5 done: zero structured coverage
existed for the revision call sites (the two `restore_session` sites plus two
`_send_message_with_wire_retry` calls got the most edits in this phase) and for
`cover_letter`/`ClTurn` specifically — both now covered by dedicated tests.
verification — verified: `.venv/bin/python -m pytest -q -m "not integration"` →
1372 passed, 2 skipped, 2 deselected, 0 failed (baseline 1345 + 27 new, zero
regressions; one `test_dev_tunnel.py` threading test failed on one run and
passed in isolation and on a full re-run — confirmed pre-existing flakiness
unrelated to any file this phase touched, not a regression).

**2026-08-30**: context — implemented Phase 6 (parity gate + integration) per this
plan, immediately after Phase 5 in the same session. actions — added
`tests/backend/test_mode_parity.py` (5 tests, always run in the default suite):
for the same logical payload, a sentinel-mode `FakeAgentBackend` vs a
`FakeAgentBackend(supports_structured_output=True)` produce identical
`Document.structured` + `Document.markdown` for both `cv_adjust` (CvTurn) and
`cover_letter` (ClTurn), identical `FollowUp.question` text for a NEED_INPUT/
question reply, and identical `Job.fit_reason` + state for both the FIT
direction (`fit_reason` discarded to `None` on both paths, per
`_run_fit_assessment`'s existing design) and the UNFIT direction (the reason
text itself, not just the state, must match — this is the direction that
actually persists). Each pair runs against two independent in-memory SQLite
sessions (`session_pair` fixture) so the two runs can't interfere. Deliberately
asserts nothing about raw `Message` content, per the plan's own note from
Phase 1-2's Change Log: `wrap_canonical_for_sentinel` emits compact JSON where a
real sentinel-mode FINAL is usually pretty-printed, so the two paths' raw text
differs by construction even for the same logical payload — only the four
observable outputs the plan names are in scope. Added
`tests/backend/integration/test_structured_output_live.py` (4 tests, marked
`@pytest.mark.integration`, skipped by default): three against the real
Anthropic API — `model_json_schema()` output (title, nested `$defs`, `anyOf:
[X, null]` nullables) accepted as a forced tool's `input_schema` for
`cv_adjust`/`cover_letter`/`fit_assessment`, each asserting a successful
`AgentReply` (which is itself proof `stop_reason == "tool_use"`, since
`_extract_structured_text` raises `ProtocolError` on anything else) — and one
against the real OpenCode Zen API, observing (not assuming) whether the
free-tier model honors `response_format` or the per-session downgrade engages,
per the open question flagged in `opencode_zen.py`'s `_call_api_once`
docstring; either outcome is a pass, matching the downgrade path's own design
intent. decisions — one test bug an advisor pass caught before considering the
phase done: `assert a.fit_reason == b.fit_reason is None` is a Python chained
comparison (`(a == b) and (b is None)`) — it happened to assert the right thing
here but doesn't read as asserting `a is None`; split into two explicit
assertions. verification — mixed, and the mixed result is itself a finding,
not a footnote:
- `.venv/bin/python -m pytest -q -m "not integration"` → 1377 passed, 2
  skipped, 6 deselected, 0 failed (baseline 1372 + 5 new parity tests; 6
  deselected = the 2 pre-existing OpenCode-Zen-live tests + the 4 new ones,
  all `-m integration`).
- The OpenCode Zen live test FAILED against the real API right now: `OpenCode
  Zen API error: Error from provider (Console): Upstream request failed: [404]
  Provider returned error`. Diagnosed as a pre-existing, provider-side issue
  and NOT a regression from this phase's code: re-ran the already-committed,
  untouched `tests/backend/integration/test_opencode_zen_live.py` (Phase 0,
  predates this whole plan) in isolation and its own non-structured happy-path
  test fails with the byte-identical error against the same free-tier model
  right now. Not something to fix in this plan — the free model itself appears
  unavailable/renamed upstream at the moment.
- **The three Anthropic integration tests SKIPPED — no `ANTHROPIC_API_KEY` is
  configured in this environment (only `OPENCODE_API_KEY` is set in the
  repo-root `.env`).** The plan's own Phase 3 addendum calls these assertions
  "non-negotiable before structured mode is trusted as a default" —
  specifically, that the real API accepts pydantic's `model_json_schema()`
  shape as a tool `input_schema` at all (a 400 there would mean the forced-
  tool-use mechanism, locked in Phase 3, is wrong). **This is an explicit open
  risk, not a footnote:** structured mode is fully wired and verified
  end-to-end against fakes (Phase 5's tests, this phase's parity tests), but
  its real-API schema acceptance for Anthropic remains UNPROVEN pending a key.
  Carry this forward explicitly — do not treat "1377 passed" as if it closed
  this gate.
