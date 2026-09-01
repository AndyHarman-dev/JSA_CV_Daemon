# Skeptic dossier — "Structured Output for API Backends — Sentinel as Fallback"

Plan file: /Users/wiam/VSCodeProjects/JSA/.opencode/plans/structured-output.md
Repo: /Users/wiam/VSCodeProjects/JSA

# Objective

Move JSA's five pipeline stages (fit_assessment, cv_adjust, cover_letter,
revising_cv, revising_cl) onto provider-enforced JSON-schema output for the two
HTTP API backends (`anthropic`, `opencode-zen`), keeping the
`<<<NEED_INPUT>>>`/`<<<FINAL>>>` sentinel grammar as the CLI-backend path and
the downgrade target. "Done" = 7 phases landed, backend tests green, sentinel
mode behavior byte-identical to today.

# Explorer claims

Source: the plan document itself (written by a prior planning session after an
advisor round — there are no independent explorer reports; every claim below is
attributed to "plan" and needs verification against the actual code).

Claims about CURRENT code:
- C1. `parse_reply(raw) -> AgentReply` in `jsa/agents/protocol.py` is the ONLY
  reply parser. — basis: plan Phase 1, not independently checked.
- C2. `ProtocolError` is deliberately NOT a BF-19 signal and hard-fails the job
  outside the fit stage. — basis: plan Context + CLAUDE.md.
- C3. Stage→schema mapping lives implicitly in `_validate_final_content`
  (`CVDocument` for cv/revising_cv, `CoverLetter` for cl/revising_cl, `None`
  for fit). — basis: plan Phase 1.
- C4. A `_self_heal_final` with a `MAX_FINAL_CORRECTIONS=2` budget exists in
  `jsa/pipeline/stages.py`; self-heal corrections mention sentinels explicitly.
  — basis: plan Context/Phase 5 (also echoed in CLAUDE.md BF-19 notes).
- C5. `AnthropicAPIBackend` sends `messages.create(model, max_tokens=8192,
  system, messages)` and parses with raw `parse_reply` — "the ONLY backend with
  no nudge" (claude-cli and opencode-zen both have `_parse_with_nudge`). —
  basis: plan Phase 3. UNVERIFIED for google-cli: does google-cli have a nudge?
- C6. `OpenCodeZenBackend` has 3-way error classification
  (`AgentLimitReached` / `_TransientOpenCodeError` retry×3 →
  `AgentBackendUnavailable` / immediate `AgentBackendUnavailable`) and
  `_parse_with_nudge` mirroring claude-cli. — basis: plan Phase 4 + CLAUDE.md.
- C7. `_with_language_directive` appends the directive to NEW sessions only,
  is a no-op for `"en"`, and is called at the two `start_session` sites in
  `run_stage`/`_run_fit_assessment`. — basis: plan Phase 2 + CLAUDE.md.
- C8. History replays verbatim (`_load_history` → backends); `Message` rows
  persist assistant replies as plain text. — basis: plan Phases 1–2.
- C9. The fit stage can run on a DIFFERENT backend instance (`fit_model` /
  `fit_timeout` overrides via `make_backend_factory`'s `model_override` /
  `timeout_override`). — basis: plan Phase 5 + CLAUDE.md.
- C10. Installed Anthropic SDK is 0.104.1; its `response_format` surface is
  unknown to the planner. — basis: plan Phase 3.
- C11. Pydantic `model_json_schema()` can emit a strict-clean schema
  (`additionalProperties: false`, all fields required) for the proposed flat
  nullable-field union models. — basis: plan Phase 1. INFERRED, not checked —
  Pydantic v2 is not known to emit `additionalProperties: false` by default.
- C12. Anthropic API accepts plain-text assistant turns in history even when
  the current turn is tool-forced (no tool_use/tool_result pairing needed
  unless tool_use blocks are present). — basis: plan "Named invariant". INFERRED
  from API lore, not verified against docs.
