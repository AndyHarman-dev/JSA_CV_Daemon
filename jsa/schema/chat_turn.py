"""Turn model for the CV-editor chat's job-less structured/sentinel reply
(``jsa/pipeline/cv_chat.py::run_cv_chat``).

Mirrors ``jsa/schema/turn_models.py``'s ``CvTurn``/``ClTurn``/``InferTurn`` shapes
deliberately, not coincidentally: ``jsa.agents.anthropic_api``/``opencode_zen``/
``_openai_compat``'s structured-mode backends all route a reply through the
GENERIC, schema-shape-based ``turn_models.parse_structured_reply_for_schema`` —
which only recognizes the envelope ``{kind, question, payload, suggested_replies}``
(``"kind" not in schema["properties"]`` selects the FitVerdict branch instead). So
``CvChatTurn`` keeps that exact envelope, with ``payload`` typed as ``CvChatPayload``
(``{answer, ops}``) instead of ``CVDocument``/``CoverLetter`` — this is what lets
every existing structured backend parse a chat turn with **zero** backend-side
changes. ``AgentReply.content`` ends up as the bare ``{"answer": ..., "ops": [...]}``
JSON in both modes: the routed-out ``payload`` dict in structured mode, and
whatever the model wrote inside the ``<<<FINAL>>>`` block in sentinel mode (per
``PROMPT_CV_CHAT.md``) — so ``run_cv_chat`` needs no per-mode branch after the
reply comes back, the same property ``run_infer`` relies on.

Absent from ``STAGE_TURN_MODELS`` and unreachable from ``json_schema_for`` — this
call is stage-less (no Job, no DB row), mirroring ``InferTurn``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

ScopeType = Literal["cv", "contact", "section", "entry"]

CvChatOpName = Literal[
    "replace_summary",
    "replace_section",
    "replace_entry",
    "edit_entry_bullets",
    "add_entry",
    "remove_entry",
    "reorder_entries",
    "replace_contact",
]

# Fields each op requires to be non-null. Everything else on CvChatOp stays whatever
# the model sent (usually null) — enforced in Python (application semantics), not the
# schema (shape only), per turn_models.py's documented division of labour.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "replace_summary": ("text",),
    "replace_section": ("section_id", "section"),
    "replace_entry": ("entry_id", "entry"),
    "edit_entry_bullets": ("entry_id", "bullets"),
    "add_entry": ("section_id", "entry"),
    "remove_entry": ("entry_id",),
    "reorder_entries": ("section_id", "order"),
    "replace_contact": ("contact",),
}

# Ops that can only name an entry that already exists — every one of them requires an
# `entry_id` drawn from the turn's CvWorkingCopy dump, except `reorder_entries`, whose
# `order` is a permutation of those same ids. On a node with no entries there is nothing
# for any of them to address. See `ops_for_scope` for the live failure this prevents.
# `add_entry` is deliberately absent: it addresses the SECTION, so it stays satisfiable.
_ENTRY_ADDRESSING_OPS: frozenset[str] = frozenset(
    {"replace_entry", "edit_entry_bullets", "remove_entry", "reorder_entries"}
)

# The op vocabulary allowed per scope (D5's schema-level narrowing layer). Order is
# fixed and hand-written (never derived from a set) so schema generation stays
# deterministic across PYTHONHASHSEED, matching every other schema generator in the
# repo (see turn_models.py's prefix-stability note).
_SCOPE_OPS: dict[ScopeType, tuple[CvChatOpName, ...]] = {
    "contact": ("replace_contact",),
    "entry": ("replace_entry", "edit_entry_bullets"),
    "section": (
        "replace_section",
        "replace_summary",
        "replace_entry",
        "edit_entry_bullets",
        "add_entry",
        "remove_entry",
        "reorder_entries",
    ),
    "cv": (
        "replace_summary",
        "replace_section",
        "replace_entry",
        "edit_entry_bullets",
        "add_entry",
        "remove_entry",
        "reorder_entries",
        "replace_contact",
    ),
}


# NOTE — every docstring on the three models below is sent to the model verbatim, as
# the `description` of that object in the generated JSON Schema. Keep them short and
# model-facing; the implementation rationale lives in these comments instead.
#
# Mirrors `jsa/agents/tool_spec.py`'s hand-written `_ENTRY_SCHEMA` field for field (and
# `jsa/schema/cv.py::Entry` minus `id`, which is server-minted and stripped defensively
# by `patch.py::_clean_entry_input`).
#
# Every field is DEFAULTED, unlike `_ENTRY_SCHEMA`'s all-required list. That costs
# nothing where it matters: `turn_models.inline_defs` rewrites every property into
# `required` for Gemini's constrained decoder regardless of Pydantic defaults (and
# drops `default` outright), and the other structured backends send `"strict": false`.
# What it buys is tolerance in SENTINEL mode -- `chat_backend` defaults to `claude-cli`,
# which gets no schema at all, so a partial object there would otherwise hard-fail the
# whole turn instead of reaching `_clean_entry_input`'s tolerant absorption. A partial
# entry does blank the fields it omits (these ops are whole-object replaces), but that
# blanking is visible as a `· dates` / `· bullets` row on the diff card BEFORE the user
# applies it, so it is recoverable -- which a failed turn is not. The prompt carries the
# "restate every field" instruction (`PROMPT_CV_CHAT.md`) as the real countermeasure.
class ChatEntry(BaseModel):
    """One CV entry — a job, degree, project or award. These ops replace the whole
    entry, so restate every field, copying unchanged ones verbatim."""

    model_config = ConfigDict(extra="forbid")

    heading: str | None = None
    subheading: str | None = None
    dates: str | None = None
    location: str | None = None
    text: str | None = None
    bullets: list[str] = []
    links: list[str] = []


# Mirrors `tool_spec.py`'s `_SECTION_SCHEMA` (and `jsa/schema/cv.py::Section` minus
# `id`), defaulted for the same sentinel-tolerance reason as `ChatEntry` above. `name`
# stays NULLABLE rather than `_SECTION_SCHEMA`'s `_STRING` so a missing name keeps
# landing on `CvWorkingCopy.replace_section`'s existing `bad_argument` rejection -- a
# per-op `rejected` entry the user now sees, not a turn-killing ValidationError.
class ChatSection(BaseModel):
    """One CV section. `replace_section` replaces the whole section, so restate every
    field, copying unchanged ones verbatim."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    text: str | None = None
    items: list[str] = []
    entries: list[ChatEntry] = []


