# CLAUDE.md — JSA Project Conventions

This file records non-derivable conventions for all agents working on this project. This repo has no separate `ARCH.md`/`PLAN.md` — architecture lives in `README.md`'s "Architecture" section, and implementation status/history lives in the relevant plan file under `.opencode/plans/`. Only things that would be unclear from reading the code belong here.

---

## Sentinel protocol — MANDATORY for CLI backends, and the structured-output downgrade target

Every prompt file (`jsa/prompts/PROMPT_CDADJUST.md`, `jsa/prompts/CVL_PROMPT.md`) **must** instruct the model to terminate every reply with exactly one of:

```
<<<NEED_INPUT>>>
<question to the user>
<<<END>>>
```
or
```
<<<FINAL>>>
<final markdown payload>
<<<END>>>
```

This is not optional — the pipeline parser (`jsa/agents/protocol.py`) will raise `ProtocolError` and mark the job `failed` if the sentinel is absent or malformed. When writing or editing prompt stubs, always include this instruction prominently at the end of the system prompt.

**This is the only channel for CLI backends** (`claude-cli`, `google-cli` — `supports_structured_output = False`), and it is also **where a structured-capable API backend lands the moment it downgrades** (OpenCode Zen, per-session, on an unparseable structured reply — see "Structured output (API backends)" below). Prompt files are never edited to describe structured mode; that section is composed onto the prompt at runtime instead — see below.

## Structured output (API backends)

The two HTTP API backends (`anthropic`, `opencode-zen`) can bypass the sentinel grammar
entirely and get a provider-enforced JSON object back instead. This is additive — the
sentinel protocol above is unchanged and is not being replaced or deprecated for CLI
backends.

**Capability flag.** `AgentBackend.supports_structured_output: ClassVar[bool]`
(`jsa/agents/base.py`) is hard-coded per backend, never runtime-detected:
`AnthropicAPIBackend`, `OpenCodeZenBackend`, `MistralBackend`, `OpenRouterBackend`, and
`GeminiBackend` are `True`; `ClaudeCliBackend` and `GoogleCliBackend` are `False` (and
never accept a `structured_schema` kwarg — under `stages.py`'s conditional-kwarg wiring,
no caller ever offers one to a backend that can't use it, so adding an accept-and-ignore
parameter to the CLI backends would be dead code).

**`OpenCodeGoBackend` is the one deliberate exception to "never runtime-detected."**
`jsa/agents/opencode_go.py` proxies two wire protocols under one backend name —
`/chat/completions` models get real structured output, `/messages` (Anthropic-shape)
models are sentinel-only because forced tool-use does not take on that gateway path (see
"Backend fallback chain (BF-19)" → "OpenCode Zen backend" below for the same shape applied
to zen). Which protocol a given *instance* speaks depends on which model it was
constructed with (`_PROTOCOL: dict[str, Literal["chat","messages"]]`), so
`supports_structured_output` is set as an **instance attribute in `__init__`**, not read
off the class. `stages.py::_structured_schema_for(backend, stage)` reads it off the
backend *instance* it was handed, which is what makes this safe — **a class-level read
(`OpenCodeGoBackend.supports_structured_output` / `cls.supports_structured_output`) would
silently see the inherited `OpenAICompatBackend` default (`True`) and be wrong** for a
`/messages`-model instance. Before adding any new call site that touches this flag, grep
for a class-level read first; do not "fix" this back to a ClassVar as a simplification —
that would silently re-enable prompt-injected JSON on a gateway path proven not to honor
it.

**Turn models and schema.** `jsa/schema/turn_models.py` holds one flat, non-nullable
Pydantic model per stage — `FitVerdict{verdict, reason}` (both fields required, `reason`
has no default: a bare verdict with no justification is a schema violation, not just weak
prose), `CvTurn{kind, question, payload}`, `ClTurn{kind, question, payload}` — each
`ConfigDict(extra="forbid")` with a cross-field validator enforcing payload-iff-`kind
== "final"` / question-iff-`kind == "question"`. `STAGE_TURN_MODELS` maps `Stage` to its
model; `json_schema_for(stage)` emits the strict-schema-clean JSON (`additionalProperties:
false`, every property in `required`) that both backends embed verbatim in their request.
`parse_structured_reply`/`parse_structured_reply_for_schema` do `json.loads` + `kind`
routing ONLY — all semantic/cross-field validation stays stage-side in
`_validate_final_content`, so the existing self-heal correction budget keeps working
unmodified for both modes.

**Session mode, not just capability.** A structured-*capable* backend is not always
running in structured *mode* for a given session — OpenCode Zen can downgrade
mid-session (below). `stages.py` computes `schema = _structured_schema_for(backend,
stage)` once per invocation and threads `structured = schema is not None` through prompt
assembly, `adapt_history`, and the `structured_schema=` kwarg passed to
`start_session`/`restore_session` (never to `send_message` — both backends resolve it
from the handle when omitted). The **fit stage resolves its own mode from the actual
fit-backend instance** (which may differ from the pipeline's general-purpose backend via
`--fit-model`/`fit_model`, see "Separate fit-assessment model" below) — a `google-cli` fit
override always stays sentinel-mode regardless of what the rest of the pipeline is doing.

**Prompt assembly.** `jsa/pipeline/prompt_assembly.py::assemble_system_prompt(prompt_text,
*, language, structured_model=None, fit_verdict=False)` is the single composition root for
ALL runtime prompt mutation (language directive + structured-output contract) — it
replaced the old `_with_language_directive` at both call sites in `stages.py`. Structured
sessions get an appended contract section (the stage's schema, explicit precedence over
the prompt file's sentinel-format section, and a line against embedding sentinel markers
inside JSON string values); prompt *files themselves* are never edited to describe
structured mode, honoring the "prompt files are edited externally by the user" rule above.