- C13. The OpenCode Zen endpoint (`https://opencode.ai/zen/v1/chat/completions`,
  OpenAI-compatible) accepts
  `response_format={"type":"json_schema","json_schema":{"name":...,"strict":true,"schema":...}}`.
  — basis: plan Phase 4. UNVERIFIED; the plan itself admits wire-level strict-
  schema rejection is possible per proxied model.
- C14. `parse_structured_reply` can re-serialize `payload` so
  `_validate_final_content` receives "the byte-identical shape as the sentinel
  path". — basis: plan Phase 1. Question: does `_validate_final_content`
  consume raw text (json.loads inside) or a parsed object? If it json.loads,
  "byte-identical" is irrelevant; if it compares strings, json.dumps round-trip
  byte-identity (key order, spacing, ensure_ascii) is fragile.

# Pre-synthesis (assertions to attack)

- A1. This is purely additive: the sentinel path is retained forever for CLIs,
  so the parity gate is a permanent equivalence invariant, not a deletion gate.
  No old code is deleted at any phase.
- A2. Keeping the backend parse layer to `json.loads` + `kind` routing, with all
  semantic validation stage-side in `_validate_final_content`, lets the existing
  2-correction self-heal budget work unchanged for structured replies.
- A3. Storing canonical normalized union-JSON text in `Message` rows and
  sentinel-wrapping only at replay into a sentinel-mode session is SUFFICIENT
  for mixed-mode history — provider wire format never round-trips through the DB.
- A4. A per-session, never-persisted `structured_enabled` downgrade flag on zen
  session handles is safe; a restart flips back to structured.
- A5. Forced tool-use (`tool_choice` forced, extract `tool_use.input`) is a
  reliable primary mechanism on Anthropic; truncation under forced tool surfaces
  as `stop_reason == "max_tokens"`.
- A6. `FitVerdict{verdict: Literal["FIT","UNFIT"], reason: str|None}` satisfies
  locked decision #4, which says `reason` is REQUIRED for both verdicts (a bare
  FIT/UNFIT without a why must be rejected). NOTE the apparent contradiction:
  a nullable `reason` in the schema lets a provider send `reason: null` — that
  is a bare verdict, which decision #4 says must be rejected. Either the model
  must be `reason: str` (required, non-null) or there is a missing validator.
- A7. New `jsa/schema/turn_models.py` importing `jsa/agents/base.py` +
  `jsa/agents/protocol.py` keeps "schema package remains a leaf; zero new edges
  into backends". NOTE: `jsa.agents.base`/`protocol` ARE the agents package —
  the "leaf" claim is textually dubious, and circular-import risk
  (agents.base → protocol → ? → schema) is unexamined. Also CLAUDE.md documents
  a prior circular-import landmine (stages must never import server), so import
  direction matters in this codebase.
- A8. `wrap_canonical_for_sentinel` keyed on session ACTIVE MODE is sufficient
  for BF-19 cross-backend switches (anthropic→claude-cli mid-conversation).
  UNADDRESSED: how does replay distinguish a canonical structured row from a
  LEGACY sentinel-wrapped row or a free-text row written before this feature?
  The plan gives no detection rule. Existing DBs have rows in sentinel form.
- A9. Routing structured-mode `ProtocolError` in `run_stage` through the SAME
  `MAX_FINAL_CORRECTIONS=2` budget keeps sentinel-mode semantics byte-identical.
- A10. Fit-verdict mapping `content=f"{verdict}\n{reason}"` lets
  `_parse_fit_verdict` be reused unchanged. UNVERIFIED: what does
  `_parse_fit_verdict` actually accept (first-line FIT/UNFIT per CLAUDE.md)?

# Candidate plan (7 phases)

1. Phase 1 — new `jsa/schema/turn_models.py`: flat union models
   (FitVerdict/CvTurn/ClTurn with iff-validators), `STAGE_TURN_MODELS`,
   `json_schema_for(stage)` (must emit additionalProperties:false),
   `parse_structured_reply` (json.loads + kind routing only), capability ClassVar
   on `AgentBackend`.
2. Phase 2 — new `jsa/pipeline/prompt_assembly.py::assemble_system_prompt`
   (language directive variants + structured-contract section with precedence
   over sentinel section) replacing `_with_language_directive` at two call
   sites; `wrap_canonical_for_sentinel` helper in turn_models.py.
