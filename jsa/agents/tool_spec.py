"""Revision-patching tool vocabulary + provider-shape renderers.

Part of the revision-tool-use plan (Phase 1). A patched revision lets the model edit
the stored CV/cover-letter document through a small set of tools instead of
re-emitting the whole artifact — see ``jsa/schema/patch.py`` for the working-copy
applier these tools drive, and ``jsa/pipeline/tool_loop.py`` (Phase 2) for the loop
that executes them.

Schemas here are **hand-written, flat, and $ref-free** — not derived from
``Entry.model_json_schema()``/``Section.model_json_schema()``, which pull in
``$defs``/``$ref``/``additionalProperties`` that some providers (Gemini) reject. This
makes ``jsa/schema/turn_models.py::inline_defs`` a safety net rather than something
these schemas depend on.

Scope is mechanically enforced by ``tools_for``: only ``Stage.revising_cv`` and
``Stage.revising_cl`` get any tools at all (D3 in the plan's locked decisions) —
``cv_adjust``/``cover_letter`` stay byte-identical to their pre-tool-use behavior.

Note on the plan's "imports only base.py" note for this module: enforcing D3
mechanically requires knowing which ``Stage`` is being asked for, so this module
imports ``jsa.db.models.Stage`` — a plain, dependency-free enum, not the ORM/session
machinery the name might suggest. This is the first import of ``jsa.db`` into
``jsa/agents/``; grep for `jsa.db` in this package before assuming otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsa.db.models import Stage

# --- vocabulary --------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One tool's name, human-facing description, and JSON Schema parameters.

    ``parameters`` is a plain JSON Schema object (``{"type": "object", ...}``) —
    already in the shape every provider's function/tool parameter field expects, so
    each renderer below only has to wrap it, never transform it.
    """

    name: str
    description: str
    parameters: dict[str, Any]


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """A flat ``{"type": "object", ...}`` JSON Schema — the shape every tool's
    ``parameters`` takes. ``additionalProperties: false`` is unconditional: these
    schemas are hand-written and never need to tolerate an unanticipated key the way
    ``jsa/schema/cv.py``'s tolerant document schemas do."""
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_NULLABLE_STRING = {"type": ["string", "null"]}
_STRING_ARRAY = {"type": "array", "items": _STRING}
_NULLABLE_INT = {"type": ["integer", "null"]}

# A CV entry, as accepted from the model — one job / degree / project / award. Mirrors
# jsa/schema/cv.py::Entry's fields exactly, minus "id" (server-issued, never accepted
# from the model — see jsa/schema/patch.py's defense-in-depth id-stripping).
_ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "heading": _NULLABLE_STRING,
        "subheading": _NULLABLE_STRING,
        "dates": _NULLABLE_STRING,
        "location": _NULLABLE_STRING,
        "text": _NULLABLE_STRING,
        "bullets": _STRING_ARRAY,
        "links": _STRING_ARRAY,
    },
    "required": ["heading", "subheading", "dates", "location", "text", "bullets", "links"],
    "additionalProperties": False,
}

# A CV section, as accepted from the model. Mirrors jsa/schema/cv.py::Section, minus
# "id".
_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _STRING,
        "text": _NULLABLE_STRING,
        "items": _STRING_ARRAY,
        "entries": {"type": "array", "items": _ENTRY_SCHEMA},
    },
    "required": ["name", "text", "items", "entries"],
    "additionalProperties": False,
}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=_obj(properties, required))


# CV tools ----------------------------------------------------------------------------

