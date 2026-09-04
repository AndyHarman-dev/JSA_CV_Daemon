# Babysit ledger — .claude/plans/hello-claude-let-s-implement-rosy-jellyfish.md
Started: 2026-09-03 (Phases 0–2 already landed: cb88671, e052ff7, 13f1243)
Worktree: .claude/worktrees/revision-tool-use — branch `feat/revision-tool-use` (from `main`)
Baseline to beat every run: **1887 passed, 2 skipped, 21 deselected**
Test runner: `/Users/wiam/VSCodeProjects/JSA/.venv/bin/python -m pytest` (system Python has no pytest-asyncio)

| # | Phase | Model | Check | Review | Status | Attempts |
|---|-------|-------|-------|--------|--------|----------|
| P3.0 | Contract fixes (pre-dispatch) | self (Opus 5) | `test_tool_loop.py test_protocol_parser.py test_stages.py` -> **127 passed** | batch B1 | **verified** | 1 |
| P3.E1 | anthropic native tools | Opus 5 (sub) | `test_anthropic_api.py` | batch B1 | running | 1 |
| P3.E3 | opencode-zen native tools | Opus 5 (sub) | `test_opencode_zen.py` | batch B1 | running | 1 |
| P3.E4 | gemini native tools | Opus 5 (sub) | `test_gemini_api.py` | batch B1 | blocked on E2 (inherits base `tools=` plumbing) | 0 |
| P3.E2+E5 | _openai_compat + opencode-go | Opus 5 (sub) | `test_openai_compat.py test_opencode_go.py test_openrouter.py` | batch B1 | running | 1 |
| P3.BF | BF-19 slots survive _ToolsRejected | self | `test_bf18_limit_detection.py` | batch B1 | pending | 0 |
| P4 | `_tool_contract` + `tool_model=` kwarg | **self** (Opus 5) | `test_prompt_assembly.py test_prompt_prefix_stability.py` -> **117 passed** | batch B1 | **verified** | 1 |
| P5 | stages.py integration + parity gate | Opus 5 (sub) | `test_patch_parity.py`, `test_stages.py`, full suite | own review, high | pending | 0 |
| P6 | Frontend tool rows | Sonnet 5 (sub) | `npm test` + `npm run build` | batch B3 | pending | 0 |
| P7 | docs/TOOLS.md + CLAUDE.md | Sonnet 5 (sub) | sync test: every tool name + 6 error codes present | batch B3 | pending | 0 |

Review batches: **B1** = P3.0+E1–E5+P4 (combined diff, `high`) · **B2** = P5 alone (`high`) · **B3** = P6+P7 (`medium`). All `--fix`.

## Log
— Gate passed. Plan's own Verification section names runnable commands for every surface.
— Phase 7 check is mine, not the plan's (the plan says only "docs land as docs/TOOLS.md"):
  a pytest asserting every name from `tools_for(revising_cv)`+`tools_for(revising_cl)` and all
  six error codes appear in `docs/TOOLS.md`, plus a grep for CLAUDE.md's new section.

