---
status: InProgress
---

# Prompt caching for the HTTP API backends

## Context

Six HTTP backends now ship in `jsa/agents/` — `anthropic`, `opencode-zen`, `mistral`,
`openrouter`, `gemini`, `opencode-go` — and **none of them sends any prompt-caching
signal**. A repo-wide grep for `cache` in `jsa/**/*.py` returns only the model-catalog
TTL cache, the OpenRouter pricing cache, a WeasyPrint font-cache comment, and the
frontend `no-cache` header. Nothing touches provider prompt caching.

That is a large, easy cost win in this codebase specifically, because of one fact
verified during research: **the assembled system prompt contains no per-job bytes.**
`jsa/pipeline/stages.py:809` and `:667` build it via
`assemble_system_prompt(prompt_text, language=..., structured_model=schema, ...)`, whose
only inputs are the on-disk prompt file, the language code, and the stage's JSON schema.
The job description, the `BASE CV STRUCTURE` and the research brief all go into the
*initial user message* (`_build_initial_user_msg`, `stages.py:1328`), never the system
prompt. So every job at a given (stage, language, structured-mode) sends a
**byte-identical system prefix**, and `PROMPT_CDADJUST.md` (10 KB) plus the
`json.dumps(schema, indent=2)` structured contract makes that prefix large. `cv_adjust`
runs once per job with that same prefix; caching it turns a full-price prefix into a
~0.1× cache read on every job after the first.

Intended outcome: each backend that documents a caching mechanism uses it, behind a
runtime kill switch, with a graceful degrade wherever sending the caching fields could
plausibly make things *worse* than not sending them.

### Scope note — the two scope answers disagree on `anthropic`

The scope question came back with both "Only the new backends"
(`mistral`, `openrouter`, `gemini`, `opencode-go`) and "Only documented mechanisms"
(`anthropic`, `openrouter`, `mistral`) selected. Their strict intersection is only
`{mistral, openrouter}`, which drops real, well-documented work. Resolution used here:

- `mistral`, `openrouter`, `gemini` — in both spirit and scope. **In.**
- `opencode-go` — outside both scope options, but the gateway-risk answer
  ("send it, degrade on 4xx") is a direct instruction about it. **In.**
- `opencode-zen` — a pre-existing backend, not a new one; its caching API is
  undocumented. **Out**, untouched. This is free: per `_openai_compat.py`'s module
  docstring, `opencode_zen.py` keeps its own independent copy of the payload builder and
  does **not** inherit from `OpenAICompatBackend`, so nothing in this plan reaches it.
- `anthropic` — contested (in "documented mechanisms", out of "new backends"). It is
  the best-documented mechanism researched and has the same gap. It is isolated as
  **Phase 6, the last phase**, so cutting it is a one-line decision.

## Decisions locked

1. **One breakpoint, on the system prompt. No message-tail breakpoint.** The system
   prefix is where essentially all the value is (byte-identical and shared across
   every job). The conversation tail is separated by human answer latency
   (`awaiting_input` → user answers), so a 5-minute tail entry is usually cold, and a
   tail breakpoint would collide with `adapt_history` (rewrites row *content* on mode
   changes), `_parse_with_nudge` (appends turns and re-issues `_call_api` with different
   messages), and the 4-breakpoint cap. This also keeps the hook surface minimal — the
   hook needs `system_prompt` only, never `messages`.
2. **Degrade-on-4xx covers `openrouter` as well as `opencode-go`** — see Phase 4's
   Problems section; this is a risk-reduction, not just a gateway concern.
3. **Kill switch is a real `Settings` field + CLI flag**, threaded through
   `make_backend_factory`, mirroring `fit_model`/`fit_timeout`.
4. **No `-> str` signature changes.** Cached-token observability is logged from inside
   `_call_api_once` / `_call_messages_api_once`, where the response body is already in
   hand.

### Mechanism per backend (researched)

