---
status: InProgress
---

# CV Editor AI Chat — scoped, chat-driven editing over the base-CV editor

## Context

The base-CV editor (`frontend/src/components/cv-editor/`) is direct-manipulation only: every
field is a controlled input wired straight to `editorStore` actions. The design hand-off
(`/Users/wiam/Downloads/cv-ai-chat-feture.zip` → `CV Editor AI Chat.dc.html` + `README.md`)
adds a **chat trigger per editable unit** (identity card, section, entry, skill group, whole
CV). Clicking one opens a scoped panel where the user types an instruction, picks a quick
action, or drops a file; the model proposes a **field-level diff**; the user applies or
discards it. The diff writes into the editor's live buffer — never to disk — so `SAVE` stays
the only persistence gate, exactly as today.

The hand-off's HTML is a **design reference**, not code: it fakes the model with canned string
transforms and `setTimeout` pacing. None of that ships. What ships is its interaction and
visual design, recreated as real components against `EDITOR_THEME`, wired to a real backend.

**User-locked options** (asked and answered before planning):

| Decision | Choice |
|---|---|
| `chatForm` | **anchored** — a `position: fixed` panel measured off the scoped unit's rect, with the dashed connector line |
| `triggerStyle` | **corner** — the overhanging `EDIT` pill; the existing `cvtools` hover clusters are left byte-identical |
| Surfaces | **all three** — Blocks, Document/Split paper, and the Split view's outline rail |
| Auto-mode | **auto-apply only** — a returned diff applies into the buffer as one undoable entry; nothing is saved to disk |
| Auto-mode entry points | in-panel `AUTO` toggle **and** a `--auto-mode` runtime flag |
| Thread lifetime | **persisted per deck**, reloaded when the editor opens |
| Model/backend | **`claude-cli` by default**, via a dedicated `Settings.chat_backend` |

### Why this does NOT port `run_tool_loop`

The hand-off README suggests porting `jsa/pipeline/tool_loop.py`. Three independent
constraints, each sufficient on its own, rule that out — this is the single most important
decision in the plan:

1. **`tools_for(stage)` raises `ValueError` outside `revising_cv`/`revising_cl`**
   (`jsa/agents/tool_spec.py:258-269`). A job-less chat has no such stage; passing one is a
   lie that also silently hands the chat the whole revision vocabulary.
2. **Tool mode never streams.** `tool_loop.py` passes no `on_chunk`/`on_retry` by
   construction, and that is *grep-pinned* by `tests/backend/test_tools_doc_sync.py:202-211`.
   The design's REASONING card is fed by `AgentChunk(kind="reasoning")`. Irreconcilable.
3. **`claude-cli` — the chosen default — cannot reach either rung.** It has
   `supports_native_tools = False` and `restore_applies_system_prompt = False`, so
   `stages.py::_tools_for` returns `None` and the ladder is skipped entirely.

**The real precedent is `jsa/pipeline/infer_structure.py::run_infer`** — the repo's only
job-less model call: a fresh `start_session`, structured mode when the backend instance can
enforce it and the sentinel grammar otherwise, progress broadcast on a `task_id`-keyed event,
no Job/DB/state, no self-heal budget. A chat turn is that shape plus a conversation history.

What *is* reused from the tool machinery is the part that matters: **`jsa/schema/patch.py`'s
`CvWorkingCopy` as the op applier**, including its id discipline, its error codes, and
`finalize()`'s content gate — which is already job-less (`_validate_final_content(...,
job=None)`, tolerated at `jsa/pipeline/validation.py:190`). The model emits an **ops array in
one reply** instead of a multi-turn tool loop; the ops themselves are the existing vocabulary.

### Locked decisions

- **D1 — Addressing.** Editor ids (`editorStore.ts:34` `uid()`) are client-only, stripped by
  `exportJson()`, and **re-minted on every save** (`save()` → `load(saved)` → `toEditor()`),
  deck switch, and import. They must never round-trip. The model instead addresses the
  `s1…sN`/`e1…eN` ids that `CvWorkingCopy` mints in document order, injected into the user
  message as `copy.get_cv()`'s payload — the same dump the revision loop's `get_cv` tool
  returns. Those ids never leave the server.
- **D2 — What the daemon sees is the BUFFER, not the deck file.** The request body carries the
  current `CVDocument` from the editor buffer. The deck id is for thread identity and logging
  only. Reading the deck file would feed the model a stale document whenever there are
  unsaved edits — silent when wrong.
- **D3 — Apply is a whole-document replace, not per-op field patching.** `replace_section`
  retires child ids and mints new ones, and `add_entry` mints beyond the original range, so
  the post-op structure cannot be mapped back to frontend ids position-by-position. The server
  returns the finalized `CVDocument`; the frontend reconciles it into the live `EditorCV`
  (reusing ids where positions align, purely for React keys/drag/selection) and pushes it
  through **one** `applyEdit(next, false)`.
- **D4 — Staleness is checked once, on the whole buffer.** The request carries `base_hash`, a
  hash of `exportJson(cv)`. At apply time the frontend recomputes it; a mismatch marks the
  card `STALE` with a re-run affordance instead of writing. **Auto-mode must refuse to
  auto-apply a stale diff** and fall back to a `PROPOSED` card — this is the one path in the
  feature that can lose user work.
- **D5 — Scope restricts what may CHANGE, never what the model may SEE.** For every scope,
  including `contact` and a single `entry`, the user message carries the **whole** `get_cv()`
  dump, with the scoped node marked `◀ EDITABLE` inline and an `EDITABLE SCOPE:` header naming
  the allowed ops. Three reasons a scope-only slice was rejected: (i) `finalize()` validates a
  whole `CVDocument` — the skeleton gate, the contact requirement and the not-a-cover-letter
  guard all operate on the full document, so a fragment would need a parallel validator and
  would lose the reuse D3 depends on; (ii) the design's own quality claims require the rest of
  the document — "Quantify impact" promises *placeholders drawn from the numbers already in
  your CV* and "Expand" promises *detail already implied elsewhere in the CV, no invented
  facts*, both of which are false if the model sees one entry; (iii) a CV is ~1–3k tokens and
  rides in the **user message**, so it never perturbs the cached system prefix. Enforcement is
  three-layered instead: the schema's `op` enum is narrowed per scope (provider-enforced on
  structured backends), `PROMPT_CV_CHAT.md` states the rule, and the **authoritative** gate is
  a server-side structural diff of the submitted document against the finalized one — any
  changed path outside scope is a hard `ChatError`. That last guard is vocabulary-independent,
  so per-op id checking is a nicety on top of it, not the mechanism.
- **D6 — `replace_contact` does NOT join `CV_TOOL_SPECS`.** `tools_for(revising_cv)` returns
  `CV_TOOL_SPECS + SHARED_TOOL_SPECS`, so adding a member would silently widen the live
  job-revision vocabulary with no parity test pinning the old behaviour. A separate
  `CV_CHAT_OP_SPECS` tuple is defined instead; `tools_for` is untouched.
  `CvWorkingCopy.replace_contact` is purely additive — nothing on the revision path calls it.
- **D7 — The turn runs in-request** (mirroring `run_infer`), streaming reasoning over the
  existing `/ws` socket, and persists the completed turn before returning. Trade-off, stated:
  a `claude-cli` turn can hold the HTTP request for minutes. Acceptable for a single-user,
  local-only tool with no proxy; a dropped connection still completes and persists the turn,
  recoverable by re-`GET`ting the thread.

### Non-goals

- No change to the direct-edit inputs, the undo/history architecture, `exportJson`'s wire
  shape, `CVDocument`, or any renderer.
- No change to `tool_loop.py`, `tools_for`, or the job revision path's behaviour.
- No new DB table and no `Job` row — the thread is a JSON file beside the deck store.
- No image/vision ingest. Image attachments are rejected with a clear 422.

---

## Phase 1 — Op vocabulary, contact support, and the turn schema

### Current State
`jsa/agents/tool_spec.py` defines `CV_TOOL_SPECS` (8 tools) + `SHARED_TOOL_SPECS`
(`finalize`, `ask_user`), all hand-written flat `$ref`-free JSON Schemas. `CvWorkingCopy`
(`jsa/schema/patch.py:123-386`) applies them and never raises — every op returns a dict with
`ok` or an `error.code` from `{unknown_id, stale_id, bad_argument, validation_failed}`.
`CVDocument.contact` is copied at construction (`patch.py:131`), echoed read-only in
`get_cv()` (`:201`), and written straight back in `_to_document_dict()` (`:357`) — **no op
mutates it, and no tool addresses it.** `jsa/schema/turn_models.py` holds one flat,
non-nullable Pydantic model per stage plus the stage-less `InferTurn`.

### Desired State
A chat turn model and a scope-narrowed op vocabulary that (a) reuse the existing appliers,
(b) work identically in structured and sentinel mode, and (c) can finally edit the identity
card.

### Problems/Bugs
- The identity card's chat trigger has literally nothing to call — the README's item 3.
- There is no schema describing a "one reply carrying many ops" turn; `STAGE_TURN_MODELS` is
  keyed by `Stage` and this call has none (same situation `InferTurn` was created for).
- A discriminated union of eight op shapes produces nested `$defs` that no provider enforces
  strictly (the same reason Anthropic's native `output_config` was rejected in favour of
  forced tool-use). A flat model is needed.

### Solutions

**1a. `jsa/schema/patch.py` — add `replace_contact`** next to `replace_summary`, same
tolerant-absorption idiom as `_clean_entry_input`:

```python
_CONTACT_FIELDS = ("name", "email", "phone", "location", "links")

def replace_contact(self, contact: Any) -> dict:
    """Replace identity fields. Only the keys present are written — an omitted key
    leaves the existing value alone, so 'fix the email' never blanks the phone."""
    if not isinstance(contact, dict):
        return _err("bad_argument", "contact must be an object")
    patch = {k: v for k, v in contact.items() if k in _CONTACT_FIELDS}
    if not patch:
        return _err("bad_argument", f"contact must name at least one of {_CONTACT_FIELDS}")
    if "links" in patch:
        patch["links"] = _clean_str_list(patch["links"])   # the re-exported jsa.schema.cv._str_list
    self._contact.update(patch)
    return _ok(fields=sorted(patch))
