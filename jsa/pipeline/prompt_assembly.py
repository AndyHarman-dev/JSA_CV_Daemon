"""Single composition root for runtime system-prompt mutation.

``assemble_system_prompt`` is the ONLY place that appends runtime instructions to a
stage's file-authored system prompt (``jsa/prompts/*.md``, read via
``jsa/prompts/loader.py`` and never edited programmatically — see CLAUDE.md → "Prompt
files"). It owns two independent concerns:

1. **Language directive** — ported verbatim from the pre-existing
   ``jsa/pipeline/stages.py::_language_directive`` / ``_with_language_directive`` (see
   ``tests/backend/fixtures/language_directive_golden.json``, captured from that
   original implementation before this module existed, and
   ``tests/backend/test_prompt_assembly.py``'s parity-gate assertion against it). The
   sentinel-mode branch (``structured_model=None``) MUST stay byte-identical to that
   original output forever — every CLI backend (``claude-cli``, ``google-cli``) depends
   on it verbatim.
2. **Structured-output contract** (only when ``structured_model`` is given) — the
   runtime-assembled instructions a structured-output API backend's session needs:
   ``kind``/``verdict`` semantics, the per-stage JSON schema, explicit precedence over
   the prompt file's sentinel-format section, and a "never embed sentinel markers in a
   payload string value" guard. This section is assembled here, at runtime, and is
   never written into the prompt files themselves.

For sentinel-mode sessions (``structured_model=None``), only ever call this for NEW
sessions (``start_session``) — never for a ``restore_session`` path; a resumed
sentinel-mode session already committed to a language, and re-injecting a changed
directive would contradict the replayed history.

For structured-mode sessions (``structured_model`` given), pass ``for_resume=True`` on
every ``restore_session`` call too. Every structured-capable backend
(``anthropic``, ``opencode-zen``, ``mistral``, ``openrouter``, the ``/chat`` half of
``opencode-go``, ``gemini``) is stateless at the wire level — there is no server-side
session, and each of these backends' ``restore_session``/``send_message`` resends the
system prompt on *every* HTTP call (see e.g. ``jsa/agents/_openai_compat.py``'s
``_call_api_once``: ``"messages": [{"role": "system", "content": system_prompt}, ...]``).
Passing the bare ``prompt_text`` on resume — as every call site did before this was
discovered — silently drops the structured-output contract (the prose explaining
``kind``/``question``/``payload`` and its precedence over the prompt file's sentinel
section) from every turn after the first. The model still produces schema-valid JSON
(the provider enforces the *shape*), but with no explanation of when to use
``kind: "final"`` it never learns to, so it can loop forever re-asking its opening
question — confirmed live (a stuck ``cv_adjust`` job on ``opencode-go``/``longcat-2.0``
re-asked "shall I proceed with this strategy" indefinitely after repeated approval).
``for_resume=True`` re-appends the (unchanged, idempotent) structured contract but
skips the language directive — resending identical contract text does not "contradict
history" the way a changed language directive would, so the language-directive
exclusion above is untouched by this.
"""

from __future__ import annotations

import json
from typing import Any

from jsa.i18n.languages import language_name


def _sentinel_language_directive(language_code: str, *, fit_verdict: bool) -> str:
    """Byte-identical port of the original ``stages.py::_language_directive``."""
    name = language_name(language_code)
    lines = [
        "\n\n## Output language",
        f"Write all natural-language, user-facing output in {name} ({language_code}) — "
        "the change-log, any clarifying questions you ask, and every text VALUE in the "
        "final JSON (summary prose, bullet text, headings, cover-letter paragraphs). Do "
        "not translate JSON keys/field names — they are fixed schema fields and must "
        "stay exactly as specified (`heading`, `subheading`, `bullets`, `text`, `items`, "
        "`entries`, etc.).",
        "The sentinel blocks `<<<FINAL>>>`, `<<<NEED_INPUT>>>`, and `<<<END>>>` must stay "
        "exactly as spelled, in English/ASCII — never translate or localize them.",
    ]
    if fit_verdict:
        lines.append(
            "The verdict word itself (`FIT` or `UNFIT`) must stay in English and be the "
            f"first word of your reply; only the reason that follows should be in {name}."
        )
    return "\n".join(lines)


