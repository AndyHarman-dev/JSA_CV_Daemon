# Revision patching tools

The `revising_cv` and `revising_cl` stages patch an existing document through a bounded
tool-calling loop instead of asking the model to re-emit the whole thing. This file is
the human-readable reference for that vocabulary.

**Source of truth is code, not this file.** The tool schemas live in
`jsa/agents/tool_spec.py`; the runtime contract the model actually reads is assembled
from those `ToolSpec` objects by `jsa/pipeline/prompt_assembly.py::_tool_contract`,
never from this markdown — a missing or malformed doc must not be able to break the
pipeline. `tests/backend/test_tools_doc_sync.py` is what keeps the two from drifting: it
fails if a tool, an error code, or the call budget documented here stops matching the
code. When you add a tool, add it in `tool_spec.py` **and** here, or that test goes red.

---

## The rung ladder

Three rungs, tried in order. `jsa/pipeline/tool_loop.py` owns rungs 1–2;
`jsa/pipeline/stages.py` owns rung 3.

| Rung | Name | How a call is signalled | Who gets it |
|---|---|---|---|
| 1 | **native** | The provider's own tool-calling channel (`tools` on the wire) | `supports_native_tools = True`: `anthropic`, `gemini`, `mistral`, `openrouter`, `opencode-zen`, `opencode-go`'s `/chat` protocol |
| 2 | **prompt** | A `<<<TOOL_CALLS>>>` sentinel block carrying a JSON array | Any backend whose `restore_session` actually applies the system prompt it is handed |
| 3 | **rewrite** | No tools at all — today's full-document re-emit, unchanged | Everything else, and every give-up from rungs 1–2 |

**Both rungs 1 and 2 get a prompt contract**, not just rung 2. A provider enforces the
*shape* of a call but says nothing about *when* to call `get_cv` versus `finalize`, or
that a question must precede any edit. Rung 1's contract is the short one (semantics, id
discipline, budget, ask-before-editing); rung 2's adds every tool's inlined JSON schema
plus the `<<<TOOL_CALLS>>>` grammar, because it has no wire channel to carry them.

**Downgrades.** Native → prompt happens once per turn, when the native rung produces no
parseable tool call (or the backend raises `ToolsUnsupported`/`ProtocolError`). Prompt →
rewrite happens when that rung also fails: `run_tool_loop` returns `None` and the caller
runs rung 3. Argument-validation failures never downgrade a rung — a bad `entry_id` comes
back as an ordinary tool result and the model gets to try again inside the same budget.

**Two backends skip the ladder entirely** and go straight to rung 3: `claude-cli` and
`google-cli`. Their `restore_session` resumes a provider-held conversation by id and
discards the `system_prompt` and `history` it is handed (`send_message` passes no
`--system-prompt`), so rung 2's only transport does not exist for them, and they have no
native channel either. `AgentBackend.restore_applies_system_prompt` is the flag;
`stages.py::_tools_for` is the gate. Without it, every revision on those backends would
burn a wasted model turn AND leave the instruction in the conversation twice.

**Cost of the ladder, stated plainly:** on a backend that *can* reach a rung but whose
model ignores the tool contract, a revision costs one extra model round trip before
rung 3 runs. That is inherent — there is no way to know the model will not use the tools
without asking it.

---

## Rules the contract states, and what enforces them

| Rule | Enforced by |
|---|---|
| Read before you write — `get_cv`/`get_letter` first; ids come from the server, never invented | `unknown_id` / `stale_id` results from `jsa/schema/patch.py` |
| Ask FIRST, before any edit — `ask_user` discards every edit made this turn | `tool_loop.py` returns `ask_user`'s reply and drops the working copy |
| Patch narrowly — touch only what the instruction asks for | `tests/backend/test_patch_parity.py`'s unpatched-region gate |
| Hard budget of **10** tool calls per turn (calls, not round trips) | `tool_loop.py::TOOL_BUDGET`; overflow returns `budget_exhausted` |
| Exactly one terminal tool ends the turn (`finalize` or `ask_user`) | Any call after a terminal tool in the same batch returns `not_executed` |
| `change_log` is a summary FOR THE USER, never document content | Never inserted into the document; logged as a `LogEvent` |

A failed call does **not** end the turn: the model can fix the problem and call
`finalize` again within the remaining budget. A `finalize` that fails validation is an
ordinary error result, not a terminal event.

---

## Result contract

Every tool returns one of:

```json
{"ok": true, ...}
{"ok": false, "error": {"code": "...", "message": "...", "hint": "..."}}
```

### Error codes

| Code | Meaning |
|---|---|
| `unknown_id` | The id was never minted — hallucinated or from another document. |
| `stale_id` | The id was minted, then retired (its entry/paragraph was removed or replaced wholesale). Call `get_cv`/`get_letter` again to refresh. |
| `bad_argument` | An argument was missing, the wrong type, or empty where a value is required. |
| `validation_failed` | `finalize` ran but the reconstructed document failed the same content gate a full rewrite is held to (`jsa/pipeline/validation.py::_validate_final_content`). |
| `budget_exhausted` | The 10-call budget for this turn is spent. |
| `not_executed` | A terminal tool already fired earlier in this same batch. |

`unknown_id` and `stale_id` are the only two that mean "your ids are out of date" — the
right response to both is another `get_cv`/`get_letter`, not a guess.

---

## ID discipline

Ids are minted by the server, once, and live in a side-map over plain dicts — never on
the Pydantic model, which is validated exactly once, at `finalize`. Section ids are
`s1, s2, …`, entry ids `e1, e2, …`, paragraph ids `p1, p2, …`, in document order.

