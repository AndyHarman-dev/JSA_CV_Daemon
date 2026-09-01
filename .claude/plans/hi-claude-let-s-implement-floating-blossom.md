---
status: InProgress
---

# Four new backends (Mistral, OpenRouter, Gemini API, OpenCode-GO) + per-backend runtime model selection

Branch to create from `main`: **`feat/multi-backend-model-select`**

## Context

JSA today ships four backends — `claude-cli`, `google-cli` (the `agy`/antigravity CLI),
`anthropic`, `opencode-zen`. The BF-19 fallback chain (`--backends a,b,c`) only has value
when the chain has genuinely independent providers behind it; with four backends and two of
them Claude-flavoured, a quota wall or an outage tends to take out most of the chain at once.
We want more variation: **Mistral**, **OpenRouter**, **Google Gemini (native API)**, and
**OpenCode-GO**. OpenRouter is the cheapest of the four to add — it is OpenAI-compatible, has
a real `GET /api/v1/models` catalog, and is itself a multi-provider aggregator, so it widens
the chain's provider diversity more per line of code than any other entry here.

Second, the model each backend runs is currently reachable **only** via env vars
(`JSA_MODEL`, `JSA_OPENCODE_ZEN_MODEL`) and requires a restart to change. There is no
`--model` flag, no write path in the API, and nothing in the UI. We want the model to be
selectable at runtime from the existing "active backends" dropdown in the header: clicking a
backend row opens a second anchored submenu listing that backend's models, and picking one
takes effect on the **next stage dispatch** without a restart.

This work is **purely additive** — nothing is replaced or deleted, so the CLAUDE.md
replace/delete **parity gate does not apply**. The sentinel protocol, the structured-output
contract, BF-19, and all four existing backends keep their current behaviour.

### Locked decisions (from Q&A)

| Question | Decision |
|---|---|
| Model-selection scope | **Global, persisted, live.** One selected model per backend, stored in `~/.jsa/backend_models.json`, PUT via API. Applies to the next dispatch; in-flight stages finish on their old instance. |
| Model catalog source | **Live listing where the provider has one, settings-file fallback otherwise.** Fetch failure ⇒ silently fall back to the catalog. |
| Catalog test | **Offline unit test** on catalog *shape*. Live model-ID validation is a separate `@pytest.mark.integration` test. |
| UI | **Extend** the existing `Header.tsx` backend dropdown with a nested anchored submenu. Do not redesign it. |
| Gemini surface | **Native `generateContent`** (`generativelanguage.googleapis.com/v1beta`), not the OpenAI-compat shim. |
| API keys | **Env-only**, read at call time. No `.env` loader, no keys in the settings file. |
| Key env vars | **Fallback chain**: `MISTRAL_API_KEY`; `OPENROUTER_API_KEY`; `GEMINI_API_KEY` → `GOOGLE_API_KEY`; `OPENCODE_GO_API_KEY` → `OPENCODE_API_KEY`. Existing zen backend keeps `OPENCODE_API_KEY` untouched. |
| OpenCode-GO protocols | **Dual dispatch.** `/chat/completions` models get real structured output; `/messages` (Anthropic-shape) models are **sentinel-only** — no prompt-injected JSON. |
| OpenCode-Zen scope | Catalog **restricted to its free-tier models**. On the docs listing these are all `/chat/completions`, so the expectation is **no transport change to `opencode_zen.py`** — but this is verified by the Phase-0 `/models` probe, and **if any free-tier model turns out to need `/messages`, add the same per-model protocol dispatch to the zen backend** (Phase 2a). |

### Reference: the bake-off project

`~/PycharmProjects/Assesser/src/assesser/providers/` has working Mistral, OpenRouter, Google
and OpenCode-GO implementations. Useful for **endpoints, payload shapes and probe-confirmed
quirks** only — its architecture does not transfer (vendor SDKs, no session/history model,
no sentinel protocol, no model listing anywhere). Two findings from it are load-bearing:

- `opencode_provider.py:10` — `OPENCODE_BASE_URL = "https://opencode.ai/zen/go/v1"`, with a
  per-model `PROTOCOL_DISPATCH` table. Its `/messages` path is the Anthropic SDK shape
  (`x-api-key`, not `Authorization: Bearer`), base URL with `/v1` stripped.
- `_augment_prompt_for_json` — its probe found **forced tool-use does not take on the
  `/messages` models of this gateway**; the model answers free text regardless. We answer
  that with sentinel mode rather than prompt-injected JSON.
- `openrouter_provider.py` — `OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"`, and the
  routing guard `extra_body={"provider": {"require_parameters": True}}` with the comment:
  *"Without this, OpenRouter may route to an upstream endpoint that silently ignores
  `response_format` instead of honoring it or failing loudly."* That flag is **mandatory** for
  us — silent schema-ignoring is precisely the failure our per-session downgrade cannot see.

---

## Phase 0 — Live probes (user-run, blocking for Phases 2 & 3)

**The agent's shell has none of the API keys** — `MISTRAL_API_KEY`, `GEMINI_API_KEY`,
`GOOGLE_API_KEY`, `OPENCODE_GO_API_KEY`, `OPENCODE_API_KEY`, `ANTHROPIC_API_KEY` are all
unset in the tool environment (verified). The only copies on disk are in
`~/PycharmProjects/Assesser/.env`. Every live probe below **must be run by the user** with so it inherits their exported shell, and the implementer records the
output in the Change Log. An implementer who writes integration tests and reports "pass" from
a silent skip has verified nothing. The implementer must write a .sh script for the user to check
the probes below.

Probes to run:

1. `GET https://opencode.ai/zen/v1/models` and `GET https://opencode.ai/zen/go/v1/models` —
   confirm the response shape (expected OpenAI `{"data":[{"id":…}]}`) and **re-derive the
   real free-tier model list**; the working list below came from a docs-page summary, not
   from the API.
2. `GET https://api.mistral.ai/v1/models` — shape + real model IDs.
2b. `GET https://openrouter.ai/api/v1/models` — shape + real model IDs. This one is public
   (no key needed) but run it anyway to pin the response shape and pick sane catalog defaults;
   note OpenRouter IDs are namespaced (`vendor/model`, e.g. `nvidia/nemotron-3-nano-30b-a3b`).
3. `GET https://generativelanguage.googleapis.com/v1beta/models` — shape + real model IDs.
4. **The Gemini structured-output probe** (see Phase 3 — determines which schema field to
   send). Send the real `json_schema_for(Stage.cv_adjust)` payload twice: once under
   `generationConfig.responseSchema`, once under `generationConfig.responseJsonSchema`.
   Record which is accepted.
5. One OpenCode-GO `/messages` call to confirm the auth header shape actually required by
   that gateway path.

If anything deviates from the probe's result, jot it down in this plan file, and with the help of user
seek for alternatives if any.
The main interaction is back and forth wtih .sh script that user executes and the implementer write.

---

## Phase 1 — Per-backend model selection: config, store, API, factory

### Current state
- `jsa/config.py:19-23` — model/timeout are **flat per-backend scalars**: `model` (shared by
  `claude-cli` *and* `anthropic`), `anthropic_timeout`, `agent_timeout`, `opencode_zen_model`,
  `opencode_zen_timeout`.
- `jsa/server.py:50-70` `_backend_factory` branches on name and dereferences those scalars
  **inside** the inner function, so it re-reads `settings` on every dispatch.
- `jsa/pipeline/orchestrator.py:312` constructs a **fresh backend instance on every single
  dispatch** — nothing is cached.
- `jsa/api/routes_meta.py:17-30` `/api/config` is GET-only and exposes no model fields. There
  is no write path for backend config anywhere in the API.
- Persisted-settings precedent: `jsa/store/preferences.py` + `jsa/api/routes_preferences.py`
  + the `Settings.preferences_path` property (`config.py:40-44`).

### Desired state
A global, persisted, per-backend model selection that reaches the next dispatch with no
restart, without disturbing the existing flat scalars or their env vars.

### Problems
1. **`settings.model` is shared by `claude-cli` and `anthropic`** (`config.py:19`, read at
   `server.py:52` *and* `:64`). Mutating it to "select a model for anthropic" would silently
   retarget `claude-cli` too. A per-backend selector **cannot** be expressed in the current
   flat shape. Splitting `model` into two fields would break `JSA_MODEL` and existing tests.
2. Selection must survive a restart ⇒ needs a persisted file, but `make_backend_factory` is
   sync and the store readers are async.
3. `google-cli` has **no model at all** (`GoogleCliBackend.__init__` takes no `model` — the
   `agy` CLI has no model flag). It must not get a meaningless submenu.

### Solutions

**(a) One additive `Settings` field, not five more scalars.**

```python
# jsa/config.py
backend_models: dict[str, str] = {}   # backend name -> selected model ID.
                                      # Overrides the flat per-backend default below.
```

**(b) Factory consults it first. Precedence: explicit override > `backend_models[name]` >
flat per-backend field.**

```python
# jsa/server.py, inside _backend_factory
def _model_for(name: str, default: str) -> str:
    if model_override is not None:
        return model_override          # the fit-gate override still wins
    return settings.backend_models.get(name) or default
```

`anthropic` → `_model_for("anthropic", settings.model)`; `claude-cli` →
`_model_for("claude-cli", settings.model)`; `opencode-zen` →
`_model_for("opencode-zen", settings.opencode_zen_model)`; and one new branch per new backend
(Phases 2–4). This keeps `test_factory_uses_opencode_zen_model_not_shared_model` and
`test_factory_model_override_applies_to_opencode_zen` green.

**Why the fit factory needs no change:** `server.py:159-163` evaluates
`model_override=settings.fit_model` **once at startup**. When `fit_model` is `None` (the
default) the override is `None`, so the fit factory falls into the same `_model_for` read —
**inside** `_backend_factory` — and picks up runtime changes like every other stage. When
`--fit-model` *was* given, the fit gate stays pinned to it, which is the flag's stated intent.
Do not "fix" this by making the override lazy.

**(c) New store `jsa/store/backend_models.py`**, mirroring `preferences.py` exactly
(sync `_load_sync`/`_save_sync` wrapped in `asyncio.to_thread`, an async `read(path)` taking a
path so non-`Settings` callers work, missing file → defaults never `None`):

```python
class BackendModels(BaseModel):
    selected: dict[str, str] = {}          # backend -> chosen model
    catalog: dict[str, list[str]] = {}     # backend -> user-configurable fallback model list
```

Path via a new `Settings.backend_models_path` property beside `preferences_path`
(`db_path.parent / "backend_models.json"` — derived from `db_path` so tests self-isolate).

Code-provided catalog defaults live in a new `jsa/agents/model_catalog.py`
(`DEFAULT_CATALOG: dict[str, list[str]]`); the store's `catalog` field only holds
**user overrides**, merged over the defaults on read. That way a new default model shipped in
code is picked up without the user editing their JSON.

**(d) Startup hydration.** In `server.py`'s startup event, before building the factory:
load `backend_models.json` and seed `settings.backend_models` from its `selected` dict.
`Settings` is a plain mutable pydantic model and `cli.py:178` already mutates `output_dir`
in place, so this is sanctioned.

**(e) New routes `jsa/api/routes_backend_models.py`**, modelled on `routes_preferences.py`:

- `GET /api/backend-models` → `{ selected: {...}, supports_model_selection: {name: bool} }`
  (`google-cli` → `false`). Cheap, no network.