3. Phase 3 — anthropic forced tool-use behind a dict-extraction adapter;
   `structured_schema` kwarg on start/send/restore; truncation → ProtocolError
   variant; None-schema = today's behavior exactly.
4. Phase 4 — zen `response_format` json_schema; per-session
   `structured_enabled` flag; downgrade on unparseable/missing-kind only →
   existing `_parse_with_nudge` on same raw text; retry-budget independence.
5. Phase 5 — pipeline wiring in `stages.py`: schema kwarg passed when
   backend.supports_structured_output AND session mode structured; fit uses the
   actual fit-backend instance (capability trap); mode-aware self-heal wording;
   structured ProtocolError spends the 2-correction budget; mode LogEvent;
   fake_backend grows a structured-mode knob.
6. Phase 6 — parity tests: same logical payload via sentinel fake vs structured
   fake → identical Document.structured/markdown, fit verdict+state,
   FollowUp.question; integration tests marked.
7. Phase 7 — CLAUDE.md/ARCH.md docs; prompt files NOT edited.

# Load-bearing assumptions (per step — the claim each dies without)

- Phase 1 dies if: Pydantic cannot emit strict-clean schemas without top-level
  `anyOf` (C11); or turn_models' imports create a cycle (A7); or
  `_validate_final_content` does not consume what the re-serialization produces
  (C14).
- Phase 2 dies if: the runtime contract section cannot actually override the
  file's sentinel instructions for stubborn models (untestable assumption); or
  legacy-row detection at replay is needed and missing (A8).
- Phase 3 dies if: SDK 0.104.1 forced-tool shape differs (C10) or truncation
  does not surface as `stop_reason == "max_tokens"` under forced tool (A5).
- Phase 4 dies if: the zen proxy rejects `response_format` outright for its
  free models (C13) — then "attempt structured, downgrade" costs a failed call
  per session; or the downgrade interacts with `_MAX_ATTEMPTS` retry loop.
- Phase 5 dies if: `_self_heal_final` does not exist as described (C4), or
  spending the budget on ProtocolError changes sentinel-mode behavior (A9), or
  the fit-backend instance is not available where the plan assumes (C9).
- Phase 6 dies if: the two fakes cannot produce byte-identical observables
  (e.g. markdown rendering differs by input path) — the invariant is then
  unstateable.
- Cross-phase: Phase 3's truncation handling is only testable once Phase 5's
  budget routing exists (ordering dependency the plan acknowledges but sequences
  3 before 5).

# Focus for the skeptic (least sure / highest blast radius)

1. Verify EVERY "Current State" claim (C1–C10, C14) against the actual code —
   especially C4 (`_self_heal_final` + budget + sentinel wording), C5 (is
   anthropic really the ONLY nudge-less backend? check google-cli), C3
   (`_validate_final_content` actual signature/consumption), and C8 (what
   exactly is stored in Message.content and where; who reads it back expecting
   sentinels — including frontend/API endpoints that surface messages).
2. Find MISSED consumers of the reply/message contract: anything that parses
   `Message.content` or backend replies expecting sentinel markers (revision
   flow, follow-ups, exports, frontend message display, `backend_switch_reset`
   replay). The plan only names stages.py and the backends.
3. Attack A6 (reason nullable vs required contradiction), A7 (import graph),
   A8 (legacy row detection), C11 (Pydantic additionalProperties emission),
   C12/C13 (provider-behavior assumptions — flag as UNVERIFIED, not refutable
   locally).
4. Check the sentinel-mode byte-identity promise: any shared code path where
   structured mode changes behavior for CLIs (e.g. shared `_parse_with_nudge`,
   shared self-heal wording, `assemble_system_prompt` replacing
   `_with_language_directive` for en/no-op cases).
5. Check BF-19 interplay: `backend_switch_reset` rewind targets (cv_adjust →
   pending → re-enters fit_assessment) with a mode change across the switch —
   does the replayed history get wrapped correctly when the NEW backend's mode
   differs and the rows were written by the OLD mode?