An id is **stable** across unrelated edits: removing entry `e2` does not renumber `e3`.
An id is **retired** — never reused — when its entry/paragraph is removed, or when its
parent section is replaced wholesale by `replace_section`. A retired id returns
`stale_id`, which is deliberately distinguished from `unknown_id` so the model can tell
"this used to exist" from "this never existed".

There is no `reorder_sections` tool: section order is fixed as loaded. Only entries
within a section can be added, removed, or reordered.

---

## CV vocabulary (`revising_cv`)

| Tool | Required | Optional | What it does |
|---|---|---|---|
| `get_cv` | — | — | Return the working CV as JSON with a stable id on every section and entry. Call first, and again after any structural change. |
| `replace_summary` | `text` | — | Replace the Summary/Profile section's text. Creates a Summary section if none exists. |
| `replace_section` | `section_id`, `section` | — | Replace a whole section (name, text, items, entries) by id. Its entries are replaced wholesale — their old ids retire. |
| `replace_entry` | `entry_id`, `entry` | — | Replace one entry's fields entirely. It keeps its section and position. |
| `edit_entry_bullets` | `entry_id`, `bullets` | — | Replace only one entry's bullet list, leaving every other field alone. |
| `add_entry` | `section_id`, `entry` | `position` | Insert a new entry at an optional 0-based position (omit/null to append). Returns the new id. |
| `remove_entry` | `entry_id` | — | Remove one entry. Its id retires. |
| `reorder_entries` | `section_id`, `order` | — | Reorder entries within one section. `order` must list every current entry id of that section exactly once. |
| `finalize` | `change_log` | — | End the revision and commit. Terminal. |
| `ask_user` | `question` | `suggested_replies` | Ask instead of finalizing. Discards this turn's edits. Terminal. |

## Cover-letter vocabulary (`revising_cl`)

| Tool | Required | Optional | What it does |
|---|---|---|---|
| `get_letter` | — | — | Return the working letter as JSON with a stable id on every paragraph. Call first, and again after any structural change. |
| `replace_paragraph` | `paragraph_id`, `text` | — | Replace one paragraph's text entirely. |
| `add_paragraph` | `text` | `position` | Insert a paragraph at an optional 0-based position (omit/null to append). Returns the new id. |
| `remove_paragraph` | `paragraph_id` | — | Remove one paragraph. Its id retires. |
| `reorder_paragraphs` | `order` | — | Reorder the letter. `order` must list every current paragraph id exactly once. |
| `set_salutation` | `text` | — | Set or clear the salutation. Pass null to remove. |
| `set_signoff` | `text` | — | Set or clear the sign-off. Pass null to remove. |
| `finalize` | `change_log` | — | End the revision and commit. Terminal. |
| `ask_user` | `question` | `suggested_replies` | Ask instead of finalizing. Discards this turn's edits. Terminal. |

`finalize` and `ask_user` are shared verbatim between the two vocabularies — they are the
only two terminal tools, and the only two that appear in both.

---

## Observability and persistence

Every executed call publishes an `AgentToolEvent` (`jsa/events/schema.py`) on the bus:
`{job_id, stage, seq, call_id, name, summary, status, detail}` with `status` one of
`ok` / `error` / `not_executed` / `budget_exhausted`. `jsa/api/ws.py` fans bus events out
generically, so the frontend's REASONING card receives them live with no per-event
backend wiring.

The same calls are persisted as `role="tool"` `Message` rows, written in one atomic
`repo.checkpoint` alongside the turn's user and assistant rows, in the order
`[user instruction, *tool rows, assistant reply]`. Two consumers read them differently
and both matter:

- `stages.py::_load_history` filters `role.in_(["user", "assistant"])`, so tool rows are
  **excluded from every replay** — they are a log, not conversation.
- `jsa/api/transcript.py` folds them into the **following** assistant turn's `tools`
  field rather than emitting them as turns of their own, so a settled REASONING card
  shows the same rows the live one did.

There is no migration: `Message.role` is a plain `String(16)` with no enum or CHECK
constraint.

`_log_session_mode` names the rung that actually ran, per stage invocation:
`structured + tools (native)`, `sentinel + tools (prompt)`, or
`… (tools downgraded)` when tools were offered but the loop fell through to rung 3.

---

## Mutual exclusions

- **Tool mode never streams.** `tool_loop.py` passes no `on_chunk`/`on_retry`, so every
  backend call degrades to non-streaming by construction.
- **Tool mode and structured mode are mutually exclusive per request.** The terminal
  tool's arguments *are* the structured output, so a tool session never receives a
  `structured_schema`, and its history is adapted with `structured=False` — computed
  separately from rung 3's own destination-mode decision, never sharing one variable.
- **Tool mode skips `_self_heal_final`.** `finalize()` already ran the same
  `_validate_final_content` gate before the loop returned, and the self-heal's soft
  "missing a Summary section" nudge would send a bare correction into a session with no
  sentinel/structured contract — a native session may answer it with another tool call,
  which `run_stage`'s guard would then hard-fail on. The loop's own `reemit_hint` retry
  is this reply's self-heal equivalent.
- **A tools-only rejection must never cost a BF-19 slot.** Every backend's
  `_ToolsRejected` subclasses `ToolsUnsupported`, **not** `AgentBackendUnavailable` — the
  latter would advance the whole job to the next backend over a degrade that should only
  cost this turn its tools. `_ToolsRejected` also stays out of `_call_api`'s existing
  `_CacheRejected`/`_ReasoningRejected` degrade loop: the tool ladder is the pipeline's
  decision, not the backend's.