**`question` must be self-contained.** On a `kind: "question"` turn, `question` is the
*only* field the user ever sees — `payload` is required to be `null` there, so there is no
schema slot for anything else. When a prompt file asks the model to write something up
before asking for confirmation (e.g. `PROMPT_CDADJUST.md`'s Phase 1 adjustment strategy),
the contract explicitly instructs the model to put that full write-up inside `question`
itself, followed by the actual question — not just a bare confirmation prompt. Without this
instruction the reply is still schema-valid (nothing enforces that `question` contains
anything beyond a short string), so a weaker model happily returns e.g.
`{"kind":"question","question":"Shall I proceed with the proposed adjustment strategy, or
would you like to adjust anything?","payload":null}` with the entire strategy silently
never generated — confirmed live against two `cv_adjust` jobs parked on
`opencode-zen`/`nemotron-3.5-lightning-free` with exactly that reply and nothing else in
the assistant `Message` row. This is backend-agnostic (any structured-capable backend can
hit it, not just opencode-zen/opencode-go) and is not a code bug — `_route_structured_data`
correctly read `payload: null` as intended; the model never wrote the content anywhere.
Do not "simplify" this instruction back out of `_structured_contract`'s `shape_rules` as
redundant verbosity — it is the fix, not padding.

**Anthropic: forced tool-use.** `AnthropicAPIBackend` (`jsa/agents/anthropic_api.py`)
implements structured mode as a forced tool call — `tools=[{"name": "respond",
"input_schema": schema}]` + `tool_choice` forced — not the SDK's native
`output_config`/`response_format` surface (confirmed unusable: it mandates recursive
`additionalProperties: false`, which the turn models' nested `$defs`, e.g. `CVDocument`'s
sections/entries, don't satisfy). `stop_reason == "max_tokens"` under forced tool-use is a
`ProtocolError("structured reply truncated")`, not a silent empty reply.

**OpenCode Zen: attempt structured, per-session downgrade.**
`OpenCodeZenSessionHandle.structured_enabled` starts `True` whenever a schema was actually
supplied, and flips to `False` — **for the rest of that session only, never persisted to
the DB** — the first time `parse_structured_reply_for_schema` fails to parse a reply as
valid JSON with a recognized `kind` (semantic/validation failures do NOT downgrade). Once
downgraded, the handle falls through to the pre-existing sentinel nudge
(`_parse_with_nudge`) with a mode-conditional nudge string, and every subsequent POST in
that session omits `response_format`. A restart re-attempts structured mode from scratch —
the downgrade flag is scoped to session flakiness, not a durable verdict on the model. The
downgrade decision is made strictly outside `_call_api`'s existing `_MAX_ATTEMPTS` transient
retry loop (see "OpenCode Zen backend" above): a malformed structured reply downgrades
after exactly one API call, and a transient/quota/timeout failure never touches the
downgrade flag either way. **`response_format` is sent with `"strict": false`, deliberately —
do not "fix" this to `true`.** `json_schema_for(stage)`'s top-level union model IS
strict-schema-clean (`additionalProperties: false`, all fields `required`), but its nested
`$defs` (`CVDocument`/`Contact`/`Section`/`Entry`/`CoverLetter`, pulled in from
`jsa/schema/cv.py`/`cover_letter.py`) are `extra="ignore"` with defaulted optional fields
and are NOT recursively strict — the same reason Anthropic's native `output_config` surface
was rejected in favor of forced tool-use (see below). `strict: true` would risk a wire-level
rejection from a strict-schema-enforcing proxy for no benefit: the per-session downgrade
above already covers a model that ignores the schema outright.

**Canonical-form invariant — the DB never stores provider wire format.** `Message` rows
always persist normalized union-JSON plain text (or, in sentinel mode, the sentinel-wrapped
text exactly as today) — never a raw `tool_use` block, never an OpenCode Zen envelope. On
replay, each row's own format is detected **by content, not by a persisted session-mode
flag** (there isn't one — the zen downgrade flag above is explicitly never persisted, so a
resumed/rebuilt session has no reliable "this session's mode was X" to consult): a row
starting with `<<<` is sentinel-wrapped, a row starting with `{` is canonical structured
JSON. `jsa/schema/turn_models.py`'s `wrap_canonical_for_sentinel` /
`unwrap_sentinel_to_canonical` / `adapt_history(history, *, structured: bool)` convert each
row to match the *destination* session's actual mode at every `restore_session` call site
in `stages.py` — fresh/resume `cv_adjust` and `cover_letter`, and both the
fresh-revision and mid-revision-resume branches of `revising_cv`/`revising_cl` (the
revision path matters because `backend_switch_reset` does NOT delete the original stage's
Messages on a revision-stage BF-19 switch — see `jsa/db/repo.py`'s docstring — so a
structured-mode `cv_adjust` history is a real, reachable replay target for a sentinel-only
backend after a fallback switch). **The `structured_schema=` kwarg and the
`adapt_history(structured=...)` flag at any one call site must always be computed from the
same single destination-mode decision** — passing one without the other replays
cross-format rows unadapted into the wrong-mode session.

**BF-19 interaction.** A structured-mode `ProtocolError` raised by `run_stage` (fresh-session
or resumed) is **not** an immediate hard fail — it spends the same
`MAX_FINAL_CORRECTIONS`-budget self-heal mechanism sentinel-mode failures already get,
via mode-aware correction text (no sentinel markers mentioned). Only once that budget is
exhausted does it propagate to `_advance_backend_or_fail` like any other stage failure —
see "Backend fallback chain (BF-19)" below. Two different recovery shapes exist depending
on whether a session handle already exists: a fresh-session failure (no handle yet) retries
by re-issuing the *whole* `start_session` call unmodified; a resumed/revision failure (handle
exists) sends a corrective follow-up turn instead. `_run_fit_assessment`'s
`ProtocolError`-only catch (see "Backend fallback chain (BF-19)" below) still applies
unchanged to structured mode — a truncated or unparseable fit reply still fails closed to
the `unfit` modal in one shot, it does not get the multi-turn correction budget fit stage
never had.

**Observability.** Every stage invocation logs a `LogEvent` naming its active mode:
`structured`, `sentinel`, or `sentinel (downgraded)`.

**Equivalence invariant (parity gate).** `tests/backend/test_mode_parity.py` asserts that
for the same logical payload, a sentinel-mode reply and a structured-mode reply produce
identical `Document.structured`, `Document.markdown`, `Job.fit_reason` + state, and
`FollowUp.question` text. This is a **permanent** regression gate, not a one-time migration
check — the sentinel path is not going away for CLI backends, so the two paths must keep
agreeing on observable output indefinitely. (Raw `Message` text is explicitly out of scope
for this equivalence — `wrap_canonical_for_sentinel` emits compact JSON where a real
sentinel-mode reply is usually pretty-printed; that's a cosmetic difference, not a bug.)

---

## State transitions

**Never set `Job.state` or `Job.current_stage` directly.** Always go through:
```python
from jsa.pipeline.state_machine import transition
transition(job, new_state, new_stage=None)  # raises InvalidTransition on violation
```
The allowed-transitions table is the single source of truth in `jsa/pipeline/state_machine.py`.

---

## Checkpoint rule

Every write that changes job state must be a **single atomic DB transaction** via:
```python
await repo.checkpoint(session, job, new_state, new_stage, messages=[], document=None, follow_up=None)
```
Never write `Message` rows, `Document` rows, and `Job` state in separate commits.

---

## Agent backend registration

New backends are registered in `jsa/agents/registry.py` by adding an entry to the `_REGISTRY` dict. The key is the CLI-flag string (e.g., `"claude-cli"`, `"google-cli"`, `"anthropic"`). Backends must subclass `AgentBackend` and implement all four abstract methods.

---

## Model selection (per-backend, runtime)

Which model each registered backend runs is selectable at runtime — no restart — through
`Settings.backend_models: dict[str, str]` (`jsa/config.py`), a **global, persisted, live**
mapping of backend name → chosen model ID. This is additive on top of each backend's
existing flat scalar default (`Settings.model`, `Settings.opencode_zen_model`, etc.) and
their `JSA_*_MODEL` env vars — neither of those was touched or renamed.

**Precedence**, resolved inside `server.py::make_backend_factory`'s `_model_for(name,
default)` closure on every single dispatch (not cached): explicit `model_override` (the
fit-gate's `--fit-model`, when set) > `settings.backend_models[name]` (a runtime UI
selection) > the backend's flat per-backend field default. **Why the fit factory needs no
extra wiring:** `server.py` evaluates `model_override=settings.fit_model` once at startup;
when `fit_model` is `None` (the default) that override is `None`, so the fit factory falls
through to the same `_model_for` read as every other stage and picks up runtime UI changes
too. Only an explicit `--fit-model` pins the fit gate independently of the dropdown. Do not
"fix" this by making the override lazy — it already is, by construction.

**Persistence.** `jsa/store/backend_models.py` (`BackendModels{selected, catalog}`)
mirrors `jsa/store/preferences.py`'s pattern exactly (sync read/write wrapped in
`asyncio.to_thread`), saved to `Settings.backend_models_path`
(`db_path.parent / "backend_models.json"`, so tests using an isolated `db_path` stay
self-isolated). `server.py`'s startup event hydrates `settings.backend_models` from this
file **before** the factory is built, so a restart doesn't lose a prior selection.
`catalog` holds only *user* overrides on top of the code-shipped
`jsa.agents.model_catalog.DEFAULT_CATALOG` — a new default model shipped in code reaches
every user without them editing their JSON.

**API.** `jsa/api/routes_backend_models.py`: `GET /api/backend-models` (cheap, no network —
current selections + `supports_model_selection` per backend, `google-cli` → `false`,
since `GoogleCliBackend.__init__` takes no `model` kwarg at all — the `agy` CLI has no
model flag, so a selection UI for it would be meaningless, not merely empty); `GET
/api/backend-models/{backend}` (lazy — called when a UI submenu opens — live listing where
`jsa/agents/model_catalog.py::list_models` has a fetcher for that backend, catalog fallback
otherwise, **any fetch failure is a 200 with `source: "catalog"`, never a 500**); `PUT
/api/backend-models` (validates registry-membership and `SUPPORTS_MODEL_SELECTION`, persists,
and mutates `request.app.state.settings.backend_models[backend]` in place so the very next
dispatch sees it, not just a future restart).

**Deliberately NOT on `/api/config`.** That endpoint is on the frontend's boot path and is
raced against an 8s timeout in `store.hydrateLanguage` — a provider model-listing fetch
must never be allowed to hang off it. Model selection has its own dedicated endpoints
instead, fetched lazily by the header dropdown only when a submenu actually opens.

---

## Backend fallback chain (BF-19)

`Settings.backends` (`jsa/config.py`) is an **ordered list**, not a single value —
`backends[0]` is the primary and the rest form a fallback chain. CLI: `--backends a,b,c`
sets the ordered chain; `--backend x` is a **backward-compat alias** for a single-item
chain, and `--backends` takes precedence when both are given (`jsa/cli.py`). Each `Job`
tracks its active backend in the `Job.backend_name` column; the orchestrator assigns
`backends[0]` on first dispatch.

When a backend raises `AgentLimitReached` (e.g. `jsa/agents/claude_cli.py` on a quota/rate
signal), `AgentTimeout` (e.g. `jsa/agents/opencode_zen.py`'s `httpx.TimeoutException`
handler, or any CLI backend's `run_killable` timeout), **or `AgentBackendUnavailable`**
(`jsa/agents/base.py` — a non-timeout, non-quota failure where retrying the SAME backend
won't help: a bad model/config, an auth error, or a transient overload/gateway failure that
already exhausted its own in-backend retry budget — see "OpenCode Zen backend" below),
`Orchestrator._run_one` (`jsa/pipeline/orchestrator.py`) routes all three into the shared
`_advance_backend_or_fail` helper — **advances the job to the next backend in the chain**
via `repo.backend_switch_reset` (resets to the failed stage, preserving the checkpoint),
emitting a `BackendSwitchedEvent`. Only when the chain is **exhausted** is the job
`mark_failed`'d, with a message specific to which of the three tripped: "Backend limit
reached — switch backends or wait for quota reset", "Backend timed out on every configured
backend — switch backends or increase the timeout", or "Backend unavailable on every
configured backend — check model/API key configuration, or try again later if this was
transient overload". Do not treat any of the three as a hard job failure; that is the
chain's job. (`_handle_limit_reached` / `_handle_backend_timeout` /
`_handle_backend_unavailable` are thin wrappers over `_advance_backend_or_fail` that only
differ in these message strings — before the timeout fix, `AgentTimeout` had no wrapper at
all and fell straight into `_run_one`'s generic `except Exception`, hard-failing the job on
the very first backend even with a working fallback configured in `--backends`.)

**Structured-mode note:** a `ProtocolError` from a structured session is not routed
straight into this chain the way `AgentLimitReached`/`AgentTimeout`/
`AgentBackendUnavailable` are — it first spends the self-heal correction budget (mode-aware
wording, no sentinel mentioned) inside `run_stage`, and only reaches BF-19 once that budget
is exhausted. See "Structured output (API backends)" above for the full mechanism; this
does not change anything about the three exception types this section covers.

**`_run_fit_assessment` (`jsa/pipeline/stages.py`) catches `ProtocolError` ONLY —
`AgentTimeout`, `AgentLimitReached`, and `AgentBackendUnavailable` must propagate through
it.** `fit_assessment` is the
FIRST stage every job hits, on the same backend/timeout as every other stage. A timeout or
quota signal there means the backend didn't answer — it is not the model saying "not a fit".
Catching it here and parking the job at `unfit` would fabricate a verdict about the user's
application from what is actually a transport failure, AND would mean the fit gate never
lets BF-19 try the next configured backend — a chain like `--backends opencode-zen,claude-cli`
would silently never reach claude-cli whenever opencode-zen was slow, because every job dies
at the very first stage before the chain logic downstream ever runs. `ProtocolError` (a
malformed/sentinel-less reply the model actually sent) is the only case that still fails to
the `unfit` modal — that is a real "unparseable answer", not an absent one. Do not widen this
except clause back to include `AgentTimeout`/`AgentLimitReached` as a "simplification"; that
was the exact shape of a real bug (see `tests/backend/test_fit_assessment.py`'s
`test_agent_timeout_propagates_for_bf19_not_swallowed_to_unfit` and
`test_timeout_from_fit_backend_switches_to_next_backend_not_unfit`).

One real, separate interaction to be aware of (not a bug): a `cv_adjust` failure's BF-19
rewind target is `pending` (`repo.backend_switch_reset`'s `failed_stage` mapping, docstring
in `jsa/db/repo.py`), so after a `cv_adjust`-stage switch the retry re-enters
`fit_assessment` on the new backend before reaching `cv_adjust` again — an extra fit turn,
by design, not a loop.

### Model ladder (tried before the backend advance)

`AgentTimeout` and `AgentBackendUnavailable` — **never `AgentLimitReached`** — first try the
next model on the SAME backend before `_advance_backend_or_fail`
(`jsa/pipeline/orchestrator.py`) falls through to the backend advance above. Rationale: a
busy-but-alive model is not the same failure as an exhausted account. A different model on
the same account is still a different upstream constraint for a timeout/availability
failure, so it's worth trying before burning a whole backend hop — but a 429/quota signal is
an account-scoped constraint that a different model on the *same* account does nothing for,
so `AgentLimitReached` skips the ladder entirely and always advances the backend, as before
this feature existed.

- **A hop is a job-level rewind, not an in-backend retry.** It calls
  `repo.backend_switch_reset` with the SAME backend name (a legal, no-op `backend_name`
  reassignment) and `new_model_name=<next rung>` / `increment_model_hops=True` — the exact
  mechanism the backend advance already uses, just without changing `backend_name`. This
  reuses that function's existing Message/FollowUp cleanup and fresh-session forcing
  (`cv_session_id`/`cl_session_id` cleared), which is required, not incidental: the
  `opencode-go` backend's ladder can cross its `chat`/`messages` protocol boundary (see "The
  four multi-backend-model-select backends" below), where `supports_structured_output`
  differs per model — only a fresh session makes that switch safe. `ModelSwitchedEvent`
  (`jsa/events/schema.py`) is emitted instead of `BackendSwitchedEvent`, and the hop returns
  without also advancing the backend in the same call.
- **Curated catalog, not the live listing.** The ladder reads `model_ladder(backend)` —
  `model_catalog.merged_catalog` filtered through `model_costs.cost_ordered` — never
  `list_models`. A failure path must not depend on a network call to the provider that is
  already failing.
- **Entry point is the UI-selected model**, not the catalog's cheapest rung:
  `job.model_name` if the job has already hopped, else `model_resolver(backend)` (the same
  `settings.backend_models[name]`-else-flat-default precedence `make_backend_factory` uses —
  see "Model selection (per-backend, runtime)" above). Starting every job at the global
  cheapest would silently override the header dropdown.
- **`google-cli` is exempt** — `SUPPORTS_MODEL_SELECTION["google-cli"]` is `False` (the `agy`
  CLI has no model flag), so `_resolve_model_hop` returns `None` immediately and every
  `google-cli` failure goes straight to the backend advance, same as pre-ladder behavior.
- **Two brakes, both must pass, in `Orchestrator._resolve_model_hop`:**
  - **Hop cap** — `job.model_hops < 5`. A count cap, not a cost cap: some backends (e.g.
    `opencode-go`) have 20+ rungs, so an unbounded ladder could re-ask the user's question
    that many times.
  - **Answered-input brake** — if the job has ANY answered `FollowUp` (checked **job-wide**,
    not scoped to the currently-failing stage) and `job.model_hops > 0`, no further hop is
    allowed. Job-wide + counting **hops** (not `model_name is not None`) is deliberate, not
    an oversight — a stage-scoped or `model_name`-based check is fooled two ways: (a) a
    `revising_*` reset deletes ALL FollowUps (including answered ones) for that stage as
    part of taking the hop itself, so a stage-scoped recheck afterward would see nothing and
    wrongly allow another hop; (b) a `cv_adjust` rewind lands the *next* failure at
    `fit_assessment`, a stage that never has FollowUps at all, so a stage-scoped check there
    would also miss the still-present answered FollowUp from the earlier stage. Counting
    hops closes both gaps: a job that has already spent its one post-answer hop stays capped
    regardless of which stage or reset touched the FollowUp rows.
  - **Pinned-fit escape** — if `failed_stage == fit_assessment` and `--fit-model` is pinned
    (`Orchestrator._fit_model_pinned`), skip the ladder and advance the backend immediately.
    Without this, the pinned fit model keeps failing, the ladder hops the *general* pipeline
    model instead (wrong target), a `cv_adjust` rewind re-enters `fit_assessment` on the same
    still-failing pinned model, and the whole ladder gets spent for zero forward progress.
- **Fail-safe.** `_resolve_model_hop` raising for any reason is caught by
  `_advance_backend_or_fail` and treated as "no hop" — falls through to the backend advance
  — so a ladder bug degrades to pre-ladder behavior instead of leaving the job stuck
  `running` forever (nothing in `list_runnable_jobs` recovers a `running` job, and
  `_run_one`'s `finally` would already have released the semaphore).
- **Chain exhaustion message.** If the job hopped models on its final backend before the
  chain ran out, `mark_failed`'s message appends `"(also tried N model(s) on <backend>)"` —
  only when `job.model_hops > 0`, so a no-ladder-configured job's message stays
  byte-identical to what it was before this feature.
- **Why this doesn't contradict "don't blindly repeat an expensive multi-minute call"** (see
  "OpenCode Zen backend"'s in-backend retry-loop rationale below): that rule is about
  repeating the *identical* request to the *identical* upstream. A ladder hop is a
  *different* model — usually a different upstream — so it is not the same trade being
  re-litigated.
- **Job-row display, one-way.** `GET /api/jobs` exposes `model_name` (raw, null until the
  first hop) and `effective_model` (`model_name` if set, else `model_resolver(backend_name)`
  — computed in `jsa/api/routes_jobs.py::_job_to_dict` via the SAME `make_model_resolver`
  closure the orchestrator uses, exposed at `app.state.model_resolver`). The frontend renders
  `effective_model` on the job row only (`JobList.tsx`) — **never** into Header's
  runtime-selection dropdown, which must keep meaning "your global selection", not "what this
  one hopped job happens to be running." `model_name` is never seeded at dispatch time —
  seeding it would corrupt the hop-cap/answered-input-brake accounting above, which relies on
  `model_name is None` meaning "never hopped."

### OpenCode Zen backend

`opencode-zen` (`jsa/agents/opencode_zen.py`) is an HTTP backend, not a CLI one — it POSTs
to `https://opencode.ai/zen/v1/chat/completions`, an **OpenAI-compatible** chat-completions
endpoint (not the Anthropic Messages API shape `anthropic_api.py` uses): the system prompt
is the first `{"role": "system", ...}` entry in `messages`, not a top-level `system` kwarg,
and the reply lives at `body["choices"][0]["message"]["content"]`. Auth is
`Authorization: Bearer $OPENCODE_API_KEY`, read from the environment at call time (never
hardcoded) — same convention as `ANTHROPIC_API_KEY`; this project loads no `.env` file
itself, so the caller must export it first.

Its model catalog is unrelated to JSA's Claude-only `model` setting (it proxies Claude,
GPT, Gemini and various free-tier models — e.g. the default `nemotron-3-ultra-free`), so
it gets **dedicated** settings, `opencode_zen_model` / `opencode_zen_timeout`
(`JSA_OPENCODE_ZEN_MODEL` / `JSA_OPENCODE_ZEN_TIMEOUT`), rather than sharing `model` /
falling back to `settings.model` the way `claude-cli` does.

**The API can return `HTTP 200` with an error payload in the body** (observed live: a
transient upstream 502 from the underlying provider surfaces as `{"error": {"type":
"server_error", ...}}` with `response.status_code == 200`). `_call_api_once` therefore
checks for an `"error"` key in the parsed body regardless of status code, not just `>= 400`
/ `429` — do not "simplify" this to a plain status-code check, it will silently swallow
these.

**Three-way error classification (`_call_api_once`, wrapped by `_call_api`'s retry loop).**
The free `nemotron-3-ultra-free` model is genuinely flaky under upstream load — intermittent
5xx gateway errors, a `{"error": {"type": "server_error"}}` envelope, or a null message
content are all things this specific backend is known for, and the right response is to
retry the SAME backend a couple of times before giving up on it, not to switch backends on
the first hiccup:

1. **Quota/rate** — `"rate"` / `"credit"` in the error message, `err_type` in
   `{"RateLimitError", "CreditsError"}`, or HTTP `429` → `AgentLimitReached`, immediately,
   no retry. Unchanged from before.
2. **Transient overload/gateway** — a structured JSON error body at any status `< 400`
   (the live-observed transient-upstream-502 shape is `{"error": {"type": "server_error"}}`
   at HTTP `200`) or `>= 500`, a non-JSON body at `>= 500`, or a null `message.content` →
   raises the module-private `_TransientOpenCodeError`, which `_call_api`'s loop retries up
   to `_MAX_ATTEMPTS` (3, with a `1.0s`/`3.0s` backoff) on the *same* backend before
   converting the final failure into `AgentBackendUnavailable`. This is "handled as
   gracefully as possible" per the user's framing — the backend gets its own retries before
   BF-19 gives up on it. **Deliberately not keyed on a guessed set of `err_type` strings**
   (e.g. `"overloaded_error"`) — the only *confirmed* live shape is the 200-status
   `server_error` envelope, so anything outside a real `4xx` defaults to transient rather
   than risking an unrecognized-but-actually-transient error type silently skipping retries.
3. **Permanent config/client error** — a structured JSON error body at a real `4xx` status
   (e.g. an `invalid_request_error` for a bad model name), or a non-JSON `4xx` →
   `AgentBackendUnavailable` immediately, with **no retry** — a bad `--opencode-zen-model`
   or a malformed request will never succeed by calling the same backend again, so retrying
   would only burn `_MAX_ATTEMPTS` × timeout of wall-clock time before reaching the same
   conclusion BF-19 could have reached immediately.

`AgentBackendUnavailable` (both from (2) exhausting its retries and from (3)) still joins
the BF-19 fallback chain exactly like `AgentLimitReached`/`AgentTimeout` — see "Backend
fallback chain (BF-19)" above. `AgentTimeout` (an `httpx.TimeoutException`, i.e. the request
already burned a full `self._timeout`) is deliberately **not** part of the in-backend retry
loop — repeating an expensive multi-minute call blindly before even trying the next backend
would be a poor trade; it keeps its own separate, unretried BF-19 path. Do not fold `429` /
quota signals into the retry loop either — those are `_TransientOpenCodeError`'s opposite
case, a signal that this backend specifically cannot serve the request right now, not "try
again in a second."

Regression coverage: `tests/backend/test_opencode_zen.py`'s
`TestCallApiRetryClassification` (retries-then-succeeds, retries-exhausted, and each
immediate/no-retry case) and `tests/backend/test_bf18_limit_detection.py`'s
`TestOrchestratorBackendUnavailableDetection` (the orchestrator-level BF-19 switch/exhaust
behavior). Do not "simplify" the three-way split back into a single `RuntimeError`/
`AgentLimitReached` pair — that was the exact shape of the reported bug: a transient
overload or a bad model both used to raise a plain `RuntimeError` that `_run_one`'s generic
`except Exception` hard-failed on the very first backend, with `--backends
opencode-zen,claude-cli` never trying `claude-cli`.

**Sentinel-compliance nudge is a different concern and is in scope.** The same free model
also, independently of any transport error, sometimes answers in full, well-formed prose —
including asking exactly the NEED_INPUT-shaped question the prompt wants — and simply never
wraps it in `<<<...>>>...<<<END>>>` (`finish_reason: "stop"`, confirmed live against
production payloads; not truncation). `OpenCodeZenBackend._parse_with_nudge` catches
`parse_reply`'s `ProtocolError("no sentinel block")` specifically and replays the
conversation plus one correction turn before giving up — this mirrors
`ClaudeCliBackend._parse_with_nudge` (`jsa/agents/claude_cli.py`) exactly, including the
same "propagate any other ProtocolError, or a second failure, immediately" rule. Before this
existed, a single non-compliant reply from this backend was an *uncaught* `ProtocolError`
that `run_stage`'s generic exception handler turned straight into a hard `mark_failed("no
sentinel block")` on the job's very first turn — no BF-19 fallback-chain engagement, because
`ProtocolError` doesn't map to `AgentLimitReached`. Do not remove this nudge as a
"simplification"; it is the fix for that failure mode, not a violation of the paragraph
above.

### The four multi-backend-model-select backends (Mistral, OpenRouter, OpenCode-GO, Gemini)

`mistral`, `openrouter`, and `opencode-go`'s `/chat/completions` path are built on a shared
base, `jsa/agents/_openai_compat.py`'s `OpenAICompatBackend` — extracted from
`opencode_zen.py` but **deliberately not wired back into it** (that refactor is optional
and gated on `opencode_zen.py`'s 935-line test file passing unchanged; it was not
attempted, so `opencode_zen.py` keeps its own independent, behaviourally identical copy —
accept the duplication rather than "DRY-ing" the two together). The shared base copies
`opencode_zen.py`'s three-way classification and retry loop **verbatim**: quota/rate
(`429`, `"rate"`/`"credit"` in the message, or `err_type` in `{"RateLimitError",
"CreditsError"}`) → immediate `AgentLimitReached`; transient overload/gateway (any
structured JSON error body at status `<400` or `>=500`, a non-JSON body at `>=500`, or a
null `message.content`) → retried in-process up to `_MAX_ATTEMPTS` (3) then
`AgentBackendUnavailable`; a permanent `4xx` → immediate `AgentBackendUnavailable`, no
retry. `gemini` (`jsa/agents/gemini_api.py`) subclasses the same base but overrides only
`_call_api_once` — its wire shape (`{"error": {"code","message","status"}}`) has nothing in
common with an OpenAI-compatible body, so its three-way split is hand-adapted to that
envelope, not literally copied. **Do not build any of these four on
`jsa/agents/anthropic_api.py`'s pattern** — that backend has no `AgentBackendUnavailable`
path at all, so an auth error or a bad model name would propagate raw and hard-fail the job
on the very first backend instead of advancing the BF-19 chain (the exact bug class this
section exists to prevent). Regression coverage:
`tests/backend/test_openai_compat.py` (the shared base, exercised via `MistralBackend`),
`tests/backend/test_gemini_api.py`, and
`tests/backend/test_bf18_limit_detection.py::TestOpenAICompatibleNewBackendsClassification`
/ `::TestGeminiClassification` (one parametrized pass over all four confirming each
concrete subclass — not just the shared base — actually raises the right BF-19-recognized
exception type).

**OpenRouter's `provider.require_parameters` routing guard is mandatory, not optional.**
`OpenRouterBackend._extra_payload()` adds `{"provider": {"require_parameters": True}}` to
every request. Without it, OpenRouter may silently route a structured request to an
upstream endpoint that ignores `response_format` entirely — the model then answers in
free-form prose instead of JSON, which this project's per-session structured→sentinel
downgrade cannot distinguish from any other unparseable reply, so it just downgrades on
every single turn instead of failing loudly once. **Removing this flag is a silent
regression, not a simplification** — nothing else in this codebase would catch its absence
except `tests/backend/test_openrouter.py`'s dedicated payload-shape assertion, which exists
specifically because a missing guard produces no error, just quietly worse behavior.

**`opencode-go` spans two wire protocols under one backend name** — see "Structured output
(API backends)" above for the instance-level `supports_structured_output` this requires.
`/messages` models use a hand-built Anthropic Messages-shape request (`x-api-key` auth, a
top-level `system` field, no `response_format` ever sent) with its own three-way
classification adapted to that envelope shape — built on raw `httpx`, not the `anthropic`
SDK, for the same `AgentBackendUnavailable`-path reason called out above.

**Env-var fallback chains** (all read at call time, never cached, never loaded from a
`.env` file — this project still loads no `.env`, same as every existing backend):
`MISTRAL_API_KEY`; `OPENROUTER_API_KEY`; `GEMINI_API_KEY` → `GOOGLE_API_KEY`;
`OPENCODE_GO_API_KEY` → `OPENCODE_API_KEY` (the existing `opencode-zen` backend keeps using
`OPENCODE_API_KEY` on its own, untouched — `opencode-go` merely falls back to the same
variable if its own is unset, it does not replace it).

---

## Prompt files

- Location: `jsa/prompts/PROMPT_CDADJUST.md`, `jsa/prompts/CVL_PROMPT.md`, and `jsa/prompts/PROMPT_FIT_ASSESSMENT.md`
- Loaded by `jsa/prompts/loader.py` → `read_prompt(name)` — **no caching**, always reads from disk
- Edited externally by the user — never programmatically overwritten
- During development, use the stubs (which already contain the sentinel grammar instructions)

---

## Fit-assessment gate

Every pending job first runs a one-shot `fit_assessment` stage (always on) before
`cv_adjust`. The agent returns a single `<<<FINAL>>>` whose **first line is `FIT` or
`UNFIT`** (the rest is a brief reason — never `NEED_INPUT`). `FIT` → `fit_done`
(continues to cv_adjust); anything else (UNFIT, unclear, unparseable, or a question) →
`unfit`, with the reason stored in the `Job.fit_reason` column and surfaced as a centered
"not a fit" modal. **Verdict parsing fails *to* the modal (closed), never silently past
it.** The modal's buttons map to `POST /api/jobs/{id}/dismiss` (→ dismissed) and
`POST /api/jobs/{id}/ignore-fit` (→ fit_done → resume pipeline). `fit_done` is treated
exactly like `cv_done` in `list_runnable_jobs` and `_next_stage_for`. See ARCH.md →
"Fit-assessment gate".

### Separate fit-assessment model

The fit gate may run on a **different model** than the rest of the pipeline — it is a
cheap one-shot pre-check, so a smaller/faster model is usually enough. Two optional
settings drive it (`jsa/config.py`), both `None` by default, which makes the whole
feature **inert**: `fit_model` (`JSA_FIT_MODEL` / `--fit-model`) and `fit_timeout`
(`JSA_FIT_TIMEOUT` / `--fit-timeout`). When unset, the fit stage uses exactly the same
backend configuration as every other stage.

The mechanism is `make_backend_factory`'s (`jsa/server.py`) `model_override` /
`timeout_override` kwargs. **Do not derive the constructor arguments yourself at a new
call site** — the per-backend mapping is non-obvious (`anthropic` takes
`anthropic_timeout`, CLI backends take `agent_timeout`, `opencode-zen` takes
`opencode_zen_timeout` and never falls back to `settings.model` — see "OpenCode Zen
backend" above — and `google-cli` takes no `model` at all because the `agy` CLI has no
model flag, so `fit_model` is silently inapplicable there). Overriding without going
through the factory is what previously gave `anthropic` a 600s timeout instead of its
180s one.

Wiring: `server.py` builds a second factory and passes it to `Orchestrator` as
`fit_backend_factory`; the orchestrator instantiates it **from `job.backend_name`**
(not `backends[0]`, so the fit gate follows a BF-19 backend switch) and only for the
`fit_assessment` stage, handing the result to `run_stage(..., fit_backend=...)`.
`run_stage` falls back to `backend` when it is `None`, which is what every test that
omits it gets. **`jsa/pipeline/stages.py` must never import `jsa/server.py`** — server
imports the orchestrator, which imports stages, so that direction is a circular import
that breaks every entrypoint. The fit backend is *injected*, never constructed inside
the pipeline.

---

## CV structure — single source of truth

`cv_structure.json` (`jsa/store/cv_structure.py`, edited via the CV Structure Editor,
`Settings.cv_structure_path` — `~/.jsa/cv_structure.json` by default) is the **only**
source of *base* CV content for the pipeline — i.e. for the stages that haven't yet
produced their own tailored CV. Both `fit_assessment` and `cv_adjust` read it at stage
time (`jsa/pipeline/stages.py::run_stage`) and inject it into their prompts — `cv_adjust`
as the `BASE CV STRUCTURE` JSON skeleton, `fit_assessment` as `cv_to_markdown(structure)`
under a `CV:` header. Neither stage reads `Job.cv_text`. **`cover_letter` is the
exception**: it reads the approved, tailored `cv_adjust` Document instead (falling back to
this base structure only if that Document is somehow missing) — see "Two-lane pipeline /
CV gate" below. Do not "fix" the cover-letter lane back onto `cv_structure.json`; that
would defeat the two-lane split's entire point (writing the letter against what will
actually be submitted).