def _structured_language_directive(language_code: str, *, fit_verdict: bool) -> str:
    """Structured-mode counterpart of ``_sentinel_language_directive``.

    Same intent (translate user-facing text, not schema field names), but with the
    sentinel carve-out replaced: this session emits no sentinel wrapper at all, so the
    only thing that must stay English/ASCII is the JSON structure itself — keys, and
    (for the fit stage) the ``verdict`` field's literal value.
    """
    name = language_name(language_code)
    lines = [
        "\n\n## Output language",
        f"Write all natural-language, user-facing output in {name} ({language_code}) — "
        "any clarifying `question` text and every string VALUE in the `payload` object "
        "(summary prose, bullet text, headings, cover-letter paragraphs). JSON keys stay "
        "in English regardless of output language — they are fixed schema fields "
        "(`kind`, `question`, `payload`, `heading`, `subheading`, `bullets`, `text`, "
        "`items`, `entries`, etc.). The sentinel-block instructions elsewhere in this "
        "prompt (`<<<FINAL>>>`/`<<<NEED_INPUT>>>`/`<<<END>>>`) don't apply this "
        "session — see the structured-output contract above.",
    ]
    if fit_verdict:
        lines.append(
            "The `verdict` field's value itself (`FIT` or `UNFIT`) must stay in English; "
            f"only `reason` should be written in {name}."
        )
    return "\n".join(lines)


def _structured_contract(schema: dict[str, Any], *, fit_verdict: bool) -> str:
    """The runtime-assembled structured-output contract section.

    Explicitly supersedes the prompt file's sentinel-format instructions — a
    structured-mode session receives no sentinel wrapper and must not emit one.
    """
    if fit_verdict:
        shape_rules = (
            "Your reply must be a single JSON object with exactly two fields: "
            "`verdict` (the literal string `FIT` or `UNFIT`) and `reason` (a required "
            "explanation of the verdict — never leave it empty, even for `FIT`)."
        )
    else:
        shape_rules = (
            'Your reply must be a single JSON object with a top-level `kind` field, '
            'either `"question"` or `"final"`. When `kind` is `"question"`, set '
            "`question` to your clarifying question and leave `payload` null. When "
            '`kind` is `"final"`, set `payload` to the completed object described by '
            "the schema below and leave `question` null.\n"
            "`question` is the ONLY field the user will see on a question turn — there is "
            "no other field to carry explanation, analysis, or a written strategy. If the "
            "prompt above asks you to propose something (e.g. a strategy, an audit, a plan) "
            "before asking for confirmation, that full write-up must be included as text "
            "inside `question` itself, followed by your actual question. A short "
            "confirmation prompt with none of that content included is incomplete and "
            "leaves the user with nothing to evaluate.\n"
            "On a `\"question\"` turn, also populate `suggested_replies` with 2 to 4 "
            "short, distinct, directly-sendable answers the user could click to reply "
            "immediately instead of typing — vary their length (include at least one "
            "short, decisive option and at least one longer option that clarifies or "
            "pushes back), and do not pad the list to a fixed count if fewer genuinely "
            "distinct answers make sense. On a `\"final\"` turn, `suggested_replies` "
            "must be left null."
        )
    return (
        "\n\n## Structured output contract\n"
        "This session uses provider-enforced structured output, NOT the sentinel-block "
        "grammar described elsewhere in this prompt. This contract supersedes any "
        "`<<<NEED_INPUT>>>`/`<<<FINAL>>>`/`<<<END>>>` sentinel-block instructions above — "
        "do not wrap your reply in sentinel markers; return the JSON object directly.\n"
        f"{shape_rules}\n"
        "Do not include the literal sentinel markers (`<<<FINAL>>>`, `<<<NEED_INPUT>>>`, "
        "`<<<END>>>`) anywhere inside a JSON string value — they have no meaning in this "
        "session and would only corrupt the payload's text.\n"
        "JSON schema your reply must conform to:\n"
        f"{json.dumps(schema, indent=2)}"
    )


# The six error codes every tool result can carry. Sourced from jsa/schema/patch.py's
# result contract (unknown_id/stale_id/bad_argument/validation_failed) plus the two the
# LOOP synthesizes rather than the applier (budget_exhausted/not_executed — see
# jsa/pipeline/tool_loop.py). Pinned against docs/TOOLS.md by
# tests/backend/test_tools_doc_sync.py, which asserts this tuple and the doc's
# documented code set are equal — adding a code here without documenting it there
# (or vice versa) fails that gate rather than drifting silently.
_TOOL_ERROR_CODES = (
    "unknown_id",
    "stale_id",
    "bad_argument",
    "validation_failed",
    "budget_exhausted",
    "not_executed",
)