- `GET /api/backend-models/{backend}` → `{ backend, models: [...], selected, source:
  "live" | "catalog" }`. Lazy, called when a submenu opens. Live listing where the provider
  has one (Phase 5), in-process TTL cache. **Any fetch failure ⇒ 200 with the catalog and
  `source: "catalog"`, never a 500.** Unknown backend → 404; `google-cli` → `models: []`.
- `PUT /api/backend-models` `{backend, model}` → validates `backend ∈ _REGISTRY` and that the
  backend supports selection, persists to the store, **and mutates
  `request.app.state.settings.backend_models[backend]`** so the next dispatch sees it.
  Returns the saved value. 422 on an unknown backend or on `google-cli`.

Register the router at `server.py:189-193`.

Note: selected-model state deliberately does **not** go on `/api/config` — that endpoint is on
the boot path and is raced against an 8s timeout in `store.hydrateLanguage`; a provider fetch
must never hang off it.

---

## Phase 2 — `mistral`, `openrouter` and `opencode-go` backends (OpenAI-compatible HTTP)

### Current state
`jsa/agents/opencode_zen.py` (503 lines) is the reference HTTP backend: raw `httpx`, a
`SessionHandle` subclass carrying `system_prompt`/`messages`/`structured_schema`/
`structured_enabled`, mutate-handle-only-after-success, `_parse_with_nudge` for a missing
sentinel block, a per-session structured→sentinel downgrade, and — most importantly — the
**three-way error classification** in `_call_api_once` (`:359-503`) wrapped by `_call_api`'s
`_MAX_ATTEMPTS=3` retry loop.

### Desired state
Three new backends built on that same skeleton, registered and selectable, participating in
BF-19 identically. All three are OpenAI-compatible chat/completions, so they differ from each
other only in base URL, env var, default model, and a small number of provider quirks —
which is exactly why they share one phase and one base class.

### Problems
- Copying `anthropic_api.py` instead would be a mistake: it has **no
  `AgentBackendUnavailable` path at all**, so an auth error or a bad model name propagates
  raw and hard-fails the job on the first backend instead of advancing the chain.
- OpenCode-GO's catalog spans two wire protocols, and structured output is only viable on one
  of them — but `supports_structured_output` is a `ClassVar`, read per-backend not per-model.

### Solutions

**Shared base.** Extract the OpenAI-compatible machinery from `opencode_zen.py` into
`jsa/agents/_openai_compat.py` — **without modifying `opencode_zen.py`'s behaviour**. It gets
the endpoint URL, auth-header builder, env-var name(s), and default model as class attributes;
everything else (payload build, `response_format` with `"strict": false`, three-way
classification, retry loop, nudge, downgrade) is inherited. Refactoring `opencode_zen.py`
onto it is **optional and gated**: only do it if `tests/backend/test_opencode_zen.py` (935
lines) passes unchanged afterwards; otherwise leave that file alone and accept the duplication.

**`jsa/agents/mistral.py` — `MistralBackend`**
- `name = "mistral"`, `supports_structured_output = True` (ClassVar).
- `POST https://api.mistral.ai/v1/chat/completions`, `Authorization: Bearer $MISTRAL_API_KEY`,
  read at call time.
- Structured: `response_format: {"type":"json_schema","json_schema":{"name":"structured_reply",
  "schema": <json_schema_for(stage)>, "strict": false}}` — **`strict: false` deliberately**,
  the same nested-`$defs` reason documented for zen and anthropic.
- Per-session downgrade to sentinel on an unparseable structured reply, exactly as zen.
- `__init__(self, model: str = <from probe>, timeout: float = 180.0)`.

**`jsa/agents/openrouter.py` — `OpenRouterBackend`**
- `name = "openrouter"`, `supports_structured_output = True` (ClassVar).
- `POST https://openrouter.ai/api/v1/chat/completions`,
  `Authorization: Bearer $OPENROUTER_API_KEY`, read at call time.
- Structured: the same `response_format` block as Mistral (`"strict": false`), **plus the
  mandatory routing guard** in the top-level payload:

  ```python
  payload["provider"] = {"require_parameters": True}
  ```

  Assesser passes this as the OpenAI SDK's `extra_body`; on raw `httpx` it is simply a
  top-level key in the JSON body. Without it OpenRouter may route the request to an upstream
  endpoint that **silently ignores `response_format`** — the model then answers in prose, and
  the per-session downgrade fires on every single turn instead of failing loudly once. Do not
  drop this flag as a simplification; add a request-shape test asserting it is present.
- **Namespaced model IDs.** OpenRouter models are `vendor/model`
  (e.g. `nvidia/nemotron-3-nano-30b-a3b`). The catalog, the settings default, and the UI must
  all carry the full namespaced string — a bare `nemotron-3-nano-30b-a3b` is a 4xx. Worth a
  catalog-shape assertion (`"/" in model_id`) in the Phase-7 offline test.
- **Cost is reported.** `usage.cost` is present on OpenRouter responses and absent everywhere
  else. JSA does not track spend today, so **ignore it** — noted only so the implementer does
  not mistake it for a required field.
- Per-session downgrade to sentinel, identical to Mistral/zen.
- `__init__(self, model: str = <from probe>, timeout: float = 180.0)`.

**`jsa/agents/opencode_go.py` — `OpenCodeGoBackend`**
- `name = "opencode-go"`. Base `https://opencode.ai/zen/go/v1`.
- Env: `OPENCODE_GO_API_KEY` → falls back to `OPENCODE_API_KEY`.
- Module-level `_PROTOCOL: dict[str, Literal["chat","messages"]]` mapping each catalog model
  to its wire protocol (re-derived from the Phase-0 `/models` probe against
  `https://opencode.ai/docs/en/go/`). An unknown model raises `ValueError` client-side, before
  any network call — same as Assesser's guard.
- `"chat"` models → `/chat/completions`, identical to Mistral above, structured enabled.
- `"messages"` models → `POST /zen/go/messages` in the Anthropic Messages shape (top-level
  `system`, `user`/`assistant` roles only, `max_tokens`), **sentinel mode only**. Forced
  tool-use is known not to take on this gateway path; prompt-injected JSON is explicitly
  rejected in favour of JSA's existing, well-tested sentinel path with its nudge.
- **`supports_structured_output` is set as an *instance* attribute in `__init__`** from the
  selected model's protocol. `stages._structured_schema_for(backend, stage)` takes an
  instance, and `tests/backend/fakes/fake_backend.py:55` already shadows the ClassVar this
  way, so this works. **Before committing, grep for any class-level read**
  (`OpenCodeGoBackend.supports_structured_output` / `cls.supports_structured_output`) — a
  class-level read would see the ClassVar default and be wrong. This deviates from the
  "hard-coded per backend, never runtime-detected" rule in `jsa/agents/base.py:66-80` and
  requires a CLAUDE.md amendment (Phase 7).

**Both:** copy zen's three-way classification verbatim — quota/rate (`429`, rate/credit
keywords) → `AgentLimitReached` unretried; transient (any structured error body at status
`<400`, anything `>=500`, non-JSON `>=500`, null `message.content`) → internal transient
error, retried `_MAX_ATTEMPTS` times then `AgentBackendUnavailable`; permanent 4xx →
`AgentBackendUnavailable` immediately, no retry. `httpx.TimeoutException` → `AgentTimeout`,
never retried in-backend.

**Assumption to state:** none of the new backends implements `run_research` — matching
`anthropic` and `opencode-zen` today. `stages.py:1506`'s `hasattr` gate means the
cover-letter research pass degrades to `_research_placeholder()`. Same gap as the existing
API backends, not a new one.

---

## Phase 2a — OpenCode-Zen protocol dispatch (CONDITIONAL on the Phase-0 probe)

### Current state
`jsa/agents/opencode_zen.py:38` hardcodes a single endpoint:
`_ENDPOINT = "https://opencode.ai/zen/v1/chat/completions"`. Every model in the zen catalog is
POSTed there. Today that is safe because the only reachable model is the configured default.

### Desired state
Every model the UI can select on `opencode-zen` reaches the endpoint that model actually
speaks.

### Problems
The Zen gateway spans four wire protocols — `/chat/completions`, `/messages` (Anthropic
shape), `/responses` (OpenAI Responses shape), and `/models/{id}` (Google shape). Once the
Phase-6 submenu makes any catalogued model selectable, a model on the wrong protocol fails at
the transport layer. Restricting the catalog to the free tier is what keeps this small — but
"free tier ⇒ all `/chat/completions`" is an inference from a docs-page summary, not a
verified fact.

### Solutions
**Gate this whole phase on Phase-0 probe #1.** Cross-reference the free-tier model IDs
returned by `GET https://opencode.ai/zen/v1/models` against the protocol table at
`https://opencode.ai/docs/zen/`.

- **If every free-tier model is `/chat/completions`** (the expected outcome): do nothing.
  `opencode_zen.py` is untouched, the catalog is a data-only change, and its 935-line test
  file stays green as-is.
- **If any free-tier model needs `/messages`**: give the zen backend the *same* per-model
  dispatch built for `opencode-go` in Phase 2 — a module-level
  `_PROTOCOL: dict[str, Literal["chat","messages"]]`, the Anthropic-shape request builder,
  and the identical rule that `/messages` models are **sentinel-only** with
  `supports_structured_output` set per-instance in `__init__`. Share the implementation with
  `opencode_go.py` rather than duplicating it — the two gateways differ only in base URL,
  env var, and catalog.
  - This changes the *default* path's behaviour for no model (the default
    `nemotron-3-ultra-free` stays `"chat"`), so `tests/backend/test_opencode_zen.py` must pass
    **unchanged**. If it doesn't, the dispatch was introduced wrong.
  - Add zen cases to the Phase-7 dispatch tests: default model → `"chat"` and
    `supports_structured_output is True`; a `/messages` free-tier model → `"messages"` and
    `supports_structured_output is False`.
- **`/responses` and `/models/{id}` models stay out of the catalog** regardless. If the probe
  shows a free-tier model on one of those (`muse-spark-1.2-contributor-free` is the likely
  candidate), **exclude it and note the exclusion in the Change Log** rather than growing the
  transport surface — the user can raise it later if they want that model.

---

## Phase 3 — `gemini` backend (native `generateContent`)

### Current state
`google-cli` drives the `agy` CLI and takes no model. There is no native Gemini API backend.

### Desired state
`jsa/agents/gemini_api.py` — `GeminiBackend`, `name = "gemini"`, raw `httpx`, no new
dependency (do **not** pull in `google-genai`; the rest of the project is SDK-free except
`anthropic`).

