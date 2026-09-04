"""Single composition root for runtime system-prompt mutation.

``assemble_system_prompt`` is the ONLY place that appends runtime instructions to a
stage's file-authored system prompt (``jsa/prompts/*.md``, read via
``jsa/prompts/loader.py`` and never edited programmatically — see CLAUDE.md → "Prompt
files"). It owns three independent concerns:

1. **Language directive** — ported verbatim from the pre-existing
   ``jsa/pipeline/stages.py::_language_directive`` / ``_with_language_directive`` (see
   ``tests/backend/fixtures/language_directive_golden.json``, captured from that
   original implementation before this module existed, and
   ``tests/backend/test_prompt_assembly.py``'s parity-gate assertion against it). The
   sentinel-mode branch (``structured_model=None``) MUST stay byte-identical to that
   original output forever — every CLI backend (``claude-cli``, ``google-cli``) depends
   on it verbatim. This is why the current-date directive below is strictly opt-in via
   the ``now`` kwarg (default ``None`` → no-op) rather than unconditional: the golden
   fixture's 84 cases call this function without ``now`` and must keep passing.
2. **Structured-output contract** (only when ``structured_model`` is given) — the
   runtime-assembled instructions a structured-output API backend's session needs:
   ``kind``/``verdict`` semantics, the per-stage JSON schema, explicit precedence over
   the prompt file's sentinel-format section, and a "never embed sentinel markers in a
   payload string value" guard. This section is assembled here, at runtime, and is
   never written into the prompt files themselves.
3. **Current-date directive** (only when ``now`` is given) — a day-granularity "today's
   date is X, treat CV/JD dates as fact, not as training-cutoff inconsistencies"
   section (``_current_date_directive``). Every real call site (``jsa/pipeline/
   stages.py``) always passes ``now=datetime.utcnow()``; ``now=None`` exists only so
   tests — including the golden-fixture parity gate above — can get the pre-existing
   output with no date section at all. Always appended LAST, after the language
   directive and the structured contract, so it can never land in the middle of either
   section's internal self-references ("the prompt above", "the schema below"). Day
   granularity (``%Y-%m-%d``, never clock time) is deliberate: this text lands in the
   cross-job system prefix the prompt-caching effort keys on (see CLAUDE.md → "Prompt
   caching (HTTP API backends)"); seconds-precision would make every request's prefix
   unique and defeat caching outright, while day granularity only invalidates the
   cached prefix once every 24h — looser than every provider's cache TTL here.
4. **Per-job prompt injection** (only when ``injection`` is given) — the user-authored
   ``prefix``/``postfix`` from ``jsa/schema/injection.py``'s ``PromptInjection``, which
   bracket the file-authored ``prompt_text`` and **nothing else**. This is applied
   FIRST, before any of the three sections above, so every machine-authored section
   keeps its existing final position AFTER the postfix. The ordering is load-bearing,
   not cosmetic: the structured-output contract asserts "This contract supersedes any
   sentinel-block instructions above", so a postfix landing after it (e.g. "reply in
   plain prose, no JSON") would become the last word over the JSON contract — a
   ``ProtocolError`` every turn, burning the ``MAX_FINAL_CORRECTIONS`` self-heal budget
   and then triggering a BF-19 backend hop. Do not reorder. ``injection=None`` and an
   all-blank injection are both byte-identical to the pre-injection output, so the
   cross-job prefix invariant above is untouched for every job without one (a job WITH
   one deliberately gets its own, different prefix — an intended trade, pinned in
   ``tests/backend/test_prompt_prefix_stability.py``).

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

The current-date directive follows the structured-contract's resend rule, not the
language directive's: it IS re-appended on ``for_resume=True`` for structured-mode
sessions (wire-stateless, so a job parked overnight in ``awaiting_input`` resumes with
the correct date, not the one at job launch), and it is NOT appended on the
sentinel-mode ``for_resume=True`` early return (a real CLI session already carries the
date from its fresh start; nothing needs to change there).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from jsa.i18n.languages import language_name
from jsa.schema.injection import PromptInjection


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


def _current_date_directive(now: datetime) -> str:
    """Day-granularity "what date is it" directive — never clock time.

    Day granularity is deliberate, not an oversight: this text lands in the same
    system prefix the prompt-caching effort keys on (see CLAUDE.md → "Prompt caching
    (HTTP API backends)"). Seconds-precision would make every request's prefix unique
    and defeat caching entirely; day granularity only invalidates the cached prefix
    once every 24h, which is already looser than every provider's cache TTL here.

    The instruction half is the actual fix, not the date alone — models trained
    before this date otherwise read a CV's future-relative-to-training-cutoff dates
    (e.g. a 2026 role) as an inconsistency to flag rather than a fact to accept,
    which is exactly the failure mode this directive exists to prevent (confirmed
    live: a fit-assessment verdict parking a job as unfit over a "future" CV date).
    """
    today = now.strftime("%Y-%m-%d")
    return (
        "\n\n## Current date\n"
        f"Today's date is {today}. Your training data has a cutoff before this date, "
        "so information in the CV, cover letter, or job description that is more "
        "recent than your training — including dates at or before today, such as "
        "current or recent job experience — is not an error, inconsistency, or "
        "hallucination. Treat every date and fact in the provided materials as "
        "accurate; never 'correct', flag, or question them for appearing to be in "
        "the future relative to your training data."
    )


def assemble_system_prompt(
    prompt_text: str,
    *,
    language: str,
    structured_model: dict[str, Any] | None = None,
    fit_verdict: bool = False,
    for_resume: bool = False,
    now: datetime | None = None,
    injection: PromptInjection | None = None,
) -> str:
    """Compose a session's system prompt from the file-authored ``prompt_text``.

    ``structured_model=None`` with ``now=None`` (the sentinel-mode / CLI-backend path,
    at its parity-gate default) is byte-identical to the pre-existing
    ``stages.py::_with_language_directive`` — see the module docstring's parity note.
    In this mode ``for_resume=True`` returns the prompt with no machine-authored
    section appended, regardless of ``now`` (no language directive, no date directive)
    — CLI backends have a real session, so a resumed call needs nothing appended; this
    is what every ``restore_session`` call site got before ``for_resume`` existed. It
    returns ``base``, not ``prompt_text``: the per-job injection wrapper MUST survive
    this path, or a CLI backend rebuilding its session from history (BF-19 switch,
    revision replay) silently drops it from turn 2 onward. With no injection the two
    are the same object, which is why this reads as unchanged for every existing caller.

    ``structured_model``, when given, is the JSON schema (``jsa.schema.turn_models
    .json_schema_for(stage)``) the destination backend will enforce; passing it appends
    the structured-output contract section. On a fresh session (``for_resume=False``)
    it also appends the structured-mode language directive when ``language != "en"``.
    On a resumed session (``for_resume=True``) the language directive is always
    skipped — only the contract (and the date directive, see below) is re-appended —
    see the module docstring for why resending the contract is required for these
    (wire-stateless) backends while resending the language directive is deliberately
    still excluded.
    ``fit_verdict=True`` selects the fit-assessment shape in both the contract and the
    language directive.

    ``now``, when given, appends the current-date directive (``_current_date_directive``)
    LAST, after everything else. Every real call site passes ``now=datetime.utcnow()``;
    only tests pass ``now=None`` to get the pre-date-directive output. See the module
    docstring's point 3 for the resend/day-granularity rationale.

    ``injection`` (the per-job ``PromptInjection``, see ``jsa/schema/injection.py``)
    brackets ``prompt_text`` and NOTHING else — see the module docstring's point 4 for
    why every machine-authored section must keep its position after the postfix.
    ``injection=None`` and an all-blank injection both produce byte-identical output to
    the pre-injection implementation (``normalized()`` collapses the blank triple to
    ``None``, and it is re-normalized here defensively rather than trusting the caller).
    """
    # User-authored wrapper, applied FIRST so every machine-authored section below
    # (structured contract, language directive, date directive) still lands last — see
    # the module docstring's point 4.
    inj = injection.normalized() if injection is not None else None
    base = prompt_text
    if inj is not None:
        if inj.prefix:
            base = f"{inj.prefix}\n\n{prompt_text}"
        if inj.postfix:
            base = f"{base}\n\n{inj.postfix}"

    if structured_model is not None:
        prompt = base + _structured_contract(structured_model, fit_verdict=fit_verdict)
        if not for_resume and language != "en":
            prompt += _structured_language_directive(language, fit_verdict=fit_verdict)
        # Wire-stateless backends resend the system prompt every call (see module
        # docstring), so injecting on resume too keeps a job parked overnight in
        # awaiting_input resuming with the correct date, not the one at job launch.
        if now is not None:
            prompt += _current_date_directive(now)
        return prompt

    if for_resume:
        # Real CLI session — it already carries the date from its fresh start (below);
        # nothing is appended here on resume, deliberately, same as the language
        # directive this path has always skipped. Returns `base`, NOT `prompt_text`:
        # a CLI backend that rebuilds its session from history (BF-19 switch, revision
        # replay) would otherwise silently drop the user's wrapper from turn 2 onward.
        return base

    prompt = base
    if language != "en":
        prompt += _sentinel_language_directive(language, fit_verdict=fit_verdict)
    if now is not None:
        prompt += _current_date_directive(now)
    return prompt