```

Purely additive — `finalize()` already round-trips `self._contact` unchanged, so nothing else
moves. Note the merge semantics (present-keys-only): an op that carried every field as
required would let a model blank a field it simply did not mention.

**1b. `jsa/agents/tool_spec.py` — add `_REPLACE_CONTACT` and `CV_CHAT_OP_SPECS`:**

```python
_REPLACE_CONTACT = ToolSpec(
    name="replace_contact",
    description="Set one or more identity fields. Omitted fields are left unchanged.",
    parameters=_obj({
        "name":     {"type": ["string", "null"]},
        "email":    {"type": ["string", "null"]},
        "phone":    {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
        "links":    {"type": "array", "items": {"type": "string"}},
    }, required=[]),
)

# The CV-editor chat's op vocabulary. Deliberately NOT folded into CV_TOOL_SPECS and NOT
# reachable from tools_for(): adding a member there would silently widen the live
# job-revision vocabulary. See the plan's D6.
CV_CHAT_OP_SPECS: tuple[ToolSpec, ...] = CV_TOOL_SPECS + (_REPLACE_CONTACT,)
```

`tools_for` is untouched, so `tests/backend/test_tools_doc_sync.py` — which enumerates
strictly via `tools_for(Stage.revising_cv)` / `tools_for(Stage.revising_cl)`
(`test_tools_doc_sync.py:58, 67, 78`) and scans only the two `## … vocabulary` headings — is
unaffected in **both** directions. **`docs/TOOLS.md` needs no row for `replace_contact`, and
adding one would break `test_the_doc_documents_no_tool_that_does_not_exist`.** Document the
chat vocabulary in a new `## CV-editor chat vocabulary` section only if a heading outside the
two scanned ones is wanted; the doc-sync test ignores it either way.

**1c. `jsa/schema/chat_turn.py` (new)** — one flat model, `extra="forbid"`, cross-field
validation in Python (the same division of labour `turn_models.py` documents: the schema does
shape, Python does semantics):

```python
CvChatOpName = Literal["replace_summary", "replace_section", "replace_entry",
                       "edit_entry_bullets", "add_entry", "remove_entry",
                       "reorder_entries", "replace_contact"]

class CvChatOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: CvChatOpName
    section_id: str | None = None
    entry_id: str | None = None
    position: int | None = None
    text: str | None = None
    bullets: list[str] | None = None
    order: list[str] | None = None
    section: dict[str, Any] | None = None
    entry: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None
    # @model_validator: per-`op` required-field table -> ValueError naming the missing field.

class CvChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["final", "question"]
    question: str | None = None   # required iff kind == "question"
    answer: str | None = None     # required iff kind == "final" — the user-facing summary
    ops: list[CvChatOp] = []      # empty is legal: "nothing worth changing here"

def json_schema_for_cv_chat(scope_type: ScopeType) -> dict: ...
```

`json_schema_for_cv_chat` narrows the `op` enum per scope — provider-enforced gating, the
schema-level mirror of `tools_for`'s Stage gating:

| scope | allowed ops |
|---|---|
| `contact` | `replace_contact` |
| `entry` | `replace_entry`, `edit_entry_bullets` |
| `section` | `replace_section`, `replace_summary`, `replace_entry`, `edit_entry_bullets`, `add_entry`, `remove_entry`, `reorder_entries` |
| `cv` | all eight |

It emits strict-schema-clean JSON (`additionalProperties: false`, every property in
`required`, nullable via `["string","null"]`) exactly as `json_schema_for` does, and must not
iterate a `set` — the prefix-stability test's `PYTHONHASHSEED` rule applies to every schema
generator in the repo.

Mirroring `InferTurn`, `CvChatTurn` is **absent from `STAGE_TURN_MODELS` and unreachable from
`json_schema_for`**, and `kind` stays a real two-member Literal because
`parse_structured_reply_for_schema` keys `is_fit` off `"kind" not in schema["properties"]`.

**1d. Tests** — `tests/backend/test_cv_chat_ops.py`: `replace_contact` merge semantics
(present-keys-only, `links` coercion, `bad_argument` cases); `CvChatOp`'s per-op required-field
validator; `json_schema_for_cv_chat` scope narrowing + determinism across `PYTHONHASHSEED`.
Plus a regression asserting `tools_for(Stage.revising_cv)` does **not** contain
`replace_contact` (D6's pin).

---

## Phase 2 — The job-less chat runner

### Current State
`jsa/pipeline/infer_structure.py` is the only job-less model call: `run_infer(backend,
cv_path, *, task_id, publish)`, a fresh `start_session`, mode chosen off the **backend
instance's** `supports_structured_output`, `assemble_system_prompt(..., document_only=True)`,
a conditional `structured_schema` kwarg, per-exception `emit(status="error")` then a terminal
`InferError` → HTTP 422. No self-heal budget. Progress is published through an **injected**
`publish` callable, never by importing `bus`.

### Desired State
`jsa/pipeline/cv_chat.py::run_cv_chat(...)` — the same shape, plus conversation history,
streamed reasoning, server-side op application through `CvWorkingCopy`, scope enforcement, and
a computed field-level diff.

### Problems/Bugs
- Every `agent_*` event is `job_id`-keyed and `jsa/api/ws.py` does **zero** filtering, so
  reusing them would broadcast a fabricated `job_id` to every client and the frontend's
  job-keyed routing would attach the stream to the wrong job (or drop it).
  `InferProgressEvent` (`jsa/events/schema.py:91`, keyed by `task_id`) is the precedent.
- `_tool_contract`/`assemble_system_prompt`'s tool branch returns early, skipping the language
  and date directives. This path must get both (it writes user-facing prose in the user's
  language, and it reasons about CV dates), so it uses the ordinary structured branch, not the
  tool branch.

### Solutions

**2a. Events — `jsa/events/schema.py`**, mirroring `InferProgressEvent`'s keying:

```python
@dataclass
class ChatChunkEvent:          # type: "chat_chunk"
    task_id: str
    kind: Literal["content", "reasoning"]
    text: str

@dataclass
class ChatTurnEndEvent:        # type: "chat_turn_end"
    task_id: str
    superseded: bool = False
```

No tool events: this is a one-shot reply, nothing is executed mid-turn.

**2b. `jsa/prompts/PROMPT_CV_CHAT.md` (new)** — carries the sentinel grammar per CLAUDE.md's
mandatory protocol (it is the only channel on `claude-cli`), plus the op semantics, the id
discipline ("ids come from the CV dump in this message and are valid for this turn only"),
and the scope rule. Loaded by `loader.read_prompt("cv_chat")` — no caching, read from disk,
never programmatically overwritten.

**2c. `jsa/pipeline/cv_chat.py` (new):**

```python
@dataclass(frozen=True)
class ChatScope:
    type: Literal["cv", "contact", "section", "entry"]
    section_index: int | None = None   # indices, not ids — the only thing both sides agree on
    entry_index: int | None = None

@dataclass(frozen=True)
class DiffItem:
    label: str      # "EXPERIENCE › Senior Gameplay Programmer · bullet 2"
    before: str
    after: str

@dataclass(frozen=True)
class ChatTurnResult:
    kind: Literal["final", "question"]
    answer: str | None
    question: str | None
    document: CVDocument | None     # None when kind == "question" or ops was empty
    items: list[DiffItem]
    rejected: list[str]             # human-readable reasons for dropped/out-of-scope ops
    reasoning: str

class ChatError(Exception): ...     # -> HTTP 422, same role as InferError

async def run_cv_chat(
    backend: AgentBackend, *,
    cv: CVDocument,
    scope: ChatScope,
    instruction: str,
    history: list[tuple[str, str]],       # (role, text) — RENDERED into the user message,
                                          # never replayed via restore_session. Deliberately
                                          # not `HistoryTurn`: that type would invite an
                                          # implementer to reach for restore_session, which
                                          # is exactly what step 5 below rules out.
    attachments: list[tuple[str, str]],   # (filename, extracted text)
    language: str,
    task_id: str,
    publish: Publish,
) -> ChatTurnResult
```

Flow:

1. `copy = CvWorkingCopy(cv)`; `dump = copy.get_cv()` — this is what gives the model stable
   ids for the turn (D1).
2. Mode: `schema = json_schema_for_cv_chat(scope.type) if backend.supports_structured_output
   else None`. **Read off the backend INSTANCE, never the class** — `OpenCodeGoBackend` sets
   it per-instance. Duplicate the three-line shape rather than importing `stages.py`, for the
   reason `infer_structure.py:105-112` spells out.
3. `system_prompt = assemble_system_prompt(loader.read_prompt("cv_chat"), language=language,
   structured_model=schema, now=datetime.utcnow())`. Ordinary structured branch — not
   `document_only`, not `tool_model`.
4. User message — **always the whole CV, whatever the scope** (D5):

   ```
   EDITABLE SCOPE: SUMMARY (section s1)
   allowed ops: replace_summary

   CURRENT CV  (ids are valid for this turn only)
     contact  {...}
     s1 Summary   ◀ EDITABLE
     s2 Experience
          e1 Senior Gameplay Programmer
             • ...bullets...
     s3 Skills / s4 Projects / s5 Education

   READ-ONLY CONTEXT — evidence only, never copied verbatim, never stored in the CV
     <attachment text>

   INSTRUCTION: <free text, or the resolved quick-action string>
   ```

   The `◀ EDITABLE` marker is rendered onto the scoped node (or `contact`) so the boundary is
   unmissable in prose as well as in the schema. **Per-turn bytes go in the user message,
   never the system prefix** — the cross-job prompt-caching invariant applies here too, and
   this path benefits directly: every chat turn at a given (scope-type, language, mode) shares
   a byte-identical system prefix regardless of which deck or unit it is editing.
5. History: the prior thread turns rendered as text into the user message. `restore_session`
   is **not** used — the backend gets a fresh `start_session` every turn. That sidesteps
   `claude-cli`'s `restore_applies_system_prompt = False` entirely and needs no persisted
   `external_id`. **The CV dump, the history and the attachment text are ONE context budget**,
   not three independent caps: trim in that priority order — attachments truncate first (last
   in, least load-bearing), then history (last 8 turns, then fewer), and the CV dump is never
   trimmed, since the ids in it are what every op addresses.
6. Streaming: pass `on_chunk` when `backend.supports_streaming`, forwarding each chunk as
   `ChatChunkEvent(task_id=…)`. In **structured** mode wrap it so only `kind == "reasoning"`
   passes through — the content delta is raw partial JSON and a stray `{` is not renderable.
   This is exactly `_openai_compat.py::_reasoning_only`'s rule; do not gate streaming on
   `structured_schema is None`, which is the bug CLAUDE.md's reasoning section documents.
7. Parse: `_strip_code_fence(reply.content)` → `json.loads` → `CvChatTurn.model_validate`.
   Malformed → `ChatError` on the first try (no self-heal budget, same as `run_infer`).
8. `kind == "question"` → return immediately, no ops applied.
9. Apply each op in order through a `_dispatch_op(copy, op)` table shaped like
   `tool_loop.py::_dispatch_cv`. A non-`ok` result is collected into `rejected` and the turn
   continues — a bad id must not kill the whole turn.
10. `result = copy.finalize(language=language)` — reuses the same content gate the job
    pipeline's FINAL uses. `validation_failed` → `ChatError` carrying the concise reason.
11. **Scope enforcement (D5):** structurally diff the submitted `cv` against the finalized
    document. Every changed path must lie inside `scope`; anything outside is a hard
    `ChatError` (the model broke its contract) rather than a silent partial apply.
12. Diff items: the same structural walk yields `DiffItem`s with human labels (`section.name`
    ›  `entry.heading` · field). Cap the payload (e.g. 60 items) — the card shows 4 and a
    "+N more" line.
13. Publish `ChatTurnEndEvent(task_id)` in a `finally`.

**2d. Tests** — `tests/backend/test_cv_chat.py` using `FakeAgentBackend`: a structured turn, a
sentinel turn, **mode parity** (identical `document` / `items` / `answer` from the same
logical payload — the permanent gate `test_mode_parity.py` establishes for stages), a
`question` turn, an out-of-scope op → `ChatError`, an unknown id → collected in `rejected`
with the turn still succeeding, empty `ops` → `document is None`, and a `reasoning`-only
stream in structured mode. Plus D5's context pin, using `CapturingBackend`
(`tests/backend/fakes/fake_backend.py:202`): **for an `entry` scope, the captured user message
still contains every other section's id and text**, and the captured schema's `op` enum is
narrowed to the entry ops. That is the regression that stops someone "optimising" the prompt
into a scope-only slice later.

---

## Phase 3 — Persistence, routes, ingest, and the two settings

### Current State
`jsa/api/routes_cv_decks.py` is the deck CRUD surface; there is no `Depends()` anywhere in
`jsa/` — routes read `request.app.state` through a module-local `_settings(request)` helper.
`app.state.backend_factory` exists **specifically** for the job-less infer endpoint
(`jsa/server.py:265-268`). `POST /api/cv-structure/infer` is the single multipart route
(`UploadFile = File(...)`, no `Form()` anywhere in the repo). `jsa/ingest/cv_loader.py::load_cv`
handles **only** `.pdf`/`.docx` and raises `ValueError` otherwise — `.txt`, `.rtf` and
extensionless are test-pinned to raise (`tests/backend/test_ingest.py:542,549,556`).
`jsa/store/preferences.py` and `backend_models.py` are the same five-function persisted-store
pattern twice over.

### Desired State
A `GET`/`POST`/`DELETE` chat surface on a deck, a per-deck thread file, a wider text ingest
that does not touch `load_cv`, and the `chat_backend` / `auto_mode` settings.

### Problems/Bugs
- The chat turn needs both a JSON payload and optional file uploads in one request. FastAPI
  cannot mix a Pydantic body model with `UploadFile`; this needs `Form()`, which the repo has
  never used.
- Widening `load_cv` to `.txt`/`.md` would break three pinned tests.

### Solutions

**3a. `jsa/ingest/text_source.py` (new)** — `load_text_source(path: Path) -> str`:
`.pdf`/`.docx` delegate to `load_cv` (untouched); `.txt`/`.md`/`.json`/`.rtf`/`.csv` read as
UTF-8 with `errors="replace"`; image and unknown extensions raise `UnsupportedSource` → 422
with a message naming what is accepted. Blocking, so the caller wraps it:
`await asyncio.to_thread(load_text_source, path)` — the same call-site convention
`infer_structure.py:91` uses.

**3b. `jsa/store/deck_chats.py` (new)** — `preferences.py`'s five-function pattern verbatim:

```python
class ChatTurn(BaseModel):
    id: str
    role: Literal["user", "agent", "scope"]
    scope: dict                      # serialized ChatScope + its display label
    text: str = ""                   # user text, or the agent's answer
    question: str | None = None
    reasoning: str = ""
    items: list[dict] = []           # DiffItem dicts, for re-rendering a settled card
    document: dict | None = None     # the finalized CVDocument, so a reloaded card can apply
    status: Literal["pending", "applied", "auto", "discarded", "none", "error"] = "pending"
    files: list[dict] = []           # {name, size} only — attachment TEXT is never persisted
    base_hash: str = ""              # D4's staleness key
    created_at: str

class DeckChat(BaseModel):
    turns: list[ChatTurn] = []

def deck_chat_path(settings, deck_id) -> Path   # settings.deck_chats_dir / f"{deck_id}.json"
async def read(path) / load(settings, deck_id) / save(settings, deck_id, chat) / clear(...)
```

- `Settings.deck_chats_dir` is a `@property` returning `db_path.parent / "deck_chats"`, like
  every other derived path — this is what keeps tests using an isolated `db_path`
  self-isolating.
- `deck_chats.deck_chat_path` **must** re-validate the deck id through
  `cv_decks.deck_path(settings, deck_id)` first: that function is the sole path-traversal
  guard between a client id and the filesystem, and a second id→path derivation that skips it
  would reopen the hole.
- Writes go through the same "tmp sibling + `os.replace`" atomic idiom, behind a per-loop
  `_lock(f"chat:{deck_id}")` (`cv_decks.py:89`'s `WeakKeyDictionary`-keyed helper, **not** a
  module-level `asyncio.Lock` — that gets poisoned by the first pytest-asyncio loop that
  contends it).
- A missing file returns an empty `DeckChat()`, never `None` (the `preferences.py` rule).
- Cap at the last 60 turns on write.
- **Attachment text is deliberately not persisted** — it is read-only context for one turn, and
  keeping it would turn the thread file into an unbounded document store.

**3c. `jsa/api/routes_cv_chat.py` (new)**, registered in `server.py`'s ordered router list:

| Method / path | Body | Returns |
|---|---|---|
| `GET /api/cv-decks/{deck_id}/chat` | — | `{"turns": [...]}` — 400 `InvalidDeckId`, 404 `UnknownDeckId` |
| `POST /api/cv-decks/{deck_id}/chat` | **multipart**: `payload: str = Form(...)` (JSON) + `files: list[UploadFile] = File(default=[])` | `{"task_id", "turns": [user_turn, agent_turn]}` |
| `DELETE /api/cv-decks/{deck_id}/chat` | — | `204` — the panel's `NEW` button |

`payload` JSON, validated by a Pydantic model after `json.loads`:
`{scope, instruction, quick_action, cv, base_hash}`. `quick_action` is resolved server-side to
a canned instruction string through a `QUICK_ACTIONS: dict[str, str]` table — the README's
rule that quick actions are **real instructions, not canned transforms**, and putting the table
on the server keeps the client from being the authority on what "Compact" means.

Route body:

```python
settings  = request.app.state.settings
cv_decks.deck_path(settings, deck_id)                 # format check -> 400
index = await cv_decks.load_index(settings)
if deck_id not in {d.id for d in index.decks}: raise HTTPException(404)
backend = request.app.state.backend_factory(settings.chat_backend)
language = (await preferences.load(settings)).language
attachments = [...]                                   # NamedTemporaryFile -> load_text_source -> unlink in finally
try:
    result = await run_cv_chat(backend, ..., task_id=task_id, publish=bus.publish)
except ChatError as exc:
    raise HTTPException(status_code=422, detail=str(exc)) from exc
turns = await _append_turns(settings, deck_id, user_turn, agent_turn_from(result))
return {"task_id": task_id, "turns": turns}
```

The temp-file dance (`NamedTemporaryFile(delete=False)` → path → `unlink(missing_ok=True)` in
`finally`) is `routes_cv_structure.py:89-110`'s shape, reused. **No `orchestrator.kick()`** —
this touches no deck file and no job.

Cap uploads: ≤6 files, ≤5 MB each, ≤40k extracted chars total (truncate with a note in the
context block rather than erroring).

**3d. `jsa/config.py` + `jsa/cli.py`:**

```python
# config.py — env vars come free from SettingsConfigDict(env_prefix="JSA_")
chat_backend: str = "claude-cli"   # JSA_CHAT_BACKEND — the CV-editor chat's backend.
                                   # Deliberately independent of backends[0]: the editor chat
                                   # is interactive and short, the pipeline chain is not.
auto_mode: bool = False            # JSA_AUTO_MODE
```

`chat_backend` gets the same registry-membership validator `_validate_backends` uses (function-local
import of `jsa.agents.registry._REGISTRY`, to keep the module cycle broken).

```python
# cli.py — tri-state, the --prompt-caching template (cli.py:115 / :173)
auto_mode: Optional[bool] = typer.Option(
    None, "--auto-mode/--no-auto-mode",
    help="Apply the CV-editor chat's proposed diffs without confirmation. Defaults to off."),
chat_backend: Optional[str] = typer.Option(
    None, "--chat-backend", help="Backend for the CV-editor chat. Defaults to claude-cli."),
...
if auto_mode is not None:   overrides["auto_mode"] = auto_mode
if chat_backend is not None: overrides["chat_backend"] = chat_backend
```

**3e. `jsa/api/routes_meta.py`** — two keys on `_config_payload`: `auto_mode` and
`chat_backend`. Both are plain scalars already in memory: no network fan-out, so the boot-path
rule that keeps model listing off `/api/config` does not apply. Keep them inside the factored
payload builder so the corrupt-deck-index fallback branch returns identical keys.

**3f. Tests** — `tests/backend/test_cv_chat_api.py` on the `test_app`/`client` fixture pair
(`Orchestrator.run` neutered, `AsyncClient` + `ASGITransport`, inside
`lifespan_context`), with `test_app.state.backend_factory = lambda name:
FakeAgentBackend([reply])` post-startup so the reply is scripted. Cases: full round trip; 400
malformed deck id; 404 unknown deck; 422 on a bad model reply; multipart with a `.txt`
attachment; 422 on a `.png`; `DELETE` clears; thread persists across a fresh `client`; and the
`bus.subscribe()` → drain-with-`get_nowait()` → `unsubscribe()` idiom asserting
`chat_chunk`/`chat_turn_end` reach the bus. Plus `tests/backend/test_ingest.py` additions
pinning that `load_cv` still raises for `.txt` (unchanged) while `load_text_source` does not.

---

## Phase 4 — Frontend state: chat store, batch apply, WS wiring

### Current State
`editorStore.ts` funnels every content mutation through `applyEdit(next, coalesce)`
(`:413-423`) — the coalescing branch sets a module-global 600 ms `_commitTimer`, the immediate
branch calls `commit()` **twice**. There is **no batch/transaction action**. `dirty` means
"differs from the newest history snapshot"; `unsaved` means "differs from what the server
holds" and is what `flushAndPersist()` gates on. `store.ts::applyEvent` is a flat switch;
`infer_progress` is the one non-job-keyed member and it forwards to
`useEditorStore.getState().onInferProgress(e)`, which **ignores `task_id`** and gates on a
single-flight boolean instead (`editorStore.ts:767-777`).

### Desired State
A sibling `cvChatStore`, one batch apply action on `editorStore`, and two new WS cases.

### Problems/Bugs
- Applying an N-field diff through the existing per-field mutators produces N (or 2N) undo
  steps. The design promises "⌘Z reverts" — with auto-mode on, undo is the *only* escape from
  an unwanted apply, so this is a correctness requirement, not polish.
- A programmatic apply landing mid-typing-burst would silently extend the user's pending
  coalesce timer if it used the `coalesce: true` branch.

### Solutions

**4a. `editorStore.ts` — one new action:**

```ts
/** Replace the whole buffer with a model-proposed document, as ONE undo entry.
 *  Ids are reused positionally so React keys, drag state and selectedId survive an
 *  in-place edit; they are meaningless outside this client anyway (see exportJson). */
applyAiDocument(next: CVDocument): void {
  const prev = get().cv;
  if (!prev) return;
  applyEdit(reconcileIds(prev, toEditor(next)), false);   // non-coalescing: flushes, never extends
}
```

`reconcileIds(prev, fresh)` is a small pure helper (unit-testable, exported for tests): walk
`fresh.sections` by index, adopt `prev.sections[i].id` when present, else keep the fresh
`uid()`; same for entries. Nothing else about `toEditor`/`exportJson` changes.

**4b. `frontend/src/cvChatStore.ts` (new)** — a separate zustand store, deliberately not part
of `editorStore`'s undo/history state:

```ts
type ScopeType = "cv" | "contact" | "section" | "entry";
interface ChatScope { type: ScopeType; sectionId?: string; entryId?: string }  // client ids, UI only

interface CvChatState {
  open: boolean; collapsed: boolean; scope: ChatScope | null;
  deckId: string | null;                       // the deck this thread belongs to
  turns: ChatTurn[]; input: string; attach: PendingFile[];
  busy: boolean; elapsed: number; taskId: string | null;
  reasoning: string; content: string;          // the live stream buffer
  auto: boolean;                               // seeded from config.auto_mode, session-overridable
  flashKey: string | null; showReason: Record<string, boolean>;

  openChat(scope): void;      // pushes a "scope" marker turn when the scope changes mid-thread
  closeChat(): void; setCollapsed(b): void; widenToCv(): void;
  setInput(v): void; addFiles(FileList): void; removeFile(id): void;
  send(quickAction?: string): Promise<void>;
  newThread(): Promise<void>;                  // DELETE, then clear
  hydrate(deckId): Promise<void>;              // GET on editor open / deck switch
  applyTurn(turnId): void; discardTurn(turnId): void;
  toggleAuto(): void;
  onChunk(e), onTurnEnd(e);                    // WS handlers
}
```

- **Scope is held as client ids** for UI purposes (highlight, dim, anchor measurement) and
  **converted to indices** at send time against the current buffer — indices are the only
  addressing the server understands (D1/2c).
- `send()` builds the `FormData` (`payload` JSON + `files`), computes `base_hash` from
  `exportJson(cv)`, and posts. On resolve it replaces the optimistic turns with the server's.
- `applyTurn()` refuses on **two** independent checks, in this order:
  1. **Deck identity.** Every turn records the `deckId` it was sent for; if
     `editorStore.activeDeckId` differs, refuse outright. This matters because `switchDeck`
     runs `flushAndPersist()` → `save()` → `load(saved)`, which re-mints every id and swaps
     the whole buffer — so a turn resolving after a mid-flight deck switch would otherwise
     write one deck's document into another's buffer. The `base_hash` check below *happens* to
     catch it today (the new deck's hash differs), but only incidentally; making it explicit
     means the guard does not depend on two documents never hashing alike.
  2. **Staleness.** Recompute `base_hash` from the current buffer; on mismatch mark the turn
     `stale` and return without writing (D4).

  Only when both pass does it call `useEditorStore.getState().applyAiDocument(doc)`, set
  `status: "applied"`, and flash the scoped block.
- **Auto-mode:** after a `final` turn with ops resolves, if `auto` is on **and** the turn is
  not stale, call `applyTurn` and mark `status: "auto"`. A stale turn under auto-mode stays
  `pending` and renders as a normal `PROPOSED DIFF` — never a silent apply over a moved base.
- `hydrate` is called from `CvEditor`'s mount effect alongside `hydrateDecks()`, and again in
  `switchDeck`. Turns whose `base_hash` no longer matches after a reload are rendered with
  their Apply button disabled + a stale note.

**4c. `frontend/src/types.ts`** — two `WSEvent` members mirroring the new dataclasses:

```ts
| { type: "chat_chunk"; task_id: string; kind: "content" | "reasoning"; text: string }
| { type: "chat_turn_end"; task_id: string; superseded: boolean }
```

**4d. `frontend/src/store.ts`** — two cases forwarding to the chat store, the `infer_progress`
precedent exactly:

```ts
case "chat_chunk":    useCvChatStore.getState().onChunk(e); break;
case "chat_turn_end": useCvChatStore.getState().onTurnEnd(e); break;
```

`store.ts` stays job-scoped; `streamBuffers` and its three lockstep literals are **not
touched**. The chat's buffer lives in `cvChatStore`, gated on `busy` (the single-flight
substitute for id keying that `onInferProgress` already establishes) with a `taskId` check
once the POST resolves.

**4e. `frontend/src/api.ts`** — `getDeckChat(deckId)`, `sendDeckChat(deckId, payload, files)`
(FormData, **no `Content-Type` header** — the browser sets the multipart boundary, per
`inferCvStructure`), `clearDeckChat(deckId)`.

**4f. Tests** — extend `editorStore.test.ts` (`applyAiDocument` pushes exactly one history
entry; `undo()` restores the pre-apply buffer; `reconcileIds` preserves ids on an in-place
edit and mints on a structural one; `unsaved` becomes true) and `store.test.ts` (the two new
cases forward and do not touch `streamBuffers`). New `cvChatStore.test.ts`: scope-change
marker turn, stale refusal under auto-mode, auto-apply on a fresh base, `send` FormData shape.

---

## Phase 5 — Frontend components

### Current State
`CvEditor.tsx` is `position: fixed; inset: 0; zIndex: 40`; its body is `DeckRail` + a single
scrolling `<main>` + an optional `JsonDrawer`. `BlocksView` renders `ContactCard`,
`SectionCard` (header with the in-flow `cvtools` cluster), `EntryEditor` (absolute `cvtools` at
`top:8; right:8`, body reserving `paddingRight: 76`), and skills-group rows. Hover reveal is
`index.css`'s `.cvsec:hover .cvtools { opacity: 1 }` — opacity only, never `display:none`.
`ReasoningCard`/`StepRow`/`segmentReasoning`/`mergeToolSteps` are job-free and reusable;
`AgentThread` and `ChatBox` are not (both require `jobId` and post to `/api/jobs/...`).
`Icon.tsx` has 41 glyphs, **no chat bubble and no paperclip**; `spark` is the established AI
glyph. **`panelBase()` sets `clipPath: chamferPath(...)` under the hud skin, which clips
descendants.**

### Desired State
The anchored panel, the corner triggers on all three surfaces, the diff card, and the
dim/connector behaviour — all in `EDITOR_THEME`, none of the prototype's palette.

### Problems/Bugs
- **The corner `EDIT` pill sits at `top: -10`, outside the card edge — and every card is
  chamfered via `panelBase`, whose `clipPath` would clip it away.** This is the single
  non-obvious implementation trap in the phase.
- `ReasoningCard` hardcodes `const T = SHELL_THEME`; dropped into the red editor unchanged it
  renders amber.
- `<main>` is the scroll container, so an anchored `position: fixed` panel must re-measure on
  its `onScroll` **and** on `window.resize`.

### Solutions

**5a. `theme/Icon.tsx`** — add five glyphs to the `IconName` union and `PATHS`, at
`viewBox="0 0 16 16"`, `strokeWidth 1.55`, round caps (TypeScript's total `Record` fails the
build if a name has no entry). Geometry lifted from the design reference:
`chat` `M2.6 3.6h10.8v7.2H7.4L4.2 13.4v-2.6H2.6z` · `arrows` · `clip` · `file` · `id`.

**5b. Corner triggers — wrap, don't clip.** A shared
`<ChatCorner scope={…} label="EDIT" />` renders `position:absolute; top:-10; right:10;
zIndex:3`, hover-revealed by a new `.cvsec .aitrig{opacity:0}` / `.cvsec:hover .aitrig,
.cvsec:focus-within .aitrig{opacity:1}` pair in `index.css` (same idiom as `.cvtools`, so the
button stays in the DOM and hit-testable in tests). It is mounted as a **sibling of the
chamfered card inside a `position: relative` wrapper**, never as a descendant:

- `SectionCard` already has an outer flex-column wrapper (`BlocksView.tsx:668`) — give it
  `position: relative` and put the pill there.
- `ContactCard` and `EntryEditor` are bare chamfered divs — wrap each in a
  `<div style={{position:"relative"}}>`.
- Skills-group rows get the same treatment.
- **`cvtools` clusters are not touched** — `triggerStyle: "corner"` means the cluster gets no
  chat button, so `ToolBtn`, the three existing buttons, and `EntryEditor`'s
  `paddingRight: 76` all stay byte-identical.

Paper surfaces (`PaperSheet.tsx`, used by both Document and Split): a small `paperT`-coloured
pill at `top:-4; right:-34` on each section and entry, inside the existing per-entry
`position: relative` wrapper. Split's outline rail (`SplitView.tsx`'s 280px `<aside>`): a
small inline chat `ToolBtn` per outline row, hover-revealed.

**5c. `CvChatPanel.tsx` (new)** — `position: fixed`, geometry from the prototype's
`panelPos()` anchored branch:

```ts
const w = 380, r = anchorEl?.getBoundingClientRect();
left = r ? clamp(r.right + 20, 16, vw - w - 16) : vw - w - 22;
top  = r ? clamp(r.top - 4, 62, Math.max(vh - 440, 62)) : 80;
maxHeight: "min(70vh, 560px)";
```

Port the prototype's **guards**, not just the math — without the viewport clamps a section
near the bottom lands off-screen.

**Flip-to-left when the clamp would overlap the anchor — the prototype does not do this and
needs to.** On a typical laptop (`vw ≈ 1440`) a 760px centred column's right edge sits around
x≈1100, so the anchored `left = r.right + 20 ≈ 1120` exceeds `vw - w - 16 = 1044` and clamps
back **on top of the card being edited**. The prototype only notices via `measure()`'s
occlusion test (`a.right > b.left - 14`), which hides the connector but leaves the panel
covering the content. So: if the clamped `left` would intersect the anchor rect, place the
panel to the **left** of it (`left = clamp(r.left - w - 20, 16, …)`) and mirror the connector's
endpoints; only if neither side fits does it fall back to the clamped right position with the
connector suppressed. State this in the component, because it is invisible until someone opens
the panel on a narrow window and wonders why the field disappeared.

Build the panel chrome by hand (`background: T.surface`,
`border: 1px solid T.bd2`, `boxShadow: T.shadowMd`), **not** with `panelBase()`, for the same
`clipPath` reason `JsonDrawer` hand-rolls its own.

A `Map<scopeKey, HTMLElement>` ref registry lives in `cvChatStore` (or a module-level
`WeakMap`), populated by each unit's wrapper `ref`. Re-measure in a `useLayoutEffect` bound to
`<main>`'s `onScroll` and `window.resize`, rAF-throttled, writing only when the rect actually
moves (the prototype's `measure()` already diffs before `setState`).

Anatomy, top to bottom — header (`chat` icon · `DAEMON // EDIT` · `AUTO` toggle · `NEW` ·
collapse · close), scope bar (scope chip + meta + `WIDEN → CV`), thread, composer (quick-action
chips · attachment chips with the `READ-ONLY CONTEXT … NOT STORED IN THE CV` note · paperclip ·
textarea with Enter-to-send/Shift+Enter-newline · SEND), and the auto/proposed footer line.
Collapsed and closed both render the bottom-right pill (`ASK DAEMON · ENTIRE CV`, or
`WORKING · Ns` while busy).

**5d. `CvDiffCard.tsx` (new)** — header (`PROPOSED DIFF` / `APPLIED` / `AUTO-APPLIED` /
`DISCARDED` / `STALE` + change count + scope), up to 4 items as
`LABEL` / `BEFORE` / `AFTER` rows on an accent left border, a `+ N more changes` line, then
`APPLY` / `DISCARD`. The `STALE` state is new relative to the design — it replaces the action
row with a short explanation and a `RE-RUN` button (D4).

**5e. Reasoning trace.** Reuse `ReasoningCard`/`StepRow`/`segmentReasoning`/`mergeToolSteps`
**as-is**, with one minimal change: give `ReasoningCard` an optional `theme?: Theme` prop
defaulting to `SHELL_THEME`, so existing call sites are byte-identical and the editor passes
`EDITOR_THEME`. Pass `tools={[]}` — this path executes no tools. The prefix-stability
invariant in `reasoningSteps.ts` is untouched.

**5f. Dimming, ring, connector.** While the panel is open and scoped to unit X, every other
unit gets `opacity: .3` and X gets an accent ring — with the design's one carve-out: an
`entry` scope does **not** dim its parent section. Purely CSS/state in the new components; the
only store addition is `scope`. The dashed connector is a `position: fixed; inset: 0;
pointerEvents: none` SVG bezier between the unit's right edge and the panel's top-left,
suppressed when occluded (`a.right > b.left - 14`) or off-screen, and for `cv` scope. Keyframes
(`aiwork` pulse while busy, `aiflash` green on apply) go in `index.css` beside the existing
`cvspin`/`cvfade`.

**5g. Mounting.** `CvEditor.tsx` renders `{cv && <CvChatPanel />}` as a sibling of `<main>`,
after `JsonDrawer`. `BlocksView`'s `maxWidth: 760; margin: 0 auto` and `PaperSheet`'s
`width: 760; maxWidth: 100%` both re-center on their own, so no layout change is needed; the
anchored panel floats over them.

**5h. Tests** — `CvChatPanel.test.tsx` and `BlocksViewChat.test.tsx`, modelled on
`DeckRail.test.tsx`'s `seed()` + action-spies pattern (there is **no** existing render harness
for `BlocksView`/`CvEditor`, so this is the first one). Assert: a corner trigger per unit;
clicking opens the panel with the right scope label; a scope change pushes the marker turn;
the diff card's APPLY calls `applyAiDocument`; DISCARD does not; a stale turn's APPLY is
inert; a turn whose `deckId` no longer matches `activeDeckId` is inert; auto-mode auto-applies
a fresh turn and refuses a stale one.

The streaming harness already exists: `FakeAgentBackend._emit_scripted_chunks` is awaited from
**`start_session`** (`tests/backend/fakes/fake_backend.py:137`), not only `send_message`, so
the one-shot path in Phase 2 can be exercised with `scripted_chunks` + `supports_streaming=True`
without touching the fake.

---

## Phase 6 — i18n, docs, and verification

### Solutions

**6a. i18n split rule — stated once, not enumerated.** Every **real sentence** the user reads
goes through `useT()` with a `cvChat.*` key in `strings.en.json`: the empty-thread copy, the
footer line (`Diffs are proposed · nothing changes until you apply` / `AUTO ON · diffs apply
without confirmation`), the stale explanation, quick-action **labels**, tooltips, the
read-only-context note, and all error text. **HUD-style code-like chrome is left inline**, per
CLAUDE.md's explicit carve-out: `DAEMON // EDIT`, `PROPOSED DIFF`, `BEFORE`/`AFTER`,
`WIDEN → CV`, `DROP AS CONTEXT`, `NEW`, `AUTO`, `SEND`, `APPLY`, `DISCARD`, `MOD_NN`. Store
actions use the non-hook `tr()` twin (`editorStore.ts:368`), never `useT`. Then run
`scripts/translate-ui.sh` (incremental) and verify with `--check`; never hand-edit
`strings.meta.json`.

**6b. Docs.** Append a `## CV-editor chat` section to `docs/TOOLS.md` describing
`CV_CHAT_OP_SPECS`, `replace_contact`, and the scope→op table — placed **outside** the two
`## … vocabulary` headings the doc-sync test scans, so it neither satisfies nor trips it.
Add a `## CV-editor AI chat` section to `CLAUDE.md` recording D1–D7, the "why not
`run_tool_loop`" reasoning, the `clipPath`/corner-pill trap, and the auto-mode-must-refuse-stale
rule.

**6c. Frontend bundle.** `cd frontend && npm run build` after the UI work — the `jsa` CLI
serves the gitignored `jsa/static` bundle, not live source.

### Verification

```bash
# 1. Backend
pip install -e .
pytest -v -m "not integration"                       # whole suite must stay green
pytest tests/backend/test_cv_chat.py tests/backend/test_cv_chat_ops.py \
       tests/backend/test_cv_chat_api.py -v
pytest tests/backend/test_tools_doc_sync.py tests/backend/test_patch_parity.py \
       tests/backend/test_mode_parity.py tests/backend/test_ingest.py -v   # the pinned gates

# 2. Frontend
cd frontend && npm install && npm test && npm run build && cd ..

# 3. i18n
scripts/translate-ui.sh && scripts/translate-ui.sh --check

# 4. Flags
jsa --csv jobs.csv --auto-mode --chat-backend claude-cli --no-browser
curl -s localhost:8765/api/config | python -m json.tool | grep -E 'auto_mode|chat_backend'
```

**5. End-to-end, in the browser** (playwright is not installed in this repo — verify by hand,
or install it first if a scripted pass is wanted):
1. Open the editor, hover the identity card → the corner `EDIT` pill appears **unclipped**
   above the card's top edge.
2. Click it → the anchored panel opens to the right of the card, the connector line draws,
   every other block dims to 30%, the card gets the accent ring.
3. Send `make the location more specific` → REASONING card fills live, then a `PROPOSED DIFF`
   with `BEFORE`/`AFTER` on the location field.
4. `APPLY` → the field updates, the card flashes green, the header shows unsaved. **One ⌘Z
   reverts the whole diff.**
5. Drop a PDF onto the panel → `DROP AS CONTEXT`, chip with size, `READ-ONLY CONTEXT` note.
   Send with no text → the identity fields are proposed from the file.
6. Open a section trigger → a `SCOPE → …` marker turn separates the two contexts.
7. `WIDEN → CV` → scope chip reads `ENTIRE CV`, dimming clears, connector disappears.
8. Scroll `<main>` → the panel tracks its anchor and hides the connector when occluded.
9. Toggle `AUTO` → the next diff applies itself and reads `AUTO-APPLIED`.
10. With `AUTO` on, edit a field **while a turn is in flight** → the returned diff is `STALE`
    and is **not** applied.
11. Reload the page, reopen the editor → the thread is still there; settled cards render;
    stale ones have APPLY disabled.
12. `NEW` → thread clears, and stays cleared after a reload.
13. Switch decks → the thread swaps to that deck's.
14. Confirm `SAVE` is still the only thing that writes to disk (check
    `~/.jsa/cv_decks/<id>.json` mtime).

---

## Change Log

_(entries appended as phases land: **YYYY-MM-DD**: context, actions, decisions, verification result)_

**2026-09-16**: Phase 1 (op vocabulary, contact support, turn schema). Added
`CvWorkingCopy.replace_contact` (`jsa/schema/patch.py`, present-keys-only merge),
`_REPLACE_CONTACT`/`CV_CHAT_OP_SPECS` (`jsa/agents/tool_spec.py`, NOT wired into
`tools_for`), and `jsa/schema/chat_turn.py` (`CvChatOp`/`CvChatPayload`/`CvChatTurn`/
`json_schema_for_cv_chat`). Decision made during implementation, not anticipated in the
plan text: `CvChatTurn` had to keep the EXACT `{kind, question, payload,
suggested_replies}` envelope `CvTurn`/`ClTurn` use (with `payload` typed as a new
`CvChatPayload{answer, ops}` rather than free-form `{answer, ops}` at the top level) —
verified that `AnthropicAPIBackend`/`OpenCodeZenBackend`/`_openai_compat` backends all
route a structured reply through the generic, schema-shape-based
`turn_models.parse_structured_reply_for_schema`, which only recognizes that exact
envelope. A top-level `{kind, answer, ops}` shape (as the plan's Phase 1c pseudocode
literally showed) would have silently failed on every structured backend with "missing
or invalid 'payload'". This keeps the "no change to backends" non-goal intact. Added
`tests/backend/test_cv_chat_ops.py` (20 tests). Verified: full backend suite green
(2392 passed, 6 pre-existing unrelated failures confirmed present identically on `main`
via `git stash` — timing-sensitive orchestrator/integration tests, not touched by this
phase).

**2026-09-16**: Phase 2 (job-less chat runner). Added `ChatChunkEvent`/`ChatTurnEndEvent`
(`jsa/events/schema.py`), `jsa/prompts/PROMPT_CV_CHAT.md` (+ `loader.py` registration),
and `jsa/pipeline/cv_chat.py::run_cv_chat` (`ChatScope`/`DiffItem`/`ChatTurnResult`/
`ChatError`). Decision made during implementation: parsing reuses `CvChatTurn`'s own
validator by wrapping the bare `{answer, ops}` reply text into a synthetic
`{"kind":"final","payload":<data>,...}` dict before calling `.model_validate` — gets
every op's required-field validation for free without a bespoke parser. D5's
authoritative structural-diff gate (`_enforce_scope`) walks by list POSITION (not id),
matching how `ChatScope.section_index`/`entry_index` are defined. Added
`tests/backend/test_cv_chat.py` (10 tests). Verified: `test_cv_chat.py` +
`test_cv_chat_ops.py` green (30/30); full suite re-run showed only the same class of
timing-sensitive orchestrator/integration flakiness as Phase 1's baseline (confirmed via
`git stash` on `test_integration.py`/`test_bf18_limit_detection.py` — different tests
fail from run to run on `main` too, unrelated to this phase's files).

**2026-09-16**: Phase 3 (persistence, routes, ingest, settings). Added
`jsa/ingest/text_source.py::load_text_source` (delegates `.pdf`/`.docx` to `load_cv`
unchanged; `.txt`/`.md`/`.json`/`.rtf`/`.csv` read as UTF-8), `jsa/store/deck_chats.py`
(`ChatTurn`/`DeckChat`, reusing `cv_decks.py`'s `_lock`/`deck_path` directly rather than
duplicating them), `jsa/api/routes_cv_chat.py` (GET/POST-multipart/DELETE on
`/api/cv-decks/{id}/chat`, registered in `server.py`), `Settings.chat_backend`/
`auto_mode` (+ registry validator, + `deck_chats_dir` property), the `--auto-mode/
--no-auto-mode` and `--chat-backend` CLI flags, and two new `/api/config` keys.
Verification note: the plan's own multipart snippet used `payload: str = Form(...)`
inside a `class`-style route signature; implemented as documented (a `Form(...)`
parameter alongside `files: list[UploadFile] = File(default=[])`) — FastAPI's own
requirement (Pydantic body models cannot mix with `UploadFile`) held as predicted, so
`ChatTurnPayload` is parsed manually via `json.loads(payload)` +
`ChatTurnPayload.model_validate` rather than being a direct route parameter. Added
`tests/backend/test_cv_chat_api.py` (14 tests: full round trip, 400/404/422 mapping,
`.txt` attachment accepted, `.png` rejected, thread persists across a fresh client, and
`chat_chunk`/`chat_turn_end` reach the bus) and `tests/backend/test_ingest.py`'s new
`TestLoadTextSource` (5 tests, including a re-pin that `load_cv` still raises for
`.txt`). Verified: `test_cv_chat_api.py` + `TestLoadTextSource` green (14/14); full
suite re-run: 2420 passed, 2 failed — same class of timing-sensitive orchestrator
flakiness as Phases 1–2 (different tests each run, `test_bf18_limit_detection.py` /
`test_orchestrator_throttling.py`, neither touched by this phase).

**2026-09-16**: `/code-review medium` over Phases 1–3, then fix-triage. Ran a
Sonnet-model, three-angle background review over the full diff. Of 8 findings, 4 were
applied, 1 rejected outright, 1 rejected in part, 2 rejected as accepted duplication:
- **Applied** — `routes_cv_chat.py::_scope_from_dict` did not validate
  `section_index`/`entry_index` at all (missing, non-int, out-of-range, or negative all
  reached `cv_chat.py`'s unguarded list indexing) — a 500 instead of a 422, and a
  negative index silently wrapped onto the wrong node via Python's slice semantics.
  Fixed to validate against the submitted `cv`'s actual shape before constructing
  `ChatScope`; 4 new regression tests in `test_cv_chat_api.py`.
  Also applied: the route's attachment temp-file `write`/`unlink` calls were plain
  synchronous I/O directly on the event loop (CLAUDE.md's explicit
  "wrap every blocking call in `asyncio.to_thread`" rule) — wrapped both. Note:
  `routes_cv_structure.py`'s near-identical temp-file dance has the same gap and was
  left untouched (out of this diff's scope; not introduced here).
- **Applied** — `CvWorkingCopy.replace_contact` copied `name`/`email`/`phone`/
  `location` straight from the model's dict with no stripping, unlike every sibling op
  and unlike this same method's own `links` handling one line below. Fixed to run
  `_clean_str_or_none` per field, with `name` (the one required `Contact` field)
  explicitly rejected as `bad_argument` on a blank/whitespace value rather than
  silently written as `None` and only failing later, opaquely, inside `finalize()`. 5
  new tests in `test_cv_chat_ops.py`.
- **Applied, but not as proposed** — the review flagged `copy.finalize(language=...)`
  in `cv_chat.py` as missing `structured=`, so a validation failure always uses
  sentinel-mode re-emit wording even in structured mode. Investigating further:
  `validation.py`'s own docstring says neither of `structured`'s two built-in wordings
  ("re-emit inside `<<<FINAL>>>`..." / "re-emit as your structured reply's `payload`")
  fits a caller with no self-heal loop — that is exactly `run_cv_chat`'s shape (a 422
  read by a human in the chat panel, never fed back to the model), the same reasoning
  `tool_loop.py` already applies via its own `reemit_hint`. Used `reemit_hint` with a
  chat-appropriate message instead of threading `structured=`. 1 new test in
  `test_cv_chat.py` pinning that neither built-in wording (nor "structured reply")
  leaks into the message.
- **Applied** — `cv_chat.py::_strip_code_fence` was a third independent copy of
  `validation.py`'s (already exported via `__all__` for reuse; `infer_structure.py`
  carries a second, pre-existing copy). Removed the third copy, imported the
  canonical one. Byte-identical logic, confirmed before removing.
- **Rejected in part** — the review's claim that the deck-chat history read
  (`existing = await deck_chats.load(...)`, for prompt-building) and `deck_chats.append`'s
  internal re-read are redundant is incorrect: `append` reads *inside* its per-deck
  lock specifically to do an atomic read-modify-write, which is what makes two
  concurrent turns on the same deck never lose one's write. The two reads serve
  different purposes (prompt context vs. atomic persist) and cannot be collapsed into
  one without breaking that guarantee.
- **Rejected** — the same finding's suggestion to parallelize the per-attachment
  read/parse loop via `asyncio.gather` (capped at 6 files, 5MB each) was left as
  sequential: the added complexity (preserving attachment order, per-file error
  ordering) isn't worth it at this cap size.
- **Rejected, accepted duplication** — `cv_chat.py::_OP_DISPATCH` duplicates
  `tool_loop.py::_dispatch_cv`'s op-name-to-method mapping. Consolidating would mean
  touching `tool_loop.py`, which the plan's own non-goals rule out ("No change to
  `tool_loop.py`... or the job revision path's behaviour"), and this repo already has
  a documented precedent for accepting exactly this kind of duplication rather than
  reconciling two independently-evolving call shapes (see CLAUDE.md's
  `opencode_zen.py`/`_openai_compat.py` note). Left as-is.
- **Rejected, accepted duplication** — `_enforce_scope` and `_diff_documents`/
  `_diff_entry` are two separate tree walks over the same `(original, finalized)`
  pair. They serve genuinely different purposes (fail-fast scope enforcement vs.
  collect-all human-readable diffing) and share their underlying shape definition via
  `_document_as_plain`/`_section_dict`/`_entry_dict` already; merging the two walks
  would entangle a raise-on-first-violation function with a collect-everything one for
  marginal benefit. Left as-is.

Verified: `test_cv_chat.py` + `test_cv_chat_ops.py` + `test_cv_chat_api.py` green
(48/48, 15 new); the four pinned regression gates
(`test_tools_doc_sync.py`/`test_patch_parity.py`/`test_mode_parity.py`/
`test_ingest.py`) green (82/82); full suite re-run: 2426 passed, 5 failed — same
pre-existing timing-sensitive orchestrator/integration flakiness class as every prior
phase, re-confirmed via `git stash`/`git stash pop` against unmodified `main` (2 of the
same tests fail there too; different tests fail each run on both trees).

**2026-09-16**: Phase 4 (frontend state: chat store, batch apply, WS wiring). Added
`editorStore.ts::applyAiDocument`/`reconcileIds` (one undoable `applyEdit(..., false)`
call; ids reused positionally so React keys/drag/selection survive an AI edit), two
`ChatTurnDTO`/`ChatDiffItemDTO` types + `chat_chunk`/`chat_turn_end` `WSEvent` members
(`types.ts`), three `api.ts` methods (`getDeckChat`, `sendDeckChat` — multipart, no
`Content-Type` header, mirroring `inferCvStructure` — `clearDeckChat`), two forwarding
cases in `store.ts::applyEvent` (`streamBuffers` untouched, per the plan), and
`frontend/src/cvChatStore.ts` (new): `open/collapsed/scope/deckId/turns/input/attach/
busy/elapsed/taskId/reasoning/content/auto/flashKey/showReason/error` +
`openChat/closeChat/setCollapsed/widenToCv/setInput/addFiles/removeFile/send/newThread/
hydrate/applyTurn/discardTurn/toggleAuto/onChunk/onTurnEnd`.

Decisions made during implementation, not spelled out in the plan text:
- **`ChatTurnDTO.status` (the wire type, mirroring `deck_chats.py::ChatTurn` exactly)
  has no `"stale"` member** — the backend model's `Literal` is `pending/applied/auto/
  discarded/none/error`. D4's staleness check is purely client-computed (comparing
  `base_hash`), so the store defines a client-only `ClientChatTurn` type
  (`ChatTurnDTO` with `status` widened by one member, plus a non-wire `deckId` field)
  rather than either polluting the wire type or hand-waving staleness as an untyped
  string. Also NOT persisted server-side — `applyTurn`/`send`'s auto-apply path only
  ever mutate the in-memory `turns` array.
- **Staleness/single-flight both mirror an existing precedent instead of inventing a
  new one**: `base_hash` is a client-side FNV-1a hash of `exportJson(cv)` (cheap,
  deterministic, no crypto needed — it only has to detect "did the buffer change",
  never resist tampering); `onChunk`'s gating is `busy`-only, NOT `task_id`-matched,
  copying `editorStore.onInferProgress`'s documented rationale verbatim (the server
  mints `task_id` only inside the request and the client cannot know it until the
  POST resolves — this is a single-user local tool, so single-flight is sufficient).
- **Deck-identity guard is checked explicitly in `applyTurn`, before the `base_hash`
  compare**, per the plan's own callout — not left to happen "incidentally" via a
  hash mismatch, since two different decks' documents are not guaranteed to hash
  differently.
- **A real 3-way circular import** (`store.ts` → `cvChatStore.ts` → `editorStore.ts`
  → `store.ts`) is introduced, on top of the pre-existing 2-way cycle
  `editorStore.ts` ⇄ `store.ts`. Confirmed safe on the same precondition the existing
  cycle already relies on: every `.getState()`/hook read happens inside a function
  body, never at module-eval time. `npx tsc --noEmit` and `vite build` both confirm
  no resolution issue in practice.

Added `describe("reconcileIds")` + `describe("applyAiDocument")` to
`editorStore.test.ts` (5 tests), a `chat_chunk`/`chat_turn_end` block to `store.test.ts`
(3 tests), and `tests/cvChatStore.test.ts` (10 tests: scope-marker-turn, FormData shape
+ scope→index resolution, auto-apply-fresh, auto-refuses-stale, deck-identity refusal,
stale refusal, successful apply, discard-does-not-touch-buffer, onChunk gating,
newThread). Verified: `npx tsc --noEmit` clean; full frontend suite 486/486 (469 + 17
new); `npm run build` clean (no new warnings beyond the pre-existing >500kB chunk-size
notice, unrelated to this phase).

Not yet wired: `CvEditor.tsx`'s mount effect calling `cvChatStore.hydrate` alongside
`hydrateDecks()` — that is Phase 5's "5g. Mounting" step, since there is no UI yet to
mount it into.

---

**2026-09-16**: `/code-review low` over the full branch diff (Phases 1-4). All 4 findings
verified against source and rejected — none required a code change:
- `_validate_chat_backend`'s "duplication" of `cli.py`'s `_VALID_BACKENDS` check matches the
  pre-existing `_validate_backends` precedent exactly (same defense-in-depth reasoning:
  `Settings()` can be constructed directly, bypassing CLI parsing).
- `clearDeckChat`'s assumed-empty-body concern was factually wrong — `routes_cv_chat.py`'s
  DELETE route declares `status_code=204` explicitly, and `apiFetch` already special-cases 204.
- `applyAiDocument`'s own silent no-op on a null `cv` matches editor convention and was left
  as-is, but a follow-up advisor pass caught that my initial "unreachable" reasoning for this
  finding didn't hold: `applyTurn` (`cvChatStore.ts`) calls `applyAiDocument` with no guard of
  its own, and it runs on a *rehydrated* turn (persisted thread reload, minutes later) — not
  only right after `send()`'s own null-cv guard. **Applied**: `applyTurn` now bails with
  `status: "error"` when `editor.cv` is null, checked before the staleness compare (which
  previously short-circuited to "pass" on a null `cv`, silently no-oping the turn to a stuck
  `"pending"`). 1 new regression test in `cvChatStore.test.ts`.
- `replace_contact`'s email/phone/location clearing "asymmetry" matches the codebase-wide
  `_clean_str_or_none` convention used identically for every other optional field
  (`entry.location`/`dates`/`heading`/`subheading`, cover-letter salutation/signoff); `name`
  is the sole deliberate exception, already stated in the method's own docstring.

Also installed `@playwright/test` + Chromium in `frontend/` (previously absent) for Phase 5's
in-browser visual verification.

**2026-09-16**: Phase 5 (frontend components: icons, corner triggers, anchored panel, diff
card, dim/ring/connector, mounting). Added `theme/Icon.tsx`'s 5 new glyphs (`chat, arrows,
clip, file, id`); `ReasoningCard.tsx`'s optional `theme?: Theme` prop (default `SHELL_THEME`,
threaded into `StepRow` too, existing call sites untouched); `components/cv-editor/ChatCorner.tsx`
(the overhanging corner pill, a sibling of the chamfered card per the `clipPath` trap);
`lib/chatAnchors.ts` (module-level `Map<scopeKey, HTMLElement>` anchor registry + `chatAnchorRef`
ref-callback factory); `lib/chatUnitState.ts` (`useUnitChatVisualState` dim/ring hook, with the
entry-scope-doesn't-dim-its-parent-section carve-out); `components/cv-editor/CvChatPanel.tsx`
(the full anchored panel: header, scope bar, thread, composer, quick actions, flip-to-left
geometry, connector SVG, collapsed/closed pill); `components/cv-editor/CvDiffCard.tsx` (all 6
status renderings including `STALE`'s RE-RUN affordance). Wired `ChatCorner` + anchor refs +
dim/ring into `BlocksView.tsx`'s `ContactCard`, `SectionCard`, `EntryEditor`, and a newly
extracted `SkillsRow` component; wired a lighter `PaperChatDot` (paper surfaces are unchamfered,
so no clipping trap) into `PaperSheet.tsx`'s per-section and per-entry wrappers; wired an inline
chat `ToolBtn` into `SplitView.tsx`'s outline rows. Mounted `CvChatPanel` in `CvEditor.tsx` as a
sibling of `<main>` (which now carries a `mainRef` the panel re-measures against on scroll/resize),
wired `cvChatStore.hydrate()` into the mount effect (alongside `hydrateDecks()`) and into
`DeckRail.tsx`'s `onSwitch` (after `switchDeck` resolves) — the deferred item flagged at the end
of Phase 4. Also fixed, during this phase, a code-review finding on `cvChatStore.ts::applyTurn`
(see the review entry above) and installed `@playwright/test` + Chromium in `frontend/` for
visual verification.

Decisions made during implementation, not spelled out in the plan text:
- **The hover-reveal wrapper class is `.cvunit`, a new class, not a reuse of `.cvsec`.**
  `.cvsec` is, in several places, the SAME element as the chamfered/clipped card itself (e.g.
  `ContactCard`, `EntryEditor`), so scoping the corner pill's hover-reveal off it would either
  not work (the pill is a sibling, not a descendant, of that element) or require moving `cvsec`
  up a level, which the plan's "cvtools clusters are not touched" rule argues against touching.
  A dedicated `.cvunit`/`.aitrig` CSS pair (`index.css`) keeps `.cvsec`/`.cvtools` byte-identical.
- **`SkillsRow` extracted as its own component** (not in the plan's text) purely so
  `useUnitChatVisualState`'s hook call happens at a stable count across renders — the skills
  case previously called into `.map()` directly inside `SectionBody`, and calling a hook inside
  that callback would vary the hook count with the section's entry count (rules-of-hooks
  violation), not something rendering multiple times.
- **Paper surfaces get a separate, lighter `PaperChatDot`, not the shared `ChatCorner`.** Paper
  section/entry wrappers are NOT chamfered (no `clipPath` — `PaperSheet.tsx`'s `cvpapersec`
  divs use a plain `border-radius`), so there is no clipping trap to route around there; a
  smaller `paperT`-colored dot matching the paper palette was used instead of `EDITOR_THEME`'s
  pill.
- **jsdom has no `Element.scrollTo`** (a real gap, not a jsdom config issue — see
  https://github.com/jsdom/jsdom/issues/1695). Rather than guard `CvChatPanel`'s auto-scroll
  effect defensively (real browsers always have this method), added a no-op polyfill to
  `setupTests.ts`, mirroring the file's existing `localStorage` polyfill precedent exactly —
  test-environment-only, production code unaffected.
- **Quick-action chips are filtered per `ChatScope` type client-side**
  (`QUICK_ACTIONS_BY_SCOPE` in `CvChatPanel.tsx`) — not specified in the plan text. The
  backend's `QUICK_ACTIONS` table (8 entries) has no notion of scope; showing all 8 regardless
  of scope (e.g. "ONE PAGE" while scoped to one bullet) would be confusing, so the panel picks
  a relevant subset per scope type. The server remains the sole authority on what each key
  means; this is a display-only filter.
- **i18n deliberately NOT done in this phase** — per the plan's Phase 6 split rule and to avoid
  doing the real-sentence-vs-HUD-chrome classification twice, every new string in this phase's
  components is a plain inline literal (both the ones that will become `useT()` keys and the
  ones that stay inline HUD chrome per CLAUDE.md's carve-out). Phase 6 will do the full pass.
- **Test scope**: `BlocksViewChat.test.tsx` and `CvChatPanel.test.tsx` are new render harnesses
  (BlocksView/CvChatPanel had none before) and focus on the render/click WIRING layer — trigger
  presence, scope resolution from a real click, dim/ring DOM output, and diff-card button clicks
  reaching the right store action end-to-end. They deliberately do not re-assert pure-logic
  branches (deck-identity mismatch, staleness math, auto-mode) already exhaustively covered by
  `cvChatStore.test.ts`'s 12 tests.

Visual verification (playwright, newly installed): (1) an isolated static-HTML reproduction of
the `panelBase()` `clipPath` + corner-pill structure, screenshotted, confirming the pill renders
fully above the card's clipped top edge, unclipped; (2) a live end-to-end check against a
temporary `jsa` server (`JSA_DB_PATH` pointed at a scratch dir, not the user's real `~/.jsa`)
with a sample CV imported via the editor's "OPEN JSON" flow: corner triggers render per unit,
hover-reveal works, clicking a trigger opens the panel scoped correctly (verified against both
a section and an entry), quick-action chips vary by scope, dim/ring visual state matches the
entry-doesn't-dim-its-own-section carve-out, and the flip-to-left geometry branch engaged
correctly at a real 1440px viewport — confirming the specific risk the plan called out (a
centered ~760px column's naive right-of-anchor placement clamping back onto the anchor). No
console or page errors during the check; the temporary server was killed afterward.

Verified: `npx tsc --noEmit` clean; full frontend suite 500/500 (487 prior + 13 new:
`BlocksViewChat.test.tsx` 6, `CvChatPanel.test.tsx` 7); `npm run build` clean (same pre-existing
>500kB chunk-size notice as every prior phase, unrelated); live browser check clean (see above).

Not yet done: i18n (Phase 6), docs (`docs/TOOLS.md`/`CLAUDE.md` sections, Phase 6), and the
final `/code-review` + merge-approval step.

**2026-09-16**: Phase 6 (i18n, docs, verification) — completed in two sessions separated
by a scheduled resume. Context: this was the last remaining phase; Phases 1–5 were fully
implemented and reviewed as recorded above.

Actions, session 1 (i18n code, pre-translation): added 24 `cvChat.*` keys to
`frontend/src/i18n/strings.en.json`, per the plan's 6a split rule (real sentences only —
empty-thread copy, footer lines, stale explanation, quick-action **labels**, tooltips, the
read-only-context note, error text; HUD chrome — `DAEMON // EDIT`, `AUTO`, `WIDEN → CV`,
`SEND`/`APPLY`/`DISCARD`, `CollapsedPill`'s three variants, `STATUS_META`'s labels, scope
fallback strings — left inline, per CLAUDE.md's explicit carve-out). Wired `useT()` into
`CvChatPanel.tsx`, `CvDiffCard.tsx`, `ChatCorner.tsx`, `PaperSheet.tsx`'s `PaperChatDot`,
and `SplitView.tsx`'s section trigger tooltip. Exported `editorStore.ts`'s previously
module-private `tr()` (the store's non-hook i18n twin) so `cvChatStore.ts` could reuse it
for its one async-action error string (`cvChat.requestFailed`) instead of carrying a
second copy of the same three-line body. Added a `## CV-editor chat vocabulary` section to
`docs/TOOLS.md`, placed at the very end of the file — read `tests/backend/
test_tools_doc_sync.py`'s actual scanning algorithm first (it bounds its scan to the two
`## … vocabulary` headings and the next `## ` heading following each) and confirmed the
insertion point falls outside both scanned ranges before writing, per the plan's own
6a/1b warning that a misplaced section would trip
`test_the_doc_documents_no_tool_that_does_not_exist`.

Decision made during implementation, not spelled out in the plan text: **the user
explicitly instructed "Don't run translation" mid-session** — `scripts/translate-ui.sh`
(incremental locale generation + `--check`) was deliberately **not** run. The `cvChat.*`
keys exist only in `strings.en.json`; per-locale `strings.<code>.json` catalogs were not
regenerated for them. This is a deliberate, user-directed deferral, not an oversight —
`useT()`/`tr()` already fall back to English then the raw key when a translation is
missing, so this is a display gap (non-English UIs show English text for the new chat
strings) rather than a broken build. Flagged here since it diverges from the plan's
literal 6a text ("Then run `scripts/translate-ui.sh`... and verify with `--check`").

Actions, session 2 (docs, build, smoke test, this entry): added a `## CV-editor AI chat`
section to `CLAUDE.md` recording D1–D7, the "why not `run_tool_loop`" reasoning, the
`clipPath`/corner-pill trap, and the auto-mode-must-refuse-stale rule, plus a short "Other
non-obvious invariants" subsection (job-less/no-DB-row, attachment text never persisted,
fresh-session-per-turn history rendering, shared context budget trim order, independent
`chat_backend` setting). Ran `cd frontend && npm run build` — clean, same pre-existing
>500kB chunk-size notice as every prior phase. Ran the flags smoke test: `JSA_DB_PATH`
pointed at a scratch dir (`/tmp/jsa-phase6-smoke`, not `~/.jsa`), `.venv/bin/jsa --csv
<scratch>/jobs.csv --auto-mode --chat-backend claude-cli --no-browser`; `GET /api/config`
confirmed `"auto_mode": true` and `"chat_backend": "claude-cli"`; `GET /api/health`
returned `{"ok": true}`; server stopped afterward. Attempted the plan's 14-step in-browser
end-to-end checklist via the Claude-in-Chrome tool — **the browser extension was not
connected in this environment** (`tabs_context_mcp` returned a connection error), so the
interactive click-through (steps 1–14 of the Verification section) was **not** performed
this session. As a partial substitute: confirmed the built bundle
(`jsa/static/assets/index-*.js`) contains both the new i18n strings (`Chat request
failed`) and the inline HUD strings (`DAEMON // EDIT`, `PROPOSED DIFF`), and confirmed
`jsa/static/index.html` references the freshly built asset hashes.

