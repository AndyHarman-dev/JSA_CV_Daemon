---
status: InProgress
---

# Revision patching via bounded tool use

## Context

Today a revision regenerates the entire artifact. `run_stage`'s revision branch
(`jsa/pipeline/stages.py:775-843`) restores the original stage's history, sends
`rev_req.instruction` verbatim (`:835`), and `_handle_final` (`:1295`) expects a
complete `CVDocument` back — so "tighten the summary" costs a full CV of output
tokens. Output tokens are the expensive half of every provider's bill, and a CV is
the largest artifact this pipeline produces.

The fix is to let the model **patch** the stored document through a small set of
tools inside a bounded agentic loop, and to reconstruct the full document
server-side. Two things make this non-trivial here, and both are the real substance:

1. **There is no tool channel anywhere.** Every backend call chain is `str` in →
   `str` out (`retry_transient(call_once) -> str`,
   `AgentReply.kind: Literal["final","needs_input"]`, `HistoryTurn.content: str`),
   across **five independent request builders** — CLAUDE.md explicitly forbids
   DRY-ing `opencode_zen.py` onto the shared base.
2. **Backends vary wildly.** OpenRouter fronts hundreds of models of mixed
   capability behind one name, `opencode-go`'s `/messages` path has a *documented
   negative result* for forced tool-use, and the CLI backends deliberately run with
   their own tools disabled.

The UI half is already scaffolded: `feat/reasoning-step-chunking` merged to `main`
as `eccad34`, bringing `ReasoningStep` as a union whose second arm is
`{kind:"tool", name, detail}`, a `StepRow` renderer with a `TOOL` badge and `braces`
icon, an `agentThread.toolBadge` string, and a DEV-only fixture whose own comment
states the contract: *"When a real channel lands, it replaces this function and
nothing else."*

**Outcome:** a one-line revision costs a handful of tool calls instead of a full
regeneration, on every backend that can take it, degrading safely to today's
behavior on every backend that cannot.

---

## Locked decisions

Settled with the user before planning. Do not re-litigate.

| # | Decision |
|---|---|
| D1 | **Bounded agent loop**, hard cap **10 tool calls per turn**. |
| D2 | **Domain tools + read tool + terminals.** `get_cv`/`get_letter`, CV/CL writers, `finalize(change_log)`, `ask_user(question, suggested_replies)`. |
| D3 | **Scope is `revising_cv` and `revising_cl` ONLY.** `cv_adjust`/`cover_letter` stay byte-identical. |
| D4 | **Server-issued synthetic IDs.** `jsa/schema/cv.py` is NOT changed — the `_SECTION_META_KEYS`/`_ENTRY_META_KEYS` denylists and positional lists stay exactly as they are. |
| D5 | **Per-session ladder:** native → prompt-based → full rewrite. Never persisted. Argument-validation failures do NOT downgrade. |
| D6 | **Native tools on every HTTP backend that can take one, `opencode-zen` included.** Exception: `opencode-go`'s `/messages` models. |
| D7 | **CLI backends use prompt-based ops.** `claude_cli.py` keeps `--tools ""`. |
| D8 | **`ask_user` discards mutations.** Only `finalize` commits, via the existing atomic `repo.checkpoint`. |
| D9 | **No kill-switch setting.** |
| D10 | Tool activity streams live **and** persists into the transcript. The pre-existing unrendered `TranscriptTurn.reasoning` gap is left alone. |
| D11 | Docs land as **`docs/TOOLS.md`** + a CLAUDE.md section. |

---

## Findings that changed this plan

A design pass over all five request builders refuted several of my working
assumptions. These are load-bearing, so they are recorded rather than silently
folded in.

1. **Tool-mode calls are never streamed — which deletes the hardest hazard.** If
   tool mode passes `on_chunk=None`, then `use_stream` is `False` *by construction*
   at `_openai_compat.py:637`, `opencode_zen.py`, `gemini_api.py:292` and
   `anthropic_api.py:274`. So `_consume_sse` / `_consume_gemini_sse` need **zero
   changes**, and the fragmented `delta.tool_calls[].function.arguments` reassembly
   I had budgeted as the hard part is unreachable. D10's live requirement is met by
   `stages.py` publishing events from the parsed calls; it never needed SSE. In-file
   precedent: `use_stream = structured_schema is None and on_chunk is not None`.
2. **`anthropic` has no `AgentBackendUnavailable` path at all.** `_call_api`
   (`:287-296`) catches only `asyncio.TimeoutError` and `anthropic.RateLimitError`.
   I had this backend down as "no degrade needed" — wrong. A `BadRequestError`
   propagates raw, past `_send_message_with_wire_retry` (which catches
   `ProtocolError` only, `stages.py:648`), into `_run_one`'s generic
   `except Exception` → hard `mark_failed`, **no BF-19 and no rung 2**. This is a
   named phase item.
3. **`gemini` has a second null-content trap I missed.** `gemini_api.py:395-398`
   raises `TransientBackendError` when `not text`; a `functionCall`-only reply has
   no text part and trips there first.
4. **`opencode-zen` is *less* work than budgeted**, because the ladder lives in
   `stages.py`: it needs a `_ToolsRejected` raise site only. `_call_api` needs no
   change, since `retry_transient` re-raises non-transient exceptions and zen has no
   local degrade loop to teach.
5. **`_extract_structured_text` returns the *first* `tool_use` block only.** Both
   Anthropic and OpenAI-compat can emit several calls per turn, so it must return an
   ordered list — which forces two rules D1 leaves open (see Phase 2).
6. **Persisting tool calls needs no migration at all.** See Phase 4 — this replaces
   the `Message.tool_calls` column the user was shown, with the same visible result.
7. **Do not assert the wire behavior of `tools` + `response_format` together.**
   Nobody has probed it. We don't need to: rung 2 *is* the sentinel path, so no
   configuration ever wants both. The exclusion is framed as design-internal —
   `finalize`'s arguments *are* the structured output.

---

## Phase 0 — Worktree

`main` is at `eccad34` and already contains the merged reasoning card.

```bash
git worktree add .claude/worktrees/revision-tool-use -b feat/revision-tool-use main
```

Branched from **`main`**. Do **not** `pip install -e .` inside the worktree — it
hijacks the global `jsa` entrypoint; the symptom is `{"detail":"Not Found"}` at `/`.

---

## Phase 1 — Vocabulary, working copy, applier

Pure Python, no HTTP. This phase must be bulletproof; everything later is transport.

**Current state.** Nothing exists. `CVDocument`/`Section`/`Entry`
(`cv.py:481,551,640`) and `CoverLetter` (`cover_letter.py:42`) are positional and
ID-free. Hard validity gates: `contact.name` non-empty, ≥1 renderable section,
`_not_a_cover_letter` (`cv.py:657`), and `MIN_COVER_LETTER_CHARS = 150`.

**Desired state.** `jsa/agents/tool_spec.py` (vocabulary + provider renderers,
imports only `base.py`) and `jsa/schema/patch.py` (working copy + applier).

**Problems.**
- Addressing must survive mutation, and a stale ID must fail loudly rather than
  silently editing the wrong entry.
- A patch can violate a document-level invariant. If that escapes as a
  `FinalContentError` into `_self_heal_final` (`stages.py:452`), we'd need a third
  correction-text variant telling the model to re-emit a full document — exactly
  wrong in tool mode.