def _tool_contract(specs: tuple[Any, ...], *, native: bool) -> str:
    """The runtime-assembled tool-use contract section (revision-tool-use plan, Phase 4).

    **Both rungs get a contract — this is deliberately NOT "native = wire only, prompt =
    prompt only".** A provider enforces the *shape* of a call but says nothing about
    *when* to call ``get_cv`` versus ``finalize``, or that a question must precede any
    edit (D8). This repo already paid for that lesson once: see this module's docstring
    on the ``opencode-go``/``longcat-2.0`` job that emitted schema-valid JSON forever,
    re-asking its opening question, because the contract explaining ``kind`` semantics
    had been dropped from resumed turns while the provider kept enforcing the schema.

    What differs between rungs is only where the schemas live and how a call is
    signalled:

    * ``native=True``  — schemas ride the wire in ``tools``; the model signals a call
      through the provider-native channel. The prompt carries the SHORT contract:
      semantics, id discipline, the budget, ask-before-editing.
    * ``native=False`` — no wire channel exists, so the prompt carries the FULL
      contract: all of the above **plus** each tool's inlined JSON schema **plus** the
      ``<<<TOOL_CALLS>>>`` grammar.

    Single-sourced from ``jsa/agents/tool_spec.py`` (the ``ToolSpec`` tuple this receives),
    NOT from ``docs/TOOLS.md``. The plan's Phase 4 text says the contracts are "generated
    from docs/TOOLS.md" — deliberately not implemented that way: making the runtime prompt
    path read a markdown file means a missing or malformed doc breaks the pipeline. The
    anti-drift guarantee the plan wanted comes instead from a doc-sync test,
    ``tests/backend/test_tools_doc_sync.py``, which pins this module's
    ``_TOOL_ERROR_CODES`` and the tool vocabulary against ``docs/TOOLS.md`` — so the
    doc stays authoritative for humans while the runtime prompt stays independent of
    it.
    """
    names = ", ".join(f"`{s.name}`" for s in specs)
    lines = [
        "\n\n## Revision tool contract",
        "You are REVISING an existing document by patching it through tools. Do not "
        "rewrite it, and do not reproduce it in your reply — the tools below read and "
        "edit the stored document directly, and the server reconstructs the full "
        "document from your edits.",
        f"Available tools: {names}.",
        "",
        "Rules, in order of importance:",
        "1. Read before you write. Call `get_cv`/`get_letter` first — it returns the "
        "current document together with the ids you must address edits to. Ids are "
        "issued by the server; never invent one.",
        "2. If you need to ask the user anything, ask FIRST, before making any edit. "
        "`ask_user` DISCARDS every edit made this turn — an edit followed by a question "
        "is thrown away, and the user sees only the question.",
        "3. Patch narrowly. Change only what the revision instruction asks for; leave "
        "every other section, entry and paragraph untouched.",
        "4. You have a hard budget of 10 tool calls for this turn. Calls beyond it "
        "return `budget_exhausted` and the turn is abandoned.",
        "5. End the turn with exactly one terminal tool: `finalize` when the revision is "
        "complete, or `ask_user` when you cannot proceed without an answer. Any call "
        "placed after a terminal tool in the same batch returns `not_executed`.",
        "6. `finalize`'s `change_log` is a short summary FOR THE USER of what you "
        "changed. It is never document content and is never inserted into the document.",
        "",
        "Every tool returns either `{\"ok\": true, ...}` or "
        "`{\"ok\": false, \"error\": {\"code\": ..., \"message\": ..., \"hint\": ...}}`. "
        f"Error codes: {', '.join('`' + c + '`' for c in _TOOL_ERROR_CODES)}. "
        "An `unknown_id` or `stale_id` means your ids are out of date — call "
        "`get_cv`/`get_letter` again to refresh them rather than guessing. A failed call "
        "does not end the turn and does not consume your remaining budget beyond its own "
        "one call: fix the arguments and try again.",
    ]

    if not native:
        lines += [
            "",
            "### How to call a tool in this session",
            "This session has no provider-native tool channel, so you signal tool calls "
            "in the reply text. To call one or more tools, emit exactly one block:",
            "",
            "<<<TOOL_CALLS>>>",
            '[{"name": "<tool name>", "arguments": {...}}]',
            "<<<END>>>",
            "",
            "The block body must be a non-empty JSON array; each item must be an object "
            "with a string `name` and an object `arguments`. Calls execute in array "
            "order. This block SUPERSEDES the `<<<FINAL>>>`/`<<<NEED_INPUT>>>` "
            "instructions elsewhere in this prompt — while you are revising, do not emit "
            "either of those; use `finalize` and `ask_user` instead. Tool results come "
            "back to you as an ordinary user message containing a JSON array of results "
            "in the same order.",
            "",
            "### Tool schemas",
        ]
        for spec in specs:
            lines.append(f"#### {spec.name}")
            lines.append(spec.description)
            lines.append(json.dumps(spec.parameters, indent=2))

    return "\n".join(lines)