Decisions made during implementation:
- **The i18n code work and the translation-generation step were split into two
  independently-verified units**, not merged into one "i18n done" claim — the code
  changes (tsc clean, 500/500 frontend tests, 130/130 targeted backend tests, 2431/2431
  full backend suite) are verified and complete; the locale-generation step is verified
  **not done**, by explicit user instruction, and is called out as a known gap rather than
  silently absorbed into "Phase 6 complete."
- **The in-browser end-to-end checklist is marked attempted-but-blocked, not
  skipped-silently or fabricated-as-passed** — per this session's own operating rules
  against ever presenting a result that wasn't actually observed. Phase 5's own visual
  verification (via a locally-installed Playwright + a temporary server) already exercised
  the corner-pill/clipPath fix, the flip-to-left geometry, and dim/ring behavior at the
  component level before i18n strings were wired in; this phase's static-string checks
  (above) are the closest available confirmation that the i18n wiring itself didn't break
  anything visible in the built bundle.

Verified: `npx tsc --noEmit` clean; frontend suite 500/500 (unchanged from Phase 5's
count — no new tests added this phase, only string/i18n wiring); targeted backend suite
(`test_cv_chat.py` + `test_cv_chat_ops.py` + `test_cv_chat_api.py` +
`test_tools_doc_sync.py` + `test_patch_parity.py` + `test_mode_parity.py` +
`test_ingest.py`) 130/130; full backend suite 2431 passed, 2 skipped, 21 deselected, zero
failures (no flaky-test class observed this run, unlike Phases 1–3's noted timing
flakiness); `npm run build` clean; flags smoke test verified via `curl`; in-browser
checklist **not verified** (browser extension unavailable) — needs user action if a
scripted or manual pass is wanted before merge. `/code-review low` and the merge-approval
question are the two steps remaining after this entry.