- The applier only works if re-validating an already-canonical document is a **fixed
  point**; `Section._classify` (`cv.py:566`) is a denylist that absorbs unrecognised
  keys by type.

**Solutions.**

*Schemas are hand-written, flat, and `$ref`-free* — not derived from
`Entry.model_json_schema()`, which pulls `$defs`/`$ref`/`additionalProperties` that
Gemini rejects. That makes `inline_defs()` (`turn_models.py:165`) a safety net rather
than load-bearing. Three pure renderers: `to_anthropic_tools` (`input_schema`),
`to_openai_tools` (`{"type":"function",...}`), `to_gemini_function_declarations`.

```
CV    get_cv, replace_summary(text), replace_section(section_id, section),
      replace_entry(entry_id, entry), edit_entry_bullets(entry_id, bullets),
      add_entry(section_id, entry, position?), remove_entry(entry_id),
      reorder_entries(section_id, order[])
CL    get_letter, replace_paragraph(paragraph_id, text), add_paragraph(text, position?),
      remove_paragraph(paragraph_id), reorder_paragraphs(order[]),
      set_salutation(text|null), set_signoff(text|null)
BOTH  finalize(change_log), ask_user(question, suggested_replies?)
```

`tools_for(stage)` raises for any stage outside `revising_cv`/`revising_cl` — the
mechanical guarantee behind D3.

*IDs: a mint-once side map, not positional-on-demand.* IDs are minted when the
working copy loads and carried in a map alongside the document; mutations update the
map. `add_*` mints past the current max and returns the new ID; `remove_*` retires
one, and it is never reused. A retired or unknown ID is simply absent from the map →
a loud error result.

> **Alternative considered and rejected:** generation-prefixed positional IDs
> (`r0:s2.e1`), where any structural change bumps `rev` and invalidates every
> outstanding ID. It is simpler to implement and equally loud, but it forces a fresh
> `get_cv` after every insert/remove/reorder — and with a 10-call budget (D1) those
> re-reads are the scarcest resource in the design. The side map keeps IDs valid
> across mutations, so a multi-edit revision fits in one read.

*Working copy holds plain dicts*, not the Pydantic model. Mutating a validated model
and re-validating per op would re-run `Section._classify`/`Entry._normalize` on every
operation. Validate exactly once, at `finalize`.

*Defense in depth on the ID projection.* `"id"` is in **both** `_SECTION_META_KEYS`
(`cv.py:74`) and `_ENTRY_META_KEYS` (`cv.py:88`), so `_classify`'s absorption loop
skips it and `_Loose`'s `extra="ignore"` drops it again. Belt and braces regardless:
the applier strips `id` from any model-supplied object before writing it in, so the
working copy never carries phantom keys. Pin this with a test.

*`replace_summary` reuses `SUMMARY_NAME_RE` (`cv.py:412`)* — do not reinvent. Absent
Summary section → insert one, keeping `cv_has_summary`'s soft-nudge invariant
(`cv.py:629`) reachable.

*Error result shape.* Every failure is a structured result, never an exception:
```json
{"ok": false, "error": {"code": "stale_id", "message": "...", "hint": "call get_cv again"}}
```
Codes: `unknown_id`, `stale_id`, `bad_argument`, `validation_failed`,
`budget_exhausted`, `not_executed`.

*Validation happens inside `finalize`, not after the loop* — and it must share code
with `_handle_final`, or the two gates can disagree. **Extract `FinalContentError`
(`stages.py:137`), `_parse_structured` (`:182`) and `_validate_final_content`
(`:246`) into a new `jsa/pipeline/validation.py`**, re-exported from `stages.py` for
existing tests. This is required, not cosmetic: without it, `tool_loop.py` importing
from `stages.py` while `stages.py` imports `tool_loop` is an import cycle. The error
message is built the way `_parse_structured` does at `stages.py:230-233` — **never
`str(exc)`**, which embeds the entire document.

**Tests.** Every tool: happy path, unknown ID, retired-after-removal ID, malformed
arguments. The ID-stability property (remove an entry, then edit a later one by an ID
read *before* the removal — still hits the intended entry). `finalize` on an
unmodified copy reproducing the input byte-for-byte through `cv_to_markdown`.
`finalize` rejecting a letter pushed under `MIN_COVER_LETTER_CHARS`, and a CV whose
last renderable section was emptied.

**Idempotence prerequisite — already verified (2026-09-03).**
`CVDocument.model_validate(json.loads(s)).model_dump_json() == s` holds across three
consecutive round-trips, and likewise for `CoverLetter`, on a document exercising
`text`, `items`, `entries`, bullets and links. The test formalises this as a
permanent guard rather than discovering it.

---

## Phase 2 — The loop

**Current state.** `AgentReply.kind` is `Literal["final","needs_input"]`
(`base.py:38`). `protocol.py`'s `_BLOCK_RE` (`:12`) knows only `NEED_INPUT|FINAL`.

**Desired state.** One loop, transport-agnostic, in `jsa/pipeline/tool_loop.py`.
The loop belongs in the pipeline layer, not in each backend — the codebase already
argues this (`_openai_compat.py:52-55`, `turn_models.py:284-287`, and `streaming.py`'s
"batching lives in the pipeline, NOT in any backend"), and the applier needs
`CVDocument`. Five copies would be the violation.

**Solutions.**

*Base types* (`jsa/agents/base.py`, after `AgentChunk` at `:58-64`): frozen
`ToolCall{id, name, arguments}` and `ToolResult{call_id, name, ok, content}`.
`AgentReply.kind` gains `"tool_calls"` plus `tool_calls: list[ToolCall] | None = None`
— a frozen dataclass with defaults, so the field appends cleanly.

`supports_native_tools: ClassVar[bool] = False` beside `supports_structured_output`
(`:106`), with a contract docstring in the same voice as `:98-105`: a backend setting
it True MUST accept `tools=` on `start_session`/`restore_session` and implement
`send_tool_results(handle, results) -> AgentReply`. **Not a fifth abstract method** —
same conditional-capability rule as `structured_schema`. Read **off the instance**,
never the class, for the `OpenCodeGoBackend` reason in CLAUDE.md.

*Two rules D1 leaves open*, forced by multi-call turns (finding 5):
- The budget counts **individual tool calls**, not round-trips.
- Intra-turn execution is in **array order, stopping at the first terminal tool**;
  every call after it returns `not_executed`.

*Loop body:*
```
copy = CvWorkingCopy(latest Document.structured);  budget = 10
reply = <first model turn: instruction, tools active>
while reply.kind == "tool_calls":
    for call in reply.tool_calls:            # array order
        terminal? -> not_executed;  budget==0 -> budget_exhausted
        budget -= 1;  result = execute(copy, call)
        publish AgentToolEvent(...)          # AFTER execution, so status is known
    if terminal: break
    reply = await backend.send_tool_results(handle, results)
```

- `finalize` → validate → synthesize `AgentReply(kind="final", raw=canonical_json)`.
  `change_log` is emitted as a `LogEvent`, **not** persisted as document content —
  `_strip_change_log` (`protocol.py:43`) exists precisely because change-logs must
  not reach the payload.