# Mirrors `tool_spec.py`'s `_REPLACE_CONTACT` parameters and
# `CvWorkingCopy._CONTACT_FIELDS` — but this is the ONE model here whose fields are
# DEFAULTED, and `links` is nullable though its `_REPLACE_CONTACT` twin is not. Both
# are load-bearing. `replace_contact` is a PARTIAL update — "only the keys present are
# written, an omitted key leaves the existing value alone, so 'fix the email' never
# blanks the phone" (`jsa/schema/patch.py`). That contract needs a representable "not
# changing this" for every field which survives `cv_chat.py`'s
# `model_dump(exclude_none=True)`:
#   - Defaults keep an omitted key legal on backends that honour optionality, so the
#     prompt's "omit what you aren't changing" guidance stays true there.
#   - `links: list[str] | None` (not `list[str]`) because `turn_models.inline_defs`
#     rewrites EVERY property into `required` for Gemini's constrained decoder. A
#     non-nullable `links` would force the model to emit *something*, it would emit
#     `[]`, `exclude_none` does not drop `[]`, and every "fix the email" turn would
#     silently clear the user's links. With `None` available, `[]` keeps its honest
#     meaning: clear them, on purpose.
# Do not "align" this with `_REPLACE_CONTACT` by dropping the defaults or the `| None`
# on `links` — that spec is tuned for the native tool-call path, where a genuinely
# absent argument is expressible on the wire.
class ChatContact(BaseModel):
    """The identity fields. This is a PARTIAL update: give a value only for the
    fields you are changing and pass null for every other one. Passing an empty
    list for `links` CLEARS the existing links — pass null to leave them alone."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] | None = None


class CvChatOp(BaseModel):
    """One edit operation, addressed by the ``s1…sN``/``e1…eN`` ids from the turn's
    ``CvWorkingCopy.get_cv()`` dump (see the CV-editor AI chat plan's D1). Every field
    besides ``op`` is required-but-nullable (no default) so it appears in JSON
    Schema's ``required`` on a strict provider — the same shape ``CvTurn.payload``
    uses and for the same reason (a defaulted field is silently dropped from
    ``required`` by Pydantic)."""

    # `section`/`entry`/`contact` are typed models, NOT `dict[str, Any]`. Pydantic
    # renders a bare `dict[str, Any]` as `{"type": "object"}` with no `properties` at
    # all, and `inline_defs` then strips the `additionalProperties` that was the only
    # thing left in it. A constrained decoder handed an object schema that declares no
    # properties has exactly one valid completion — `{}` — so replace_contact/
    # replace_section/replace_entry came back with empty payloads on every structured
    # turn while the model's own `answer` text described the edit as done (confirmed
    # live against Gemini, 2026-09-16; `replace_summary`, whose argument is a typed
    # scalar, worked in the same turns). Same class of failure as commit `c51592d` and
    # as `inline_defs`' all-properties-required rewrite: a schema that is technically
    # valid but tells the decoder nothing. Do not "simplify" these back to dicts.

    model_config = ConfigDict(extra="forbid")

    op: CvChatOpName
    section_id: str | None
    entry_id: str | None
    position: int | None
    text: str | None
    bullets: list[str] | None
    order: list[str] | None
    section: ChatSection | None
    entry: ChatEntry | None
    contact: ChatContact | None

    @model_validator(mode="after")
    def _required_fields_present(self) -> "CvChatOp":
        for field in _REQUIRED_FIELDS[self.op]:
            if getattr(self, field) is None:
                raise ValueError(f"op {self.op!r} requires a non-null {field!r}")
        return self


class CvChatPayload(BaseModel):
    """The ``final`` turn's payload: a short user-facing summary plus the ops to
    apply. ``ops`` may be empty — "nothing worth changing here" is a legal final
    turn, not an error."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    ops: list[CvChatOp]


class CvChatTurn(BaseModel):
    """The CV-editor chat's structured reply envelope. Same ``{kind, question,
    payload, suggested_replies}`` shape as ``CvTurn``/``ClTurn`` (see the module
    docstring for why that shape is load-bearing, not cosmetic)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["question", "final"]
    question: str | None
    payload: CvChatPayload | None
    suggested_replies: list[str] | None

    @model_validator(mode="after")
    def _payload_iff_final(self) -> "CvChatTurn":
        if self.kind == "final":
            if self.payload is None:
                raise ValueError("kind='final' requires a non-null `payload`")
            if self.suggested_replies is not None:
                raise ValueError("kind='final' requires a null `suggested_replies`")
        else:
            if self.question is None:
                raise ValueError("kind='question' requires a non-null `question`")
        return self


def ops_for_scope(scope_type: ScopeType, *, has_entries: bool | None = None) -> tuple[CvChatOpName, ...]:
    """The op vocabulary for ``scope_type``, optionally narrowed by the scoped node's
    actual SHAPE as well as its type.

    ``has_entries=None`` means "shape unknown" and returns the type-only vocabulary —
    the pre-narrowing behaviour, kept so every existing caller and test is unaffected.

    Why the shape matters (live evidence, 2026-09-18): every op in
    ``_ENTRY_ADDRESSING_OPS`` can only name an entry that exists, because the
    ``e1…eN`` ids come from the turn's ``CvWorkingCopy.get_cv()`` dump. On a
    text-only section like ``Summary`` (``entries == []``) the dump contains no entry
    id at all, so those four ops are *unsatisfiable* — yet the type-only ``section``
    vocabulary still offered them. A weak model picks one and the whole turn dies on a
    cross-field ``ValidationError`` (``run_cv_chat`` has no self-heal budget by
    design): ``qwen/qwen3-30b-a3b-instruct-2507`` answered a "compact the summary"
    request with ``edit_entry_bullets`` + ``entry_id: null``. Narrowing the enum makes
    that unrepresentable on a structured backend rather than merely wrong.

    ``add_entry`` is deliberately NOT dropped. It addresses the *section*
    (``_REQUIRED_FIELDS["add_entry"] == ("section_id", "entry")``), never an entry id,
    so it stays satisfiable on an empty section and is a real capability — "turn this
    prose into entries" is a legitimate request. Dropping it would narrow capability,
    not just remove an impossible choice, which is not what this function is for.

    Sentinel-mode backends get no schema at all, so this narrowing simply does not
    apply to them; ``run_cv_chat``'s structural diff stays the authoritative guard for
    both modes (the plan's D5).
    """
    ops = _SCOPE_OPS[scope_type]
    if has_entries is False:
        ops = tuple(op for op in ops if op not in _ENTRY_ADDRESSING_OPS)
    return ops


def json_schema_for_cv_chat(
    scope_type: ScopeType, *, has_entries: bool | None = None
) -> dict[str, Any]:
    """The JSON schema a structured-output backend should enforce for a CV-editor
    chat turn scoped to ``scope_type`` — narrows ``CvChatOp.op``'s enum to the ops
    that scope allows (D5's provider-enforced layer; the AUTHORITATIVE enforcement
    is ``run_cv_chat``'s post-hoc structural diff, see the plan).

    ``has_entries`` narrows further, by the scoped node's shape — see
    ``ops_for_scope``. Omitting it preserves the exact pre-narrowing schema.
    """
    schema = CvChatTurn.model_json_schema()
    schema["$defs"]["CvChatOp"]["properties"]["op"]["enum"] = list(
        ops_for_scope(scope_type, has_entries=has_entries)
    )
    return schema


__all__ = [
    "ScopeType",
    "CvChatOpName",
    "ChatEntry",
    "ChatSection",
    "ChatContact",
    "CvChatOp",
    "CvChatPayload",
    "CvChatTurn",
    "ops_for_scope",
    "json_schema_for_cv_chat",
]