| Backend | Mechanism | Cached-token field |
|---|---|---|
| `mistral` | top-level `prompt_cache_key` (stable app-level id); 64-token cache blocks, so prompts under 64 tokens never hit | `usage.prompt_tokens_details.cached_tokens` |
| `openrouter` | explicit `cache_control: {"type":"ephemeral"}` on a text content block (required for Anthropic/Qwen/Gemini upstreams; OpenAI/DeepSeek/Grok/Groq/Z.AI/Gemini-2.5 cache automatically) | `usage.prompt_tokens_details.cached_tokens` |
| `gemini` | implicit caching, on by default for 2.5+ models — **no request change** | `usageMetadata.cachedContentTokenCount` |
| `opencode-go` `/messages` | Anthropic-shape `system` as a block array with `cache_control` — **undocumented for this gateway, speculative** | `usage.cache_read_input_tokens` / `cache_creation_input_tokens` |
| `opencode-go` `/chat` | OpenAI-compat `cache_control` block — same speculative status | `usage.prompt_tokens_details.cached_tokens` |
| `anthropic` | `system` as a block array with `cache_control` (5-min TTL default); **min cacheable prefix is model-dependent** — 4096 tokens for the repo default `claude-haiku-4-5` | `usage.cache_read_input_tokens` / `cache_creation_input_tokens` |