**`Job.cv_text` is DEPRECATED.** It is never populated (the CLI's `--cv` no longer
stamps it) and never read by any prompt. The column still exists only because
`jsa/db/engine.py`'s migration story is `create_all` + additive `ALTER TABLE` — there is
no column-drop path, and existing sqlite DBs have it `NOT NULL`. Do not read or write it
in new code; do not "fix" this by reintroducing a `CV TEXT:` block into a prompt.

**`--cv` is optional and bootstrap-only.** `jsa/cli.py::_bootstrap_cv_structure` seeds
`cv_structure.json` from `--cv` exactly once, if the file doesn't exist yet (via the same
`jsa/pipeline/infer_structure.py::run_infer` the editor's "infer" button uses). If a
structure already exists, `--cv` is ignored (a note is printed). If neither `--cv` nor a
saved structure exists, startup proceeds anyway — see the gate below.

**CV structure gate.** `Orchestrator.run()` (`jsa/pipeline/orchestrator.py`) checks
`cv_structure_path.exists()` at the top of every dispatch cycle when a path was given
(production always passes one via `server.py`; tests passing `cv_structure_path=None`
are exempt — the gate is inert for them). While the file is missing, the loop skips
dispatch entirely and jobs stay `pending` (never `failed`); a one-shot `LogEvent`
announces the block. `PUT /api/cv-structure` calls `orchestrator.kick()` on save, and
`GET /api/config`'s `cv_structure_exists` field drives the frontend's gate banner
(`frontend/src/components/JobList.tsx`) — so saving a structure in the editor unblocks
pending jobs live, no restart required.