— **Blocking contract question settled before dispatch (deviation from plan text, deliberate).**
  Plan Phase 3's preamble says `class _ToolsRejected(AgentBackendUnavailable)` "mirroring
  `_CacheRejected`". That contradicts BOTH the plan's own E2 detail ("`_ToolsRejected`
  propagates ... nobody adds a third branch") AND `ToolsUnsupported`'s shipped docstring in
  `base.py`. Resolution: **every backend's `_ToolsRejected` subclasses `ToolsUnsupported`**,
  raised on a tools-present permanent 4xx, with NO in-backend retry-clean. Reason the
  `_CacheRejected` mirror is wrong: caching/reasoning are optional enrichments with no
  functional fallback so they must shed in-backend; tools have a fallback owned by a
  DIFFERENT layer (tool_loop's rung ladder), and a clean in-backend retry would send a
  tool-contract system prompt with no tools attached — incoherent. Canonical docstring
  written once in `_openai_compat.py::_ToolsRejected`; zen and anthropic carry their own
  copies (zen must not be wired onto the shared base, per CLAUDE.md).
— **Hole this exposed, fixed:** `tool_loop.py` caught `ToolsUnsupported` around its two
  `restore_session` entries but NOT around `send_tool_results`. A backend accepting tools on
  round 1 and rejecting on round 3 would have escaped into `_run_one`'s generic
  `except Exception` -> hard `mark_failed`, no BF-19, no rung 3. Now caught -> returns None
  (rung 3). Deliberately NOT a mid-loop rung downgrade: "one attempt per rung" is a per-turn
  rule. +2 tests.
— **TOOL_CALLS parse-precedence carry-forward (from the 2026-09-03 (3) code review) resolved.**
  `parse_reply` now ranks a NEED_INPUT/FINAL block above a TOOL_CALLS block regardless of
  position, instead of a plain `matches[-1]`. Satisfies both binding constraints: (a) a stray
  trailing TOOL_CALLS in a non-tool session can no longer outvote a legitimate FINAL; (b) a
  prompt-rung reply that is ONLY a TOOL_CALLS block still parses, so it never trips
  `_parse_with_nudge`'s `ProtocolError("no sentinel block")` and burns a nudge turn — which
  is what narrowing `_BLOCK_RE` back to `NEED_INPUT|FINAL` would have caused. Residual
  accepted: a non-tool session emitting ONLY a TOOL_CALLS block still hits run_stage's guard.
  +5 tests.
— Dispatched E1 / E3 / E2+E5 in parallel (file-disjoint). E4 held: gemini inherits the
  `tools=` kwarg plumbing E2 adds to `OpenAICompatBackend`.
— I own `test_bf18_limit_detection.py` myself (E2 and E4 would both have edited it).
— P4 taken by me rather than a subagent: the advisor's read is right that Phase 4 is much
  smaller than its plan text implies — Phase 2 already pulled forward the TOOL_CALLS verb,
  `AgentToolEvent` and the run_stage guard, and persistence (`role="tool"` rows) is Phase 5's.
  What was actually left: `_tool_contract(specs, *, native)` + a `tool_model=`/`native_tools=`
  pair on `assemble_system_prompt`. +9 tests; golden parity byte-identity holds.
  **Deviation recorded:** contracts are single-sourced from `tool_spec.py`, NOT generated from
  `docs/TOOLS.md` as Phase 4's text says — a runtime prompt path that reads a markdown file
  means a missing doc breaks the pipeline. The anti-drift guarantee becomes
  `tests/backend/test_tools_doc_sync.py` instead (**owed by Phase 7 — a comment in
  prompt_assembly.py already references it**).
  Also: `structured_model` + `tool_model` together now raises ValueError, which is Phase 3's
  "mutually exclusive per request — assert it" enforced at the composition root.

### E3 (opencode-zen) — verified
Agent reported complete. I re-ran the check myself, in this session:
`.venv/bin/python -m pytest tests/backend/test_opencode_zen.py -q` -> **100 passed in 1.03s** (was 77; +23).

Agent's three flags, my assessment:
1. *Malformed-`arguments` ProtocolError escapes tool_loop on the native rung.* Accepted —
   symmetric with `_parse_tool_calls_block`'s existing prompt-rung escape. Carry to B1 review
   as an explicit question: should `tool_loop` also catch `ProtocolError` around the tool path?
2. *`_call_api`/`_call_api_once` return type widened to `str | list[Any]`.* Private to the
   module; `stages.py` never calls them. No action.
3. *Extra `RuntimeError` guard in `send_tool_results` when the last handle message carries no
   `tool_calls`.* Defensible — it stops a contract violation from being misdiagnosed as
   `_ToolsRejected` and burning a rung. Kept.

### E1 (anthropic) — verified
Re-ran both checks myself:
- `pytest tests/backend/test_anthropic_api.py -q` -> **102 passed in 1.01s** (was 72; +30).
- `pytest tests/backend/{test_bf18_limit_detection,test_tool_loop,test_mode_parity,test_streaming_anthropic}.py -q`
  -> **101 passed in 2.48s**. `test_anthropic_other_errors_not_caught` (line 298) survived:
  it raises `ValueError`, not `BadRequestError`, so E1's new clause never sees it.

E1's supervisor flag was correct and I fixed it (P3.0, my file): `jsa/agents/base.py`'s
`ToolsUnsupported` docstring still asserted that per-backend `_ToolsRejected` "DOES subclass
`AgentBackendUnavailable` on purpose" — the plan's original, self-contradicting text. Rewrote
it to state the settled contract: every `_ToolsRejected` subclasses `ToolsUnsupported`, not
`AgentBackendUnavailable`; both reasons; and the three required consequences (no degrade-loop
branch, tool_loop must guard both `restore_session` and `send_tool_results`, genuine auth/
bad-model 4xx still raise `AgentBackendUnavailable`).

E1's other observation — the full suite showing shifting failures in `test_gemini_api.py`
from `_openai_compat.py` passing `tools=` into a `GeminiBackend._call_api_once` whose
signature E4 has not widened — is the **expected** E2->E4 dependency, and is exactly why E4
was held. It is not a defect; it is the reason E4 is dispatched next, not in parallel.

### E2+E5 (openai-compat base + opencode-go) — verified
Re-ran both checks myself:
- `pytest tests/backend/{test_openai_compat,test_opencode_go,test_openrouter,test_streaming_openai_compat,test_prompt_caching_phase1}.py -q`
  -> **154 passed in 1.02s**.
- `pytest tests/backend/{test_bf18_limit_detection,test_gemini_api,test_tool_loop}.py -q`
  -> **135 passed in 2.48s**.
`openrouter.py` itself was not touched — everything it needed came from the shared base, which
is the right outcome.

### Full-suite checkpoint after E1+E2+E3+E5
`pytest tests/ -q -m "not integration"` -> **1992 passed, 2 skipped, 21 deselected in 33.59s**.
Baseline was 1887/2/21, so **+105 tests, zero regressions**. This is E4's floor.

Contract re-verified by import, not by reading — all three `_ToolsRejected` definitions:
`_openai_compat`, `anthropic_api`, `opencode_zen` -> each `issubclass(ToolsUnsupported)` True,
`issubclass(AgentBackendUnavailable)` False.

### E4 (gemini) — dispatched
Held until now on purpose: gemini inherits from `OpenAICompatBackend`, and E2 left `_call_api`
dispatching `tools` as a **conditional** kwarg (`{"tools": tools} if tools else {}`) specifically
because `GeminiBackend._call_api_once` had not been widened. E4 closes that. E4 also closes the
one genuine hole in the tree right now: gemini inherits `supports_native_tools = True` and cannot
honor it (a tools call drops the vocabulary, returns text, and `tool_loop` downgrades to rung 2 —
degraded, not broken). Brief told E4 to import the canonical `_ToolsRejected` from
`_openai_compat.py` rather than define its own (gemini is a real subclass; the no-shared-base rule
applies only to `opencode_zen.py`), and pointed it at the `:395-398` null-content trap that a
`functionCall`-only reply hits first.

### P3.BF (mine) — written, gated on E4
Added to `tests/backend/test_bf18_limit_detection.py` (the file I reserved to avoid an
E2/E4 collision) three classes, +11 tests:

- `TestToolsRejectedDoesNotCostABF19Slot` — parametrized over mistral / openrouter /
  opencode-go-chat / gemini / opencode-zen. Pins the tools gate in **both** failure
  directions, which is the actual risk Phase 3 introduced:
  - *too eager* — a genuine bad-model/auth 4xx misclassified as `_ToolsRejected` would
    stop being an `AgentBackendUnavailable` and hard-fail the job on the first backend
    with a working fallback configured (this file's founding bug, through a new door);
  - *too timid* — `_ToolsRejected` subclassing `AgentBackendUnavailable` would advance
    the whole job over what should cost only a rung.
  One error envelope carries both the OpenAI-compatible (`message`/`type`) and Gemini
  (`code`/`status`) shapes, so a single body drives the whole matrix. The no-tools case
  asserts `supports_native_tools is True` first — so it proves the gate reads the
  **payload**, not the capability flag.
- `TestAnthropicToolsRejectedDoesNotCostABF19Slot` — same pair, own construction (SDK
  `BadRequestError`, not httpx).
- `TestToolsUnsupportedIsNotRoutedByBF19` — orchestrator-level: an escaped
  `ToolsUnsupported` fails the job on the backend it started on and does **not** advance
  the chain. Passes.

Run: `pytest tests/backend/test_bf18_limit_detection.py -q` -> **79 passed, 1 failed**.
The single failure is `[gemini] TypeError: GeminiBackend.start_session() got an
unexpected keyword argument 'tools'` — expected, and deliberately left red: it is now an
**independent** gate on E4, written before E4 reported, so E4's own test file is not the
only thing vouching for E4.

Incidental finding while reading this file: `ProtocolError` **is** BF-19-routed
(`TestOrchestratorProtocolErrorDetection`, ~:676 — it switches backends). So E3's flag #1
(a malformed-`arguments` `ProtocolError` escaping `run_tool_loop`) does not hard-fail as I
first assessed — it burns a **backend hop** instead. That is arguably worse than a hard
fail for a model-formatting problem, and strengthens the case for raising it at B1.

### E4 (gemini) — verified
- `pytest tests/backend/test_gemini_api.py -q` -> **69 passed in 0.59s** (42 pre-existing + 27 new).
- `pytest tests/backend/test_bf18_limit_detection.py -q` -> **80 passed** (was 79 passed / 1 failed).
  This is the strong result: my P3.BF gemini row went green **without my touching it**, and E4
  never saw that file. Independent confirmation rather than a self-report.
- `pytest tests/ -q -m "not integration"` -> **2032 passed, 2 skipped, 21 deselected**.

E4 also mutation-tested its three load-bearing behaviours (return only the first call / move
extraction after the `if not text` raise / drop the tools-first branch in `_permanent_4xx`);
each produced a distinct failure set. That is the right instinct and I am recording it, but it
is E4's own report — the numbers above are mine.

### P3.G (mine) — gemini union-type rendering, E4's headline flag, CONFIRMED and FIXED
E4 flagged (correctly, and correctly declined to fix — `tool_spec.py` was outside its two-file
scope) that `to_gemini_function_declarations` lets JSON Schema union types through. I verified
it directly rather than taking the report: **17 array-typed `type` values** survive for
`revising_cv`, 3 for `revising_cl`, with **zero `anyOf`**.

Why it matters: Gemini's `functionDeclarations.parameters` is a restricted OpenAPI-3.0 subset —
`type` is a single value, nullability is a sibling `nullable: true`. Unconverted, gemini answers
400 INVALID_ARGUMENT, `_permanent_4xx` correctly raises `_ToolsRejected`, and `tool_loop`
downgrades native -> prompt on **every single gemini revision**. The failure is invisible: it
presents as a working rung-2 degrade, so no end-to-end "does a revision succeed" test would ever
catch it. Native tools on gemini would have shipped inert.

Fix: rewrote `_strip_additional_properties` into `_to_gemini_schema`, which also converts
`{"type": [T, "null"]}` -> `{"type": T, "nullable": True}`, and raises `ValueError` on a genuine
multi-type union rather than shipping something the wire will reject. Deliberately **gemini-local**
— `_NULLABLE_STRING`/`_NULLABLE_INT` also feed `to_anthropic_tools`/`to_openai_tools`, where the
union form is valid and already covered; changing them at the source would have rewritten two
payloads that are already correct. Verified: gemini 0 unions / 17 nullable; anthropic and openai
still 17 unions each.

+5 tests in `tests/backend/test_tool_spec.py` -> **22 passed**. Full suite -> **2037 passed,
2 skipped, 21 deselected**.

**Honest limit:** not verified against the live Gemini API — that needs a key and a real call.
The evidence is the shape asymmetry (the structured path reaches gemini via `anyOf`, a form
confirmed accepted per `inline_defs`' docstring; array-typed `type` is a form nothing in this
repo has ever put on a Gemini wire). Recorded in the function's docstring too.

### Phase 3 — COMPLETE. Phase 4 — COMPLETE (landed earlier this run).
Proceeding to the B1 review batch.

---

## Routing change — 2026-09-03, from the user

> "Further implementation once the existing agents land is with /opencode-delegate"

Phases 5, 6 and 7 are to be **implemented via the `opencode-delegate` skill** (OpenCode's
headless `opencode run`), not via Claude subagents. This changes only WHO writes the code.

Unchanged, explicitly:
- **Verification stays mine.** Every check is still re-run personally in this session. A
  delegated diff is exactly as unverified as a subagent's report was — arguably more so,
  since the delegate runs outside this context entirely.
- Code review still batches, still `--fix`, still Sonnet, still drained-and-re-verified
  before the next dispatch.
- The Step-6 halt rules still apply to whatever comes back.

---

## B1 review — DRAINED and re-verified (2026-09-04)

`/code-review high --fix`, Sonnet, over the P3.0 + E1-E5 + P3.G + P4 diff. 8 findings,
6 fixed by the reviewer, 2 declined with reasons.

**Re-run personally after the fixes landed** (the whole point — the green numbers I
recorded before described a state that no longer existed on disk):

| Check | Result |
|---|---|
| full suite `-m "not integration"` | **2043 passed, 2 skipped, 21 deselected** |
| test_anthropic_api | 102 passed |
| test_opencode_zen | 100 passed |
| test_gemini_api | 69 passed |
| test_bf18_limit_detection | 80 passed |
| test_tool_spec | 22 passed |
| test_prompt_assembly | 105 passed |
| test_protocol_parser | 55 passed |
| test_tool_loop | 30 passed |

Softening audit: `git diff HEAD -- tests/` shows **zero** deleted test functions and
**zero** added skip/xfail markers across the entire branch. The +6 net came from new
regression tests, each confirmed by the reviewer to fail against the unfixed code.

**Correction to my own earlier ledger figure:** I recorded test_prompt_assembly at 117.
The real count is 105 (13 test defs at HEAD -> 22 now, none deleted). Transcription
error on my part, not a lost test.

### Step-6 assessment: accept, do not halt.
Findings 1/2/4/5 implement behavior `_tool_contract` (Phase 4) had already *promised the
model* but nobody wired; they do not change the approach, do not edit outside the batch's
surface, and no check broke. Finding 3 makes `fake_backend` ENFORCE the capability
contract it only claimed to enforce — strengthening a check, never softening one.

### E3 flag #1 — RESOLVED by this review.
My queued question was: a malformed-`arguments` `ProtocolError` escaping `run_tool_loop`
burns a whole BF-19 backend hop for what is a pure model-formatting problem. Finding 2's
fix catches `ProtocolError` at both rung entries AND mid-loop, returning `None` so the
caller runs rung 3. That is exactly the disposition I wanted; no separate action needed.

### Carried forward — three items NOT closed by B1
1. **Finding 6 -> a direct Phase 5 input.** `run_tool_loop` took ownership of
   `restore_session` but never calls `adapt_history`. CLAUDE.md's canonical-form
   invariant requires `structured_schema=` and `adapt_history(structured=...)` to come
   from ONE destination-mode decision at every restore site. The loop restores with no
   schema, so a structured-mode `cv_adjust` history (canonical `{`-prefixed rows) replays
   verbatim into a non-structured tool session. Reviewer declined to guess the tool
   rung's history format — correctly, it is Phase 5's call. **Reachable today.**
2. **Finding 7 — declined, genuine known gap.** `opencode_zen.py:459` appends the
   assistant `tool_calls` turn immediately where `_openai_compat` deliberately parks it;
   after a terminal `finalize` the handle ends on an unanswered `tool_calls` turn, so a
   follow-up `send_message` on it 400s. No production caller yet.
3. **Merge-time, not fixable on this branch.** This branch forked before `e7c116e`, so
   `assemble_system_prompt` has no `now` param here. On merge it gains one — and the tool
   branch `return`s before any date directive is appended, so revision turns would
   silently lose the current-date directive that exists because a model parked a job
   `unfit` over a "future" CV date.

## Phase 5 dispatched via opencode-delegate
Model: `opencode-go/kimi-k2.7-code` (MEDIUM tier, escalated from the LongCat default up
front). Named axis: **verifiability**. Phase 5's subtle requirements fail SILENTLY —
instance-vs-class `supports_native_tools`, instruction staying verbatim on rungs 1-2,
both revision sub-branches. LongCat's documented quirk is forgetting repo conventions,
and the single most dangerous available mistake here is a class-level
`supports_native_tools` read, which CLAUDE.md flags as a silent-wrongness trap.

---

## OpenCode delegation FAILED — fell back to Claude subagents (2026-09-04, ~01:20)

Four dispatch attempts, ~70 minutes, **zero lines of code produced**.

| # | Model | Outcome | Evidence |
|---|-------|---------|----------|
| 1 | kimi-k2.7-code | hung after 8 min of orientation | 18 tool_use events, ALL read/grep/todowrite; then 43 min silent on a `step_start` |
| 2 | kimi-k2.7-code | **my dispatch bug** | used `nohup ... &` inside a foreground Bash call -> child reaped on return; 0 bytes stdout AND stderr |
| 3 | glm-5.2 | alive 9+ min, never emitted a first event | 0 bytes, 7.9s CPU (waiting on API, not computing) |
| 4 | longcat-2.0 (Phase 6) | same | 0 bytes |

Only #2 was my fault; fixed by returning to the Bash tool's own `run_in_background`.

**Decisive probe:** `opencode run "Reply with exactly the word: PONG" -m opencode-go/longcat-2.0
--dir /tmp` hung 60s+ with empty stdout. opencode itself starts fine (it printed its
`> build · longcat-2.0` status line to stderr) but gets nothing back from the model. So the
**opencode-go gateway is unresponsive** — not model choice, not prompt size, not my dispatch.
`opencode models` confirms every model name I used is valid and present in the catalog.

Both monitors confirmed **0 bytes / 0 changed files** on exit, so the failed runs left no
partial state in the worktree. Verified `git status --porcelain` clean of stages.py and
frontend/src changes before re-dispatching.

**Decision (mine, stated to the user):** the user's fallback authorization read "default to
your own subagents if limit is reached." A hung gateway is not a rate limit, so this is not a
literal match — but it is the same blocker in substance (opencode cannot serve), and waiting on
a dead gateway serves nobody. Fell back to Claude Sonnet subagents for Phases 5 and 6,
dispatched in parallel on disjoint surfaces (`jsa/pipeline/stages.py` vs `frontend/`).
Reversible: if the gateway recovers, remaining work can go back to opencode.

**Note:** a SEPARATE opencode run (PID 2393, since 00:30) is working the `prompt-injection`
worktree — claude-stat task #97, another session. Different worktree, no collision. Left alone.
It may also be stalled against the same dead gateway; the user was told.

### Plan-vs-reality drift caught while preparing the Phase 6 brief
- Plan says the third lockstep literal is at `AgentThread.tsx:411`. It is actually at **:332**.
  Brief was re-anchored on literal TEXT rather than line numbers.
- Plan says settled turns render "through the same `StepRow` (exported from
  `ReasoningCard.tsx`)". `StepRow` is currently **not exported** (`function StepRow` at :31).
  Exporting it is now an explicit task in the brief.

---

## Phase 6 — VERIFIED (Claude Sonnet subagent, 2026-09-04)

Re-run personally, not taken from the agent's report:

| Check | Result |
|---|---|
| `npm test` | **355 passed, 25 files** |
| `npm run build` | **built in 1.33s** (only the pre-existing >500kB chunk advisory) |

Softening audit:
- `src/__tests__/reasoningSteps.test.ts` — **untouched** (`git status --porcelain` empty). Its
  prefix-stability gate, which re-segments EVERY prefix and asserts closed steps stay a prefix
  of the final list, still passes unmodified.
- Exactly ONE test deleted: `it("renders tool-call rows through the same step list (dev preview
  toggle)")`. That test covered the DEV mock toggle this phase deliberately removes, so it
  tested code that no longer exists — a correct deletion, not a softened check. Verified its
  two replacements are genuine (they assert real rendering through the live `streamBuffers.tools`
  channel AND the settled `turn.tools` path, with `getAllByTestId("reasoning-tool-step")`
  length assertions), not vacuous.
- Zero `.skip` / `.only` / `.todo` added.

i18n: **no new keys** — it correctly reused the existing `agentThread.toolBadge`, so
`scripts/translate-ui.sh` is NOT owed. `useT()` in use in both changed components.
`src/lib/reasoningMock.ts` deleted (the one sanctioned deletion); zero dangling references.

Nice touch worth recording: `bounds?: number[]` was made OPTIONAL and *omitted* (not an empty
array) when `closed` is empty, specifically so the protected test file's whole-object
`toEqual({closed:[], open:null})` assertions keep passing byte-for-byte. That is working WITH
the gate instead of editing it.

### !! PLAN INCONSISTENCY FOUND — needs a user decision (halt candidate)

The Phase 6 agent honestly flagged that it added `tools` to the frontend `TranscriptTurn` type
but "the backend doesn't populate it yet, so it degrades to no rows." I verified that: the
backend `TranscriptTurn` builder in `jsa/api/transcript.py` has a `reasoning` field and **no
`tools` field at all**.

The plan says two incompatible things:
- **Persistence section (:416-440):** role="tool" rows "fall through to the plumbing branch at
  :279-289 and render" — i.e. as SEPARATE, hidden-by-default, TRUNCATED machine rows.
- **Phase 6 (:500):** "Settled turns render persisted tool rows through the same `StepRow`
  ..., so live and settled look identical."

Plumbing rows do NOT look identical to live tool rows — different component, hidden by default,
truncated. So the two sections describe different systems, and the current state satisfies
neither: `turn.tools` is dead code the backend never fills.

Not resolvable by guessing — the two readings produce materially different UX. Taking it to the
user rather than picking one.

## 2026-09-04 — Phase 5 salvage + Option A

**Phase 5 recovered, not rewritten.** The stalled agent had completed the whole
`stages.py` integration (`_tools_for`, `_tools_kwargs`, the single destination-mode
block, both revision sub-branches, verbatim instruction on rungs 1-2, `_log_session_mode`,
`adapt_history(structured=False)`, `role="tool"` persistence) and had already added
`fakes/finals.py::tool_loop_miss()` plus partial bf9 updates. It died mid-test-writing.
Committed as `2f2ce20` before touching anything.

**Finding — the plan is wrong about the prompt rung's reach (plan :574).** The plan's
verification block names `jsa --backends claude-cli` as the prompt-rung target. But
`ClaudeCliBackend.restore_session` / `GoogleCliBackend.restore_session` IGNORE the
`system_prompt` and `history` they are handed whenever `external_id` is set (always, for
a revision) — the CLI holds the conversation and is resumed by id; `send_message` passes
no `--system-prompt`. The prompt rung's ONLY transport is that system prompt. So on those
two backends the rung cannot work by construction, and as recovered Phase 5 entered the
loop for them anyway: one wasted model turn per revision, a good `<<<FINAL>>>` discarded
as unparseable, and rung 3 re-sending the SAME instruction into a persisted conversation
that then holds it twice.

Fixed (`b078316`) with `AgentBackend.restore_applies_system_prompt` (ClassVar, default
True; False on both CLI backends) and a gate in `_tools_for`: neither native nor
restore-applies → skip the loop, byte-identical pre-feature behavior. Chose this over
gating on `supports_native_tools` alone, which would also have killed the prompt rung for
`opencode-go`'s `/messages` protocol, where it genuinely works.

**Accepted cost, stated explicitly:** on every OTHER non-native backend a revision now
costs one extra model round trip whenever the model ignores the tool contract. That is
inherent to the ladder and the plan did not budget for it.

**The 5 regressions were this, not fixture noise.** bf9/bf10 assert exact send_message /
restore_session counts for revisions. Updated to script the probe turn via the existing
`tool_loop_miss()` helper; every discriminating assertion preserved (resume sends the
answer not the instruction; second revision sends the new instruction; revisions reuse
cv_session_id) — just applied to both rungs' calls. Backend suite back to the 2043
baseline, then 2054 with the parity gate.

**Parity gate landed** (`a4e247b`), 11 tests, verified DISCRIMINATING by mutation:
dropping `dates` from `CvWorkingCopy._to_document_dict` fails both CV assertions.

**Option A blocker — `turn.reasoning` is already dead code, and `turn.tools` would be
too.** `transcript.py` attaches `reasoning` at exactly ONE place (:287, the plumbing
branch). `AgentThread.tsx:465` renders a `plumbing` turn as `PlumbingLine`, which does
NOT render `ReasoningCard` at all AND is gated behind `showInternals`. So CLAUDE.md's
claim that "`TurnBubble` renders `turn.reasoning`" is true of the component but
unreachable in practice — no turn that reaches `TurnBubble` ever carries `reasoning`
(`question`/`answer` turns come from FollowUp rows, which have no reasoning column).
Attaching `tools` to the assistant plumbing turn — the semantically correct owner — would
inherit the same invisibility. This is pre-existing, not introduced by Phase 6.

### Option A — resolved as built

`turn.reasoning`/`turn.tools` land only on `plumbing` turns, and plumbing renders as a
gated `PlumbingLine` with no card. So attaching the field alone would have shipped dead
code. Considered and rejected: attaching to the `delivery` turn (needs Document↔Message
correlation heuristics + a NoticeLine rewrite) and to the `question` turn (only exists on
the ask_user park, invisible for a finalize revision).

Built instead: `transcript.py::_tool_mark` folds `role="tool"` rows into the FOLLOWING
assistant turn, and `AgentThread`'s map hoists that turn's `ReasoningCard` OUT of the
`showInternals` gate while leaving the raw machine text gated. Honest attribution (the
assistant row IS what made the calls), no correlation guessing, visible by default.
Side effect noted in-code and in CLAUDE.md: a tool turn's reasoning is now visible while
a non-tool turn's still is not — a pre-existing gap, deliberately not widened.

`at` on a settled mark is the row's INDEX in the turn, not a buffer offset: tool mode
never streams, so there is no reasoning buffer to anchor against, and `mergeToolSteps`'
trailing-append preserves exactly the persisted call order. Pinned by a test.

### Ledger

| # | Phase | Model | Check | Review | Status | Attempts |
|---|-------|-------|-------|--------|--------|----------|
| 5 | stages.py integration | salvaged from stalled agent → self | `pytest -m "not integration"` → 2043 then 2054 | batch B2 high | verified | 5 (kimi hang, nohup bug, glm no-output, Sonnet stall, self) |
| 5p | patch parity gate | self | `test_patch_parity.py` → 11 passed | batch B2 high | verified | 1 |
| 6 | Frontend | Sonnet 5 (subagent) | `npm test` → 357, `npm run build` → ok | batch B2 high | verified | 1 |
| A | Option A transcript wiring | self | backend 2060, frontend 357, build ok | batch B2 high | verified | 1 |
| 7 | Documentation | self | `test_tools_doc_sync.py` → 13 passed | batch B2 high | verified | 1 |

Commits: `2f2ce20` (salvage) → `b078316` (rung gate) → `4051bf5` (parity) → `9077593`
(Option A) → `83c581b` (Phase 7) → `b40b293` (lockfile restore).

Effort stepped up from the plan's stated `medium` to `high`: 45 files / 6381 insertions
since the last reviewed commit (`13f1243`), and what landed is bigger than the plan
implied — an unplanned backend capability flag plus the Option A transcript work.

**Stray file report:** none. Nothing untracked was deleted; the only tracked file
reverted was `frontend/package-lock.json`, npm-version drift no phase asked for
(`b40b293`), and the frontend suite was re-run green after restoring it.

10:00 B2 review (high --fix, Sonnet) landed. 3 files touched, all in-scope, no deletions.
      Re-ran every check in the batch MYSELF after the fixes:
        pytest -m "not integration"        -> 2073 passed, 2 skipped, 21 deselected
        targeted 7 suites                  -> 320 passed
        npm test / npm run build           -> 357 passed, 25 files / built 1.02s
      Verified each fix against its claim rather than trusting the report:
        - prompt_assembly.py comments: stale->correct. Accepted.
        - tool_loop.py finalize_fired: VERIFIED protocol.py::_parse_tool_calls_block
          only checks isinstance(arguments, dict); no `required` enforcement, so
          finalize with {} really does complete a revision. Real fix. Accepted.
        - tool_loop.py _event_detail cap: VERIFIED store.ts:292 uses
          `e.summary || e.detail` and _summarize is never empty -> detail unrendered.
          Truncation safe, row still persists verbatim. Accepted.
        - patch.py _summary_section_id: HALT. Fix is real (German CV edits in place,
          confirmed) but the shape pass overwrites a text-only NON-summary first
          section. Probe case 3: [Kontakt(text), Zusammenfassung(text), Berufserfahrung]
          -> "Kontakt" body DESTROYED, real Zusammenfassung left stale. Silent data
          loss, strictly worse than the duplicate-section bug it fixes.
      Status: blocked -- awaiting user. Nothing further dispatched. NOT merged.

10:20 User chose option A. Narrowed the shape pass: fires only when the CV has exactly
      one text-only section AND it is first. Re-probed all 5 CV shapes -- German edits
      in place; Kontakt, Zusammenfassung and a trailing Interessen all preserved; the
      genuinely-absent case still creates. No destructive case remains.
      Added the 9 regression tests the review pass did not write (4 summary-lookup,
      5 tool-loop). Mutation-tested all four fixes:
        rev finalize_fired -> string    -> 2 failures (right tests)
        rev event-detail cap            -> initially PASSED: my test was not
                                           discriminating (replace_summary returns a
                                           small {ok,id}). Rewrote it to assert on the
                                           get_cv event after an oversized write ->
                                           now fails on revert. Gate is real.
        rev shape pass -> reviewer's    -> 1 failure (the clobber test)
        rev shape pass -> pre-review    -> 1 failure (the German test)
      Re-ran everything: backend 2082 passed / 2 skipped / 21 deselected;
      targeted 329 passed; frontend 357 passed 25 files; npm run build clean.
      Committed 1a38586. Change Log entries appended to the plan (Decisions Log
      untouched -- user's hand only). Batch B2 CLOSED. Not merged.