_GET_CV = _tool(
    "get_cv",
    "Return the current working CV as JSON, with a stable id on every section and "
    "entry. Call this first, and again after any structural change (add/remove/"
    "reorder) if you need fresh ids for a follow-up edit.",
    {},
    [],
)
_REPLACE_SUMMARY = _tool(
    "replace_summary",
    "Replace the CV's Summary/Profile section text. Creates a Summary section if one "
    "doesn't exist yet.",
    {"text": _STRING},
    ["text"],
)
_REPLACE_SECTION = _tool(
    "replace_section",
    "Replace an entire section (name, text, items, entries) by its id. Any entries "
    "in the section are replaced wholesale — their old ids stop working.",
    {"section_id": _STRING, "section": _SECTION_SCHEMA},
    ["section_id", "section"],
)
_REPLACE_ENTRY = _tool(
    "replace_entry",
    "Replace one entry's fields entirely, by its id. The entry stays in its current "
    "section and position.",
    {"entry_id": _STRING, "entry": _ENTRY_SCHEMA},
    ["entry_id", "entry"],
)
_EDIT_ENTRY_BULLETS = _tool(
    "edit_entry_bullets",
    "Replace only the bullet list of one entry, by its id, leaving every other field "
    "unchanged.",
    {"entry_id": _STRING, "bullets": _STRING_ARRAY},
    ["entry_id", "bullets"],
)
_ADD_ENTRY = _tool(
    "add_entry",
    "Insert a new entry into a section, by the section's id, at an optional "
    "0-based position (omit or null to append). Returns the new entry's id.",
    {"section_id": _STRING, "entry": _ENTRY_SCHEMA, "position": _NULLABLE_INT},
    ["section_id", "entry"],
)
_REMOVE_ENTRY = _tool(
    "remove_entry",
    "Remove one entry from its section, by its id. The id is retired and cannot be "
    "reused.",
    {"entry_id": _STRING},
    ["entry_id"],
)
_REORDER_ENTRIES = _tool(
    "reorder_entries",
    "Reorder the entries within one section, by its id. `order` must list every "
    "current entry id of that section exactly once, in the new order.",
    {"section_id": _STRING, "order": _STRING_ARRAY},
    ["section_id", "order"],
)

CV_TOOL_SPECS: tuple[ToolSpec, ...] = (
    _GET_CV,
    _REPLACE_SUMMARY,
    _REPLACE_SECTION,
    _REPLACE_ENTRY,
    _EDIT_ENTRY_BULLETS,
    _ADD_ENTRY,
    _REMOVE_ENTRY,
    _REORDER_ENTRIES,
)

# Cover-letter tools --------------------------------------------------------------------

_GET_LETTER = _tool(
    "get_letter",
    "Return the current working cover letter as JSON, with a stable id on every "
    "paragraph. Call this first, and again after any structural change (add/remove/"
    "reorder) if you need fresh ids for a follow-up edit.",
    {},
    [],
)
_REPLACE_PARAGRAPH = _tool(
    "replace_paragraph",
    "Replace one paragraph's text entirely, by its id.",
    {"paragraph_id": _STRING, "text": _STRING},
    ["paragraph_id", "text"],
)
_ADD_PARAGRAPH = _tool(
    "add_paragraph",
    "Insert a new paragraph at an optional 0-based position (omit or null to "
    "append). Returns the new paragraph's id.",
    {"text": _STRING, "position": _NULLABLE_INT},
    ["text"],
)
_REMOVE_PARAGRAPH = _tool(
    "remove_paragraph",
    "Remove one paragraph, by its id. The id is retired and cannot be reused.",
    {"paragraph_id": _STRING},
    ["paragraph_id"],
)
_REORDER_PARAGRAPHS = _tool(
    "reorder_paragraphs",
    "Reorder the letter's paragraphs. `order` must list every current paragraph id "
    "exactly once, in the new order.",
    {"order": _STRING_ARRAY},
    ["order"],
)
_SET_SALUTATION = _tool(
    "set_salutation",
    "Set or clear the letter's salutation line (e.g. \"Dear Hiring Manager,\"). Pass "
    "null to remove it.",
    {"text": _NULLABLE_STRING},
    ["text"],
)
_SET_SIGNOFF = _tool(
    "set_signoff",
    "Set or clear the letter's sign-off (e.g. \"Sincerely,\\nJane Doe\"). Pass null to "
    "remove it.",
    {"text": _NULLABLE_STRING},
    ["text"],
)

CL_TOOL_SPECS: tuple[ToolSpec, ...] = (
    _GET_LETTER,
    _REPLACE_PARAGRAPH,
    _ADD_PARAGRAPH,
    _REMOVE_PARAGRAPH,
    _REORDER_PARAGRAPHS,
    _SET_SALUTATION,
    _SET_SIGNOFF,
)

# Shared terminal tools -----------------------------------------------------------------

_FINALIZE = _tool(
    "finalize",
    "End the revision and commit your edits. `change_log` is a short, user-facing "
    "summary of what changed — it is logged, never rendered into the document.",
    {"change_log": _STRING},
    ["change_log"],
)
_ASK_USER = _tool(
    "ask_user",
    "Ask the user a clarifying question instead of finalizing. Any edits made so far "
    "this turn are discarded — the user is answering against the last committed "
    "version, not a half-finished draft.",
    {"question": _STRING, "suggested_replies": _STRING_ARRAY},
    ["question"],
)

SHARED_TOOL_SPECS: tuple[ToolSpec, ...] = (_FINALIZE, _ASK_USER)