- `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
- Auth: `x-goog-api-key: $GEMINI_API_KEY` (falling back to `GOOGLE_API_KEY`), read at call
  time.
- Payload: `contents: [{role: "user"|"model", parts:[{text}]}]`, system prompt as top-level
  `systemInstruction: {parts:[{text}]}` — Gemini has no `system` role in `contents`.
- Reply text at `candidates[0].content.parts[0].text`; join all text parts.
- `finishReason == "MAX_TOKENS"` → `ProtocolError("structured reply truncated")`, mirroring
  `anthropic_api.py:53-84`, so it spends the self-heal budget rather than hard-failing.
- Error classification: same three-way split as Phase 2.

### Problems
Gemini's `responseSchema` is a restricted OpenAPI subset that historically rejects
`$ref`/`$defs` — and JSA's turn models have nested `$defs` (`CVDocument`/`Contact`/`Section`/
`Entry`/`CoverLetter`). This repo has lost this exact fight twice and both times resolved it
the same way: **send the loose form and lean on the downgrade + self-heal budget**, rather
than fight the restriction (Anthropic's native `output_config` was rejected in favour of
forced tool-use; zen sends `strict: false` on purpose).

### Solutions

**Probe first, then decide** (Phase 0, probe #4). Send the real
`json_schema_for(Stage.cv_adjust)` under each of:
- `generationConfig.responseSchema` (+ `responseMimeType: "application/json"`)
- `generationConfig.responseJsonSchema` (+ `responseMimeType: "application/json"`)

Take whichever the API accepts. **Do not hardcode either from memory.**

Fallbacks, pre-committed so the implementer never has to improvise:
1. If neither accepts the schema with `$defs`, add `inline_defs(schema)` to
   `jsa/schema/turn_models.py` — a `$ref`/`$defs` dereferencer with a cycle guard, fully
   offline-unit-testable — and send the inlined schema.
2. If that still fails, ship `GeminiBackend` with `supports_structured_output = False`
   (sentinel-only) and say so in the Change Log. Structured mode is an optimisation; the
   sentinel path is the contract.

**Critical:** a schema rejection must trigger the **per-session structured→sentinel
downgrade**, never `AgentBackendUnavailable` — the latter would permanently drop Gemini out
of the BF-19 chain on a config-shaped error.

---

## Phase 4 — Registration, config fields, CLI validation

### Current state
- `jsa/agents/registry.py:36-45` — four deferred-import `register()` calls.
- **`jsa/cli.py:37`** — `_VALID_BACKENDS = {"claude-cli","google-cli","anthropic",
  "opencode-zen"}`, a hardcoded duplicate of the registry checked at `:131`/`:142` that exits
  with `typer.Exit(1)` **before** pydantic's registry-driven `_validate_backends` ever runs.
- Help text at `cli.py:101` lists the same four names.

### Problems
Registering `mistral` in `registry.py` alone is **not enough** — `--backends mistral` would
still be rejected by `cli.py:37` with a confusing pre-pydantic error. This is the single most
likely thing to be missed.

### Solutions
1. `registry.py` — four new `register()` calls: `mistral`, `openrouter`, `gemini`,
   `opencode-go`.
2. **`cli.py:37`** — replace the hardcoded set with `set(_REGISTRY)` imported locally inside
   `main()` (keeping the fast-validation intent while removing the duplicate), or, if the
   import-timing cost matters, extend the literal set and add a unit test asserting
   `_VALID_BACKENDS == set(_REGISTRY)` so it can never drift again. **Prefer the test-guarded
   literal** — it preserves the existing "validate before importing the registry" property.
3. `cli.py:101` — update the `--backends` help string.
4. `jsa/config.py` — eight new flat defaults plus the Phase-1 dict:
   `mistral_model` / `mistral_timeout` (`180.0`), `openrouter_model` / `openrouter_timeout`
   (`180.0`), `gemini_model` / `gemini_timeout` (`180.0`), `opencode_go_model` /
   `opencode_go_timeout` (`180.0`), env vars `JSA_MISTRAL_MODEL`, `JSA_OPENROUTER_MODEL` etc.
   `backend` field comment at `:14` updated.
5. `jsa/server.py` — four new `_backend_factory` branches. **The bare fall-through at
   `server.py:70` constructs with `timeout=` only and no `model=`** — a new backend that
   lands there is silently mis-constructed. Add explicit branches; consider making the
   fall-through raise for an unrecognised name once `google-cli` is the only intended
   resident.
6. `server.py:142`/`:147` — the `fit_model`-inapplicable log currently special-cases only
   `google-cli`; that stays correct (it is still the only model-less backend).

---

## Phase 5 — Model listing: live where available, catalog fallback

### Desired state
`jsa/agents/model_catalog.py` holding:
- `DEFAULT_CATALOG: dict[str, list[str]]` — one entry per registered backend.
- `SUPPORTS_MODEL_SELECTION: dict[str, bool]` — `google-cli` → `False`.
- `async list_models(backend: str) -> tuple[list[str], Literal["live","catalog"]]` — an
  in-process TTL cache (e.g. 300s) over a per-backend live fetcher; any exception, timeout,
  or missing key falls back to the merged catalog and reports `"catalog"`.

Live fetchers (all raw `httpx`, short timeout ~10s, key read at call time):

| Backend | Listing |
|---|---|
| `opencode-zen` | `GET https://opencode.ai/zen/v1/models`, then **filter to the free tier** per the locked decision |
| `opencode-go` | `GET https://opencode.ai/zen/go/v1/models`, intersected with `_PROTOCOL`'s known models |
| `mistral` | `GET https://api.mistral.ai/v1/models` |
| `openrouter` | `GET https://openrouter.ai/api/v1/models` — public, no key required; IDs are namespaced `vendor/model` |
| `gemini` | `GET https://generativelanguage.googleapis.com/v1beta/models`, filtered to models supporting `generateContent` |
| `anthropic` | `GET https://api.anthropic.com/v1/models` |
| `claude-cli` | catalog only (no listing API) |
| `google-cli` | none — model selection unsupported |

**Working free-tier zen list**: `nemotron-3-ultra-free` (the current default),
`nemotron-3.5-lightning-free`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`. **Re-derive this
from the Phase-0 live `/models` probe before hardcoding it** — the list above came from a
summary of a docs page, not from the API. (`muse-spark-1.2-contributor-free` is also
free-tier but is a `/responses` model — see Phase 2a for how to decide on it.)

---

## Phase 6 — Frontend: nested model submenu in the header dropdown

### Current state
`frontend/src/components/Header.tsx`:
- `BACKEND_LABELS` `:19-24`, `backendLabel()` `:26`.
- `BackendState` `:53-56`; `/api/config` read at `:69-89`; BF-19 live re-order at `:91-101`.
- The anchored panel: wrapper `position:relative` at `:214`, trigger at `:215`, panel at
  `:273-282` (`position:absolute; top:100%; left:0; marginTop:6; width:240; zIndex:30`,
  `panelBase(T,{chamfer:10})`, `cornerMarks(T,T.bd2,8)`), rows at `:299-350`.
- Closest existing analogue for a *preference-writing, filterable* dropdown:
  `frontend/src/components/cv-editor/LanguagePill.tsx:15`.

### Desired state
Purely additive: each backend row gains a right-side affordance showing the currently selected
model. Hovering/clicking it opens a **second anchored panel** to the right of the first
(`position:absolute; left:100%; top:<row offset>; zIndex:31`), reusing `panelBase` +
`cornerMarks` and `useOutsideClick`, with the `LanguagePill` filter-input idiom when the list
is long. The active model is check-marked. Selecting one PUTs and optimistically updates.

### Solutions
1. `frontend/src/api.ts` — beside `getPreferences`/`putPreferences` (`:166-176`):
   `getBackendModels()`, `getBackendModelsFor(backend)`, `putBackendModel(backend, model)`.
2. `Header.tsx` — add `BACKEND_LABELS` entries for `mistral` → `"MISTRAL"`, `openrouter` →
   `"OPENROUTER"`, `gemini` → `"GEMINI API"`, `opencode-go` → `"OPENCODE GO"` (these stay un-i18n'd; they are proper
   nouns, matching the existing deliberate exception). Add `openModelMenuFor: string | null`
   state, a per-backend lazily-fetched model list, and a `selected` map from
   `GET /api/backend-models`.
3. **`google-cli` gets no submenu** — render its row with a dimmed
   `t("header.noModelSelection")` caption and no chevron, rather than an empty panel.
4. Selection is **optimistic with revert on failure**, copying `store.setLanguage`
   (`store.ts:200-211`).
5. Loading state while a live listing is in flight; on `source: "catalog"` show a subtle
   `t("header.modelsFromCatalog")` hint so a stale list is legible.
5b. **OpenRouter's list is long and its IDs are namespaced**, so the `LanguagePill` filter
   input is not optional there — make it appear whenever a list exceeds ~12 entries, and let
   the row text wrap or ellipsize rather than widening the panel.
6. New keys in `frontend/src/i18n/strings.en.json` **only** (`header.modelMenuTitle`,
   `header.noModelSelection`, `header.modelsFromCatalog`, `header.modelFilterPlaceholder`,
   `header.modelSelectFailed`) — the `useT()` fallback chain covers the other 20 locales until
   `scripts/translate-ui.sh` runs.

---

## Phase 7 — Tests and documentation

### Backend tests
- `tests/backend/test_mistral.py`, `test_openrouter.py`, `test_gemini_api.py`,
  `test_opencode_go.py` — clone the
  structure of `tests/backend/test_opencode_zen.py` (935 lines): start/restore/send/end,
  `TestCallApiTimeout`, `TestCallApiErrors`, `TestCallApiRetryClassification` (with the
  `_no_retry_delay` fixture at `:27`), `TestNudgeOnMissingSentinel`, `TestRegistry…`,
  `TestConfigDefaults`, `TestMakeBackendFactory`, `TestTypeErrorOnWrongHandle`, and the
  structured-output half (request shape, final, question, fit verdict, semantic-failure-does-
  not-downgrade, downgrade-on-unparseable, retry/downgrade independence).
- `test_openrouter.py` additionally: a request-shape test asserting the payload carries
  `provider: {"require_parameters": true}` **on every structured request** — the guard is
  silent when missing, so only a test catches its removal.
- `test_opencode_go.py` additionally: protocol dispatch per model, unknown-model `ValueError`,
  and **`supports_structured_output` is `False` on an instance constructed with a
  `/messages` model and `True` on a `/chat/completions` one**.
- `tests/backend/test_bf18_limit_detection.py` — add per-backend classification cases so all
  four join the BF-19 chain correctly.
- `tests/backend/test_make_backend_factory.py` — four new cases, plus the
  **precedence** case: explicit override > `backend_models[name]` > flat field.
- `tests/backend/test_cli_backends.py` — `--backends mistral,openrouter,gemini,opencode-go`
  accepted;
  plus the drift guard `_VALID_BACKENDS == set(_REGISTRY)`.
- `tests/backend/test_registry.py` — reuse its autouse `clean_registry` fixture (`:28`).
- **New `tests/backend/test_model_catalog.py` — the offline catalog test the user asked for.**
  Assert: every name in `_REGISTRY` has a `DEFAULT_CATALOG` entry; every entry is a non-empty
  list of unique non-empty strings; `SUPPORTS_MODEL_SELECTION` covers every registered
  backend; each backend's *default* model (`Settings.<x>_model`) is in its catalog; every
  `openrouter` entry is namespaced (contains a `/`); and a persisted `selected[name]` must be
  a member of that backend's catalog. **No network.**
- `tests/backend/test_backend_models_store.py` + API tests modelled on `test_preferences.py`
  / `test_api.py`: GET/PUT round-trip, 422 on unknown backend, 422 on `google-cli`, live-fetch
  failure returns 200 with `source: "catalog"`, and **PUT mutates `app.state.settings.
  backend_models` so the next factory call picks it up**.

### Integration tests (`tests/backend/integration/`, `@pytest.mark.integration`, off by default)
- `test_model_catalog_live.py` — for each backend with a listing API, assert every
  `DEFAULT_CATALOG` entry appears in the live `/models` response. This is the "models are
  valid once specified" check.
- One live round-trip per new backend (all four), mirroring `test_opencode_zen_live.py`.
- **These require the user's exported keys** — the agent's shell has none. They must be run by
  the user via `! pytest -m integration …`; a silent skip is not a pass.

### Frontend tests
- Extend `frontend/src/__tests__/Header.test.tsx` (its `vi.mock("../api", …)` at `:8-13` needs
  the three new api methods): submenu opens on row click, renders fetched models, marks the
  selected one, PUTs on click and reverts on failure, and **renders no submenu for
  `google-cli`**.

### Documentation
`CLAUDE.md` amendments (this repo keeps architecture in `README.md` + `CLAUDE.md`, no ARCH.md):
1. A new "Model selection" section: the `backend_models` dict, the precedence rule, the store
   file, the API endpoints, why `/api/config` is deliberately not used, and why the fit factory
   needs no change.
2. Amend the "Structured output (API backends)" capability-flag paragraph
   (`jsa/agents/base.py:66-80`'s rule) to document `opencode-go`'s **instance-level**
   `supports_structured_output` and why (per-model protocol).
3. Extend "Backend fallback chain (BF-19)" / "OpenCode Zen backend" with the four new
   backends' classification, and document OpenRouter's mandatory
   `provider.require_parameters` routing guard and why removing it is a silent regression, noting they copy zen's three-way split rather than
   `anthropic_api.py`'s (which has no `AgentBackendUnavailable` path).
4. Note the env-var fallback chains and that JSA still loads no `.env`.
5. `README.md` — Architecture section: eight backends.

---

## Verification

Run in order. **Anything network-touching must be run by the user with `! <cmd>`** so it
inherits the exported API keys; the agent's shell has none of them.

```bash
pip install -e .
pytest -v -m "not integration"           # full backend suite must be green
pytest tests/backend/test_model_catalog.py -v
```

```bash
# user-run, needs exported keys
! pytest -v -m integration
```

Frontend:
```bash
cd frontend && npm install && npm test
npm run build                            # REQUIRED — the jsa CLI serves gitignored jsa/static,
                                         # not live frontend source