---

## Two-lane pipeline / CV gate

The pipeline runs as two sequential lanes — CV, then cover letter — separated by a parked
gate state, `cv_review`, between them. `jsa/pipeline/state_machine.py` is the source of
truth for the transition table; the shape that matters for new code:

- `running(cv_adjust)` and `running(revising_cv)` land in `cv_review`, never in `cv_done`.
  `cv_review` is a genuine park: the user must call `approve-cv` or request a revision to
  leave it. A bare `cv_review` job (no unconsumed `RevisionRequest`) is **not** dispatched
  by the orchestrator.
- `cv_done` is **unchanged** from before the split: "CV approved, cover letter pending,
  runnable" — the orchestrator dispatches it straight into `cover_letter`. It is reached
  **only** via `POST /api/jobs/{id}/approve-cv` (`cv_review → cv_done`), never directly from
  a finishing `cv_adjust`/`revising_cv` run. `cv_done` also remains the **BF-19 backend-
  fallback rewind target**: when the `cover_letter` stage hits `AgentLimitReached`,
  `Orchestrator._handle_limit_reached` rewinds the job to `cv_done` (not `cv_review`) so the
  next backend re-enters `cover_letter` directly — the CV was already approved, there is
  nothing to re-review. Do not redirect this rewind to `cv_review`.
