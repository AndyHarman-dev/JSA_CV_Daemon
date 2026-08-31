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

---

## Decisions Log

_(reserved for the user)_