def assemble_system_prompt(
    prompt_text: str,
    *,
    language: str,
    structured_model: dict[str, Any] | None = None,
    tool_model: tuple[Any, ...] | None = None,
    native_tools: bool = False,
    fit_verdict: bool = False,
    for_resume: bool = False,
) -> str:
    """Compose a session's system prompt from the file-authored ``prompt_text``.

    ``structured_model=None`` (the sentinel-mode / CLI-backend path) is byte-identical
    to the pre-existing ``stages.py::_with_language_directive`` — see the module
    docstring's parity note. In this mode ``for_resume=True`` returns ``prompt_text``
    unchanged (no language directive) — CLI backends have a real session, so a resumed
    call needs nothing appended; this is what every ``restore_session`` call site got
    before ``for_resume`` existed.

    ``structured_model``, when given, is the JSON schema (``jsa.schema.turn_models
    .json_schema_for(stage)``) the destination backend will enforce; passing it appends
    the structured-output contract section. On a fresh session (``for_resume=False``)
    it also appends the structured-mode language directive when ``language != "en"``.
    On a resumed session (``for_resume=True``) the language directive is always
    skipped — only the contract is re-appended — see the module docstring for why
    resending the contract is required for these (wire-stateless) backends while
    resending the language directive is deliberately still excluded.
    ``fit_verdict=True`` selects the fit-assessment shape in both the contract and the
    language directive.

    ``tool_model``, when given, is the stage's ``ToolSpec`` tuple
    (``jsa.agents.tool_spec.tools_for(stage)``) and appends the tool-use contract INSTEAD
    of the structured one — the two are mutually exclusive and passing both raises.
    ``native_tools`` selects the short (wire-schema) contract over the full (inlined
    schemas + ``<<<TOOL_CALLS>>>`` grammar) one; it is a separate flag rather than being
    folded into ``tool_model`` because the SPECS are identical on both rungs and only the
    transport differs. ``for_resume``/``language`` are ignored on this path — see the
    tool branch's comment.

    ``structured_model=None, tool_model=None`` is the golden-parity path and must stay
    byte-identical forever (``tests/backend/test_prompt_assembly.py`` against
    ``fixtures/language_directive_golden.json``).
    """
    if tool_model is not None:
        if structured_model is not None:
            # Phase 3's "tool mode and structured mode are mutually exclusive per
            # request — assert it". The terminal tool's arguments ARE the structured
            # output, so no configuration ever wants both; a caller computing them
            # independently rather than from one destination-mode decision is a bug.
            raise ValueError(
                "structured_model and tool_model are mutually exclusive: a tool-mode "
                "session sends no response_format/schema — finalize's arguments are the "
                "structured output"
            )
        # No language directive on either rung: run_tool_loop only ever restores a
        # session (revisions never start_session), so this is always the for_resume
        # shape, and a resumed session already committed to its language.
        return prompt_text + _tool_contract(tool_model, native=native_tools)

    if structured_model is not None:
        prompt = prompt_text + _structured_contract(structured_model, fit_verdict=fit_verdict)
        if not for_resume and language != "en":
            prompt += _structured_language_directive(language, fit_verdict=fit_verdict)
        return prompt

    if for_resume:
        return prompt_text

    if language == "en":
        return prompt_text
    return prompt_text + _sentinel_language_directive(language, fit_verdict=fit_verdict)