- A revision's landing state is decided by `RevisionRequest.origin_state`
  (`jsa/pipeline/stages.py`, the `revising_cv`/`revising_cl` branch): a `revising_cv`
  completion returns to `cv_review` if `origin_state == "cv_review"`, otherwise to `review`
  (also the fallback for legacy `NULL` rows). `revising_cl` always returns to `review` — there
  is no `cl_review` parked state; the cover letter has no gate of its own.
- The `cover_letter` stage's initial prompt is built against the **approved `cv_adjust`
  Document's rendered markdown** (`"TAILORED CV (approved by the user — write the letter
  against this)"` block, `jsa/pipeline/stages.py`), not `cv_structure.json` — the letter is
  written against what will actually be submitted. Falling back to the base structure only
  happens if no `cv_adjust` Document exists yet, which the CV gate makes unreachable in
  normal flow; that branch logs a warning if hit.
- `fit_done` is unaffected by any of this — it still parallels `cv_done` as "ready to be
  picked up for the next stage" (`cv_adjust`), and is still treated identically to `cv_done`
  in `list_runnable_jobs`/`_next_stage_for`. See "Fit-assessment gate" above.
- `cv-research` (`jsa/prompts/GEMINI_CV_RESEARCH.md`) and `cl-research`
  (`jsa/prompts/GEMINI_CL_RESEARCH.md`) are prompt files only — there is no wired agent
  backend that invokes `cv-research`; it is retained deliberately for a future CV-lane
  research pass, not dead code to delete. `cl-research` **is** wired (`_gather_research` in
  `jsa/pipeline/stages.py`, called for the `cover_letter` stage only).

