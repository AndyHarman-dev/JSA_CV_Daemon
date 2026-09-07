"""Turn models for structured-output (JSON-schema-enforced) API backends.

Two independent jobs live here, and they must not be conflated:

1. **Schema generation** (``json_schema_for``) — flat, strict Pydantic models
   (``CvTurn``, ``ClTurn``, ``FitVerdict``) whose ``model_json_schema()`` is handed to a
   provider (Anthropic forced tool-use, an OpenAI-compatible ``response_format``) so it
   *generates* a conforming reply. Strict on purpose: ``ConfigDict(extra="forbid")`` plus
   no-default fields make every field appear in JSON Schema's ``required`` list (a
   defaulted field is silently dropped from ``required`` by Pydantic — see the empirical
   note on ``FitVerdict`` below) and ``additionalProperties: false`` land unconditionally
   at the top level. A "genuinely optional" field (``question``, ``payload``) stays
   ``X | None`` with **no default** — required-but-nullable, not absent-but-defaulted —
   so its presence in ``required`` doesn't regress; a ``model_validator`` enforces the
   real optionality (payload iff final, question iff question) as an *application* rule,
   not a schema one.

   These models' nested payload fields (``CVDocument``, ``CoverLetter``) are themselves
   tolerant schemas (``extra="ignore"``, defaulted optional fields — see
   ``jsa/schema/cv.py``) — their generated ``$defs`` are therefore NOT recursively strict.
   This is fine for Anthropic's ``input_schema`` (no recursive-strictness requirement) and
   a known, deliberately-deferred risk for an OpenAI-compatible ``strict: true`` mode
   (tracked for the OpenCode Zen phase's integration tests) — tightening those models
   would also change what the sentinel path's ``_validate_final_content`` accepts, which
   is out of scope here.

2. **Reply parsing** (``parse_structured_reply``) — deliberately NOT model validation.
   It does ``json.loads`` + ``kind`` routing only: enough structure to route a reply to
   "needs_input" or "final" and hand ``_validate_final_content``
   (``jsa/pipeline/stages.py``) the same bare-JSON-text shape the sentinel path already
   produces. It never calls ``CvTurn.model_validate``/``ClTurn.model_validate`` — running
   the full nested-payload validation here would reject CV/cover-letter semantic failures
   as a structured-mode ``ProtocolError`` (Phase 5's re-emit-conforming-object budget)
   instead of the existing ``_validate_final_content`` self-heal path
   (``_self_heal_final``'s CV/CL-specific correction wording), silently splitting one
   failure mode into two different recovery budgets. Semantic validation of the payload
   stays exactly where it already lives.

Canonical-form invariant: for a structured reply, ``AgentReply.raw`` returned by
``parse_structured_reply`` IS the model's canonical union-JSON text (the same string that
gets ``json.loads``'d above) — never a provider envelope (``tool_use`` block, SSE chunk,
etc.). ``stages.py`` persists ``reply.raw`` verbatim into ``Message`` rows, and that
persisted text is the only thing that makes the plan's "DB stores canonical form; provider
wire format never round-trips" invariant real. A backend adapter (Phases 3/4) that hands
this function anything other than the plain reply text breaks that invariant.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from jsa.agents.base import AgentReply, HistoryTurn
from jsa.agents.protocol import ProtocolError, parse_reply
from jsa.db.models import Stage
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument


class FitVerdict(BaseModel):
    """The fit-assessment stage's structured reply shape.

    ``reason`` is required (no default, not ``Optional``) for BOTH verdicts — a bare
    ``FIT``/``UNFIT`` with no justification fails the provider's own strict-schema
    validation at generation time, which is what makes "the model must justify itself"
    (CLAUDE.md → "Fit-assessment gate" / this plan's decision #4) a real constraint rather
    than aspirational prompt wording. This does not change ``job.fit_reason`` storage,
    which still discards the reason on a FIT verdict (``stages.py::_run_fit_assessment``).

    CONFIRMED empirically (skeptic gate): a ``reason: str | None = None`` shape produces
    NO ``additionalProperties`` key at all and drops ``reason`` from ``required`` — Pydantic
    excludes any field with a default from ``required`` entirely, which is worse than
    nullable-but-required for a strict provider. Hence: no default, no ``Optional``.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["FIT", "UNFIT"]
    reason: str


def _check_payload_iff_final(
    kind: Literal["question", "final"],
    question: str | None,
    payload: Any,
    suggested_replies: list[str] | None,
) -> None:
    """Shared iff-rule for ``CvTurn``/``ClTurn``: payload present iff final, question
    present iff question, suggested_replies null unless question. Raising ``ValueError``
    inside a ``model_validator`` is how Pydantic surfaces a validation failure, so this
    is called from each model's own ``mode="after"`` validator rather than shared as a
    validator itself (pydantic validators are bound per-model)."""
    if kind == "final":
        if payload is None:
            raise ValueError("kind='final' requires a non-null `payload`")
        if suggested_replies is not None:
            raise ValueError("kind='final' requires a null `suggested_replies`")
    else:  # kind == "question"
        if question is None:
            raise ValueError("kind='question' requires a non-null `question`")


class CvTurn(BaseModel):
    """The cv_adjust / revising_cv stages' structured reply shape.

    A single flat schema carrying both possible replies (a clarifying question, or the
    final CV) rather than an ``anyOf`` discriminated union — strict JSON-schema providers
    reject a top-level ``anyOf`` and require ``additionalProperties: false`` plus every
    field present in ``required``.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["question", "final"]
    question: str | None
    payload: CVDocument | None
    suggested_replies: list[str] | None

    @model_validator(mode="after")
    def _payload_iff_final(self) -> "CvTurn":
        _check_payload_iff_final(self.kind, self.question, self.payload, self.suggested_replies)
        return self


class ClTurn(BaseModel):
    """The cover_letter / revising_cl stages' structured reply shape. See ``CvTurn``."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["question", "final"]
    question: str | None
    payload: CoverLetter | None
    suggested_replies: list[str] | None

    @model_validator(mode="after")
    def _payload_iff_final(self) -> "ClTurn":
        _check_payload_iff_final(self.kind, self.question, self.payload, self.suggested_replies)
        return self


# InferTurn — the job-less CV-structure inference call's reply shape
# (jsa/pipeline/infer_structure.py::run_infer). Design notes, kept OUT of the class
# docstring on purpose: Pydantic copies ``__doc__`` into the schema's ``description``,
# and this schema is embedded verbatim in the system prompt of every inference request,
# so maintainer rationale there is tokens the model pays for and cannot use.
#
# * ``run_infer`` is stage-less — it has no Job, no DB row, no resume path — so this
#   model stays out of ``STAGE_TURN_MODELS`` and unreachable from ``json_schema_for``;
#   ``json_schema_for_infer`` below is its only entry point, mirroring how
#   ``FitVerdict`` is special-cased there.
# * ``kind`` is a ONE-MEMBER ``Literal``, and that is the point — not a vestigial field
#   copied from ``CvTurn``. It makes this schema route through
#   ``parse_structured_reply_for_schema``'s existing turn-union branch with that
#   function left untouched: it keys ``is_fit`` off ``"kind" not in properties``, so a
#   bare ``CVDocument`` schema would be misrouted into ``_parse_fit_structured`` and
#   fail every single reply with "invalid 'verdict'".
# * It also turns PROMPT_INFER_STRUCTURE.md's prose rule — always FINAL, never
#   NEED_INPUT, "there is no one to answer" — into a provider-enforced constraint: with
#   ``extra="forbid"`` and no ``question`` field, a question turn is not expressible.
#   No ``suggested_replies`` either, for the same reason.
class InferTurn(BaseModel):
    """A completed CV structuring turn: `kind` is always `final`, `payload` is the
    CVDocument."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["final"]
    payload: CVDocument


# Stages whose structured reply is a {kind, question, payload} union. fit_assessment is
# deliberately excluded — its shape (FitVerdict) has no "question" branch at all (the fit
# gate is a one-shot verdict, never a NEED_INPUT) and is handled separately below.
STAGE_TURN_MODELS: dict[Stage, type[BaseModel]] = {
    Stage.cv_adjust: CvTurn,
    Stage.revising_cv: CvTurn,
    Stage.cover_letter: ClTurn,
    Stage.revising_cl: ClTurn,
}


def json_schema_for(stage: Stage) -> dict[str, Any]:
    """The JSON schema a structured-output backend should enforce for ``stage``."""
    if stage is Stage.fit_assessment:
        return FitVerdict.model_json_schema()
    try:
        model = STAGE_TURN_MODELS[stage]
    except KeyError:
        raise ValueError(f"no structured turn model for stage {stage!r}") from None
    return model.model_json_schema()


def json_schema_for_infer() -> dict[str, Any]:
    """The JSON schema a structured-output backend should enforce for the job-less
    CV-structure inference call (``jsa/pipeline/infer_structure.py``).

    Separate from ``json_schema_for`` because inference has no ``Stage``: it is not part
    of the job pipeline, has no DB row, and no resume path. See ``InferTurn``.
    """
    return InferTurn.model_json_schema()


def inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively inline every ``$ref`` in ``schema`` against its own ``$defs``,
    dropping ``$defs``, ``additionalProperties``, ``title`` and ``default`` from the
    result, and rewriting ``const`` to a single-member ``enum`` (Gemini's
    ``responseSchema`` is a restricted OpenAPI subset that rejects all five).

    Multi-backend-model-select plan, Phase 0 probe #4 (2026-08-31): both
    ``generationConfig.responseSchema`` and ``responseJsonSchema`` reject the raw
    ``json_schema_for(Stage.cv_adjust)`` output (nested ``$defs`` for
    ``CVDocument``/``Contact``/``Section``/``Entry``) on every current Gemini model
    tried, but accept it once inlined — confirmed live. This is the plan's
    pre-committed fallback #1: send the inlined form rather than fight the
    restriction, the same call already made for Anthropic's forced tool-use
    (``input_schema`` has no recursive-strictness requirement) and for OpenCode
    Zen/Mistral/OpenRouter's ``response_format`` (``"strict": false`` — see
    ``_openai_compat.py``'s module docstring). ``GeminiBackend`` is the only caller.

    A cycle guard (each ``$defs`` key inlines at most once per branch) returns an
    empty object rather than recursing forever on a self-referential schema — none
    of the turn models are actually cyclic; this just keeps the function total.
    """
    defs = schema.get("$defs", {})
    _DROP = ("$defs", "additionalProperties", "title", "default")

    def _inline(node: Any, seen: frozenset[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                key = node["$ref"].rsplit("/", 1)[-1]
                if key in seen or key not in defs:
                    return {}
                return _inline(defs[key], seen | {key})
            if "const" in node and "enum" not in node:
                # `responseSchema` has no `const` — CONFIRMED live (2026-09-07,
                # gemini-3.5-flash): `Invalid JSON payload received. Unknown name
                # "const" at 'generation_config.response_schema.properties[0].value'`,
                # a 400 that cost the whole request its structured mode via
                # `_SchemaRejected`. Pydantic emits `const` for a ONE-member
                # `Literal` and `enum` for a multi-member one, so this only ever fires
                # for `InferTurn.kind`; every Stage-keyed model's Literal has two or
                # more members and already ships as `enum`.
                #
                # Rewritten to a single-member `enum` rather than added to `_DROP`,
                # deliberately: `enum` is documented in the same restricted subset and
                # is exactly equivalent, whereas dropping the key would leave `kind` an
                # unconstrained string. Gemini could then return any value and
                # `_route_structured_data` would reject it as "invalid 'kind'" — trading
                # a loud, once-per-session schema rejection for a per-reply parse
                # failure, which is strictly worse.
                node = {k: v for k, v in node.items() if k != "const"} | {
                    "enum": [node["const"]]
                }
            return {k: _inline(v, seen) for k, v in node.items() if k not in _DROP}
        if isinstance(node, list):
            return [_inline(v, seen) for v in node]
        return node

    return _inline(schema, frozenset())


def _parse_fit_structured(raw: str, data: Any) -> AgentReply:
    if not isinstance(data, dict):
        raise ProtocolError("structured reply unparseable: expected a JSON object")
    verdict = data.get("verdict")
    reason = data.get("reason")
    if verdict not in ("FIT", "UNFIT"):
        raise ProtocolError(f"structured reply unparseable: invalid 'verdict' {verdict!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise ProtocolError("structured reply unparseable: missing or invalid 'reason'")
    content = f"{verdict}\n{reason}"
    return AgentReply(raw=raw, content=content, kind="final", question=None)


def _load_structured_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"structured reply unparseable: invalid JSON ({exc})") from exc


def _route_structured_data(raw: str, data: Any, *, is_fit: bool) -> AgentReply:
    """Shared routing core behind both ``parse_structured_reply`` (stage-keyed) and
    ``parse_structured_reply_for_schema`` (schema-keyed, see below) — the two entry
    points differ only in HOW they compute ``is_fit``, never in what happens after.
    """
    if is_fit:
        return _parse_fit_structured(raw, data)

    if not isinstance(data, dict):
        raise ProtocolError("structured reply unparseable: expected a JSON object")

    kind = data.get("kind")
    if kind == "question":
        question = data.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ProtocolError("structured reply unparseable: missing or invalid 'question'")
        suggested_replies = data.get("suggested_replies")
        if suggested_replies is not None and not (
            isinstance(suggested_replies, list)
            and all(isinstance(item, str) for item in suggested_replies)
        ):
            suggested_replies = None
        return AgentReply(
            raw=raw,
            content=question,
            kind="needs_input",
            question=question,
            suggested_replies=suggested_replies,
        )
    if kind == "final":
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("structured reply unparseable: missing or invalid 'payload'")
        return AgentReply(raw=raw, content=json.dumps(payload), kind="final", question=None)

    raise ProtocolError(f"structured reply unparseable: invalid 'kind' {kind!r}")


def parse_structured_reply(raw: str, stage: Stage) -> AgentReply:
    """Parse a structured-output backend's raw reply text into an ``AgentReply``.

    ``json.loads`` + ``kind`` routing only — see the module docstring's "Reply parsing"
    section for why this deliberately does not run ``CvTurn``/``ClTurn`` model validation.
    Raises ``ProtocolError`` on invalid JSON or an unrecognized/missing ``kind`` (or, for
    ``fit_assessment``, an invalid ``verdict``/``reason``) — the same exception the
    sentinel path raises on a malformed reply, so both paths converge on one failure type.

    Stage-keyed entry point: for callers holding the ``Stage`` enum (there are none in
    production yet — see ``parse_structured_reply_for_schema`` below for who actually
    calls this family of functions today). Kept because 33 tests in
    ``test_turn_models.py`` already hang off this exact signature from Phase 1.
    """
    data = _load_structured_json(raw)
    return _route_structured_data(raw, data, is_fit=(stage is Stage.fit_assessment))


def parse_structured_reply_for_schema(raw: str, schema: dict[str, Any]) -> AgentReply:
    """Schema-keyed sibling of ``parse_structured_reply``, for call sites that hold a
    ``json_schema_for(stage)`` result rather than the ``Stage`` enum itself.

    This is the one production backends actually call: ``AnthropicAPIBackend``
    (Phase 3) receives ``structured_schema`` at call time — not ``Stage`` — because the
    backend layer is deliberately stage-agnostic (it only knows "here is the schema to
    enforce"), matching how the sentinel path's ``parse_reply(raw)`` already takes no
    stage either. Routing is derived from the schema's own top-level ``properties``: the
    turn-union models (``CvTurn``/``ClTurn``) always declare a ``kind`` property;
    ``FitVerdict`` never does — so ``"kind" not in schema["properties"]`` is an exact,
    schema-shape-based stand-in for ``stage is Stage.fit_assessment``, with no risk of
    misclassifying an actual reply payload (the check runs against the fixed schema,
    never against whatever the model happened to return). ``InferTurn`` declares ``kind``
    too, which is exactly why it carries that one-member field — see its docstring.

    Equivalence with ``parse_structured_reply`` for the same logical payload is asserted
    by ``test_turn_models.py``'s ``TestSchemaKeyedParityWithStageKeyed`` — the two entry
    points share ``_route_structured_data`` so they cannot silently diverge, but the test
    pins the observable behavior anyway.
    """
    data = _load_structured_json(raw)
    is_fit = "kind" not in schema.get("properties", {})
    return _route_structured_data(raw, data, is_fit=is_fit)


# ---------------------------------------------------------------------------
# Replay adapter — mixed-mode history (see the plan's "Named invariant").
# ---------------------------------------------------------------------------
#
# There is no persisted "session mode" column anywhere (the OpenCode Zen per-session
# downgrade flag is explicitly never persisted), so at replay time (resume after
# restart, a BF-19 backend switch, a revision resume) a row's own origin format is the
# only thing that identifies it: a sentinel row starts with ``<<<`` (it is always
# ``reply.raw`` from the sentinel path — see jsa/pipeline/stages.py's Message writes);
# a canonical structured row is bare JSON (starts with ``{``). fit_assessment rows are
# excluded by construction — that stage has no resume/restore_session path at all, so
# its rows (``FIT\n<reason>`` — not a ``{kind: ...}`` shape) are never replayed through
# this adapter.


def _is_sentinel_wrapped(text: str) -> bool:
    return text.lstrip().startswith("<<<")


def wrap_canonical_for_sentinel(canonical_text: str) -> str:
    """Wrap a canonical ``{kind, question, payload}`` turn as sentinel-block text.

    For replaying a canonical (structured-mode-produced) row into a sentinel-mode
    destination session (e.g. after a BF-19 switch from an API backend to a CLI
    backend). Already-sentinel-wrapped text, non-JSON text, and any shape other than
    the recognized ``kind`` union pass through unchanged — this must never raise, since
    an unrecognized row is still valid plain-text input to a sentinel-mode session.
    """
    if _is_sentinel_wrapped(canonical_text):
        return canonical_text
    try:
        data = json.loads(canonical_text)
    except json.JSONDecodeError:
        return canonical_text
    if not isinstance(data, dict):
        return canonical_text
    kind = data.get("kind")
    if kind == "question" and isinstance(data.get("question"), str):
        suggested_replies = data.get("suggested_replies")
        suggestions_block = ""
        if isinstance(suggested_replies, list) and all(
            isinstance(item, str) for item in suggested_replies
        ) and suggested_replies:
            suggestions_block = "\n<<<SUGGESTIONS>>>\n" + "\n".join(suggested_replies)
        return f"<<<NEED_INPUT>>>\n{data['question']}{suggestions_block}\n<<<END>>>"
    if kind == "final" and isinstance(data.get("payload"), dict):
        return f"<<<FINAL>>>\n{json.dumps(data['payload'])}\n<<<END>>>"
    return canonical_text


def unwrap_sentinel_to_canonical(sentinel_text: str) -> str:
    """Unwrap sentinel-block text into a canonical ``{kind, question, payload}`` turn.

    For replaying a sentinel-mode row into a structured-mode destination session.
    Never raises: a fit_assessment-shaped ``FINAL`` body (``FIT\\n<reason>``) or any
    other non-JSON FINAL is legacy/foreign content this adapter doesn't own — a
    plain-text assistant turn is valid input to a structured-mode session regardless
    (see the plan's Named Invariant), so any parse failure passes the row through
    unchanged rather than raising.
    """
    if not _is_sentinel_wrapped(sentinel_text):
        return sentinel_text
    try:
        reply = parse_reply(sentinel_text)
    except ProtocolError:
        return sentinel_text
    if reply.kind == "needs_input":
        data: dict[str, Any] = {
            "kind": "question",
            "question": reply.question,
            "payload": None,
        }
        if reply.suggested_replies:
            data["suggested_replies"] = reply.suggested_replies
        return json.dumps(data)
    try:
        payload = json.loads(reply.content)
    except json.JSONDecodeError:
        return sentinel_text
    if not isinstance(payload, dict):
        return sentinel_text
    return json.dumps({"kind": "final", "question": None, "payload": payload})


def adapt_history(history: list[HistoryTurn], *, structured: bool) -> list[HistoryTurn]:
    """Adapt each assistant turn in ``history`` for a destination session's mode.

    ``structured`` names the DESTINATION mode explicitly — the caller decides it (e.g.
    hardcoded ``False`` at every sentinel-only call site today); this function never
    infers it from backend capability, since capability and active session mode are
    different things (a structured-capable backend can be mid-downgrade). User turns
    are never touched — only assistant replies carry a mode-specific wire shape, and
    per-row content-based detection means a turn already in the destination's own form
    is returned unchanged (which is every row in the system today, making this
    currently a no-op end to end).
    """
    adapted: list[HistoryTurn] = []
    for turn in history:
        if turn.role != "assistant":
            adapted.append(turn)
            continue
        new_content = (
            unwrap_sentinel_to_canonical(turn.content)
            if structured
            else wrap_canonical_for_sentinel(turn.content)
        )
        if new_content == turn.content:
            adapted.append(turn)
        else:
            adapted.append(HistoryTurn(role=turn.role, content=new_content))
    return adapted