**2026-09-16**: Task — run `/code-review low` against `feat/cv-editor-ai-chat` (pinned via
explicit `cd` + branch confirmation before invoking, per CLAUDE.md's "Pin the review
target" rule) and triage every finding. Actions — the review returned two findings, both
verified against source and applied:
- **CONFIRMED, severe.** `jsa/pipeline/cv_chat.py::_enforce_scope` had no branch for
  `scope.type == "cv"` — it fell through into the entry-scope check, which compares
  against `scope.section_index`/`scope.entry_index` (both `None` for `cv` scope); since an
  `int` index is never `== None`, every section's "changed outside scope" test was always
  true, and the preceding unconditional contact-invariant check also blocked
  `replace_contact` even though `cv` scope's own `_SCOPE_OPS` allows it. Net effect: the
  "ENTIRE CV" scope — the default/most common entry point — could never actually apply an
  edit. Fixed by adding an explicit `cv`-scope branch that only enforces the (structurally
  always-true, since no op adds/removes whole sections) section-count invariant and returns.
  Pinned with two new regression tests in `tests/backend/test_cv_chat.py`
  (`test_cv_scope_allows_summary_edit`, `test_cv_scope_allows_contact_edit`).
- **Rejected.** `jsa/store/deck_chats.py::save()` has no caller today (`append()`
  reimplements the same load-mutate-save sequence independently) — technically true, but
  this plan's own Phase 3b spec (line ~484-485) names `save(settings, deck_id, chat)`
  explicitly as part of "`preferences.py`'s five-function pattern verbatim". Deleting it
  would have silently reopened a locked design decision the reviewer had no visibility
  into — it sees only the diff, not this plan. Reverted the deletion; `save()` and its
  `__all__` entry stay, kept for interface parity with `preferences.py`'s documented shape
  even though nothing currently calls it.

Verification: targeted suite (`test_cv_chat.py` + `test_cv_chat_ops.py` +
`test_cv_chat_api.py`) 50/50; full backend suite 2432 passed, 2 skipped, 21 deselected (one
unrelated `test_dev_tunnel.py` flake observed in the full run, confirmed pre-existing and
passing in isolation via a standalone rerun — not touched by this diff); full frontend
suite 500/500 unchanged (no UI files touched by either finding, so no rebuild was needed).
Triage: finding 1 (the `cv`-scope bug) applied with two new regression tests; finding 2
(dead `save()`) rejected as above, so the review's net code change is the one-function fix
in `cv_chat.py` plus its tests.

**2026-09-16**: Task — close the plan's step-6 verification gap: the 14-step in-browser
checklist (Phase 6, "Verification" §5) had never actually been run (previous session noted
the Claude-in-Chrome extension wasn't connected). Per this session's explicit instruction,
installed Playwright (`@playwright/test`, already a devDependency on this branch, chromium
browser already cached locally) and drove the real app — `npm run build`'d bundle served by
`jsa`, backend started with `--chat-backend gemini` and a user-supplied, one-time
`GEMINI_API_KEY` passed only as a process env var (never written to any file; scrubbed all
server logs and temp files afterward and confirmed no leakage — key is to be rotated by the
user). Ran against the real decks in `~/.jsa/cv_decks/` with a real Gemini backend, so this
is a genuine end-to-end pass, not a mocked one.

Actions/results per checklist step: 1 (unclipped corner pill) — verified. 2 (anchored panel,
IDENTITY scope) — verified. 3 (send → reasoning → PROPOSED DIFF with BEFORE/AFTER) —
verified. 4 (APPLY writes the field, header shows unsaved, one ⌘Z reverts) — verified. 5
(PDF attach → chip + READ-ONLY CONTEXT note) — verified; the "send with no text" half
surfaced **Finding C** below. 6 (scope marker turn) — verified. 7 (WIDEN → CV) — verified.
8 (panel tracks `<main>` scroll) — verified. 9 (AUTO toggle + AUTO-APPLIED) — verified. 10
(mid-flight edit under AUTO → returned diff marked STALE, not applied, and the in-flight
edit itself survives) — verified. 11 (reload persistence; diff cards render) — verified,
with a caveat, see below. 12 (NEW clears the thread) — verified. 13 (deck switch swaps the
thread) — verified. 14 (SAVE is the only thing that writes to disk) — verified directly via
`stat` on the deck JSON files before/after the whole session: both untouched (mtimes from
before this session), while the separate `~/.jsa/deck_chats/<id>.json` side-files did
change — confirming the chat feature never touches the deck's CV file, only its own thread
store, exactly as D2/the "SAVE is the only writer" invariant requires.

Three real findings surfaced by actually exercising the feature against a live model,
beyond what the unit/integration suites (which use `FakeAgentBackend`) can catch:

- **Finding A — Applied.** `cvChatStore.ts::send()` read `res.turns` but silently discarded
  `res.rejected` — the array `jsa/pipeline/cv_chat.py::_apply_ops` populates when an
  individual op fails validation (bad id, `bad_argument`, etc.) without failing the whole
  turn. Reproduced live: asked Gemini to change the contact email under `contact` scope; its
  own visible reasoning correctly planned `{"email": "andrei.k@example.com"}`, but the
  actual op it emitted had `contact: {}` (empty), which `CvWorkingCopy.replace_contact`
  correctly rejected ("contact must name at least one of (...)") — yet the turn still came
  back `status: "pending"`/`"applied"` with the model's own confident answer text ("Updated
  your email address to..."), and the user had **no way to know the edit never happened**.
  Fixed in `cvChatStore.ts::send()`: when `res.rejected` is non-empty, surface it via the
  existing `error` state slot (already rendered as a `role="alert"` line in the panel — no
  new status/UI element added, per the instruction not to redesign the card). New i18n key
  `cvChat.partialApplyWarning` (`strings.en.json`). Regression test added:
  `cvChatStore.test.ts`'s `"surfaces a per-op rejection instead of silently dropping it"`.
  Frontend suite re-run: 501/501 (was 500/500). Rebuilt `jsa/static` after this change.

- **Finding B — Escalated, not fixed.** `CvChatOp`'s `section`/`entry`/`contact` fields are
  typed `dict[str, Any] | None` (`jsa/schema/chat_turn.py`), which renders in the generated
  JSON Schema as a bare `{"type": "object", "additionalProperties": true}` with **no
  declared properties**. Confirmed via `json_schema_for_cv_chat("contact")` (no model call
  needed) that this is exactly the schema shape sent to structured-mode providers. This is
  the same class of problem CLAUDE.md's own history already names (`c51592d`, "Gemini:
  require every property in responseSchema") — a provider enforcing structured output has no
  declared keys to target inside a property-less object and appears to fall back to `{}`,
  discarding the model's actual intent even though its own reasoning trace states the
  correct payload. This is Finding A's root cause for the Gemini repro above, and by the
  same schema shape it plausibly affects `replace_section`/`replace_entry`/`add_entry` too,
  under **any** structured-mode backend (`anthropic` forced-tool-use, `opencode-zen`,
  `mistral`, `openrouter`, `opencode-go` `/chat`, `gemini`) — not sentinel-mode backends
  (`claude-cli`, `google-cli`), which have no such wire-schema constraint at all. Could NOT
  confirm or rule out sentinel mode empirically this session: two attempts to reproduce
  against the shipped-default `claude-cli` backend both hit `AgentLimitReached` ("rate limit
  reached... seven_day utilization 0.31") — that CLI shares this very Claude Code session's
  own account quota, so repeated attempts were stopped rather than risking further
  consumption. **Not fixed** — this is a schema-design call across three op fields (define
  an explicit typed sub-model per field, mirroring how the rest of the schema layer already
  treats "every property required" for strict providers) with unknown full blast radius,
  not a contained bug fix; escalating for the user/a follow-up phase rather than patching it
  inside a verification pass.

- **Finding C — Escalated, not fixed.** `POST /api/cv-decks/{id}/chat`
  (`jsa/api/routes_cv_chat.py`) unconditionally 422s with `"instruction or quick_action is
  required"` when both are empty — **even when files are attached**. This directly
  contradicts the plan's own step-5 spec ("Send with no text — the identity fields are
  proposed from the file") and the frontend's own send-guard in `cvChatStore.ts`, which
  explicitly permits sending when `attach.length > 0` with no text. Reproduced live:
  attaching the CV PDF with an empty composer and clicking SEND returns HTTP 422 verbatim.
  The frontend was clearly built assuming file-only turns are legal; the backend was never
  updated to allow them, and `run_cv_chat`'s `instruction` parameter has no synthesized
  default for this case either. Fixing this means choosing what default instruction text to
  send the model when the user supplies only a file (a prompt/design decision) plus relaxing
  the route's validation — bigger than a contained fix, so escalating rather than patching.

Verification for this pass: frontend suite 501/501 (Finding A's fix + its regression test);
`jsa/static` rebuilt after the frontend change. No backend files were touched this pass —
Findings B and C are escalated, not applied, so no backend regression tests were added for
them. The two decks used for manual verification (`~/.jsa/cv_decks/`) were confirmed
byte-unchanged throughout (mtimes predate this session); only their separate chat-thread
side-files (`~/.jsa/deck_chats/`) changed, which is expected and by design.

**2026-09-17**: *Context* — the user accepted Finding C as-is and directed that Finding B
("structured-mode providers silently return `{}` for the `contact`/`section`/`entry` op
payloads") be scoped and fixed. *Actions* — root cause confirmed by dumping the schema
Gemini actually receives: `CvChatOp.section`/`entry`/`contact` were `dict[str, Any]`, which
Pydantic renders as a bare `{"type": "object"}`, and `turn_models.inline_defs` then strips
the `additionalProperties` that was its only remaining key. An object schema declaring no
properties has exactly one valid completion under constrained decoding — `{}`. Fixed by
declaring three real models in `jsa/schema/chat_turn.py` (`ChatEntry`, `ChatSection`,
`ChatContact`), mirroring the hand-written `_ENTRY_SCHEMA`/`_SECTION_SCHEMA`/
`_REPLACE_CONTACT` that `jsa/agents/tool_spec.py` already uses on the revision path — the
precedent was in-repo the whole time; only the chat path had skipped it.
*Decisions/friction* — four judgement calls, none of them cosmetic:
(a) **`links: list[str] | None`, not `list[str]`** — `inline_defs` forces every property
into `required`, so a non-nullable `links` would compel the model to emit `[]`,
`exclude_none` does not strip `[]`, and every "fix the email" turn would silently clear the
user's links. Nullable keeps `[]` meaning "clear them, deliberately".
(b) **`exclude_none=True` at the `replace_contact` dispatch site only** — it is a partial
update ("an omitted key leaves the existing value alone"); the entry/section ops are
whole-object replaces and dump plainly.
(c) **All three models are DEFAULTED, diverging from their `tool_spec.py` twins' all-required
lists** — `inline_defs` re-requires everything for Gemini regardless, and the other
structured backends send `"strict": false`, so defaults cost nothing where it matters while
preserving sentinel-mode tolerance. `chat_backend` defaults to `claude-cli`, which gets no
schema at all, so a partial object there would otherwise have hard-failed the whole turn.
(d) **A second, distinct defect surfaced during live re-verification and was fixed too**:
`_render_cv_dump` never rendered an entry's `dates`/`location`/`text`/`links`. Since
`replace_entry` is a whole-object replace, a field the model was never shown is a field it
cannot restate — "add the location, keep everything else the same" came back with
`dates: null` and wiped `2021 - present`. The dump now renders every field `ChatEntry`
accepts, and `PROMPT_CV_CHAT.md` gained literal JSON shape examples for all three object
arguments plus a corrected `replace_contact` instruction (the old "omit, never pass null"
wording became actively wrong once structured mode started forcing every key present).
(e) **`exclude_none` silently removed `null`'s old "clear this field" meaning**, so
"remove my phone number" would have returned a confident answer, an empty diff and no
`rejected` entry — the exact silent-success shape Finding A was about, reintroduced on a
different path. `links` already had `[]` as its clear affordance; scalars had none. Closed
with zero code (`_clean_str_or_none("")` is already `None` inside `replace_contact`): the
prompt now documents three distinct values per field — a value sets, `null`/omitted leaves
alone, `""`/`[]` clears — and `name` correctly stays unclearable.
*Verification* — **verified live** on gemini-3.1-flash-lite, three paths: `replace_contact`
produces a real single-field edit with name/phone/location/links preserved; `replace_entry`
produces a one-row diff (`location` only) where it previously also blanked `dates`; and
"remove my phone number" clears exactly that field. Backend suite 2455/2455. Pinned by new
regression tests asserting on the **inlined** schema (the form Gemini actually receives),
the contact partial-update and clear paths end-to-end through `_OP_DISPATCH`, the dump's
entry-field completeness, and a drift guard that parses `PROMPT_CV_CHAT.md`'s own JSON
examples and validates them against the models — the closest available stand-in for
sentinel-mode coverage, and worth having because the models are `extra="forbid"`, so a
typo'd field in an example would hard-fail every sentinel turn that copied it faithfully
while the suite stayed green. The five invariants above are recorded in CLAUDE.md's
"CV-editor AI chat" → "Other non-obvious invariants". **Unverified**: sentinel mode
(`claude-cli`, the shipped default) still could not be exercised — it shells out to this
session's own CLI quota, which was rate-limited. There the prompt is the only channel, so
the JSON examples are the whole fix and remain untested against a live model.
*Noted, not changed*: `CV_CHAT_OP_SPECS` (`jsa/agents/tool_spec.py`) has no runtime consumer
— the chat path builds its schema from `json_schema_for_cv_chat`, not from tool specs. It is
referenced only by a composition assertion in `test_cv_chat_ops.py` and by `docs/TOOLS.md`.
Left in place deliberately rather than removed; flagging it as a possible cleanup.

---

**2026-09-18**: Live multi-provider verification of the chat against `gemini`, `openrouter`
and `mistral` (user-supplied keys), after a report that "gemini failed to work, with HTTP
request". No code changed — the goal turned out to be a configuration issue plus two
findings worth recording.

*Setup*: isolated `JSA_DB_PATH` under a scratch dir (so `cv_decks_dir` / `deck_chats_dir` /
`backend_models.json` all isolate with it), seeded with one fresh deck built from
`~/Documents/Life/CVs/Per Industry/cv_master_all.json` — 4 sections, a 2136-char Summary,
31 Skills entries, 7 Experience entries. Exercised three ways: `run_cv_chat` directly, the
real multipart HTTP route, and the built UI under Playwright (the Chrome extension was not
connected).

*Result — all three providers work on their shipped default models, in structured mode,
with no schema downgrade* (`server_gemini.log` has zero `_SchemaRejected` / downgrade
warnings). Verified turns: `gemini-3.1-flash-lite` — Summary compact (13.9s), whole-CV
one-page (22.4s, 60 diff items), Skills compact (41 items); `mistral-small-2603` — Summary
compact, Experience-entry expand (25.2s); `openrouter` / `nvidia/nemotron-3-nano-30b-a3b` —
Skills compact (56.8s, 37 items), Summary expand. UI pass confirmed the full loop on both
gemini and openrouter: corner trigger → panel → REASONING card streaming real steps →
PROPOSED DIFF → **APPLY** landing in the live editor buffer as one undo entry, zero console
errors. `inline_defs` handles the chat schema fine on Gemini — the `$defs`/`$ref`/`default`
concern was unfounded, and the `anyOf`+`{"type":"null"}` shape it leaves behind is the same
one `json_schema_for(Stage.cv_adjust)` already ships.

*Cause of the reported failure — almost certainly a missing `GEMINI_API_KEY` in the
server's environment.* Reproduced exactly: with the key unset the route returns 422
`{"detail": "backend error: gemini API error: Method doesn't allow unregistered callers
..."}`, and because `api.ts::apiFetch` throws `HTTP ${status}: ${body}`, the panel renders
that raw JSON blob verbatim, beginning with the literal text "HTTP 422:". That is the
"failed with HTTP request" symptom. The backend is fine; the key must be exported in the
same shell that launches `jsa`, and `chat_backend` is launch-time only
(`JSA_CHAT_BACKEND` / `--chat-backend`) — `/api/config` exposes it read-only and no
frontend code reads it, so switching providers needs a restart.

*Finding 1 (not fixed, needs a decision)*: **one malformed op costs the whole turn.**
`run_cv_chat` has no self-heal budget by design (mirroring `run_infer`), so a single
cross-field validation failure raises `ChatError` → 422 with no retry. Shipped defaults did
not trip it in ~10 turns, but two weaker models did, immediately: `qwen/qwen3-30b-a3b-
instruct-2507` emitted `edit_entry_bullets` with a null `entry_id` against the entry-less
Summary section, and `openai/gpt-5-nano` produced 5 validation errors at once.

The sharper diagnosis: `json_schema_for_cv_chat(scope.type)` narrows the op enum by scope
*type* only, never by the scoped node's actual shape. Summary has `entries=0`, so the
`section` enum still offered `replace_entry` / `edit_entry_bullets` / `add_entry` /
`remove_entry` / `reorder_entries` for a node where there is no valid `entry_id` in the
dump to supply — four of those five are unsatisfiable, and a weak model picks one. Two
candidate fixes: narrow the enum by node shape (provider-enforced, contained to that
function plus its `cv_chat.py` call site), or add a one-shot correction retry feeding the
validation error back (recovery-after-failure, and a departure from the deliberate
no-self-heal design). Neither attempted pending a decision.

*Finding 2 (not fixed, latent)*: **nothing bounds total streaming duration.** All four HTTP
backends do `httpx.AsyncClient(timeout=self._timeout)` then `client.stream(...)`
(`gemini_api.py:642`, `_openai_compat.py:990`, `opencode_zen.py:851`,
`opencode_go.py:434`); httpx applies that as a *per-read* timeout, so as long as chunks
keep arriving the 300s budget never fires. This half is a code reading, verified at those
four sites. The empirical half is weaker and should not be over-read: one `gemini` Skills
turn ran 15 minutes without raising and was **killed before it returned**, so whether it
would ever have completed is unknown; a later call on the same scope finished in ~2
minutes. An unbounded hang was NOT reproduced. Fixing this is a four-file change (and `opencode_zen.py` keeps its own
independent copy by CLAUDE.md's no-shared-base rule), so it is recorded rather than
attempted here.

*Verification result*: verified live for all three providers; findings 1 and 2 need user
action/decision.

---

**2026-09-18** (same session, after the user chose both recommended options): implemented
the two fixes the verification above called for. Finding 2 (the unbounded stream) was
deliberately left alone.

*Fix 1 — narrow the op enum by the scoped node's SHAPE, not just its type.*
`jsa/schema/chat_turn.py` gains `_ENTRY_ADDRESSING_OPS` (`replace_entry`,
`edit_entry_bullets`, `remove_entry`, `reorder_entries`) and a new
`ops_for_scope(scope_type, *, has_entries=None)`; `json_schema_for_cv_chat` takes the
same keyword and drops those four when `has_entries is False`.
`jsa/pipeline/cv_chat.py::_scope_has_entries(cv, scope)` computes it at the one call site
(section → that section's entries; cv → any section's; contact/entry → always True).
**`has_entries=None` is byte-identical to the pre-narrowing schema**, so the narrowing is
opt-in and no existing caller changed — pinned by
`test_cv_chat_ops.py::TestShapeNarrowing::test_omitting_has_entries_is_byte_identical_to_pre_narrowing`.
**`add_entry` is deliberately NOT dropped**: it requires `("section_id", "entry")`, never
an entry id, so it stays satisfiable on an empty section and is a real capability ("turn
this prose into entries"). Dropping it would narrow capability rather than remove an
impossible choice. A drift guard asserts the surviving vocabulary against `_REQUIRED_FIELDS`
itself, so adding a new entry-addressing op without registering it fails the suite.

*Fix 2 — unwrap FastAPI's `detail` in `frontend/src/api.ts`.* New exported
`errorMessage(status, body)` prefers a non-blank string `detail`, else falls back to the
previous `HTTP ${status}: ${body}`. The fallback deliberately survives for a non-JSON body,
a missing `detail`, and a **list-shaped** `detail` (FastAPI's request-model 422s), which
would otherwise render as `[object Object]`.

*Verification*: backend `2467 passed, 2 skipped` (0 failures); frontend `507/507`, `tsc
--noEmit` clean, `npm run build` clean (only the pre-existing >500kB chunk notice);
`npm run build` re-run so the gitignored `jsa/static` bundle the CLI serves is current.
Live: `qwen/qwen3-30b-a3b-instruct-2507` on the entry-less Summary no longer reaches
`edit_entry_bullets` at all — it now picks the valid `replace_summary`, proving the enum
narrowed on the wire. `gemini-3.1-flash-lite` on the same scope still succeeds (no
regression).

*Fix 3 — the prompt's "allowed ops:" line is now derived, not hard-coded* (found by
`/code-review low`, which flagged `_scope_label` for an unrelated reason). That line was a
per-scope string literal listing the full section/entry vocabulary. Correct while both it
and the schema came from scope TYPE alone — but Fix 1 made the schema also narrow by node
SHAPE, so on an entry-less section the prompt would have advertised `edit_entry_bullets`
while the schema forbade it. `run_cv_chat` now resolves `has_entries` **once** and feeds
both channels (`ops_for_scope` → the prompt line, `json_schema_for_cv_chat` → the enum),
the same single-resolution rule CLAUDE.md applies to `injection` and to the
`structured_schema`/`adapt_history` pairing. This matters most in **sentinel mode**, where
there is no schema and that line is the only constraint — i.e. on `claude-cli`, the default
`chat_backend`. Pinned by `test_prompt_allowed_ops_line_matches_the_schema_enum`
(parametrized over ALL FIVE scope shapes, asserting the advertised set EQUALS the enum)
and `test_sentinel_mode_prompt_is_narrowed_too`.

**One deliberate prompt-byte change beyond the narrowing, called out because it is easy to
miss:** the old `_scope_label` literals matched `_SCOPE_OPS` exactly for `section`, `entry`
and `contact`, but at **`cv` scope the literal was the vague `"all ops"`**. Deriving the
line changes that to the explicit 8-op list on every whole-CV turn — a path where nothing
was narrowed. Kept, not reverted: `PROMPT_CV_CHAT.md` already describes this line as "the
exact op names you are allowed to use this turn", so naming them is strictly more specific
than `"all ops"` and more consistent with that sentence. So the "no existing caller
changed" property is exact for the **schema** channel (`has_entries=None` is byte-identical)
and has this one intended exception in the **prompt** channel. Pinned by
`test_cv_scope_line_is_the_explicit_vocabulary_not_all_ops`, and verified live afterwards:
`gemini-3.1-flash-lite` at `cv` scope returned 60 diff items with `rejected: []`.

*No `PROMPT_CV_CHAT.md` change was needed.* Its op list is a reference catalog that was
always broader than any one scope's allowed set, and line 9 already designates the per-turn
`EDITABLE SCOPE` / `allowed ops:` line as authoritative — so the catalog listing
`edit_entry_bullets` does not contradict a turn that excludes it.

*The other two review findings were verified and NOT acted on*, both pre-existing and
outside this change: `ChatTurnEndEvent` is published in the `start_session` `finally`
(before op-application), which is defensible — it marks the end of *streaming*, while the
turn's result/error travels over the HTTP response the client is still awaiting; and
`end_session` sits outside a `try/finally`, which on every HTTP backend is a no-op that
clears in-memory history (`_openai_compat.py:774`), so the practical leak is nil. Both are
worth a separate look, neither belongs in a provider-verification pass.

*Pre-existing suite flakiness, NOT introduced here — do not chase it as a regression.*
Two wall-clock-sensitive tests fail intermittently on this branch:
`test_integration.py::TestRevisionFlow::test_revised_document_content_matches_reply` (a
5.0s `_run_orchestrator_until` poll) and
`test_dev_tunnel.py::TestStartTunnelUrlPrinted::test_daemon_thread_is_started` (a
thread-start race). Evidence: three consecutive full runs at IDENTICAL composition gave
three different outcomes — one failure in each of those tests and one clean
`2469 passed`; and a baseline run with both new test classes deselected still failed
`test_daemon_thread_is_started`. Adding tests perturbs timing and surfaces it more often,
but does not cause it. Re-running is the current workaround.

*Known remaining, NOT fixed*: the narrowing removes the **unsatisfiable-op** class only.
Two weak models still fail on a different class — correct op, wrong field: qwen now returns
`replace_summary` with the prose in `bullets` and `text: null`, and `openai/gpt-5-nano`
still fails outright. That class is what the one-shot self-heal retry would have caught;
the user chose enum-narrowing over the retry, so it remains open by decision, not oversight.
Both models are outside the shipped defaults, all three of which pass.

## Decisions Log

_(for the user's own hand — entries on rejected approaches, overrides, and deliberate deferrals.)_