---

## Testing conventions

- **Fakes over mocks.** Use `tests/backend/fakes/fake_backend.py` (`FakeAgentBackend`) for all pipeline and orchestrator tests. Do not `patch` or `MagicMock` internal functions.
- **FakeRenderer** should write a stub file (e.g., write `b"PDF"` to the output path) — do not skip the renderer call in tests.
- Integration tests (real Claude/Gemini/Anthropic API) live in `tests/backend/integration/`, are marked `@pytest.mark.integration`, and are **skipped by default in CI**.
- Backend tests: `pytest` + `pytest-asyncio` with `asyncio_mode = "auto"` in `pyproject.toml`.
- Frontend tests: `vitest` + `@testing-library/react`.
- Follow TDD: write tests before or alongside implementation, not after.

---

## Renderer invocation

Renderers (`WeasyPrintRenderer` for PDF, `DocxRenderer` for DOCX) fire at **three** points
in `jsa/pipeline/stages.py`, all via the shared `_render` helper:

1. **`_render_cv`** (CV-only, `stages=(Stage.cv_adjust,)`) — on entry to `cv_review`, i.e.
   `cv_adjust`'s first FINAL and every `revising_cv` completion that returns to `cv_review`.
   Only the CV is rendered here; there is no cover-letter Document yet.
2. **`_render_for_review`** (both artifacts, `stages=(Stage.cv_adjust, Stage.cover_letter)`)
   — on entry to `review`: `cover_letter`'s first FINAL, and every `revising_cv`/`revising_cl`
   completion whose `RevisionRequest.origin_state` was `review` (not `cv_review`).
3. Manual re-render of an already-approved job via `POST /api/jobs/{id}/export`.

`_render` itself skips any stage with no Document yet rather than erroring — this is what
lets step 1 render CV-only without special-casing the missing cover letter.

The frontend preview is a PDF `<iframe>` fed by `GET /api/files/{relpath}` (served
`Content-Disposition: inline`), not in-browser Markdown. Neither `POST /api/jobs/{id}/approve-cv`
(`cv_review → cv_done`) nor `POST /api/jobs/{id}/approve` (`review → approved`) does **any**
rendering — both are pure state transitions; the render already happened on entry to the
state they leave. Do not move rendering onto either approve endpoint, and do not treat the
CV-gate or review-entry pre-renders as a bug. See ARCH.md → "Renderer runs on review entry
(pre-render)" (predates the CV gate; the principle — render on park, not on approve — now
applies at both parking states).

---

## Concurrency — do not block the event loop

Any synchronous, CPU-bound, or blocking I/O call must be wrapped:
```python
await asyncio.to_thread(sync_function, *args)
```
This applies to: WeasyPrint, python-docx parsing, pypdf parsing, and any future sync library.

---

## Job identity and re-run semantics

- Job primary key: `sha1(f"{company}|{role}|{link}")[:16]`
- JD hash: `sha1(jd)[:16]`
- On re-run: `approved` + unchanged `jd_hash` → skip. `jd_hash` changed → reset to `pending`. `failed` → reset to `pending`. Everything else → resume from last checkpoint.

---

## Port and local-only

- Default port: `8765`. Override via `--port` flag or `JSA_PORT` env var.
- CORS is wide-open for `http://localhost:*`. This is intentional — single-user, local-only tool.
- No auth, no multi-user, no remote deployment ever intended for v1.

---

## Language preference

A single global setting (`jsa/store/preferences.py`, `GET`/`PUT /api/preferences`) drives
three things: (1) the LLM pipeline's output language for the CV/cover-letter JSON, its
clarifying questions, and its change-log; (2) the fit-assessment reason text; (3) the whole
frontend's UI chrome. The language catalog (`jsa/i18n/languages.py`) is the single source of
truth — served to the frontend via `/api/config`'s `languages` key; never hand-copy the list
into TypeScript.

**Pipeline directive.** `jsa/pipeline/stages.py::_with_language_directive` appends a language
directive to a **NEW** session's system prompt only (`start_session` call sites: fresh
cv_adjust/cover_letter, `_run_fit_assessment`) — never to a `restore_session` call (a resumed
session already committed to a language; re-injecting a changed directive would contradict
replayed history). The directive carves out two things that must always stay
English/ASCII: the `<<<FINAL>>>`/`<<<NEED_INPUT>>>`/`<<<END>>>` sentinels, and (for
`fit_assessment` only) the literal `FIT`/`UNFIT` verdict word matched by `_parse_fit_verdict`.
`jsa/schema/cv.py`'s `_not_a_cover_letter` guard is per-language
(`_LETTER_FORMULA_RE_BY_LANG`, selected via `ValidationInfo.context["language"]`) — when
adding a new language to the catalog that pipeline output may actually use, add its letter-
formula tuple too, or the guard silently falls back to English-only matching for that
language.

**Frontend i18n (UI string convention — applies to all future frontend work, not just this
feature).** Any new user-visible frontend string **must**:
1. Be added to `frontend/src/i18n/strings.en.json` with a stable dotted key
   (`"<component>.<label>"`, e.g. `"jobList.emptyState"`).
2. Be rendered via `const t = useT();` (`frontend/src/i18n/useT.ts`) — never a hardcoded
   English literal in JSX. `useT()` reads the global `language` store field and falls back to
   English, then the raw key, if a translation is missing.
3. Trigger a re-run of `scripts/translate-ui.sh` (incremental — only translates new/changed
   keys) before shipping, so generated locale catalogs (`strings.<code>.json`) stay in sync.
   Two backends, mirroring `jsa/agents/anthropic_api.py` vs `jsa/agents/claude_cli.py`:
   `--backend api` (default, needs `ANTHROPIC_API_KEY`) or `--backend cli` (shells out to the
   Claude Code CLI, no API key needed). Use `scripts/translate-ui.sh --check` in CI/pre-merge
   to catch a catalog that's fallen behind.

Do not externalize: CSS class names, `data-testid`, console/log strings, dates/numbers/IDs/
URLs, or HUD-style terminal abbreviations that are intentionally code-like (see
`frontend/src/theme/chrome.tsx`'s `StateMeta.code` vs `.label`).

---

## Prompt caching (HTTP API backends)

**The cross-job system-prefix invariant.** `assemble_system_prompt` (`jsa/pipeline/
prompt_assembly.py`) builds the system prompt from only the on-disk prompt file,
the language code, and the stage's JSON schema — **no per-job bytes** (JD, company,
role, CV structure, research brief) ever land in it; those go into the initial user
message (`_build_initial_user_msg`/`_build_fit_user_msg`). So every job at a given
(stage, language, structured-mode) sends a **byte-identical system prefix**, which is
what makes prompt caching worth doing here: cache the system prompt, not the message
tail. `tests/backend/test_prompt_prefix_stability.py` pins this two ways — same-input
determinism (including across `PYTHONHASHSEED` values, since schema generation must
never iterate a `set`) and cross-job identity (two jobs with different JD/company/role
produce the same system prompt and different user messages). Do not add a
message-tail cache breakpoint — the tail is separated by human answer latency
(`awaiting_input` → user answers), so it's usually cold, and it would collide with
`adapt_history`/`_parse_with_nudge` rewriting message content.

**Kill switch.** `Settings.prompt_caching: bool = True` (`JSA_PROMPT_CACHING`),
`--prompt-caching`/`--no-prompt-caching` (tri-state, only overrides when explicitly
passed). Forwarded via `make_backend_factory` to the `mistral`, `openrouter`,
`gemini`, `opencode-go`, and `anthropic` branches — never to `opencode-zen`,
`claude-cli`, or `google-cli`, which don't accept the kwarg (same no-unused-parameter
rule as `structured_schema`). With the switch off, every touched backend's payload
must be byte-identical to its pre-prompt-caching shape — this is a permanent parity
invariant, not a one-time migration check (see `tests/backend/
test_openai_compat.py::TestPromptCacheKey::
test_prompt_caching_false_is_byte_identical_to_pre_phase2_payload` and
`test_anthropic_api.py::TestSendMessageSystemPrompt::
test_prompt_caching_false_sends_bare_string`).

**Per-backend mechanism** (all six phases wired — `mistral`, `gemini`, `openrouter`,
`opencode-go`, and `anthropic`; `opencode-zen` is permanently out of scope, see
"Known non-caching cases" below):