./scripts/translate-ui.sh                # after adding strings.en.json keys
./scripts/translate-ui.sh --check        # confirm no catalog has fallen behind
```

End-to-end smoke (user-run, needs keys):
```bash
! jsa --csv jobs.csv --backends mistral,openrouter,gemini,opencode-go --no-browser
curl http://localhost:8765/api/backend-models
curl http://localhost:8765/api/backend-models/mistral
curl -X PUT http://localhost:8765/api/backend-models \
     -H 'Content-Type: application/json' -d '{"backend":"mistral","model":"<id>"}'
```
Then in the UI at `http://localhost:8765`: open the header backend dropdown, click a backend
row, confirm the anchored model submenu appears, pick a model, confirm the selection
persists across a page reload **and** across a `jsa` restart, and confirm the next job's
`LogEvent` names the newly selected model. Confirm `google-cli` shows no submenu.

Playwright is worth using for the header-submenu visual check if it is installed.

---

## Change Log

**2026-08-31**: Phase 0 live probes — all 7 probes complete and verified against the live APIs.

- **1a `GET zen/v1/models`** (63 models): free tier re-derived as the last 7 entries — `deepseek-v4-flash-free`, `muse-spark-1.2-contributor-free`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, **`laguna-s-2.1-free`**. The last one is NEW — not in the plan's working free-tier list (which came from a docs-page summary). `muse-spark-1.2-contributor-free` is present and confirmed `/responses` (excluded from catalog per Phase 2a).
- **1b `GET zen/go/v1/models`** (33 models): confirmed. `qwen3.8-max` present (the known `/messages`-protocol model). Uses `OPENCODE_GO_API_KEY` (distinct from zen's `OPENCODE_API_KEY`).
- **1c `GET openrouter/api/v1/models`** (425 models): confirmed OpenAI-shaped `{data:[{id,name,supported_parameters,...}]}`. Per-model `response_format` / `structured_outputs` flags visible — usable for catalog filtering.
- **2 `GET mistral/v1/models`** (48 models): confirmed. `mistral-small-2603` (Assesser's CHEAP) present.
- **3 `GET gemini/v1beta/models`** (50 models supporting `generateContent`): confirmed. Includes `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-3.6-flash` (Assesser's three tier models).
- **4 Gemini structured-output schema probe**: decisive result. Both `generationConfig.responseSchema` and `generationConfig.responseJsonSchema` ACCEPT the cv_adjust schema **only when `$defs`/`$ref`/`additionalProperties` are inlined**. Raw `$defs` schema is rejected on all three models (confirmed baseline). All three Asserer-verified tier models work with the inlined form. **Implementation decision: use `responseSchema` (canonical, stable field) + an `inline_defs()` dereferencer in `jsa/schema/turn_models.py`** — the plan's pre-committed fallback #1. `responseJsonSchema` (experimental) also works but offers no advantage.
- **5 OpenCode-GO `/messages` auth**: `x-api-key` header → 200, `Authorization: Bearer` → 401. Confirms the `/messages` path uses Anthropic-shape auth exactly as Assesser's `opencode_provider.py:153` assumes. No bug — Bearer is simply the wrong scheme for this gateway path.

**Decisions / deviations from the original plan:**
- OpenRouter added as a 4th new backend (was not in the original Phase 0). Probe 1c folded into the same script.
- `gemini-2.0-flash` (originally used for probe 4) found deprecated → switched to Assesser's `tiers.py` verified models (`gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-3.6-flash`).
- Free-tier zen catalog gains `laguna-s-2.1-free`; to be added to the Phase 5 default catalog.

**Verification result:** verified — all probes ran live against production APIs with real keys; no silent skips.

**2026-08-31**: Phase 1 (per-backend model selection: config, store, API, factory) implemented on branch `feat/multi-backend-model-select`.

- **Context**: build the persisted/live model-selection plumbing so a later dispatch can pick up a runtime model choice without a restart, per the locked "global, persisted, live" decision.
- **Actions**: added `Settings.backend_models: dict[str,str]` + `Settings.backend_models_path` (`jsa/config.py`); new `jsa/agents/model_catalog.py` (`DEFAULT_CATALOG`, `SUPPORTS_MODEL_SELECTION`, `merged_catalog`); new store `jsa/store/backend_models.py` mirroring `preferences.py`'s sync-wrapped-in-`asyncio.to_thread` pattern; new routes `jsa/api/routes_backend_models.py` (`GET /api/backend-models`, `GET /api/backend-models/{backend}`, `PUT /api/backend-models`); `make_backend_factory`'s `_backend_factory` now resolves each model through a `_model_for(name, default)` closure implementing the precedence `model_override > settings.backend_models[name] > flat default`; `server.py`'s startup event hydrates `settings.backend_models` from the persisted file before the factory is built. Registered the new router. Added `tests/backend/test_model_catalog.py` (offline shape), `tests/backend/test_backend_models_store.py` (store round-trip + API + startup-hydration), and precedence cases in `tests/backend/test_make_backend_factory.py`.
- **Decisions**: `GET /api/backend-models/{backend}`'s `source` field is hardcoded `"catalog"` for now — no live fetcher exists until Phase 5, this is the pre-committed fallback path, not a shortcut. `PUT` validates registry-membership and `SUPPORTS_MODEL_SELECTION` only, not catalog-membership (not in the Phase 1 spec — the catalog is advisory, not an enum). The CLI's one-shot bootstrap `make_backend_factory(settings)` call (`cli.py:254`) deliberately gets no hydration — it's a single infer call outside the dispatch loop, not a stage; hydration is scoped to `server.py`'s startup only, matching where the plan places it. `claude-cli`/`anthropic` share the same `DEFAULT_CATALOG` entries (`claude-haiku-4-5`, `claude-sonnet-5`, `claude-opus-5`) since both already share `Settings.model`'s ID convention today — this is not a new coupling, just cataloging what already exists.
- **Verification**: `venv/bin/python -m pytest -q -m "not integration"` — 1402 passed, 2 skipped (one unrelated pre-existing flaky failure on first run, `TestHappyPath::test_two_jobs_reach_review`, passed clean on immediate re-run — not caused by this change, not investigated further as pre-existing). Verified — full suite green on branch `feat/multi-backend-model-select`, no regressions.

**2026-08-31**: Phase 2 (`mistral`, `openrouter`, `opencode-go` backends) + Phase 2a (OpenCode-Zen protocol dispatch decision) implemented on branch `feat/multi-backend-model-select`.

- **Context**: build the three new OpenAI-compatible backends on a shared base extracted from `opencode_zen.py`, and resolve whether zen's free-tier catalog needs the same per-model protocol dispatch.
- **Actions**: new `jsa/agents/_openai_compat.py` — `OpenAICompatBackend` (shared start/restore/send/end, three-way error classification, per-session structured→sentinel downgrade, sentinel nudge, `retry_transient` generic retry helper), extracted from `opencode_zen.py` **without modifying that module** (confirmed untouched via `git status`; its 935-line test file passes unchanged — the gated refactor was correctly *not* attempted, duplication accepted per the plan). `jsa/agents/mistral.py` (`MistralBackend`, zero overrides) and `jsa/agents/openrouter.py` (`OpenRouterBackend`, overrides `_extra_payload` for the mandatory `provider.require_parameters` routing guard) both subclass it directly. `jsa/agents/opencode_go.py` (`OpenCodeGoBackend`) subclasses it too but overrides `start_session`/`restore_session`/`send_message` to dispatch per-model on a module-level `_PROTOCOL` table: `"chat"` models fall through to the inherited OpenAI-compatible machinery (structured output enabled), `"messages"` models route through a dedicated Anthropic-shape `/v1/messages` implementation (`x-api-key` auth, top-level `system`, sentinel-only — no `response_format` ever sent) built on raw `httpx` with its own three-way classification adapted to the Anthropic error envelope shape (deliberately not built on the `anthropic` SDK, since `anthropic_api.py` has no `AgentBackendUnavailable` path — see CLAUDE.md). `supports_structured_output` is set as an **instance** attribute in `OpenCodeGoBackend.__init__` from the selected model's protocol; verified via a repo-wide `grep -rn "supports_structured_output"` (excluding tests) that the only non-test read site (`stages.py::_structured_schema_for`) reads it off an instance, so no class-level read exists that would see the wrong (inherited `True`) default.
- **Model IDs and protocol table provenance**: `mistral-small-2603` (Mistral default) and `nvidia/nemotron-3-nano-30b-a3b` (OpenRouter default) come from the reference bake-off project's `providers/tiers.py` (CHEAP tier, verified against each vendor's pricing page, Aug 2026) and are confirmed present in the Phase-0 live `/models` probe. OpenCode-GO's `_PROTOCOL` table (23 models) was re-derived via a live fetch of `https://opencode.ai/docs/en/go/` **during Phase 2 implementation** (not captured by the Phase-0 probe script, which only confirmed the `/messages` auth header shape for one model). Three entries are double-corroborated (docs fetch + the reference project's own `PROTOCOL_DISPATCH` table + live probe #5): `glm-5.3`→chat (chosen as `default_model`), `kimi-k3`→chat, `qwen3.8-max`→messages. The other ~20 entries are single-source (the docs fetch only) — flagged in the module's docstring for Phase 5's catalog curation to cross-check against the live `/models` listing rather than trust uniformly. `/responses`-protocol models (`grok-4.6`, `gpt-5.6-luna`, `muse-spark-1.2-contributor`) are excluded from `_PROTOCOL` entirely — selecting one raises `ValueError`, consistent with the plan's "an unknown model raises ValueError client-side" guard.
- **Phase 2a determination: no transport change to `opencode_zen.py`.** Live-fetched the full raw endpoints table from `https://opencode.ai/docs/zen/` (56 rows) and cross-referenced it against the six free-tier models already in `DEFAULT_CATALOG["opencode-zen"]` (`jsa/agents/model_catalog.py`, seeded in Phase 1). Four are directly confirmed `/chat/completions` in the table: `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`. `muse-spark-1.2-contributor-free` is confirmed `/responses` (already excluded from the catalog per the original plan — no change needed). `deepseek-v4-flash-free` is not itself a row in the docs snapshot, but its non-free sibling `deepseek-v4-flash` is `/chat/completions`, and every other free-tier sibling pair in the table (e.g. `mimo-v2.5` / `mimo-v2.5-free`) shares its base model's protocol — inferred `/chat/completions`. `laguna-s-2.1-free` does not appear in this docs snapshot at all (likely too new to be documented yet); its protocol is genuinely undetermined, but a structural check across the **entire** 56-row table found no `/messages`-protocol model anywhere carries a `-free` suffix (every `/messages` entry is a premium Claude/Qwen model) — the free tier and the `/messages` protocol never co-occur in this catalog. On that pattern, conclusion: **no free-tier zen model needs `/messages`**, so `opencode_zen.py` is left untouched (confirmed via `git status`) and its dispatch is a no-op, exactly the plan's "expected outcome" branch. Residual risk (`laguna-s-2.1-free`'s protocol being genuinely unconfirmed) is noted for Phase 5/6's live testing to catch as a wire-level 4xx if the inference turns out wrong — it would surface as `AgentBackendUnavailable`, not a silent miscall, since zen's existing single-endpoint code doesn't change.
- **Tests added**: `tests/backend/test_openai_compat.py` (58 shared-machinery cases via `MistralBackend`, the purest concrete subclass — start/restore/send/end, timeout, three-way classification incl. retry-then-succeed and retry-exhaustion, sentinel nudge incl. double-failure propagation, structured request shape, downgrade-on-unparseable incl. persistence across `send_message`), `tests/backend/test_mistral.py` (config-only, since machinery is covered above), `tests/backend/test_openrouter.py` (config + the routing-guard payload assertion on both sentinel and structured requests — the one CLAUDE.md calls out as "silent when missing, only a test catches its removal"), `tests/backend/test_opencode_go.py` (protocol dispatch, unknown-model `ValueError`, instance-level `supports_structured_output`, chat-path smoke test, and a full messages-path suite: endpoint/auth-header shape, system-prompt placement, no-structured-schema guarantee, nudge, three-way classification adapted to the Anthropic error envelope, `AgentTimeout`, `end_session`). Not attempted here (explicitly Phase 7's job per the plan): exhaustive per-backend cloning of `test_opencode_zen.py`'s full ~90-case matrix, BF-19 cross-backend chain tests, CLI/registry/config wiring tests (registry.py/config.py/cli.py are untouched — Phase 4's job), and the offline `test_model_catalog.py` additions for these three backends (Phase 5's job, since `DEFAULT_CATALOG`/`SUPPORTS_MODEL_SELECTION` aren't touched yet either).
- **Decisions**: kept `registry.py`/`config.py`/`cli.py` completely untouched — these three backends are not yet registered or selectable via `--backends`, by design (the plan's Phase 2 "Solutions" section only lists the three new agent modules; registration is explicitly Phase 4's job, and the plan's own Phase 4 "Problems" section confirms registering without the `cli.py:37` validation-set update would produce a confusing pre-pydantic rejection). **Ordering constraint for the next session**: Phase 4's `register()` calls will make `test_model_catalog.py` (Phase 1) fail its "every `_REGISTRY` name has a `DEFAULT_CATALOG` entry" assertion, since `DEFAULT_CATALOG` isn't populated for these three backends until Phase 5 — so Phase 4 cannot land before Phase 5, or must land together with it. `OpenCodeGoBackend`'s `/messages` path was built on raw `httpx` rather than the `anthropic` SDK specifically to get a real `AgentBackendUnavailable` path (see CLAUDE.md's explicit warning against copying `anthropic_api.py`'s pattern for the new backends).
- **Verification**: `venv/bin/python -m pytest -q -m "not integration"` — 1460 passed, 2 skipped, 1 failed (`TestHappyPath::test_two_jobs_reach_review` — the same pre-existing flake noted in the Phase 1 Change Log; re-ran in isolation and it passed clean, confirming it's unrelated to this change). `tests/backend/test_opencode_zen.py` re-run standalone: 74/74 passed, file confirmed untouched via `git status`. Verified — full suite green on branch `feat/multi-backend-model-select`, no regressions.
- **Assumption stated (per the plan's "Assumption to state" note):** none of the three new backends implements `run_research` — matching `anthropic` and `opencode-zen` today, same pre-existing gap, not a new one. `stages.py`'s `hasattr` gate means the cover-letter research pass degrades to `_research_placeholder()` for all of them.
- **Landmine flagged for Phase 4/5 (advisor-caught, not yet fixed — no code changed for this):** Phase 1's `PUT /api/backend-models` validates registry-membership and `SUPPORTS_MODEL_SELECTION` only, never catalog membership. Once Phase 4 registers `opencode-go`, a PUT of a real go model that's deliberately outside `_PROTOCOL` (e.g. `grok-4.6`, a `/responses` model) will return 200 and persist, and the *next dispatch* will hit `OpenCodeGoBackend.__init__`'s `ValueError` — which is none of BF-19's three typed exceptions, so it falls into `_run_one`'s generic `except Exception` and hard-fails the job with no chain engagement (exactly the failure shape CLAUDE.md's BF-19 section warns about). Phase 5's "intersected with `_PROTOCOL`'s known models" catalog-population plan covers the *catalog* path but not this *PUT* path. Fix at Phase 4/5 time: either give `PUT /api/backend-models` catalog-membership validation for `opencode-go` specifically, or change the constructor's `ValueError` to `AgentBackendUnavailable` so a bad PUT degrades gracefully into a BF-19 switch instead of a hard failure.
- **`laguna-s-2.1-free`'s protocol is Phase 5's explicit verification item, not just a residual note.** It's live in `DEFAULT_CATALOG["opencode-zen"]` since Phase 1 but absent from the live docs snapshot fetched during 2a, and there is now a real precedent (`muse-spark-1.2-contributor-free`) of a `-free` model NOT being `/chat/completions`. Worst case if the inference is wrong is a clean `AgentBackendUnavailable` + BF-19 switch (zen's single-endpoint code is unchanged, so this fails loud, not silent) — but Phase 5 should cross-check this specific model ID against the live `/models` response before shipping it as selectable, rather than inheriting the assumption.

**2026-08-31**: Phase 3 (`gemini` native `generateContent` backend) implemented on branch `feat/multi-backend-model-select`.

- **Context**: build `GeminiBackend` against Google's native `generateContent` endpoint (not the OpenAI-compat shim `google-cli` uses, no `google-genai` SDK dependency), reusing as much of the Phase-2 shared machinery as the wire-shape mismatch allows.
- **Actions**: `jsa/agents/gemini_api.py` — `GeminiBackend` subclasses `OpenAICompatBackend` but overrides only `_call_api_once` (payload build + response parsing) plus `start_session`/`send_message` (for the schema-rejection downgrade, below); everything else — session handle, nudge-on-missing-sentinel, per-session structured→sentinel downgrade on an unparseable 2xx reply, `retry_transient`'s three-way classification — is inherited unchanged, mirroring how `OpenCodeGoBackend`'s `/messages` path reuses `retry_transient`/`TransientBackendError` without inheriting the `/chat/completions` payload builder. Added `inline_defs()` to `jsa/schema/turn_models.py` — a recursive `$ref`/`$defs` dereferencer (cycle-guarded) that also strips `additionalProperties`/`title`/`default`, per the Phase-0 probe's confirmed fallback #1. `GeminiBackend` sends `generationConfig.responseSchema` = `inline_defs(json_schema_for(stage))` in structured mode.
- **Three fixes made after an advisor pass caught wire-level bugs the mocked tests couldn't see on the first draft** (all fixed before landing, not deferred):
  1. **`maxOutputTokens` was unset on the first draft** — Gemini would have used its own undocumented per-model default, and the new `finishReason == "MAX_TOKENS"` → `ProtocolError` check would have fired against an uncontrolled limit. Fixed: `generationConfig.maxOutputTokens` is now pinned to `8192` unconditionally (both structured and sentinel mode), matching every other backend's `max_tokens`.
  2. **`contents` role mapping had no adjacency/leading-role normalization** — Gemini rejects a `contents` array with two adjacent same-role entries or a non-`user` first entry; a BF-19-switched or resumed session's replayed history is not guaranteed to preserve strict alternation. Fixed: `_to_gemini_contents` merges adjacent same-role turns into one content entry with multiple `parts` instead of emitting an invalid array.
  3. **A rejected structured-output schema (permanent 4xx) was classified as plain `AgentBackendUnavailable`**, contradicting the plan's Phase 3 "Critical" line ("a schema rejection must trigger the per-session downgrade, never `AgentBackendUnavailable`"). Fixed: `_call_api_once` now raises the module-private `_SchemaRejected` (a subclass of `AgentBackendUnavailable`, so an unhandled instance still classifies correctly) instead, whenever `structured_schema is not None`; `start_session`/`send_message` catch it and retry the same call once with structured mode off, landing the session in the same downgraded state an unparseable-2xx-reply downgrade produces. A retried sentinel-mode failure propagates normally.
- **Decisions / provenance** (advisor-flagged, documented rather than silently left ambiguous):
  - **Auth header form is plan-specified, not live-probe-confirmed.** The Phase-0 probe script (`scripts/probe_phase0_gemini.sh`) used `?key=$gemini_key` in the query string and succeeded; this implementation ships the plan's locked `x-goog-api-key` header instead (both forms are Google-documented, but only the query-string form has live evidence in this repo). Flagging for a user-run live smoke test before relying on it in production.
  - **A safety-filtered / empty-candidates reply is classified `TransientBackendError`** (retried `_MAX_ATTEMPTS` times, then `AgentBackendUnavailable`) rather than a distinct immediate-fail case — matches the existing "no choices"/"null content" precedent in `_openai_compat.py` and `opencode_go.py`, but note it burns up to 3 full-timeout calls on what may be a deterministic `finishReason: "SAFETY"` block. Not fixed here; flagged for Phase 5/7 if it proves to matter live.
  - **`default_model = "gemini-3.1-flash-lite"`** — Assesser's CHEAP tier (`providers/tiers.py`, verified Aug 2026) and confirmed present + `generateContent`-capable in the Phase-0 live `/models` probe. Noted here (as Phase 2 did for `mistral-small-2603`/`glm-5.3`) so Phase 5's `DEFAULT_CATALOG` population doesn't have to rediscover the provenance.
  - `registry.py`/`config.py`/`cli.py` kept untouched, same rationale as Phase 2 — registration is explicitly Phase 4's job.
- **Tests added**: `tests/backend/test_gemini_api.py` (32 cases) — start/restore/send/end, model-specific URL, `x-goog-api-key` header (not Bearer) + `GOOGLE_API_KEY` fallback, `systemInstruction` placement, timeout, three-way error classification incl. `RESOURCE_EXHAUSTED`→`AgentLimitReached`, retry-then-succeed, sentinel nudge, `MAX_TOKENS` truncation in both structured and sentinel mode, structured request shape (`inline_defs` output has no `$defs`/`$ref`/`additionalProperties`), `maxOutputTokens` always present, contents role-merging (both a restore-then-send scenario and a manually-injected adjacent-role turn), and the schema-rejection downgrade (structured-mode 4xx downgrades-and-retries; sentinel-mode 4xx raises immediately with no retry; downgrade mid-session via `send_message`). `tests/backend/test_turn_models.py` gained `TestInlineDefs` (6 cases: no banned keys survive across all three stage schemas, a `$ref`-inside-`anyOf` case — the exact shape the Phase-0 probe's first inliner attempt missed — description preservation, a no-`$defs` no-op case, and an unresolvable-`$ref` cycle-guard case).
- **Verification**: `venv/bin/python -m pytest -q tests/backend/test_gemini_api.py tests/backend/test_turn_models.py` — 101 passed. Full suite `venv/bin/python -m pytest -q -m "not integration"` — 1499 passed, 2 skipped, 6 deselected, run three times. One flaky failure appeared on two of those runs, but a **different** test each time (`TestOrchestratorTimeoutDetection::test_agent_timeout_switches_to_next_backend_in_chain` once, `TestStartTunnelUrlPrinted::test_daemon_thread_is_started` once) — both pass in isolation, and `git stash -u` + re-run confirmed the same flake reproduces on the pre-Phase-3 baseline (Phase 1 only), so this is pre-existing full-suite flakiness (timing/ordering, not this change) rather than a regression. Verified — no regressions attributable to Phase 3.
- **Not yet run**: the live Gemini smoke test (real API key, confirming the `x-goog-api-key` header form and the inlined-schema request actually succeed end-to-end) — deferred to the plan's user-run integration-test step, same as every other network-touching verification in this plan.

**2026-08-31**: Phase 4 (registration, config fields, CLI validation) + Phase 5 (live model listing + catalog) implemented together on branch `feat/multi-backend-model-select`, per the ordering constraint flagged in the Phase 2 Change Log (`DEFAULT_CATALOG`'s registry-coverage test forces the two phases to land together).

- **Context**: register the four Phase 2/3 backends (`mistral`, `openrouter`, `gemini`, `opencode-go`) so they're actually selectable via `--backends`/`--backend`, and give each a live model-listing endpoint with catalog fallback.
- **Actions (Phase 4)**: `jsa/agents/registry.py` — four new `register()` calls. `jsa/cli.py` — extended the hardcoded `_VALID_BACKENDS` literal (kept as a literal per the plan's stated preference over `set(_REGISTRY)`, guarded by a new drift test) plus both `--backend`/`--backends` help strings. `jsa/config.py` — eight new flat fields (`mistral_model`/`_timeout`, `openrouter_model`/`_timeout`, `gemini_model`/`_timeout`, `opencode_go_model`/`_timeout`, all `180.0` timeouts) with defaults hardcoded as literals matching each backend class's `default_model` (following the existing `model`/`opencode_zen_model` precedent — not imported, to avoid pulling agent modules into `config.py`'s import graph) plus the `backend` field's doc-comment. `jsa/server.py` — four new explicit `_backend_factory` branches, each resolving through the existing `_model_for` precedence closure; left the bare fall-through untouched so `google-cli` stays its only resident and an unregistered name still raises `KeyError` (pinned by `test_make_backend_factory_not_a_backend`).
- **Actions (Phase 5)**: `jsa/agents/model_catalog.py` — added `DEFAULT_CATALOG`/`SUPPORTS_MODEL_SELECTION` entries for the four backends (`opencode-go`'s catalog is `sorted(_PROTOCOL)`, imported directly from `jsa.agents.opencode_go` rather than hand-duplicated, so every catalog entry is guaranteed constructible — closes the practical PUT-then-hard-fail gap flagged as a "landmine" in the Phase 2 Change Log, without changing `OpenCodeGoBackend.__init__`'s `ValueError` contract, which Phase 2's plan text and `test_opencode_go.py` both pin). Added `list_models(backend, *, catalog_overrides=None) -> (models, "live"|"catalog")`: an in-process TTL cache (300s, keyed per backend, written **only on a successful fetch** — a transient blip must not pin the stale/empty catalog result for the full TTL) over six live fetchers (`opencode-zen`, `opencode-go`, `mistral`, `openrouter`, `gemini`, `anthropic`), each a short-timeout (10s) raw `httpx` GET with the auth shape each provider actually needs (`Authorization: Bearer` for the OpenAI-compatible four, `x-goog-api-key` for Gemini matching `gemini_api.py`'s own header choice, `x-api-key`+`anthropic-version` for Anthropic); `claude-cli` (no listing API) and `google-cli` (no selection at all) short-circuit straight to the catalog with zero network calls. Any exception (missing key, timeout, HTTP error, empty result) falls back silently to `merged_catalog(catalog_overrides)`, never raises. `jsa/api/routes_backend_models.py`'s `GET /api/backend-models/{backend}` now calls `list_models` (threading the caller's persisted `BackendModels.catalog` through as `catalog_overrides`) instead of the Phase 1 hardcoded `source: "catalog"` placeholder.
- **Gemini prefix fix caught before landing**: the live `/v1beta/models` listing returns ids as `"models/gemini-x"`, but `GeminiBackend`'s request URL is already `".../models/{model}:generateContent"` — an unstripped id would have silently doubled to `models/models/...` and 404'd only at actual dispatch time, not at listing time. `_fetch_gemini` strips the `models/` prefix; a mocked-response test (`test_gemini_live_fetch_strips_models_prefix`) pins this against a response payload that includes the prefix, so a regression would fail loudly in CI rather than surfacing as a live 404.
- **`laguna-s-2.1-free` decision**: withheld from both `DEFAULT_CATALOG["opencode-zen"]` and the live-listing filter (`_ZEN_FREE_EXCLUDE`), not just left as an inherited assumption a third time (the Phase 2a Change Log escalated this specifically — its `-free` suffix does not confirm `/chat/completions`, and `muse-spark-1.2-contributor-free` is live proof a `-free` id can be on a different protocol). Follow-up for the user's next live-key session: re-run `scripts/probe_phase0.sh`'s probe 1a and cross-check `laguna-s-2.1-free` against `https://opencode.ai/docs/zen/`'s endpoint table directly (not inferred from the `-free` pattern); if confirmed `/chat/completions`, add it back to both the catalog and `_ZEN_FREE_EXCLUDE`'s exclusion removed.
- **Tests added**: `TestValidBackendsDriftGuard` (`test_cli_backends.py`) asserting `_VALID_BACKENDS == set(_REGISTRY)`; `TestNewBackendsFactory` + `TestSettingsDefaultsMatchBackendClassDefaults` (`test_make_backend_factory.py`) covering all four new backends' factory construction, runtime-selection precedence, explicit-override precedence, and the `Settings.<x>_model == <Backend>.default_model` drift guard the advisor flagged (two independently-maintained literals with no compiler check otherwise); `test_model_catalog.py` extended with per-backend catalog-membership assertions, an OpenRouter namespacing assertion, an `opencode-go` catalog-subset-of-`_PROTOCOL` assertion, and three new test classes (`TestListModelsNoListingApi`, `TestListModelsLiveFetch`, `TestListModelsFallback`, `TestListModelsCache`) covering all six live fetchers, the Gemini prefix-stripping fix, cache hit/expiry, and — per the plan's own explicit callout — that a failed fetch does **not** write a cache entry.
- **Scope fence honored** (per advisor): did not touch CLAUDE.md/README (Phase 7), `test_bf18_limit_detection.py` per-backend classification cases (Phase 7), `Header.tsx`/`api.ts` (Phase 6), or the pre-existing `fit_model`-inapplicable-to-non-Claude-backend interaction (already correctly scoped to `google-cli` only, per the plan's own Phase 4 solution #6).
- **Verification**: `venv/bin/python -m pytest tests/backend/test_model_catalog.py tests/backend/test_make_backend_factory.py tests/backend/test_cli_backends.py tests/backend/test_backend_models_store.py -q` — 125 passed. Full suite `venv/bin/python -m pytest -q -m "not integration"` — 1529 passed, 2 skipped, 6 deselected, run twice; `TestOrchestratorTimeoutDetection::test_agent_timeout_switches_to_next_backend_in_chain` failed both full runs but passed clean in isolation, and `git stash -u` back to the Phase 1 commit (4e067c1) reproduced a clean run there too — consistent with the same class of timing/ordering full-suite-only flakiness documented in the Phase 3 Change Log (different test tripped there), not a regression from this change. `python -c "import jsa.agents.model_catalog; import jsa.agents.registry; import jsa.cli; import jsa.server; import jsa.config"` confirmed no import cycle from the new `model_catalog.py -> jsa.agents.opencode_go` import. Verified — no regressions attributable to Phase 4/5.
- **Not yet run**: the live end-to-end smoke test (`--backends mistral,openrouter,gemini,opencode-go`, `curl /api/backend-models`, `curl /api/backend-models/mistral`, a `PUT`) and every `list_models` live fetcher against the real APIs — deferred to the plan's user-run integration-test step, same as every other network-touching verification in this plan. Phase 7's offline `test_model_catalog.py`/`test_bf18_limit_detection.py` additions and the frontend submenu (Phase 6) remain outstanding.

**2026-08-31**: Context — user ran a live `laguna-s-2.1-free` probe (wanted to include it: "very well coherent on writing"). Actions — wrote `scripts/probe_laguna.sh` (GET `/models` presence check + up-to-3-attempt POST to the hardcoded `/chat/completions` endpoint with production's own 1.0s/3.0s backoff + a diagnostic-only POST to `/responses`). Live result: present in `/models` (63 total), but `/chat/completions` 503'd ("Endpoint is unavailable") on all 3 attempts and `/responses` 500'd too — genuinely unservable on this key right now, not a one-off blip, not a protocol question. Decision — user chose "leave excluded, re-probe later" over "add anyway" when shown the evidence (BF-19 would burn a full retry-then-switch on every real use of a selectable-but-always-503ing model). Confirmed the existing exclusion already covers both the catalog fallback and the live-listing path (`_ZEN_FREE_EXCLUDE` filters `_fetch_opencode_zen`'s raw response too, pinned by `test_opencode_zen_live_fetch_filters_to_free_tier`) — no code change needed, `scripts/probe_laguna.sh` kept in the repo for a future re-check. Then ran `/code-review medium --fix` over Phases 4–5: one finding — `backend_for()` (`jsa/agents/registry.py`) let a backend constructor's `ValueError` (e.g. `OpenCodeGoBackend` rejecting an unrecognized model — reachable since `PUT /api/backend-models` never validates the model against any catalog before persisting it) escape uncaught into `Orchestrator._run_one`'s generic `except Exception`, hard-failing the job on the very first backend with no BF-19 fallback-chain engagement. Fixed: `backend_for` now catches `ValueError` from the constructor call and re-raises `AgentBackendUnavailable` (CLAUDE.md's own classification for "a bad model/config"), chained via `raise ... from exc`. Added `TestBackendForConvertsConstructorValueError` (`test_cli_backends.py`, 2 tests) pinning this against the real `opencode-go` registration. Verification — full suite `venv/bin/python -m pytest -m "not integration"`: 1532 passed, 2 skipped, 6 deselected (up from 1529 pre-fix + the 2 new tests + 1 already-landed test from a prior run); no failures, no flake this run.

**2026-08-31**: Phase 6 (frontend: nested model submenu in the header dropdown) implemented on branch `feat/multi-backend-model-select`.

- **Context**: wire the header's backend failover-queue dropdown so each backend row exposes its currently selected model and opens a second anchored panel to pick another, per the plan's Phase 6 solutions section.
- **Actions**: `frontend/src/api.ts` — added `getBackendModels()`, `getBackendModelsFor(backend)`, `putBackendModel(backend, model)` beside the `getPreferences`/`putPreferences` pair. `frontend/src/components/Header.tsx` — added `BACKEND_LABELS` entries for the four Phase 2–3 backends (`mistral`→"MISTRAL", `openrouter`→"OPENROUTER", `gemini`→"GEMINI API", `opencode-go`→"OPENCODE GO"); a `ModelSubmenu` component with the `LanguagePill`-style filter input (shown once a catalog exceeds 12 entries, per 5b); new state for `supportsModelSelection`, `selectedModels`, `openModelMenuFor`, a per-backend `modelMenus` cache, and `modelFilter`; each failover-queue row now shows the selected model (or an italic "no model selection" caption when `supportsModelSelection[id]` is false) and, when selectable, opens the submenu on click with a lazy fetch via `getBackendModelsFor`; selection is optimistic (`selectModel`) with revert-on-failure, mirroring `store.ts`'s `setLanguage`. New i18n keys added to `strings.en.json` only: `header.modelMenuTitle`, `header.noModelSelection`, `header.modelsFromCatalog`, `header.modelFilterPlaceholder`, `header.modelSelectFailed`.
- **Bug caught and fixed during live verification (not by the unit tests): the submenu was invisible.** The first implementation nested `ModelSubmenu` inside each row (itself inside the failover-queue panel div), positioned via `left:"100%"` relative to the row. `jsdom`'s `getBoundingClientRect()` always returns zeros, so the 16 existing Vitest cases plus 3 new ones all passed green despite this. A live Playwright smoke test against a real running server (see below) showed the submenu rendering with zero visible pixels: `panelBase(T, {chamfer: 10})` sets `clip-path` on the chamfered-skin panel for its corner-cut visual, and CSS `clip-path` clips ALL descendant painting to the element's own box — including absolutely-positioned children that escape via `left:100%`, the same way `overflow:hidden` would. **Fix**: the submenu is now a *sibling* of the failover-queue panel (both direct children of the outer `ref={backendMenuRef}` wrapper, which has no `clip-path` of its own), positioned via a fixed `left: QUEUE_PANEL_WIDTH(240) + SUBMENU_GAP(6)` and a `top` measured at open-time from `rowEl.getBoundingClientRect().top - containerEl.getBoundingClientRect().top` (via a new `rowRefs` map), rather than CSS-relative positioning nested inside the clipped ancestor. This is documented as a comment on `QUEUE_PANEL_WIDTH`/`SUBMENU_GAP` in `Header.tsx` so a future reader doesn't reintroduce the nested version as a "simplification."
- **Tests added**: `frontend/src/__tests__/Header.test.tsx` — updated the `vi.mock("../api", ...)` to include the three new methods (`getBackendModels` resolving to `{selected: {}, supports_model_selection: {}}` by default), and added a `"per-backend model submenu"` describe block: submenu opens on row click and renders fetched models with the selected one check-marked (scoped via a `queuePanel()` helper + `getByRole("button", {name: exact-model-id})` to avoid ambiguity with the trigger button's own repeated backend-label text), PUT-on-click with revert-on-failure (asserts the row's optimistic caption flips immediately, then reverts once the rejected PUT settles), and no submenu affordance (no `role="button"`, no fetch) for `google-cli`. Also added the same `getBackendModels` mock to `frontend/src/__tests__/mobile_responsive.test.tsx` (a never-resolving promise, matching that file's existing `config` mock idiom, since it also renders `<Header/>`) — that file's 4 tests were failing with `api.getBackendModels is not a function` before this fix, an artifact of the shared-mock-per-file convention rather than a Phase 6 regression in the tested behavior itself.
- **Live verification (per CLAUDE.md's "Web-Application" instruction to visually verify UI changes in a browser before reporting done, and per this plan's own Verification section calling out Playwright specifically for the header-submenu check)**: started the real `jsa` server pointed at a scratch `--db` path (isolating `cv_structure_path`/`backend_models_path`, both derived from `db_path`, from the user's real `~/.jsa` state) with `--backends mistral,openrouter,gemini,opencode-go,google-cli --no-browser` on a throwaway port. Since no `cv_structure.json` exists at that scratch path, the CV-structure gate blocks all dispatch — jobs stay `pending` and no backend is ever actually called, so this needed no API keys and triggered no real spend. Drove it with a local Playwright (chromium, already cached on this machine) script: confirmed (1) the dropdown's per-row model caption and the "no model selection" dimmed caption on `google-cli`; (2) the submenu opens showing the catalog-fallback models with the `header.modelsFromCatalog` hint (no `MISTRAL_API_KEY` in this shell, so `source: "catalog"` end-to-end, exercising the real Phase 5 fallback path); (3) the filter input appears for `opencode-go`'s 23-model catalog; (4) selecting a model fires the real `PUT /api/backend-models` and the row caption updates, verified by an in-page `fetch("/api/backend-models")` round-trip showing `{"selected":{"mistral":"mistral-small-2603"}, ...}`; (5) clicking the non-selectable `google-cli` row causes no crash and no visible state change (screenshots before/after are pixel-identical) — the stronger guarantee that this specifically fires *no* `getBackendModelsFor` request is pinned by the unit test, not independently re-confirmed by this Playwright run (the click used `force: true` against a text locator, which doesn't rule out the click landing on nothing). This Playwright pass is what caught the clip-path bug above — the Vitest suite alone (jsdom never computes real layout) would have shipped it silently.
- **Not attempted here (deliberately out of scope, per the plan's own phase split)**: `scripts/translate-ui.sh` was not run — it requires `ANTHROPIC_API_KEY` (confirmed unset in this shell, same as every other network-touching step in this plan) even for `--check`, since the bash wrapper gates on the key before ever invoking the Python module. Deferred to the user, consistent with Phase 0's "user-run" precedent. The full end-to-end smoke test with real backend dispatch (an actual job reaching a real Mistral/OpenRouter/Gemini/OpenCode-GO call) remains a user-run step requiring real exported keys.
- **Advisor-caught gap, fixed before landing**: the first draft's revert test only asserted the reverted-to value was present, via `waitFor` — which passes on the very first poll if the assertion is already (trivially) true, and never checked the *failed* optimistic value was gone. That shape would pass identically whether `selectModel`'s catch block reverted correctly, reverted to a wrong value, or (in a slower variant) hadn't run yet. Fixed both existing and new revert tests to assert the negative half first (`queryByText(newValue)` is `null`) then the positive half (`getByText(oldValue)` present). **Mutation-tested**: temporarily deleted the `catch` block's `setSelectedModels` call in `Header.tsx`, confirmed both revert tests fail (they did — `queryByText` never resolved to `null`), then restored the real implementation and confirmed all tests pass again. Also added a second revert case for the `previous === undefined` branch (a backend's first-ever selection, `selected: {}` — the actual shape returned by a fresh install per the live probe above) reverting to "—" rather than some stale value; this branch existed in `selectModel` since the first draft but had no test exercising it. Separately, `lastBackendSwitch`'s reorder effect now also resets `openModelMenuFor` to `null` — a BF-19 switch while a submenu was open would otherwise leave it pinned at a stale `top` offset measured against the pre-reorder row position, since `submenuTop` is captured once at open time and never recomputed.
- **Verification**: `npx tsc --noEmit` clean. `npm test` (frontend) — 279 passed, 21/21 files green (20 in `Header.test.tsx`, including the new mutation-verified revert case; the 4 `mobile_responsive.test.tsx` failures seen mid-phase were self-inflicted by the new `getBackendModels()` call hitting that file's separate `vi.mock("../api", ...)` with no matching entry, fixed in-phase by extending that mock the same way `Header.test.tsx`'s was). `npm run build` — succeeds, `jsa/static` regenerated (per the `rebuild-frontend-bundle` convention — the CLI serves the built bundle, not live source). Backend sanity re-run (`venv/bin/python -m pytest -q -m "not integration"`, no backend files touched this phase): 1531 passed, 2 skipped, 6 deselected, 1 failed (`TestOrchestratorTimeoutDetection::test_agent_timeout_switches_to_next_backend_in_chain` — the same full-suite-only flake first documented in the Phase 4/5 Change Log; re-ran in isolation immediately after, passed clean, confirming no regression). Live Playwright verification as described above — verified, no console errors or `pageerror` events across the whole flow.

**2026-09-01**: Phase 7 (tests and documentation) implemented on branch `feat/multi-backend-model-select`, closing out the plan.

- **Context**: fill the specific gaps Phases 2–6's own Change Log entries repeatedly flagged as "deferred to Phase 7" (`test_bf18_limit_detection.py` per-backend classification, integration tests, CLAUDE.md/README docs), rather than re-cloning `test_opencode_zen.py`'s full ~90-case matrix onto every new backend file — an advisor pass confirmed the existing per-backend files (`test_mistral.py`/`test_openrouter.py` thin + `test_openai_compat.py` covering the shared base via `MistralBackend`; `test_opencode_go.py` and `test_gemini_api.py` already near-full, 22 and 32 cases respectively from Phases 2–3) already give the shared machinery real coverage without duplication, and redirected effort at concrete named gaps instead.
- **Actions (offline unit tests)**: `tests/backend/test_openai_compat.py` — added `TestEndSession::test_raises_type_error_for_wrong_handle` (the `send_message` half already existed; `end_session`'s identical guard had no test), `TestStructuredQuestionKind` (only `kind="final"` had coverage before — the other half of the `CvTurn`/`ClTurn` union), `TestFitVerdictStructuredParse` (fit_assessment's `FitVerdict` shape, routed by `parse_structured_reply_for_schema`'s `is_fit` branch, had no backend-level test at all — every existing structured test used `Stage.cv_adjust`), and `TestSemanticFailureDoesNotDowngrade` (CLAUDE.md's "json.loads + kind routing ONLY, never CVDocument validation" invariant — confirmed by tracing `_route_structured_data`: a `kind="final"` reply with an empty `payload: {}` dict passes the parse layer's own `isinstance(payload, dict)` check and never reaches Pydantic, so it must not trip the unparseable-reply downgrade; this is a real assertion on existing behavior, not a speculative "should" — traced the actual code path before writing it, per the advisor's flag that this test was previously absent and a refactor could silently start downgrading on semantic gaps). `tests/backend/test_opencode_go.py` — added the `/messages`-protocol `send_message`'s own `TypeError`-on-wrong-handle test (a separate code path from the shared base's, with its own `isinstance` check, that had no coverage).
- **Actions (`test_bf18_limit_detection.py` — the plan's explicitly named, twice-deferred gap)**: added `TestOpenAICompatibleNewBackendsClassification` (parametrized over `MistralBackend`/`OpenRouterBackend`/`OpenCodeGoBackend`'s chat-protocol default) and `TestGeminiClassification` (separate, since `GeminiBackend` overrides `_call_api_once` with its own error-envelope shape) — both asserting 429→`AgentLimitReached`, permanent-4xx→`AgentBackendUnavailable`, exhausted-5xx-after-3-retries→`AgentBackendUnavailable`, and timeout→`AgentTimeout` fire through each concrete subclass, not just the shared base already exercised via `MistralBackend` elsewhere. This is the file the plan's own Phase 7 section named ("add per-backend classification cases so all four join the BF-19 chain correctly"); per-backend request/response-shape detail stays in each backend's own dedicated test file, this class only proves the classification actually reaches each concrete class.
- **Actions (integration tests, `@pytest.mark.integration`, off by default)**: five new files mirroring `test_opencode_zen_live.py`'s structure (duplicated `_load_dotenv_key` helper per file, matching that file's own no-shared-helpers convention) — `test_mistral_live.py`, `test_openrouter_live.py` (also the live check for the `provider.require_parameters` routing guard actually working, not just being present in the payload), `test_gemini_live.py` (also a live structured-mode round-trip — the first end-to-end check of `inline_defs()` against the real API, beyond the Phase-0 probe's one-off curl and the mocked shape assertion in `test_gemini_api.py`; and the first live check of the plan-specified `x-goog-api-key` header form, which the Phase-0 probe never actually exercised — it used `?key=` instead, per the Phase 3 Change Log's own flag), `test_opencode_go_live.py` (both wire protocols — chat AND the `/messages` path, since the Phase-0 probe only confirmed the `/messages` auth header shape with a bare curl, never through `OpenCodeGoBackend` itself), and `test_model_catalog_live.py` (the plan's named "models are valid once specified" check — every `DEFAULT_CATALOG` id asserted present in each backend's real live `/models` response, parametrized over all six backends with a listing API; fails loud, not silent-skip, if a fetch that should succeed instead falls back to `source: "catalog"`).
- **Actions (documentation)**: `CLAUDE.md` — four amendments per the plan's Phase 7 doc list: (1) new "Model selection (per-backend, runtime)" section (the `backend_models` dict, the `model_override > backend_models[name] > flat default` precedence, the store file, the three API endpoints, why `/api/config` is deliberately not used — the 8s `hydrateLanguage` boot-path race — and why the fit factory needs no extra wiring); (2) the "Structured output" capability-flag paragraph now names all five structured-capable backends and gets a new paragraph on `opencode-go`'s instance-level `supports_structured_output` — flagged by the advisor as the single highest-risk undocumented gap, since it directly contradicts the section's own pre-existing "hard-coded ClassVar, never runtime-detected" rule and a future session could plausibly "fix" it back, silently re-enabling prompt-injected JSON on a gateway path proven not to honor it; (3) new "The four multi-backend-model-select backends" subsection under BF-19 (three-way classification provenance, the explicit "do not build on `anthropic_api.py`'s pattern" warning, and the OpenRouter routing-guard paragraph — "removing this flag is a silent regression, not a simplification"); (4) env-var fallback chains, folded into (3). `README.md` — scoped to what this branch made wrong or incomplete, not a full rewrite: the "Three AI backends"/"Three-stage..." overview lines (now correctly "eight", also fixing pre-existing staleness from `opencode-zen` never having been counted either), the architecture Mermaid diagram (four new nodes), the sentinel-protocol and structured-output paragraphs (now naming all five structured-capable backends and `opencode-go`'s dual-protocol split), the `--backend`/`--backends` CLI table row, four new `### backend` subsections mirroring the existing four's format, and the repo-layout `agents/` line.
- **Deliberately not done, per the advisor's read and the plan's own precedent**: cloning `test_opencode_zen.py`'s full ~90-case matrix onto `test_mistral.py`/`test_openrouter.py` — Mistral is already the concrete subclass `test_openai_compat.py`'s 26 cases run against directly, and OpenRouter's only real deviation (the routing guard) already has its own dedicated payload-shape test from Phase 2; duplicating the shared-machinery matrix a third and fourth time would test the same code path with no new coverage. `scripts/translate-ui.sh`/`--check` (needs `ANTHROPIC_API_KEY`, unset in this shell — user-run, same as Phase 6). `npm test`/`npm run build` (no frontend files touched this phase — Phase 6 already rebuilt the bundle and CLAUDE.md's `rebuild-frontend-bundle` rule only applies after a UI change).
- **Verification**: `venv/bin/python -m pytest tests/backend/test_openai_compat.py tests/backend/test_opencode_go.py tests/backend/test_bf18_limit_detection.py tests/backend/test_mistral.py tests/backend/test_openrouter.py tests/backend/test_gemini_api.py -q` — 143 passed (was 122 before this phase's additions: 22/22/32/7/7/32 across the six files, +21 new: 4 in `test_openai_compat.py`, 1 in `test_opencode_go.py`, 16 in `test_bf18_limit_detection.py`). Full suite `venv/bin/python -m pytest -q -m "not integration"` run three times: 1552/1552/1547 passed, 2 skipped, 21 deselected each time, with 1/0/6 failures respectively — every failing test passed clean in isolation immediately after (spot-checked `TestOrchestratorTimeoutDetection::test_agent_timeout_switches_to_next_backend_in_chain`, `TestLogEventsBF7::TestPointA_PickedUpLogEvent`, `TestOrchestratorTimeoutDetection`/`TestKickUnblocksLoop` from `test_orchestrator.py`), and a different set of tests failed each run — the same full-suite-only timing/ordering flakiness signature documented in every prior phase's Change Log (Phases 1, 3, 4/5, 6 each hit a different tripped test), not a regression introduced by this phase's additions. New integration test files collect cleanly (13 new tests across 5 files, confirmed via `--collect-only`) and are correctly deselected by `-m "not integration"` (0 collected, 21 deselected against the whole `tests/backend/integration/` dir including the 6 pre-existing tests). Verified — no regressions attributable to Phase 7.
- **Not yet run (user-run, needs real exported keys — same as every other network-touching step in this plan)**: every new `@pytest.mark.integration` test (`! pytest -v -m integration`), `scripts/translate-ui.sh` + `--check`, and the plan's end-to-end smoke test (`jsa --backends mistral,openrouter,gemini,opencode-go`, the `curl`/`PUT` round-trip, and the browser confirmation that a real job's `LogEvent` names the newly selected model).

**2026-09-01**: Context — user ran the plan's last outstanding verification step, `! pytest -v -m integration`, with real exported keys.

- **Actions**: diagnosis only, no code changed. Result: 14 passed, 4 skipped (Anthropic structured-output live tests, no `ANTHROPIC_API_KEY` in this shell — expected skip, not investigated), 3 failed.
- **Failure 1 & 2 — both `opencode-go` live tests (`/chat/completions` and `/messages` protocol) failed with `Invalid API key.`** Traced to the exported `OPENCODE_GO_API_KEY` shell env var itself (`tests/backend/integration/test_opencode_go_live.py`'s fixture checks `os.environ` before falling back to `.env`, and `.env` only has `OPENCODE_API_KEY` — confirmed via `grep`, masked). The value pytest printed ends in a literal `%` (`...JCPXmbYUZ7GTik7L%`) — the classic zsh artifact where the terminal prints a reverse-video `%` after output with no trailing newline; almost certainly picked up when the key was copied from a terminal display and re-exported. **Not a code bug** — no stripping was added, since a key with a stray trailing character legitimately should fail auth rather than be silently "fixed" client-side.
- **Failure 3 — `opencode-zen` timed out after 60s on all 5 of the test's own built-in retry attempts.** Consistent with the pre-existing, already-documented "the free `nemotron-3-ultra-free` model is flaky under upstream load" behavior (CLAUDE.md's "OpenCode Zen backend" section) — not a new code issue, though 5/5 timeouts (vs. the usual intermittent 502) is a heavier miss than typical and worth a solo re-run to confirm it's transient rather than a sustained outage.
- **Decision — deferred to user**: re-export `OPENCODE_GO_API_KEY` without the trailing `%` and re-run just the three failing tests:
  ```bash
  pytest -v -m integration tests/backend/integration/test_opencode_go_live.py tests/backend/integration/test_opencode_zen_live.py
  ```
- **Verification result**: needs user action (14/21 targeted tests verified; 3 failures diagnosed as environmental, not yet re-confirmed green).

**2026-09-01**: Context — user re-ran `! pytest -v -m integration` after re-exporting `OPENCODE_GO_API_KEY` without the trailing `%`.

- **Result**: 15 passed, 4 skipped (Anthropic, no key — expected), 2 failed. Both `opencode-go` tests now pass, confirming the trailing-`%` diagnosis above was correct — no code change was needed.
- **Both remaining failures are the same live `opencode-zen` issue**, and this run's captured log confirms the root cause directly rather than by inference: `test_start_session_returns_final_reply` logged `OpenCode Zen API transient failure (attempt 1/3), retrying: ... [502] Upstream error from Nvidia: Service temporarily overloaded` before ultimately failing with `AgentTimeout` after 60s. Mechanics: attempt 1 hit a fast transient 502 (correctly classified, retried per `_call_api`'s in-process loop, `jsa/agents/opencode_zen.py:343-357`); attempt 2 hung until the per-call 60s `httpx` timeout fired, raising `AgentTimeout`, which by design bypasses that retry loop entirely (CLAUDE.md: "repeating an expensive multi-minute call blindly ... would be a poor trade"). The test's own outer 5-attempt loop (`test_opencode_zen_live.py:60-74`, `test_structured_output_live.py`'s zen class) is the intended absorber for exactly this — and all 5 outer attempts still failed the same way across ~11 minutes wall-clock, indicating a sustained NVIDIA-side overload window for the free `nemotron-3-ultra-free` model during this run, not a one-off blip and not a code defect. Classification and retry logic both behaved exactly as designed.
- **No code change made.** Deferred to the user to re-run these two specific tests later when the free tier isn't saturated:
  ```bash
  pytest -v -m integration tests/backend/integration/test_opencode_zen_live.py tests/backend/integration/test_structured_output_live.py::TestOpenCodeZenStructuredWireAcceptance
  ```
- **Verification result**: verified for `opencode-go` (both live tests green, key-corruption theory confirmed). `opencode-zen`'s two live tests remain environmentally flaky (upstream free-tier overload) — needs user action to re-confirm at a less-loaded time; not a blocker on the plan itself, since every other phase's full non-integration suite is already green and this exact free-model flakiness was anticipated and documented before this session (CLAUDE.md's "OpenCode Zen backend" section).

**2026-09-01**: Context — user ran a full end-to-end pass and reported a "weird discrepancy": the header showed `opencode-zen` as the active backend, but request logs showed traffic hitting `https://opencode.ai/zen/go/v1/chat/completions` (opencode-go's endpoint).

- **Investigation**: confirmed `OpenCodeZenBackend`/`OpenCodeGoBackend`, `registry.py`, and `make_backend_factory` all dispatch correctly by name — no routing bug. Queried the live `jsa.sqlite`: several jobs' `Job.backend_name` had genuinely flipped from `opencode-zen` to `opencode-go` (real BF-19 failovers), so the go-endpoint traffic was correct for those jobs. The actual bug: `GET /api/config`'s `"backend"` field is `settings.backend`, set once at process startup and never mutated by `backend_switch_reset` (only `Job.backend_name` in the DB changes on a switch). The frontend's active-backend indicator (`Header.tsx`) only self-corrected via the live `backend_switched` WS event — and `Job.backend_name` was never serialized to the frontend at all (`_job_to_dict` omitted it), so a page reload or a WS gap spanning a switch left the header pinned on the stale boot-time value with no way to reconcile.
- **Fix**: added `"backend_name": job.backend_name` to `_job_to_dict` (`jsa/api/routes_jobs.py`) and `backend_name: string | null` to `JobDTO` (`frontend/src/types.ts`, plus all `makeJob`/fixture literals across the frontend test suite). Added a new reconciliation `useEffect` in `Header.tsx` (alongside the existing `lastBackendSwitch`-driven one, left unchanged) that derives the true active backend from the most-recently-`updated_at` job with a non-null `backend_name`, on every `jobs` store update — covering initial load, WS reconnect's `refetchAll`, and mid-session drift, not just events received while connected.
- **Verification**: `tsc --noEmit` clean; frontend suite 280/280 passing (added `"reconciles the active backend from job.backend_name on load, without a WS event"` to `Header.test.tsx`, mutation-style checked by confirming it fails without the new effect); backend suite (`.venv/bin/pytest -m "not integration"`) 1553 passed, 2 skipped, 21 deselected, no regressions from the `_job_to_dict` addition. Ran `npm run build` to refresh `jsa/static` per project convention. Verified.

---

## Decisions Log

_(reserved for the user)_