def tools_for(stage: Stage) -> tuple[ToolSpec, ...]:
    """The tool vocabulary for ``stage``.

    Raises ``ValueError`` for any stage other than ``revising_cv``/``revising_cl`` —
    the single mechanical enforcement of the plan's D3 ("scope is revising_cv and
    revising_cl ONLY"). ``cv_adjust``/``cover_letter`` must never be handed tools.
    """
    if stage is Stage.revising_cv:
        return CV_TOOL_SPECS + SHARED_TOOL_SPECS
    if stage is Stage.revising_cl:
        return CL_TOOL_SPECS + SHARED_TOOL_SPECS
    raise ValueError(f"no tool vocabulary for stage {stage!r} (tools are revision-only)")


# --- provider renderers --------------------------------------------------------------


def to_anthropic_tools(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    """Anthropic Messages API tool-definition shape: ``input_schema``."""
    return [
        {"name": s.name, "description": s.description, "input_schema": s.parameters}
        for s in specs
    ]


def to_openai_tools(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    """OpenAI-compatible ``/chat/completions`` tool-definition shape:
    ``{"type": "function", "function": {...}}`` — used by the ``_openai_compat.py``
    base (mistral, openrouter, opencode-go's ``/chat`` protocol) and by
    ``opencode_zen.py``'s independent copy."""
    return [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in specs
    ]


def to_gemini_function_declarations(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    """Gemini ``functionDeclarations`` shape.

    Applies **two** transforms, both required because Gemini's
    ``functionDeclarations.parameters`` is a restricted OpenAPI-3.0 subset rather
    than full JSON Schema (the same family of restriction
    ``turn_models.py::inline_defs`` works around for the structured-output
    schemas). The schemas above have no ``$ref``/``$defs``, so inlining is not
    among them.

    1. **Strip ``additionalProperties``** — not a member of that subset.
    2. **Rewrite union types to ``nullable``** — ``_NULLABLE_STRING`` and
       ``_NULLABLE_INT`` are JSON Schema's ``{"type": ["string", "null"]}`` form,
       which every other renderer on this module takes verbatim. OpenAPI 3.0 has
       no union ``type``: it is a single value plus a sibling ``nullable: true``.

    Transform 2 is not cosmetic and must not be dropped as a simplification. Left
    unconverted, all 17 nullable properties in the revision vocabulary go out as
    array-typed ``type``, Gemini answers ``400 INVALID_ARGUMENT``, and
    ``gemini_api.py``'s ``_permanent_4xx`` correctly classifies that as
    ``_ToolsRejected`` — so ``tool_loop.py`` downgrades native -> prompt on
    **every single gemini revision**. The failure is invisible: it presents as a
    working rung-2 degrade, not as an error, and the native rung is simply never
    exercised on this backend.

    Fixed here, in the gemini-local renderer, rather than by changing
    ``_NULLABLE_STRING``/``_NULLABLE_INT`` themselves — those feed
    ``to_anthropic_tools`` and ``to_openai_tools`` too, where the union form is
    valid and already covered by tests.

    Not verified against the live Gemini API (that needs a key and a real call).
    The evidence is the shape asymmetry: the structured-output path reaches this
    provider through ``anyOf``, a form confirmed accepted, while array-typed
    ``type`` is a form nothing in this repo has ever put on a Gemini wire.
    """
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": _to_gemini_schema(s.parameters),
        }
        for s in specs
    ]


def _to_gemini_schema(node: Any) -> Any:
    """Recursively strip ``additionalProperties`` and rewrite ``{"type": [T, "null"]}``
    into ``{"type": T, "nullable": True}``. See the caller's docstring for why."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "additionalProperties":
                continue
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                # Only the nullable-scalar shape this module actually emits is
                # convertible. A genuine multi-type union has no OpenAPI 3.0
                # equivalent, so fail loudly rather than silently sending
                # something Gemini will reject at request time.
                if len(non_null) != 1:
                    raise ValueError(
                        f"cannot render union type {v!r} as a Gemini schema: "
                        "OpenAPI 3.0 allows exactly one type plus 'nullable'"
                    )
                out["type"] = non_null[0]
                if len(non_null) != len(v):
                    out["nullable"] = True
                continue
            out[k] = _to_gemini_schema(v)
        return out
    if isinstance(node, list):
        return [_to_gemini_schema(v) for v in node]
    return node


__all__ = [
    "ToolSpec",
    "CV_TOOL_SPECS",
    "CL_TOOL_SPECS",
    "SHARED_TOOL_SPECS",
    "tools_for",
    "to_anthropic_tools",
    "to_openai_tools",
    "to_gemini_function_declarations",
]