| Backend | Mechanism | Cached-token field |
|---|---|---|
| `mistral` | top-level `prompt_cache_key` (`jsa/agents/mistral.py::_extra_payload`), a `sha1(system_prompt)[:16]` hash — a hash of the byte-identical prefix, not `job.id`, so it groups every job that shares that prefix. Confirmed live against Mistral's own `/v1/chat/completions` API reference (not just the separate Conversations API) that `prompt_cache_key` is a real top-level parameter on this endpoint — an unrecognized-field 4xx was considered and ruled out, so this backend has no degrade path. | `usage.prompt_tokens_details.cached_tokens` |
| `gemini` | **observability only, no request change.** Gemini's implicit caching is on by default for 2.5+ models given a stable prefix (`systemInstruction` first, growing `contents` after) — the existing request shape already satisfies it. `GeminiBackend._call_api_once` (`jsa/agents/gemini_api.py`) logs `body["usageMetadata"]["cachedContentTokenCount"]` at `logger.info` when present, `isinstance`-guarded the same way as Mistral's `usage` read. Deliberately does **not** add explicit `cachedContents` — that requires a separate create/manage lifecycle for a prefix implicit caching already covers. `Settings.prompt_caching=False` is a true no-op here — there is no field to omit. This makes `gemini` the one **permanent** no-op-on-the-wire backend among the six — see `tests/backend/test_prompt_caching_phase1.py::TestKillSwitchIsCurrentlyANoOpOnTheWire`. | `usageMetadata.cachedContentTokenCount` |
| `openrouter` | explicit `cache_control: {"type": "ephemeral"}` on the system text block (`OpenRouterBackend._system_content`, `jsa/agents/openrouter.py`) — required for OpenRouter's Anthropic/Qwen/Gemini upstreams to cache at all; harmless for upstreams that cache automatically. **Has a degrade-on-4xx path** — see below. | `usage.prompt_tokens_details.cached_tokens` |
| `opencode-go` `/chat` | Same `cache_control` breakpoint as OpenRouter, via the identical `_system_content` override (`OpenCodeGoBackend._system_content`, `jsa/agents/opencode_go.py`) — inherits the shared base's `_CacheRejected` degrade for free, no protocol-specific code needed. **Speculative**: this gateway documents no cache API (same caveat as its forced-tool-use gap, see the module docstring). | `usage.prompt_tokens_details.cached_tokens` |
| `opencode-go` `/messages` | Same breakpoint shape, hand-built (this protocol does not go through the shared `_call_api_once`/`_system_content` machinery at all — see the module docstring's dual-protocol split): `_call_messages_api_once` sends `"system": [{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}]` when `self._prompt_caching`, and `_call_messages_api` carries its own copy of the catch-`_CacheRejected`-and-retry-clean wrapper (imported from `_openai_compat.py`, reused rather than duplicated — only the wrapping code around it is protocol-specific). Also speculative, same caveat. | `usage.cache_read_input_tokens` / `usage.cache_creation_input_tokens` |
| `anthropic` | Documented, first-class `cache_control: {"type": "ephemeral"}` (default 5-minute TTL — **not** `ttl: "1h"`, see below) on a single system text block (`AnthropicAPIBackend._call_api`, `jsa/agents/anthropic_api.py`), sent unconditionally (no `require_parameters`-style routing risk — see "No degrade path" below). Render order is `tools → system → messages`, so this one breakpoint also covers the forced-tool schema in structured mode (see "Anthropic: forced tool-use" above) — no separate breakpoint on the tool definition is needed, and structured mode's `tool_choice`/tool list must stay byte-identical turn-to-turn within a session for that cache to hold (it already does — the schema is fixed per stage for a session's whole life). | `usage.cache_read_input_tokens` / `usage.cache_creation_input_tokens` |

**OpenRouter's degrade-on-4xx (Phase 4).** Sending `cache_control` is the one place
in this plan where the field itself could make `provider.require_parameters: true`
(the mandatory routing guard above) filter out every eligible provider, returning a
4xx — not "no caching", but "no eligible provider", which under the three-way
classification is an immediate, unretried `AgentBackendUnavailable` that would drop
`openrouter` out of the BF-19 chain over a caching-only rejection. The fix lives in
the *shared* base (`jsa/agents/_openai_compat.py`), not in `openrouter.py`, so any
future `_system_content` override gets it for free:
- `OpenAICompatBackend._system_content(self, system_prompt) -> str | list[dict]` is
  a hook, default returns `system_prompt` unchanged (byte-identical for Mistral,
  which never overrides it — its caching signal is the `_extra_payload` top-level
  key, not a system-content shape change). `_call_api_once` computes
  `cache_fields_present = isinstance(self._system_content(system_prompt), list)`
  once per call and uses that value to pick between `AgentBackendUnavailable` and
  the module-private `_CacheRejected` (a subclass of it) at all three permanent-4xx
  raise sites.
- `_CacheRejected` is caught in `_call_api`, **outside** `retry_transient`'s budget
  (mirroring the structured→sentinel downgrade's rule: a reshaped retry never
  shares a budget with retries of the identical request) — `self._prompt_caching`
  is flipped to `False` for the rest of that backend **instance's** life (not
  persisted), and the call is retried exactly once, clean. A second failure raises
  a plain `AgentBackendUnavailable`/whatever BF-19 recognizes, same as before this
  feature existed.
- Do not key the degrade off `self._prompt_caching` alone — it must be gated on
  `cache_fields_present` (the actual payload shape sent), so a subclass like
  Mistral that carries a top-level cache key but never changes `_system_content`
  never accidentally raises `_CacheRejected` and silently disables its own,
  separately-verified-safe caching mechanism on an unrelated 4xx.

**OpenCode-GO's speculative `cache_control` on both protocols (Phase 5).** This
gateway publishes cached-read/cached-write pricing but documents no cache API for
either wire shape, so both are informed guesses — there is direct precedent for
this gateway silently not honoring a documented Anthropic feature (forced tool-use
does not take on the `/messages` path either, see the module docstring), so a
degrade path is mandatory here, not optional:
- `/chat` reuses the Phase 4 mechanism verbatim: `OpenCodeGoBackend._system_content`
  overrides the shared hook exactly like `OpenRouterBackend` does, so it gets the
  `_CacheRejected` catch-and-retry-clean wrapper in `OpenAICompatBackend._call_api`
  for free — no protocol-specific degrade code needed.