- `ask_user` → **discard the working copy** (D8), `LogEvent` naming how many
  mutations were dropped, synthesize `AgentReply(kind="needs_input", ...)`.
  `_handle_needs_input` (`stages.py:1057`) needs **zero changes** — verified: its
  `raw_text.find("<<<NEED_INPUT>>>")` returns `-1` on canonical JSON, so
  `display_question = reply.question` at `:1075`.
- Budget exhausted without a terminal → downgrade to rung 3.

*The ladder.* `ToolMode = Literal["native","prompt","rewrite"]`, a per-turn local,
never persisted. Entry rung is `native` if `supports_native_tools` on the instance,
else `prompt`. `native → prompt` on `ToolsUnsupported` or a reply with no parseable
tool call; `prompt → rewrite` on no parseable `<<<TOOL_CALLS>>>` block. Argument
validation never downgrades (D5). **A downgrade rebuilds the handle by re-calling
`restore_session` from the same DB history** — a pure local construction, no model
call — so the failed rung's turns are never replayed into the next. Same rule zen's
downgrade docstring states: a reshaped retry never shares a budget with retries of
the identical request. One attempt per rung.

Rung 3 is **not implemented by this module**: `run_tool_loop` returns `None` and the
caller falls through to the existing, unmodified `_send_message_with_wire_retry`
path. That is what "today's behavior" means, and it keeps the fallback
byte-identical.