Sources: [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching),
[OpenRouter prompt caching](https://openrouter.ai/docs/features/prompt-caching),
[Mistral prompt caching](https://docs.mistral.ai/studio-api/conversations/advanced/prompt-caching),
[Gemini context caching](https://ai.google.dev/gemini-api/docs/caching),
[OpenCode Zen pricing](https://opencode.ai/docs/en/zen/).

---

## Phase 0 — Byte-stability gate (do this first; it can invalidate the whole feature)

**Current State.** `_structured_contract` (`jsa/pipeline/prompt_assembly.py`) embeds
`json.dumps(schema, indent=2)` where `schema = json_schema_for(stage)`
(`jsa/schema/turn_models.py`). Nothing asserts that this rendering is byte-stable.

**Desired State.** A test proves the assembled system prompt for a given
(stage, language, structured-mode) is byte-identical within a process **and across
processes**.

**Problems/Bugs.** Prompt caching is a pure prefix match. If any part of the Pydantic
schema-generation path iterates a `set` (e.g. an enum or a `Literal` union materialised
through a set), string iteration order varies with `PYTHONHASHSEED` — so the prefix would
differ *between server restarts*, silently reducing the feature to a permanent
cache-write surcharge with zero reads, while every unit test still passes. This is the
single failure mode that would make the whole plan a no-op.

**Solutions.** Add `tests/backend/test_prompt_prefix_stability.py`:

```python
def test_assembled_system_prompt_is_byte_stable_across_processes():
    prompt = assemble_system_prompt(read_prompt("PROMPT_CDADJUST"), language="en",
                                    structured_model=json_schema_for(Stage.cv_adjust))
    a = hashlib.sha256(prompt.encode()).hexdigest()
    # same value again in-process, plus once more via subprocess with a different
    # PYTHONHASHSEED — all three must match
```
Run the subprocess arm twice with `PYTHONHASHSEED=0` and `PYTHONHASHSEED=1`. If the
hashes differ, **stop and fix the non-determinism before any other phase** (sort the
offending collection at the point it is built).

---

## Phase 1 — Kill switch: `Settings.prompt_caching` + `--prompt-caching/--no-prompt-caching`

**Current State.** `jsa/config.py` has per-feature scalars (`fit_model`, `fit_timeout`,
`opencode_zen_timeout`, …). `jsa/server.py::make_backend_factory` has one branch per
backend that maps settings → constructor kwargs. No caching setting exists.

**Desired State.** One boolean, default `True`, reachable from every backend that this
plan touches, settable by env var and CLI flag.

**Problems/Bugs.** Without a switch, a gateway that starts rejecting the caching field
(or an upstream routing regression) requires a code change and redeploy to work around.

**Solutions.**
- `jsa/config.py`: `prompt_caching: bool = True  # via JSA_PROMPT_CACHING`.
- `jsa/cli.py`: `--prompt-caching` / `--no-prompt-caching` (a `BooleanOptionalAction`-style
  pair, matching how the existing flags are declared).
- `jsa/agents/_openai_compat.py::OpenAICompatBackend.__init__` gains
  `prompt_caching: bool = True`, stored as `self._prompt_caching`. `OpenCodeGoBackend`
  already overrides `__init__` and calls `super().__init__(...)` — forward the kwarg.
- `jsa/server.py::make_backend_factory`: pass `prompt_caching=settings.prompt_caching`
  in the `mistral`, `openrouter`, `gemini`, `opencode-go` branches only. Do **not** add
  it to the `opencode-zen`, `claude-cli`, or `google-cli` branches — those backends never
  accept it (same reasoning as the `structured_schema` kwarg contract in
  `jsa/agents/base.py`: no unused accept-and-ignore parameters).
- The default `True` must produce a payload **byte-identical to today** on any backend
  where caching turns out to be a no-op — see each phase's test.

---

## Phase 2 — Mistral: top-level `prompt_cache_key`

**Current State.** `MistralBackend` (`jsa/agents/mistral.py`, 25 lines) is pure
configuration on top of `OpenAICompatBackend`; it has no `_extra_payload` override.
`OpenAICompatBackend._extra_payload()` (`_openai_compat.py:174`) takes no arguments and
is called at `_call_api_once`'s single call site (`:346`).

**Desired State.** Every Mistral request carries a `prompt_cache_key` derived
deterministically from the shared prefix, so requests that share a system prompt route to
a server that already holds it.

**Problems/Bugs.** `_extra_payload()` has no access to the system prompt, so the key
cannot be computed today. A per-*job* key (e.g. `job.id`) would be worse than useless
here: it would isolate each job into its own cache namespace and forfeit exactly the
cross-job system-prefix reuse that is the whole point.

**Solutions.**
- Change the hook signature to `_extra_payload(self, system_prompt: str) -> dict[str, Any]`
  and update the one call site plus `OpenRouterBackend`'s existing override (which ignores
  the argument and keeps returning `{"provider": {"require_parameters": True}}`).
- `MistralBackend._extra_payload`:
  ```python
  def _extra_payload(self, system_prompt: str) -> dict[str, Any]:
      if not self._prompt_caching:
          return {}
      digest = hashlib.sha1(system_prompt.encode("utf-8")).hexdigest()[:16]
      return {"prompt_cache_key": f"jsa-{digest}"}
  ```
  A system-prompt hash is the right key: it groups exactly the requests that share the
  byte-identical prefix, across jobs, and stays stable across restarts (given Phase 0).
- Log cached tokens from `_call_api_once` after the body parses:
  `body["usage"]["prompt_tokens_details"]["cached_tokens"]` — guarded with `.get` chains,
  never raising if the provider omits it.
- Zero routing risk: `prompt_cache_key` is a top-level hint, and a wrong/unknown key
  degrades to a cache miss, not an error. No degrade path needed for this backend.

---

## Phase 3 — Gemini: observability only

**Current State.** `GeminiBackend._call_api_once` (`jsa/agents/gemini_api.py:157`) builds
`{"contents", "systemInstruction", "generationConfig"}` and discards everything in the
response body except `candidates[0]`.

**Desired State.** No request change; the existing request already satisfies implicit
caching (stable `systemInstruction` first, growing `contents` after). Cache activity is
visible in the logs.

**Problems/Bugs.** None functionally — implicit caching is on by default for Gemini 2.5+
and needs no request field. The gap is purely that a zero cache rate would today be
invisible, so a future prompt-assembly regression that breaks the prefix could not be
detected. Note the default model `gemini-3.1-flash-lite`'s minimum is 4096 tokens.

**Solutions.** In `_call_api_once`, after `body` parses, log
`body["usageMetadata"]["cachedContentTokenCount"]` at `logger.info`, `.get`-guarded.
Explicitly **do not** add explicit `cachedContents` — it requires a separate
create/manage lifecycle for a prefix that implicit caching already covers.

---

## Phase 4 — OpenRouter: `cache_control` breakpoint + degrade-on-4xx

**Current State.** `OpenAICompatBackend._call_api_once` builds
`"messages": [{"role": "system", "content": system_prompt}, *messages]` (`:343`) — the
system content is a bare string. `OpenRouterBackend._extra_payload` already sends the
mandatory `{"provider": {"require_parameters": True}}` routing guard.

**Desired State.** The system message carries an explicit `cache_control` breakpoint for
the upstreams that need one (Anthropic/Qwen/Gemini), while remaining harmless for the
upstreams that cache automatically.

**Problems/Bugs.** This is the one place where sending the caching field could be
**strictly worse than the status quo**, and it is why the degrade path is not optional
here. `provider.require_parameters: true` filters candidate providers by the parameters
the request supplies. If OpenRouter counts `cache_control` in that filter, the failure
mode for the default `nvidia/nemotron-3-nano-30b-a3b` is not "no caching" — it is *no
eligible provider*, returned as a 4xx. Under `_call_api_once`'s existing three-way
classification that is an immediate, unretried `AgentBackendUnavailable`, which drops
`openrouter` out of the BF-19 chain entirely (see CLAUDE.md → "Backend fallback chain
(BF-19)"). A silent caching miss is acceptable; losing a chain link is not.

**Solutions.**
- Add an overridable hook to the base, defaulting to today's exact behaviour so
  `mistral` and any future subclass are byte-unchanged:
  ```python
  def _system_content(self, system_prompt: str) -> str | list[dict]:
      return system_prompt          # OpenAICompatBackend default: unchanged
  ```
  and use it at `:343`. `OpenRouterBackend` overrides it:
  ```python
  def _system_content(self, system_prompt):
      if not self._prompt_caching:
          return system_prompt
      return [{"type": "text", "text": system_prompt,
               "cache_control": {"type": "ephemeral"}}]
  ```
- **Degrade path**, modelled exactly on the `_SchemaRejected` pattern already proven in
  `jsa/agents/gemini_api.py:82`: a module-private
  `class _CacheRejected(AgentBackendUnavailable)` in `_openai_compat.py`, raised by
  `_call_api_once` in place of plain `AgentBackendUnavailable` **only** when a permanent
  4xx occurs while cache fields were actually present in the payload. `_call_api` catches
  it, sets `self._prompt_caching = False`, logs a warning, and retries the same call once
  with caching off. Subclassing `AgentBackendUnavailable` is deliberate: an instance that
  somehow escapes the catch still classifies correctly for BF-19.
- The retry-once-clean must happen **outside** `retry_transient`'s budget, the same way
  the structured→sentinel downgrade sits outside it (`opencode_zen.py`'s established rule:
  the retry loop retries the *identical* request, never a reshaped one).
- Log `usage.prompt_tokens_details.cached_tokens`.

---

## Phase 5 — OpenCode-GO: speculative `cache_control` on both protocols

**Current State.** `/chat` inherits `_call_api_once` verbatim. `/messages`
(`opencode_go.py:231`) hand-builds `{"model", "max_tokens", "system": <str>, "messages"}`
against an Anthropic-shape endpoint.

**Desired State.** Both protocols send a system-prompt cache breakpoint in the shape
their wire protocol expects, and fall back cleanly if the gateway rejects it.

**Problems/Bugs.** The gateway publishes cached-read/cached-write pricing but documents
no cache API, so both shapes are informed guesses. There is direct precedent for this
gateway silently not honouring a documented Anthropic feature — forced tool-use does not
take on the `/messages` path, which is why those models are sentinel-only (see the module
docstring). A guess that 4xx's must not cost the backend its BF-19 slot.

**Solutions.**
- `/chat`: override `_system_content` on `OpenCodeGoBackend` exactly as OpenRouter does.
  It inherits the Phase 4 `_CacheRejected` degrade for free.
- `/messages`: in `_call_messages_api_once`, when `self._prompt_caching`, send
  `"system": [{"type": "text", "text": system_prompt,
  "cache_control": {"type": "ephemeral"}}]` instead of the bare string, and give
  `_call_messages_api` the same catch-`_CacheRejected`-and-retry-clean wrapper. Raise
  `_CacheRejected` from the two permanent-4xx branches (`:294`, `:302`) when cache fields
  were present.
- Log `usage.cache_read_input_tokens` / `usage.cache_creation_input_tokens`.
- Update `tests/backend/test_opencode_go.py:131`
  (`assert payload["system"] == "my system prompt"`) — a legitimate wire-shape change.
  Keep an explicit `prompt_caching=False` case asserting the old bare-string shape.

---

## Phase 6 — Anthropic (cuttable; the two scope answers disagree on this one)

**Current State.** `AnthropicAPIBackend._call_api` (`jsa/agents/anthropic_api.py:214`)
passes `"system": system_prompt` as a bare string. `AnthropicAPIBackend.__init__` takes
`(model, timeout)` only.

**Desired State.** The system prompt is sent as a single text block with a
`cache_control` breakpoint.

**Problems/Bugs.** The repo default model is `claude-haiku-4-5`, whose **minimum
cacheable prefix is 4096 tokens** — well above the other backends' minimums. A
`cv_adjust` system prompt (10 KB prompt file + a large `json.dumps` schema) clears it
comfortably; a sentinel-mode `fit_assessment` prompt does not (see "Known non-caching
cases" below). This backend needs no degrade path — `cache_control` is a first-class,
documented field, not a routing hint.

**Solutions.**
- `__init__` gains `prompt_caching: bool = True`; `make_backend_factory`'s `anthropic`
  branch forwards `settings.prompt_caching`.
- In `_call_api`:
  ```python
  "system": ([{"type": "text", "text": system_prompt,
               "cache_control": {"type": "ephemeral"}}]
             if self._prompt_caching else system_prompt),
  ```
  Default 5-minute TTL — not `ttl: "1h"`: the 2× write premium only pays off across a
  5–60 minute start-to-start gap, and the value here comes from job-to-job reuse under
  the orchestrator's continuous dispatch, not from a single job's human-latency turns.
- No message-tail breakpoint and no top-level auto-`cache_control` (locked decision #1).
- Log `response.usage.cache_read_input_tokens` / `cache_creation_input_tokens`.
- Update `tests/backend/test_anthropic_api.py:321` and `:546`
  (`assert call_kwargs["system"] == "..."`) to the block-array shape, and add a
  `prompt_caching=False` case pinning the bare-string shape.

---

## Known non-caching cases (expected, not bugs)

State these in CLAUDE.md so a zero cached-token count is not misread as a broken
implementation:

- **Sentinel-mode `fit_assessment`.** `PROMPT_FIT_ASSESSMENT.md` is 2.6 KB (≈660 tokens)
  and, in sentinel mode, gets no schema appended — below every provider minimum
  (Mistral 64, OpenRouter/Gemini 1024–4096, Anthropic-on-Haiku 4096). It will not cache.
  Structured-mode fit is larger and may.
- **`opencode-zen`.** Deliberately out of scope; assumed to cache automatically upstream,
  unverified.
- **CLI backends** (`claude-cli`, `google-cli`). Not applicable — the CLI manages its own
  caching.

## Files to be modified

- `jsa/config.py`, `jsa/cli.py`, `jsa/server.py` — the kill switch (Phase 1)
- `jsa/agents/_openai_compat.py` — `_system_content` hook, `_extra_payload` signature,
  `_CacheRejected` + degrade, `prompt_caching` ctor kwarg
- `jsa/agents/mistral.py`, `jsa/agents/openrouter.py`, `jsa/agents/gemini_api.py`,
  `jsa/agents/opencode_go.py` — per-backend mechanism
- `jsa/agents/anthropic_api.py` — Phase 6 only
- `CLAUDE.md` — a new "Prompt caching" section (mechanism table, the
  system-prefix-byte-stability invariant, the known non-caching cases, and a
  "do not simplify the degrade path away" note in the style of the existing
  `require_parameters` warning)
- **Untouched:** `jsa/agents/opencode_zen.py`, `claude_cli.py`, `google_cli.py`,
  `jsa/pipeline/prompt_assembly.py`, `jsa/pipeline/stages.py`

## Verification

**Unit tests prove the field is sent — they prove nothing about a cache hit.** Both
layers are required.

1. **Byte-stability** (Phase 0) — `pytest tests/backend/test_prompt_prefix_stability.py -v`,
   including the cross-`PYTHONHASHSEED` subprocess arm.
2. **Payload shape**, per backend, mirroring `tests/backend/test_openrouter.py`'s existing
   `require_parameters` assertion: capture the posted `json=` payload from a stubbed
   `httpx.AsyncClient` and assert the exact caching field is present.
3. **Kill-switch parity**: with `prompt_caching=False`, assert the payload is
   **byte-identical** to the current implementation's, for every touched backend.
4. **Degrade path**: stub a permanent 4xx while cache fields are present; assert exactly
   one clean retry without them, that `self._prompt_caching` flipped to `False`, and that
   the eventual failure (if the clean retry also fails) is a plain
   `AgentBackendUnavailable` that BF-19 recognises. Cover `openrouter` and both
   `opencode-go` protocols.
5. **Full suite unchanged**: `pytest -v -m "not integration"` — in particular
   `tests/backend/test_mode_parity.py` (the permanent sentinel/structured equivalence
   gate) and `tests/backend/test_bf18_limit_detection.py` must pass untouched.
6. **Real cache hits** — `tests/backend/integration/`, `@pytest.mark.integration`, skipped
   in CI: issue two back-to-back requests sharing a system prompt against a live key and
   assert the second reports cached tokens > 0 in that provider's usage field. This is the
   only test that actually verifies the feature works.
7. **Manual smoke**: `jsa --csv jobs.csv --backends mistral --no-browser`, run two jobs,
   and check the log lines for a non-zero cached-token count on the second.
8. Post-implementation: `/code-review medium --fix` (multi-file, touches the BF-19
   failure-classification path).

## Change Log

**2026-09-02**: Phase 0 — byte-stability gate. Added
`tests/backend/test_prompt_prefix_stability.py`: an in-process stability check plus a
cross-process check (via `subprocess` + `sys.executable`, parametrized over
`PYTHONHASHSEED=0` and `PYTHONHASHSEED=1`) that hashes the assembled `cv_adjust`
structured-mode system prompt and asserts all three hashes match. All 3 pass — the
assembled prompt (`PROMPT_CDADJUST.md` + `json_schema_for(Stage.cv_adjust)`) is
byte-stable across restarts, so the plan is not a no-op. No source changes were needed.
Ran the full suite (`pytest -q -m "not integration"`, via `.venv`, the correct
project virtualenv — `venv/` also exists but is a duplicate/stale env, both had `jsa`
+ `pytest-asyncio` installed but this session used `.venv`): 1635 passed, 1 pre-existing
flake (`test_dev_tunnel.py::TestStartTunnelUrlPrinted::test_daemon_thread_is_started`,
a timing-sensitive thread-start test that fails only under full-suite parallel load and
passes in isolation — unrelated to this change, not investigated further). Verified:
proceed to Phase 1.

**2026-09-02 (same session, advisor follow-up)**: the advisor flagged that Phase 0's
original test only proved *rendering* determinism, not the plan's actual value claim
("every job at a given (stage, language, structured-mode) sends a byte-identical
system prefix" — a cross-job claim, not a same-input-twice claim). Verified directly
by reading `stages.py`'s three real call sites (`run_stage` for cv_adjust/cover_letter/
their revision variants, and `_run_fit_assessment`): `_get_system_prompt(stage)` takes
no job argument at all, and all job-derived content (JD, company, role, CV structure,
research brief) is built exclusively by `_build_initial_user_msg`/`_build_fit_user_msg`
into the *initial user message*, never the system prompt — confirmed by reading, not
assumed. Strengthened `test_prompt_prefix_stability.py`: parametrized the existing
hash tests over all three stages (`fit_assessment`, `cv_adjust`, `cover_letter` — not
just `cv_adjust`), and added `TestCrossJobSystemPromptIdentity`, which constructs two
jobs with deliberately different JD/company/role and asserts (a) `_get_system_prompt`'s
signature takes only `stage`, (b) the two jobs' initial user messages differ and each
contains only its own JD, and (c) neither job's JD/company/role ever appears in the
raw prompt file text. All 12 tests pass. Also grepped `_extra_payload` repo-wide ahead
of Phase 2 (which changes its signature): exactly one call site
(`_openai_compat.py:352`) and one override (`OpenRouterBackend`), no test calls it
directly — Phase 2's signature change is unblocked.

**2026-09-02**: Phase 1 — kill switch. Added `Settings.prompt_caching: bool = True`
(`JSA_PROMPT_CACHING`); `--prompt-caching`/`--no-prompt-caching` CLI flag (tri-state
`Optional[bool]`, only overrides when explicitly passed, mirroring `--fit-model`'s
override pattern); `OpenAICompatBackend.__init__` gained `prompt_caching: bool = True`
stored as `self._prompt_caching` (inherited unchanged by `MistralBackend`,
`OpenRouterBackend`, `GeminiBackend`, none of which override `__init__`);
`OpenCodeGoBackend.__init__` forwards the kwarg to `super().__init__`.
`make_backend_factory` forwards `prompt_caching=settings.prompt_caching` in exactly
the `mistral`/`openrouter`/`gemini`/`opencode-go` branches — confirmed `opencode-zen`,
`claude-cli`, `google-cli`, and `anthropic` (Phase 6, not yet wired) all still
construct without the kwarg. Added `tests/backend/test_prompt_caching_phase1.py` (24
tests): Settings default/env override, ctor default+override on all four backends,
factory forwarding (both values, all four backends), the four untouched backends
still construct fine, and a same-payload assertion proving the switch is currently a
true no-op on the wire (Phase 1 adds no request field yet — that starts at Phase 2).
Manually smoke-tested `--help` shows the new flag. Full suite: `pytest -q -m "not
integration"` → 1669 passed, 2 skipped (dev_tunnel flake from the Phase 0 run did not
reproduce). Verified: proceed to Phase 2.

## Decisions Log

*(reserved for the user)*