- `/messages` does **not** go through `_call_api`/`_call_api_once` at all (it is a
  hand-built Anthropic-shape POST, see the module docstring's dual-protocol split),
  so it carries its own copy of the wrapper: `_call_messages_api` catches
  `_CacheRejected` (imported from `_openai_compat.py`, not redefined) outside
  `retry_transient`'s budget, flips `self._prompt_caching` off, and retries once
  clean — same shape as `_call_api`'s wrapper, just duplicated onto the
  protocol-specific call site. `_call_messages_api_once` raises `_CacheRejected`
  from exactly its **two** JSON-parsed permanent-4xx branches (the structured
  `{"type": "error", ...}` envelope branch and the generic `status_code >= 400`
  fallback) — **not** from the non-JSON-body branch, which stays a plain
  `AgentBackendUnavailable` unconditionally, since an unparseable body gives no
  reliable signal that the rejection was cache-related.
- Both protocols are gated by the same `Settings.prompt_caching` kill switch and
  the same per-instance `_prompt_caching` flag — a Phase-4-style
  `prompt_caching=False` case exists for both wire shapes in
  `tests/backend/test_opencode_go.py`, pinning the pre-Phase-5 bare-string shape.

**Anthropic: no degrade path needed (Phase 6).** Unlike OpenRouter/OpenCode-GO,
`cache_control` on the Claude API is a first-class, documented field, not a routing
hint that a gateway's `require_parameters`-style guard could reject — so there is no
`_CacheRejected`-equivalent here, no retry-on-4xx, and `self._prompt_caching` never
flips at runtime for this backend. `AnthropicAPIBackend.__init__` gains `prompt_caching:
bool = True`; when it's on, `_call_api` sends `system` as
`[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]`
instead of the bare string, for both sentinel and structured mode identically. The
repo's default model is `claude-haiku-4-5`, whose **minimum cacheable prefix is 4096
tokens** (verified against Anthropic's own prompt-caching reference: 512 on
Opus 5-tier, 1024 on Sonnet 5/Opus 4.8-tier, 4096 on Opus 4.6/Haiku 4.5 — the minimum
is **not monotonic** across model generations, so switching the configured model can
silently change whether a given prompt caches at all) — a `cv_adjust`/`cover_letter`
system prompt clears that easily; sentinel-mode `fit_assessment`'s prompt does not
(see "Known non-caching cases" below).

**Default 5-minute TTL, not `ttl: "1h"` — deliberately.** The 1-hour TTL's 2× write
premium only pays off when the start-to-start gap between requests sharing the prefix
is 5–60 minutes; the value here comes from job-to-job reuse under the orchestrator's
continuous dispatch loop, which is well under 5 minutes apart in normal operation, so
the plain 5-minute TTL is strictly cheaper. Do not add `ttl: "1h"` as a "safer" default
without re-deriving this from the actual dispatch cadence.

**No message-tail breakpoint, no top-level auto-`cache_control`** — same locked
decision as every other backend in this plan (see "Locked decisions" above): the
conversation tail is separated by human answer latency and would collide with
`adapt_history`/`_parse_with_nudge`'s history rewriting.

**Cached-token observability lives in `OpenAICompatBackend._call_api_once`**
(`jsa/agents/_openai_compat.py`) for the `/chat/completions` shape, and in
`OpenCodeGoBackend._call_messages_api_once` (`jsa/agents/opencode_go.py`) for the
`/messages` shape — both guarded with `isinstance(..., dict)` checks (not just
`.get` chains — an API returning `"usage": null` or a wrong-typed field must not
raise past a request that otherwise succeeded). Because the `/chat/completions`
logging lives in the *shared* base, `openrouter` and `opencode-go`'s `/chat`
protocol inherit it automatically — this is not extra code per backend. Anthropic's
equivalent is the module-level `_log_cache_usage(response)` helper in
`jsa/agents/anthropic_api.py`, called once after every `_call_api` response —
`isinstance`-guarded the same way (`response.usage` is a real SDK object here, not a
dict, so the guard is on the attribute *value* being an `int`, not on the container
being a `dict`).

**`_extra_payload` takes `system_prompt`.** Any override (currently only
`MistralBackend` and `OpenRouterBackend`) receives the assembled system prompt as an
argument — added specifically so a subclass can derive a cache key or breakpoint from
it. Do not revert this to a no-argument hook.

**Known non-caching cases (expected, not bugs).** Sentinel-mode `fit_assessment`'s
prompt (`PROMPT_FIT_ASSESSMENT.md`, ≈660 tokens, no schema appended) sits below every
provider's minimum cacheable-prefix size and will show zero cached tokens even once a
backend implements caching — this is not a broken implementation. `opencode-zen` is
deliberately out of scope (undocumented caching API, pre-existing backend, keeps its
own independent payload-builder copy per `_openai_compat.py`'s module docstring).
CLI backends (`claude-cli`, `google-cli`) manage their own caching and are not
applicable here.

---

## Reasoning / thinking stream (streaming backends)

The chat UI's REASONING card is fed by `AgentChunk(kind="reasoning", ...)`. Whether a
backend produces any is a **three-part** question — capability, opt-in, and field
convention — and all three have to line up. Getting one wrong looks identical from the
UI (a "Thinking…" placeholder that never fills in), so diagnose by naming which part
is missing, not by assuming "the model doesn't think".

**Streaming happens in BOTH modes.** `response_format`/`responseSchema` + `stream:
true` is a normal, supported combination on every wire shape here. In structured mode
`_openai_compat.py::_reasoning_only` (and its verbatim twin in `opencode_zen.py`)
wraps `on_chunk` so only `reasoning` chunks pass through — the `content` delta is raw
partial JSON and a stray `{` is not useful to render. **Do not "simplify" this back to
gating streaming on `structured_schema is None`.** That gate is exactly the bug this
section documents: `_structured_schema_for` returns a schema unconditionally for every
structured-capable backend, so such a gate is not a narrow special case — it disables
streaming outright, permanently, for every real pipeline call. It was fixed in
`_openai_compat.py`/`opencode_zen.py` first and survived undetected in
`gemini_api.py::_call_api_once` (which overrides the whole method and carried its own
copy) until the three affected backends were audited.

**Three field conventions on the `/chat/completions` shape**, all handled in the
shared `_openai_compat.py::_reasoning_delta_text` / `_split_content_delta`:

| Convention | Who | Shape |
|---|---|---|
| `delta.reasoning_content` | `opencode-go`'s routed models, `opencode-zen` | plain string |
| `delta.reasoning` / `delta.reasoning_details` | `openrouter` | legacy string / array of objects — OpenRouter sends **both for the same tokens**, so the first non-empty wins; concatenating doubles the text |
| list-shaped `delta.content` | `mistral` | `[{"type": "thinking", "thinking": [...]}, {"type": "text", ...}]` — reasoning is inside `content` itself, there is no separate field |

Mistral's list shape is a **type guard, not just a feature**: before `_split_content_delta`
existed, a list `content` was appended straight into the content accumulator and the
closing `"".join` raised `TypeError`. The same split is applied on the non-streaming
path (`choices[0].message.content` can be a list too). `gemini` is off this table
entirely — its parts carry `"thought": true` and are split in `_consume_gemini_sse`,
which deliberately keeps thought text OUT of the returned body so it can never corrupt
a structured parse.

**Reasoning is an opt-in, per backend, via the `_reasoning_payload()` hook**
(`OpenAICompatBackend`, default `{}` — a backend that doesn't override it sends a
byte-identical payload to its pre-reasoning shape and can never trip the degrade
below):

| Backend | Opt-in |
|---|---|
| `openrouter` | top-level `{"reasoning": {"enabled": True}}` |
| `mistral` | top-level `{"reasoning_effort": "high"}` — the parameter is two-valued (`high`/`none`), there is no middle setting, so asking for reasoning at all means `high` |
| `gemini` | `generationConfig.thinkingConfig = {"includeThoughts": True}` — merged in `_call_api_once`, not a top-level key; without it the API never sets `"thought": true` on any part, so streaming alone yields nothing |
| `opencode-zen`, `opencode-go`, `anthropic`, `claude-cli` | none — these emit reasoning without being asked |

An override MUST return `{}` when `self._reasoning` is False, or the degrade can't
degrade.

**`_ReasoningRejected` degrade — the same shape as `_CacheRejected`, for the same
reason.** Reasoning is an optional enrichment, so a provider that rejects the field
(a model that doesn't support thinking; on OpenRouter, a `provider.require_parameters`
guard that filters out every eligible upstream *because* of it) must cost this backend
its thinking stream, never its BF-19 slot. `_call_api_once` raises `_ReasoningRejected`
instead of `AgentBackendUnavailable` at every permanent-4xx site when
`_reasoning_payload()` was non-empty; `_call_api` catches it **outside**
`retry_transient`'s budget (a reshaped retry never shares a budget with retries of the
identical request), flips `self._reasoning` off for the rest of that backend
*instance's* life — never persisted — and retries once clean.

**Degrade precedence is reasoning → caching → fail**, one field shed per attempt, in
`_call_api`'s bounded loop. Gemini layers its pre-existing `_SchemaRejected` downgrade
underneath: a 4xx sheds `thinkingConfig` first (keeping structured mode), and only if
the clean retry also 4xxs does the schema downgrade fire. The cost is at most two extra
HTTP attempts on a genuinely-broken config, which is the deliberate trade for never
losing a BF-19 slot to an optional field.

**A silent REASONING card is usually the model, not the wiring.** The shipped catalog
defaults (`gemini-3.1-flash-lite`, `mistral-small-2603`,
`nvidia/nemotron-3-nano-30b-a3b`) are non-reasoning models and will show nothing even
with every switch above on. Check `effective_model` on the job row before suspecting
code. Regression coverage:
`tests/backend/test_streaming_openai_compat.py::TestReasoningFieldConventions`
(all four conventions, including the no-double-count rule and the `TypeError` guard),
`::TestMistralReasoningOptIn`, `test_openrouter.py::TestReasoningOptIn`, and
`test_gemini_api.py::TestGeminiStreaming` / `::TestGeminiThinkingConfig` (the
structured-mode streaming regression itself).

---

## How to test a phase

After each phase is implemented, verify it with the following steps in order.

### 1. Install / sync the Python package

```bash
pip install -e .
```

Re-run this whenever `pyproject.toml` dependencies change (i.e., after Phase 1 and any phase that adds a new dep).

### 2. Run the backend test suite

```bash
pytest -v
```

Or, to run only the tests added in the current phase:
```bash
pytest tests/backend/test_<phase_module>.py -v
```

All tests must pass. `asyncio_mode = "auto"` is set in `pyproject.toml` — no extra flags needed for async tests.

To skip slow integration tests (default):
```bash
pytest -v -m "not integration"
```

### 3. Run the frontend test suite (phases 9–10 onward)

```bash
cd frontend
npm install        # first time or after package.json changes
npm test           # runs: vitest run
```

### 4. Manual smoke-test the CLI (Phase 1+)

```bash
# Verify basic invocation prints the scaffold message
jsa --csv /path/to/jobs.csv --cv /path/to/resume.pdf

# --cv is optional — it only seeds cv_structure.json once, if none exists yet.
# Jobs stay pending (gated, not failed) until a structure exists — see CLAUDE.md
# → "CV structure gate".
jsa --csv /path/to/jobs.csv

# Verify validation rejects bad inputs (the --cv extension check only fires when --cv is given)
jsa --csv jobs.txt --cv resume.pdf         # should error: bad csv extension
jsa --csv jobs.csv --cv resume.txt         # should error: bad cv extension
jsa --csv jobs.csv --cv resume.pdf --backend bad  # should error: bad backend
```

### 5. Manual smoke-test the server (Phase 8+)

```bash
jsa --csv jobs.csv --cv resume.pdf --no-browser
# Open http://localhost:8765/api/health in a browser or curl:
curl http://localhost:8765/api/health      # should return {"ok": true}
curl http://localhost:8765/api/jobs        # should return []
```

### 6. Manual smoke-test the frontend dev server (Phase 9+)

```bash
cd frontend && npm run dev
# Open http://localhost:5173 in a browser
```