*Budget composition — three budgets, strictly sequential, never nested*
(`_self_heal_final`'s docstring at `stages.py:470-483` sets this rule):
1. **Tool budget (new, 10).** Individual calls. On exhaustion → rung 3.
2. **`_send_message_with_wire_retry` (`:603`) is BYPASSED for tool turns.** Its
   semantics are "re-send the same text" — wrong here, and using it would nest. The
   loop calls `send_message`/`send_tool_results` directly.
3. **`_self_heal_final` (`:452`) runs exactly once, unmodified,** on the loop's
   synthesized reply. Its correction text is a rung-3 full-rewrite re-prompt, never
   a re-entry into the loop.

`_start_session_with_retry` (`:553`) is not on this path — revisions always restore.

---

## Phase 3 — Native wiring, per backend

Each of E1–E5 is independent and shippable alone, because
`supports_native_tools=False` keeps a not-yet-wired backend on rung 2.

**Tool mode and structured mode are mutually exclusive per request** — assert it.
When tools are active we send no `response_format`/`responseSchema`; the terminal
tool's arguments *are* the structured output. On `anthropic` this is a
generalization of what `respond` already does.

**Degrade precedence: tools → reasoning → caching → fail.** Tools shed **first**,
for two reasons: it is the newest and least-supported field, so it is the most likely
culprit and shedding it first minimizes total HTTP attempts; and unlike the two
enrichments it has a real functional fallback (rung 2 still patches, via prompts), so
the loss is recoverable rather than simply gone. Each `_ToolsRejected` is caught
outside `retry_transient`'s budget and retried once clean, mirroring `_CacheRejected`.

| Backend | Request | Reply | Degrade |
|---|---|---|---|
| `anthropic` (E1) | `_forced_tool_kwargs(:53)` → `_tool_kwargs(*, schema=None, tools=None)`; `tool_choice {"type":"any"}` | new `_extract_tool_calls` returning **all** `tool_use` blocks in order, same `max_tokens` guard | **must be built** — see below |
| `_openai_compat` (E2) → mistral, openrouter, opencode-go/chat | `tools` + `tool_choice: "required"` at `:617-625` | `message.tool_calls` extracted **before** the `content is None` check at `:743-747` | `_ToolsRejected` in `_permanent_4xx` (`:673`), **first** branch |
| `opencode-zen` (E3) | own copy, same shape | own copy | `_ToolsRejected` raise site only; `_call_api` (`:355`) unchanged |
| `gemini` (E4) | `tools: [{"functionDeclarations":…}]` + `toolConfig.functionCallingConfig.mode = "ANY"` | `functionCall` parts extracted **before** the `if not text` raise at `:395-398`; synthesize `call_{i}` ids | `_ToolsRejected` in `_permanent_4xx` (`:321`) |
| `opencode-go` `/messages` (E5) | **not wired** | — | `self.supports_native_tools = self._protocol == "chat"`, instance attr at `:144` |
| CLI (E6) | **zero changes** | — | inherited `False`; `--tools ""` stays at all three sites |

**E1 detail — the gap.** Add `AgentBackendUnavailable` to `anthropic_api.py`'s import
(`:12-22`) and define `class _ToolsRejected(AgentBackendUnavailable)`. Subclassing is
the safety net, exactly as `_SchemaRejected` does at `gemini_api.py:97-103` — an
instance that escapes still classifies correctly for BF-19. Then at `:287-296` add
`except anthropic.BadRequestError` → `_ToolsRejected` when tool kwargs were present,
else `AgentBackendUnavailable`. Add `_call_api_tools(...) -> list[ToolCall]` beside
`_call_api` rather than widening its `-> str`, which the rest of the module depends
on. *(Widening this to a general `anthropic.APIStatusError → AgentBackendUnavailable`
would close a real adjacent hole, but that is scope creep — flag it, don't do it.)*

**E2 detail.** `_call_api`'s `for _ in range(2)` degrade loop at `:538-559` needs
**no change** — it catches only `_ReasoningRejected`/`_CacheRejected`, so
`_ToolsRejected` propagates. State this in a comment so nobody adds a third branch
and re-introduces nesting. Widening `_call_api_once` to `str | list[dict]` means
`retry_transient` (`:235`) becomes a `TypeVar` — a pure typing change, zero runtime
effect, but it touches `opencode_go._call_messages_api`'s two call sites (`:281`,
`:294`). Add a comment at `:589` recording that tool mode never streams, so
`_consume_sse` is never "fixed" to parse `delta.tool_calls`.

**E3 detail.** Amend `_call_api_once`'s docstring at `:461-475`, don't leave it: it
currently records that a structured-output 4xx is deliberately *not* special-cased,
and tools-present 4xx now **is**. The divergence needs its rationale on record —
`response_format` failing means structured mode is dead for that model and BF-19 is
right; `tools` failing has a strictly better local answer (rung 2, same backend) that
did not exist when that paragraph was written.

**OpenRouter interaction, expected not a bug:** `provider.require_parameters: True`
(mandatory per CLAUDE.md) will now also filter on tool support, which can empty the
provider pool → 4xx → `_ToolsRejected` → rung 2. Assert it in `test_openrouter.py`'s
existing payload-shape test.

---

## Phase 4 — Prompt transport, events, persistence

### Prompt transport (rung 2)

`protocol.py`: `_BLOCK_RE` (`:12`) and `_OPEN_MARKER_RE` (`:18`) gain a `TOOL_CALLS`
verb; `parse_reply` (`:60`) gains a branch that `json.loads`es the body as an array
of `{"name","arguments"}` and synthesizes `call_{i}` ids. Malformed → `ProtocolError`.

**Backward-compat risk that would otherwise ship silently:** a *non-tool* session
(`cv_adjust`, or rung 3) that spontaneously emits `<<<TOOL_CALLS>>>` would parse to
`kind="tool_calls"` and fall through `run_stage`'s `if needs_input … else
_handle_final` dispatch (`:990`, `:1017`) **as if it were final**. Mitigation:
`run_stage` raises `ProtocolError("unexpected tool-call block outside a tool
session")`, which routes into the existing self-heal budget. The
`"no sentinel block"` string that `_parse_with_nudge` keys on is unchanged, so nudges
are unaffected.

`prompt_assembly.py`: `_tool_contract(specs, *, native)` beside `_structured_contract`
(`:107`), reached by a new `tool_model=` kwarg on `assemble_system_prompt` (`:156`).

**Both rungs get a system-prompt contract — this is not "native = wire only, prompt =
prompt only".** What differs is where the schemas live and how a call is signalled:

| | Native rung | Prompt rung |
|---|---|---|
| Schemas | on the wire, in `tools` | inlined as JSON into the system prompt |
| System prompt carries | **short** contract: semantics, ID discipline, 10-call budget, ask-before-editing | **full** contract: all of the above **+** inlined schemas **+** the `<<<TOOL_CALLS>>>` grammar |
| Model signals a call via | provider-native `tool_use` / `tool_calls` / `functionCall` | a `<<<TOOL_CALLS>>>` block containing a JSON array |
| Results return as | `tool_result` blocks / `role:"tool"` messages | a plain user message |
| Forcing | `tool_choice: "any"` / `"required"` / `mode: "ANY"` | prompt instruction only |

The native rung is deliberately **not** schema-only. A provider enforces the *shape*
of a call but says nothing about *when* to call `get_cv` versus `finalize`, or that
questions must precede edits (D8). This repo already paid for that lesson once — see
`prompt_assembly.py`'s module docstring on the `opencode-go`/`longcat-2.0` job that
emitted schema-valid JSON indefinitely, re-asking its opening question forever,
because the contract explaining `kind` semantics had been dropped from resumed turns
while the provider kept enforcing the schema.

Both contracts are generated from `docs/TOOLS.md` (Phase 7), so they cannot drift
from each other or from the applier.

Prompt *files* are never touched. `test_prompt_assembly.py`'s golden parity for
`structured_model=None, tool_model=None` must stay byte-identical against
`fixtures/language_directive_golden.json`.

### Live events

New `AgentToolEvent` after `AgentTurnEndEvent` (`events/schema.py:117-126`) carrying
`job_id, stage, seq, call_id, name, summary, status, detail`, published **after**
execution so `status` is known. A tool call is a discrete record, not a text delta —
routing it through `ChunkAccumulator`'s 75ms/200-char batching would mean changing
three dict literals for a payload that must not be concatenated. `bus.py` and
`api/ws.py` are dict-agnostic and need **nothing**.

### Persistence — `role="tool"` rows, no migration

This **replaces the `Message.tool_calls` column** the user was shown when choosing
"add tool storage only". Same visible outcome — tool rows survive the turn and render
on a reopened job — with strictly less machinery. Flagging the substitution rather
than burying it.

Three preconditions, all verified by the design pass:
1. `Message.role` is `mapped_column(String(16))` with no enum and no CHECK
   constraint → **no migration at all**, which matters given `engine.py`'s
   `create_all` + additive-`ALTER TABLE` story.
2. `_load_history`'s `role.in_(["user","assistant"])` filter (`stages.py:1511`)
   excludes them from every replay with **zero code changes** — which is what makes
   this safe. (Extra `role="assistant"` rows, the naive alternative, would replay
   tool syntax into a session possibly running rung 3.)
3. `transcript.py` skips only `role == "system"` (`:230-231`), so a tool row falls
   through to the plumbing branch at `:279-289` and renders; `_unwrap_for_display`
   (`:74`) handles arbitrary JSON.

A repo-wide check confirms the only other reader of a role value is
`gemini_api._to_gemini_role` (`:106-109`), which sees in-memory `handle.messages`,
never DB rows.

Content is compact JSON `{"call_id","name","arguments","result"}`. One
`repo.checkpoint` (`repo.py:437`), unchanged: `accumulated_messages` becomes
`[user instruction, *tool_rows, assistant canonical_final]`. `repo.checkpoint`
captures `message_stage = job.current_stage` **before** `transition` (`repo.py:469`),
so tool rows land under `revising_cv`/`revising_cl` automatically.

**The honest cost.** The persisted assistant row stays a full canonical
union-JSON document turn — the model's tool-call reasoning is not replayed on a
revision resume, only the final document. This is what keeps the canonical-form
invariant, `adapt_history`, and a mid-conversation downgrade to rung 3 all working
with zero changes. It is bounded precisely *because* of D8: `ask_user` discards
mutations, so there is no mutation state to stay consistent with, and the model
re-reads via `get_cv` anyway. Separately: a replayed full document still costs input
tokens. The win is on **output** tokens (3–5× the price); the input side is cacheable
and was never the target.

---

## Phase 5 — `stages.py` integration

- **`:328`** add `_tools_for(backend, stage)` beside `_structured_schema_for`,
  returning tools only for `revising_cv`/`revising_cl` — the single mechanical
  enforcement of D3.
- **`:278`** add `_tools_kwargs(backend, tools)` in the same conditional-kwarg idiom,
  returning `{}` unless `supports_native_tools` is True **on the instance**.
- **`:753-754`** the single destination-mode decision block — compute `tools` here
  too, alongside `schema`/`structured`. One decision, threaded everywhere.
- **`:775-843` — both revision sub-branches, not just `:835`.** Because `ask_user`
  parks the job (D8), `_is_revision_resume` (`:1568`) returns True on resume and takes
  the branch at `:813-830`, which sends `answer_text` with no document injected. That
  path must enter the loop too and re-establish read state via `get_cv`.
- **Instruction composition differs per rung.** On rungs 1–2, `:835`'s
  `instruction = rev_req.instruction` stays **verbatim** — do *not* inject the CV;
  that would defeat the entire token saving, since `get_cv` provides it. On rung 3 the
  CV **is** injected, matching today's fallback.
- **`:342` `_log_session_mode`** — extend to `structured + tools (native)` /
  `(prompt)` / `structured (tools downgraded)`, keeping the existing
  `getattr(handle, ..., True)` defaulting style.

---

## Phase 6 — Frontend

**Current state.** `ReasoningCard.tsx` renders the `{kind:"tool"}` arm via `StepRow`
behind a DEV-only `interleaveMockTools` toggle.

**Problem.** The card derives steps purely from one flat `reasoning` string via
`segmentReasoning`, which returns no offsets — nothing to interleave against.

**Solutions.**
- Store: `streamBuffers[job]` gains `tools: {name, detail, ok, at}[]` with
  `at = reasoning.length` at arrival, and a `case "agent_tool"` arm. **Three literals
  change in lockstep** or the reducer silently no-ops: the type at `store.ts:69`, the
  `base` reset at `:263`, and the `isJobRunning` fallback at `AgentThread.tsx:411`.
- `lib/reasoningSteps.ts`: add `bounds: number[]` (closed-step end offsets) to
  `ReasoningSegments`. **Additive to the return value, not a change to
  `ReasoningStep`'s shape** — deliberately the smaller edit, since this file is
  freshly merged from another session.
- New pure `mergeToolSteps(segments, bounds, marks)`, unit-tested independently.
  `ReasoningCard` takes a `tools` prop; **delete `reasoningMock.ts` and its DEV
  toggle.**
- Settled turns render persisted tool rows through the same `StepRow` (exported from
  `ReasoningCard.tsx`), so live and settled look identical.
- New strings via `useT()` + `strings.en.json`, then `./scripts/translate-ui.sh`.
  `agentThread.toolBadge` already exists and is reused.
- **Run `npm run build`** — the `jsa` CLI serves the gitignored `jsa/static`.

---

## Phase 7 — Documentation

- **`docs/TOOLS.md`** — every tool's name, schema, semantics, error codes, the ID
  discipline, the 10-call budget, and the native/prompt/rewrite matrix. It is the
  source text Phase 4's `_tool_contract` is generated from, so the two cannot drift.
- **CLAUDE.md** — a "Tool use (revision patching)" section: the capability matrix,
  the ladder and its precedence, and the do-not-simplify warnings (tool mode never
  streams; tool mode excludes `response_format`; null content + `tool_calls` is
  success, not transient; `opencode-go` `/messages` is excluded on evidence;
  `_ToolsRejected` must not join `_call_api`'s existing degrade loop).

---

## Parity gate

Additive feature, so CLAUDE.md's replace/delete gate does not strictly bind. It gets
a **mechanical** gate anyway, because silent drift in an unpatched region is exactly
what this design can produce.

Deliberately **not** asserted: that a patched revision and a full-rewrite revision
produce the same *prose*. Two runs of a generative step differ; that diff never
closes and asserting it would be permanently red.

`tests/backend/test_patch_parity.py`, modeled on `test_mode_parity.py` — reuse its
`session_pair` fixture and its explicit non-assertion on raw `Message` text. Run the
same instruction against the same seed Document twice, once through a tool-scripted
fake and once through a full-rewrite fake emitting the equivalent document:
- `Document.markdown` **byte-identical**;
- `Document.structured` compared as **parsed JSON, not strings** (key order differs
  between a patched-and-redumped doc and a model-emitted one);
- resulting `Job.state` identical.

What makes this a real claim rather than a tautology: the patched document reaches
`_handle_final` as an ordinary final reply and goes through the same
`cv_to_markdown(structured_obj)` call at `stages.py:1341`. **Permanent** gate — the
full-rewrite path lives forever as rung 3.

Plus: `cv_adjust`/`cover_letter` never receive a `tools` kwarg and never enter the
loop, asserted at `_tools_for` level (D3).

`tests/backend/fakes/fake_backend.py` gains `supports_native_tools`, a `tools=`
kwarg, and `send_tool_results` — on the *enforcing* side of the capability contract,
as it already is for `structured_schema`.

---

## Sequencing

Phase 1 → **`validation.py` extraction** (unblocks the loop) → Phase 2 → Phase 4's
protocol + prompt_assembly (rungs 2/3 fully testable with zero backend work) →
E1 (anthropic, incl. the gap) → E2 → E3 → E4 → E5 → events/persistence → Phase 6 →
Phase 7.

---

## Verification

```bash
pytest -v -m "not integration"
pytest tests/backend/test_patch_applier.py tests/backend/test_tool_loop.py \
       tests/backend/test_patch_parity.py -v
pytest tests/backend/test_bf18_limit_detection.py -v     # BF-19 slots survive _ToolsRejected
cd frontend && npm test && npm run build

jsa --csv jobs.csv --backends anthropic  --no-browser    # native
jsa --csv jobs.csv --backends openrouter --no-browser    # native + require_parameters
jsa --csv jobs.csv --backends claude-cli --no-browser    # prompt rung
```

Manual: drive a job to `cv_review`, request a narrow revision ("tighten the
summary"), and confirm — TOOL rows appear in the reasoning card in the right order
against the prose; only the summary changed in the rendered PDF; the settled turn
still shows tool rows after reload; the `LogEvent` names the active rung. Playwright
is worth using for the visual check if installed.

**Post-implementation:** `/code-review medium --fix` (large, multi-surface change).
Review model is always Sonnet.

---

## Open risk register

- **`--tools ""` may not do what it claims.** Verified this session that `--tools`
  *is* a real flag on the installed CLI — but its help documents `"none"` and
  `"default"` as values, and `claude_cli.py` passes an empty string. The three
  regression tests only assert argv membership against a mocked subprocess, so
  nothing proves the effect. If it is a no-op, claude-cli may improvise file tools
  while we also ask it for prompt-based ops. One live check during Phase 4; **out of
  scope to fix here** — report it.
- **`opencode-zen` is the riskiest native target** (D6) — flakiest backend, and the
  one whose docstring warns about unrecognised request fields. Its `_ToolsRejected`
  raise site is not optional.
- **`segmentReasoning` is freshly-merged code from another session.** Phase 6 edits
  it; keep the edit to the additive `bounds` return value.
- **`anthropic`'s missing `AgentBackendUnavailable` path is a pre-existing hole**
  this feature only partially closes (tools-related 4xx only). The general widening
  is flagged, not done.

---

## Change Log

_(entries appended as phases land: **YYYY-MM-DD**: context, actions, decisions, verification result)_

**2026-09-03**: Phase 0 + Phase 1.
Actions:
- Phase 0: created worktree `.claude/worktrees/revision-tool-use` on branch
  `feat/revision-tool-use` from `main` (`eccad34`), per the plan's exact command.
- Phase 1: extracted `FinalContentError`, `_strip_code_fence`, `_capture_failed_payload`,
  `_parse_structured`, `_validate_final_content` out of `jsa/pipeline/stages.py` into new
  `jsa/pipeline/validation.py`, re-exported unchanged from `stages.py` (all existing
  `from jsa.pipeline.stages import ...` call sites/tests untouched). Widened
  `_validate_final_content`'s `job` parameter to `Job | None` to match its actual
  already-None-tolerant runtime behavior.
- Added `jsa/agents/tool_spec.py`: hand-written, flat, $ref-free `ToolSpec` vocabulary
  (8 CV tools, 7 CL tools, `finalize`/`ask_user` shared), `tools_for(stage)` raising for
  every stage but `revising_cv`/`revising_cl` (D3's mechanical enforcement), and three
  provider renderers (`to_anthropic_tools`, `to_openai_tools`,
  `to_gemini_function_declarations` — the last strips `additionalProperties`).
- Added `jsa/schema/patch.py`: `CvWorkingCopy`/`ClWorkingCopy`, a mint-once id side-map
  over plain dicts (never the Pydantic model — validated once, at `finalize`), the
  `{"ok": bool, ...}` / `{"ok": False, "error": {code, message, hint?}}` result contract
  (`unknown_id`/`stale_id`/`bad_argument`/`validation_failed`), and `finalize()` reusing
  `jsa.pipeline.validation._validate_final_content` (via `json.dumps` of the
  reconstructed plain-dict document) so a patched revision is held to the exact same
  content gate as a full rewrite.
- Added `tests/backend/test_tool_spec.py` (18 tests) and `tests/backend/
  test_patch_applier.py` (57 tests) — every tool's happy path / unknown-id / retired-
  (stale-)id / malformed-argument cases, the id-stability property (edit-after-unrelated-
  removal), the id-stripping defense-in-depth, and finalize's idempotence-on-an-
  unmodified-copy + rejection-of-an-invalid-result (empty CV, too-short letter).

Decisions:
- Kept `CvWorkingCopy`/`ClWorkingCopy` OUT of `jsa/schema/__init__.py`'s exports
  deliberately, so `jsa.schema.patch` can safely import `jsa.pipeline.validation`
  (which imports `jsa.schema`) without risking a partial-import cycle through the
  package `__init__`. Import `jsa.schema.patch` directly.
- `_id_error` distinguishes `stale_id` (minted, then retired) from `unknown_id`
  (never minted / hallucinated) via a per-copy `_retired: set[str]`, matching the
  plan's error-code list; `budget_exhausted`/`not_executed` are left for Phase 2's
  `tool_loop.py` (loop-level, not applier-level) and are never returned by `patch.py`.
- Section reordering has no tool in the vocabulary (plan's tool list has no
  `reorder_sections`) — section order is fixed as loaded; only entries within a
  section can be added/removed/reordered.

Post-implementation advisor review caught two real gaps, fixed before calling Phase 1
done: (1) the idempotence guard tests only asserted markdown equality (weaker than the
plan's `model_dump_json()` round-trip requirement) and used a fixture with no
`items`/`links`/`dates`/`location` — added `TestIdempotencePrerequisite` (3-round-trip
`model_dump_json()` equality for both `CVDocument` and `CoverLetter`) plus a richer
`_RICH_CV_JSON` fixture, exercised by both the round-trip test and a new
markdown-equality `finalize()` test; (2) `finalize()` hardcoded `structured=True`,
which is wrong for a rung-2/prompt-based session (no `payload` field exists there) —
exposed `structured: bool = False` on both `finalize()` methods, with a docstring
noting Phase 2 owns the real fix-and-retry wording. Also hardened
`jsa/pipeline/validation.py` to import `CVDocument`/`CoverLetter` from their submodules
rather than the `jsa.schema` package, removing the import-cycle *fragility* (not just
documenting around it) that motivated keeping `patch.py` out of `jsa/schema/__init__.py`.

Verification: full backend suite green modulo two pre-existing, order-dependent
flaky tests unrelated to this change (`test_dev_tunnel.py::...test_daemon_thread_is_started`,
`test_integration.py::TestHappyPath::test_both_jobs_approved_with_pdfs` — both pass in
isolation, and both passed in the final full run too). `pytest tests/backend -q -m "not
integration"`: 1851 passed, 2 skipped, 21 deselected. New suites (`test_tool_spec.py`,
93 tests total across both after the advisor fixes) all green. Ran via the project's
existing `.venv` (`/Users/wiam/VSCodeProjects/JSA/.venv/bin/python`, which has
`pytest-asyncio`) since the worktree itself has no venv and the global Homebrew Python
is externally-managed (PEP 668) — did not `pip install` anything into either. Confirmed
`jsa` resolves to the worktree's own package (not the main checkout) when run from the
worktree's cwd. **Verified.** Committed as `cb88671` on `feat/revision-tool-use`
(confirmed via `git log` at the start of the Phase 2 session — the "nothing committed
yet" note above was written before that commit landed).

**2026-09-03 (2)**: Phase 2 — the loop.
Actions:
- `jsa/agents/base.py`: added `ToolCall`/`ToolResult` frozen dataclasses; widened
  `AgentReply.kind` to include `"tool_calls"` plus a `tool_calls` field; added
  `AgentBackend.supports_native_tools: ClassVar[bool] = False` (docstring contract
  mirrors `supports_structured_output`, including the `OpenCodeGoBackend`
  instance-vs-class-read warning) and a concrete (non-abstract) `send_tool_results`
  that raises `NotImplementedError` by default; added `ToolsUnsupported` — deliberately
  NOT a subclass of `AgentBackendUnavailable` (opposite rationale to Phase 3's planned
  `_ToolsRejected`, which subclasses it on purpose as a BF-19 escape net), documented
  as caught only inside `tool_loop.py`'s native rung.
- `jsa/agents/protocol.py`: added the `TOOL_CALLS` verb to `_BLOCK_RE`/`_OPEN_MARKER_RE`
  and a `parse_reply` branch (`_parse_tool_calls_block`) that parses the body as a JSON
  array of `{"name","arguments"}`, synthesizing `call_0`, `call_1`, ... ids. This is
  live for every sentinel-parsing backend immediately (not gated behind Phase 4), which
  is why the `run_stage` guard below shipped in this phase too, not deferred to Phase 4
  as the plan originally sketched.
- `jsa/pipeline/tool_loop.py` (new): `run_tool_loop` — owns session establishment for
  both the native and prompt rungs (always via `restore_session`, never `start_session`)
  via a caller-supplied `build_system_prompt(mode) -> str` callback, so this module stays
  decoupled from Phase 4's not-yet-built `_tool_contract`/`docs/TOOLS.md`. Implements the
  10-call budget (D1), the "stop at the first terminal-tool NAME this round, everything
  after gets `not_executed`" array-order rule, a **failed** finalize NOT ending the turn
  (only a *successful* finalize/ask_user is genuinely terminal — the model can keep
  working within the same budget), D8's ask_user-discards-mutations (free by construction:
  the working copy is simply never finalized), and the one-shot native→prompt downgrade
  (rebuilds the handle from the same DB history, never replays the failed rung's turns).
  Rung 3 ("rewrite") is NOT implemented here — `run_tool_loop` returns `None` and the
  caller (Phase 5) falls through to the existing full-rewrite path unchanged.
- `jsa/pipeline/validation.py` / `jsa/schema/patch.py`: added `reemit_hint: str | None`
  to `_parse_structured`/`_validate_final_content`/both `finalize()` methods — overrides
  the sentinel-vs-structured built-in wording entirely, since neither fits a tool-mode
  failure (the model sees the error as an ordinary `finalize` tool result, not a
  whole-document re-emit instruction). `tool_loop.py` always passes its own
  `_TOOL_REEMIT_HINT`.
- `jsa/events/schema.py`: added `AgentToolEvent` (job_id, stage, seq, call_id, name,
  summary, status, detail) — pulled forward from Phase 4's "events/persistence" bucket
  per advisor guidance, since the loop's own pseudocode publishes it after every call.
- `jsa/pipeline/stages.py`: added the `run_stage` dispatch-site guard
  (`elif reply.kind == "tool_calls": raise ProtocolError(...)`) — **pulled forward from
  Phase 4 into this phase**, not optional: once the TOOL_CALLS verb is live in
  `protocol.py` (previous bullet), a hallucinated block in a non-tool session (every
  session today — `run_stage` never calls `run_tool_loop` until Phase 5) would otherwise
  silently misroute through `_handle_final` as a confusing schema-validation failure
  instead of a diagnosable `ProtocolError`, AND would skip the sentinel-nudge retry
  (`_parse_with_nudge`) a genuine "no sentinel block" reply gets — a real regression the
  advisor caught before commit. Correction to Phase 4's own text: that section claims
  this guard's `ProtocolError` "routes into the existing self-heal budget" — it does not
  (`_self_heal_final` only catches `FinalContentError`); it hard-fails in one shot with a
  diagnosable message instead, which is strictly better than the pre-guard behavior but
  not the budgeted mitigation Phase 4 currently describes.
- `tests/backend/fakes/fake_backend.py`: `FakeAgentBackend` gains
  `supports_native_tools`, a `tools=` kwarg on `restore_session` (recorded in
  `received_tools`), and `send_tool_results` (recorded in `received_tool_results`) — the
  enforcing side of the capability contract, needed now (not deferred to Phase 7) since
  `test_tool_loop.py` exercises the ladder.
- New tests: `test_tool_loop.py` (24 tests — ladder entry/downgrade/give-up, budget
  counting and the terminal-at-budget-zero edge, array-order `not_executed`, failed vs.
  successful finalize, `reemit_hint` wording, ask_user + D8's discard, CL-stage parity,
  unknown-tool-name recovery), `TestToolCallsBlock` in `test_protocol_parser.py` (13
  tests), `TestUnexpectedToolCallsBlockOutsideToolSession` in `test_stages.py` (2 tests,
  the guard regression).

Decisions:
- Signature design not spelled out by the plan (all confirmed against the advisor before
  writing code): `run_tool_loop` takes `history`/`external_id`/`build_system_prompt`
  rather than a pre-built `handle`, so it owns every `restore_session` call (entry AND
  the one downgrade) through one code path — Phase 5 must NOT call `restore_session`
  itself before invoking this function, or a revision would open two sessions.
- "Terminal" is evaluated in two layers: array-order stopping (`stop_calls`) fires on the
  tool NAME alone (`finalize`/`ask_user`, regardless of success), matching "stop at the
  first terminal tool" literally; but the outer while-loop only actually ends
  (`final_reply` set) on a *successful* finalize/ask_user — a failed finalize still halts
  that round's remaining calls but the turn continues into another round-trip. This
  reading is not spelled out by the plan's pseudocode and was the one place genuine
  interpretation was required; documented in `_dispatch`'s docstring.
- `ToolMode` is `Literal["native", "prompt"]`, not the plan's `Literal["native","prompt",
  "rewrite"]` — "rewrite" (rung 3) is never a value this module holds, since it isn't
  implemented here (`None` is the "use rung 3" signal instead). Deliberate deviation, not
  an omission.

Carry-forwards recorded for Phase 5 (not yet acted on):
- `stages.py`'s existing `revising_cv`/`revising_cl` branches (~:694-715) still call
  `restore_session` themselves before any future `run_tool_loop` call — Phase 5 must
  replace that call, not add to it.
- `adapt_history`'s `structured: bool` destination-mode flag has no third value for a
  tool-mode destination; `run_tool_loop` currently forwards `history` verbatim
  un-adapted. A canonical-JSON `cv_adjust` row replayed into a native tool session on a
  BF-19 switch is the reachable case Phase 5 needs to resolve.
- `ToolsUnsupported` has no raiser yet — E1-E5 (Phase 3) must convert a tools-present
  permanent-4xx into it or the native→prompt downgrade edge stays dead code in
  production (only `test_tool_loop.py`'s fake exercises it today). Same
  `_ToolsRejected`-propagates-vs-caught-and-retried inconsistency flagged in Phase 3's
  own E2 detail applies here — whichever E1-E5 lands on, it must end in `ToolsUnsupported`
  reaching this loop, not `AgentBackendUnavailable` (which would wrongly trigger BF-19).

Verification: `pytest tests/backend -q -m "not integration"`: 1887 passed, 2 skipped, 21
deselected (up from 1851; a `test_integration.py::TestHappyPath::test_two_jobs_reach_review`
failure in one full run reproduced as pre-existing order-dependent flakiness — passed in
isolation and in a clean re-run of the full suite). `test_tool_loop.py` (24/24),
`TestToolCallsBlock` (13/13), guard regression (2/2) all green in isolation too.
**Verified.** Committed as `e052ff7` on `feat/revision-tool-use`.

**2026-09-03 (3)**: `/code-review medium --fix` on Phase 1+2 (`git diff main...HEAD`).
This was the first `/code-review` invocation of this session — everything through Phase
2 had only had `advisor` (full-transcript consult) review, not the CLAUDE.md-mandated
post-implementation `/code-review` pass; caught mid-session when the user asked directly
whether one had run yet.

Findings (6, 8 finder angles run in parallel): 2 fixed, 4 reported and deliberately left
unfixed (each a legitimate observation but out of scope for a narrow `--fix` pass — see
rationale per item below).

Fixed:
- `jsa/pipeline/tool_loop.py`: `finalize`'s success-`LogEvent` was gated on
  `if finalize_change_log:` — a schema-valid empty-string `change_log` (the tool's
  `_STRING` param has no `minLength`) silently suppressed the "revision finalized" log
  even though finalize succeeded. Changed to `is not None`, with a
  `"(no summary provided)"` fallback for the empty-string case.
- `jsa/schema/patch.py`: `_clean_str_list` was a local reimplementation of
  `jsa.schema.cv._str_list` that dropped its bare-string-coercion branch (a plain
  non-empty string input became `[]` instead of a one-item list) — a real absorption gap
  between the tool-patch path and a full `cv_adjust` re-emission. Replaced with a direct
  import of the real `_str_list`.

Reported, not fixed:
- **The sharpest one, and it sharpens something already flagged in Phase 4's own text
  above (the "routes into the existing self-heal budget" correction from the previous
  entry):** widening `_BLOCK_RE` to match `TOOL_CALLS` means `parse_reply`'s "multiple
  blocks → take the last" rule can now pick a `TOOL_CALLS` match over a legitimate
  `FINAL`/`NEED_INPUT` block in a **non-tool** session (`cv_adjust`, `cover_letter`,
  `fit_assessment` — every session today) if the model's reply happens to contain
  trailing text shaped like `<<<TOOL_CALLS>>>...<<<END>>>` — and the `run_stage` guard
  that fires on that misdetection hard-fails in one shot, no self-heal, no nudge. Not
  fixed here because the correct fix is session-aware parsing (`parse_reply` doesn't
  know today whether its caller is inside a tool session) — a cross-cutting change that
  belongs to Phase 4/5's prompt-contract and `stages.py` wiring, not a blind regex patch
  in a `--fix` pass. **Carried forward: Phase 4/5 must resolve this precedence question
  explicitly, not just add the contract text.**
- `jsa/agents/tool_spec.py`'s `_strip_additional_properties` duplicates
  `turn_models.py`'s `inline_defs` (same Gemini-schema-restriction motivation) as a
  second independent recursive-strip function. Not merged — `inline_defs`'s own
  docstring asserts "GeminiBackend is the only caller," an invariant a reuse here would
  silently break without a deliberate decision.
- The native→prompt downgrade recomputes from the class-level `supports_native_tools`
  flag on every `run_tool_loop` call, unlike the `_CacheRejected`/`_ReasoningRejected`
  idiom (CLAUDE.md) of permanently flipping an instance flag after first rejection — a
  backend that reliably fails native pays a wasted round-trip every turn, forever. Not
  fixed: changes intended per-session-vs-per-instance degrade scope, and has no wired
  production caller yet in this diff (Phase 3 owns the raiser).
- `patch.py`'s mutation tools (`replace_entry` etc.) accept structurally-coerced but
  content-hollow entries with no validation until `finalize()` — a failure there can't
  be traced back to which prior call caused it. Not fixed: changes when validation
  happens, a real design question, not a narrow bug.

Verification: after re-pointing to the project's `.venv` (the worktree's system Python
had no `pytest-asyncio` in this session — see Phase 1's entry on why the `.venv` is the
one to use), `pytest tests/backend -q -m "not integration"`: 1887 passed, 2 skipped, 21
deselected — unchanged from Phase 2's own count, confirming the two applied fixes
introduced no regression. One failure in the first full run
(`test_integration.py::TestParkAndResume::test_job_resumes_to_review_after_answer`)
reproduced as the same pre-existing order-dependent flakiness class already documented
in Phase 1/2's entries — passed in isolation and in a clean full re-run.
**Verified.** Committed as `13f1243` on `feat/revision-tool-use` (2 files:
`jsa/pipeline/tool_loop.py`, `jsa/schema/patch.py`).

**2026-09-04** — *Phase 5, `stages.py` integration.* Context: the delegated Phase 5 agent
stalled without returning; its edits were on disk but uncommitted. Actions: committed the
recovered work first (`2f2ce20`) so it could not be lost, then finished the integration.
Decisions: **the plan is wrong at `:574`.** It names `jsa --backends claude-cli` as the
prompt-rung verification target, but `ClaudeCliBackend.restore_session` (and
`GoogleCliBackend`'s) discards the `system_prompt` and `history` it is handed whenever
`external_id` is set — always, for a revision — and `send_message` passes no
`--system-prompt`. The prompt rung's only transport IS that system prompt, so the contract
never reaches the model there: the loop burned a turn per revision, discarded a good
`<<<FINAL>>>`, and rung 3 then re-sent the same instruction into a conversation already
holding it. Added `AgentBackend.restore_applies_system_prompt` (`ClassVar`, `True` by
default, `False` on both CLI backends) and gated `_tools_for` on
`native or restore_applies_system_prompt`. Chose this over gating on
`supports_native_tools` alone, which would also have killed the prompt rung for
`opencode-go`'s `/messages` protocol where it genuinely works. Five test regressions came
from the rung-2 probe consuming one scripted reply per revision; fixed by scripting the
probe through the stalled agent's own `fakes/finals.py::tool_loop_miss()` helper and
widening call-count assertions to slices — no assertion was softened. **Verified**: backend
2043 → 2054, baseline restored. Commits `2f2ce20`, `b078316`.

**2026-09-04** — *Phase 5 parity gate.* Context: the plan's `## Parity gate` requires proving
the patch path and the rewrite path produce identical observable output. Actions: added
`tests/backend/test_patch_parity.py` (11 tests) asserting byte-identical `Document.markdown`,
equal `json.loads(structured)`, and matching `Job.state`/`current_stage` across both paths,
plus stage-scoping and untouched-region classes. Decisions: the `RevisionRequest` column is
`target`, not `target_stage` — corrected. Confirmed the gate is discriminating by deliberate
mutation (dropping `dates` from `_to_document_dict` → 2 failures; reverted). **Verified**:
2054 → 2065. Commit `4051bf5`.

**2026-09-04** — *Option A: wire the backend transcript.* Context: `TranscriptTurn.tools` was
dead — the plan left `:416`/`:500` contradictory about whether the backend populates it.
User chose Option A. Actions: `transcript.py::_make_turn` gained a `tools` field; new
`_tool_mark` projects a persisted `role="tool"` row into `{name, detail, ok, at}`,
reproducing `tool_loop.py::_summarize`'s wording without importing the pipeline layer; the
plumbing loop buffers tool marks and folds them into the FOLLOWING assistant turn.
`AgentThread.tsx` hoists a `ReasoningCard` for settled turns carrying tool marks. Decisions:
discovered `turn.reasoning` was *already* dead code for the same reason — `transcript.py`
attaches it only to plumbing turns, which render as a `showInternals`-gated `PlumbingLine`
with no card. Rejected delivery-turn attachment (needs Document↔Message correlation plus a
`NoticeLine` rewrite) and question-turn attachment (only exists on the `ask_user` park);
built the hoist instead and documented the asymmetry rather than widening the general gap.
Also reverted `frontend/package-lock.json` `libc`-field drift from an `npm install` no phase
asked for (`b40b293`). **Verified**: 2065 → 2060 (parity-gate renumber), frontend 357, build
clean, and a live end-to-end smoke — role sequence
`[user, tool, tool, tool, assistant]`, tool rows `[get_cv, replace_summary, finalize]`,
transcript turn `kind=plumbing role=assistant tools=[(get_cv,0),(replace_summary,1),(finalize,2)]`,
new summary in, old summary gone, Experience section preserved, state `cv_review`,
RevisionRequest consumed. Commits `9077593`, `b40b293`.

**2026-09-04** — *Phase 7, documentation and the anti-drift gate.* Actions: added
`docs/TOOLS.md` (rung ladder, rules-and-enforcement, result contract with all six error
codes, ID discipline, both vocabularies, observability/persistence, mutual exclusions), a
"Tool use (revision patching)" section in `CLAUDE.md`, and
`tests/backend/test_tools_doc_sync.py` (13 tests) pinning the doc against the code.
Decisions: the doc-sync tool-name regex initially over-matched the error-code table; scoped
extraction to the two `## … vocabulary` sections. A budget assertion used `or`, letting a
stale table cell hide behind a correct mention elsewhere — found by mutation testing and
tightened to a conjunction plus a "no other number claimed as budget" check. Corrected two
now-false claims in `CLAUDE.md`'s REASONING-card section. **Verified**: 2073 passed, 2
skipped; gate confirmed discriminating by mutation (deleting a tool row, deleting an
error-code row, staling the budget → 4 failures; reverted). Commit `83c581b`.

**2026-09-04** — *Batch B2 code review (`high --fix`, Sonnet) and its drain.* Context: 45
files / 6381 insertions since `13f1243`; effort stepped up from the plan's `medium` because
what landed exceeded what the plan implied. Actions: re-ran every check personally after the
review edited code that had already been verified. Accepted three fixes after checking each
claim against the source rather than the report — the stale `prompt_assembly.py` comments,
the `finalize_fired` split (confirmed `protocol.py::_parse_tool_calls_block` only validates
`isinstance(arguments, dict)` and never enforces `required: ["change_log"]`, so a prompt-rung
`finalize` with `{}` completes a revision and used to log nothing), and the
`AgentToolEvent.detail` cap (confirmed `store.ts` uses `summary || detail` with a
never-empty `summary`, so the field is unrendered). Decisions: **narrowed the fourth fix.**
The review correctly found that `SUMMARY_NAME_RE` is English-only, so a German CV got a
second English-titled "Summary" section prepended that shipped into the rendered PDF — but
its fallback ("the first section with text and no entries or items") is not unique to
summaries. Probing `[Kontakt(text), Zusammenfassung(text), Berufserfahrung(entries)]` showed
`replace_summary` destroying the `Kontakt` body while leaving the real `Zusammenfassung`
stale: silent data loss, strictly worse than the duplicate it replaced. Halted and put three
options to the user; user chose to make the shape pass refuse to guess. It now fires only
when the CV has exactly one text-only section AND it is first, so every ambiguous
arrangement falls through to the additive, visible create branch. The precise long-term fix
is a per-language name regex mirroring `_LETTER_FORMULA_RE_BY_LANG`, which needs the
pipeline language plumbed into `CvWorkingCopy` — deferred, and carried as an open risk in
this entry rather than woven into the plan body. Also added the 9 regression tests the review pass itself did not, all
mutation-tested (reverting each fix fails exactly the test covering it). **Verified**:
backend 2073 → 2082 passed, 2 skipped, 21 deselected; targeted suites 329 passed; frontend
357 passed / 25 files; `npm run build` clean. Commit `1a38586`.

---

## Decisions Log

_(For the user's own hand only — records approaches considered and rejected.)_
