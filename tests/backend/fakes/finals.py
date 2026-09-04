"""Shared FINAL-reply factories emitting schema-valid JSON payloads.

The cv_adjust / cover_letter stages now require JSON conforming to ``jsa.schema``. These
helpers produce valid payloads for ``FakeAgentBackend`` scripts so pipeline tests exercise
the real validation + serialization path. ``marker`` text is embedded in the CV summary so
callers can assert it survives into the serialized Markdown (assertions use substrings).
"""

from __future__ import annotations

import json

from jsa.agents.base import AgentReply


def _wrap(payload: str) -> AgentReply:
    return AgentReply(raw=f"<<<FINAL>>>\n{payload}\n<<<END>>>", content=payload, kind="final")


def cv_final(marker: str = "Adjusted CV", name: str = "Jane Doe") -> AgentReply:
    """A valid cv_adjust FINAL (CV JSON).

    Uses the tolerant uniform section shape (``name`` + ``entries`` with ``role``/
    ``company`` aliases) the schema normalizes — mirroring real model output rather than
    the schema's post-normalization canonical keys.
    """
    return _wrap(json.dumps({
        "contact": {"name": name, "email": "jane.doe@example.com", "phone": "+1-555-867-5309"},
        "sections": [
            {"name": "Summary", "text": f"{marker}: senior engineer with eight years of experience."},
            {"name": "Experience", "entries": [
                {"role": "Senior Engineer", "company": "Acme", "dates": "2019–present",
                 "bullets": ["Built a distributed payment pipeline", "Led a service migration"]},
            ]},
        ],
    }))


def cl_final(body: str | None = None) -> AgentReply:
    """A valid cover_letter FINAL (cover-letter JSON)."""
    body = body or (
        "I am excited to apply because your mission resonates with my four years of "
        "shipping production systems and developer tooling."
    )
    return _wrap(json.dumps({
        "salutation": "Dear Hiring Manager,",
        "paragraphs": [body, "I am confident my background aligns well with what your team needs."],
        "signoff": "Sincerely,\nCandidate Name",
    }))


def fit_reply() -> AgentReply:
    """A passing fit-assessment verdict (plain text, not JSON)."""
    return AgentReply(raw="<<<FINAL>>>\nFIT\n<<<END>>>", content="FIT", kind="final")


def tool_loop_miss() -> AgentReply:
    """A throwaway FINAL reply that jsa/pipeline/tool_loop.py's PROMPT rung cannot
    parse as a ``<<<TOOL_CALLS>>>`` block (``kind`` is ``"final"``, not
    ``"tool_calls"``).

    Since the revision-tool-use plan's Phase 5, ``run_stage`` always attempts
    ``run_tool_loop`` first for a ``revising_cv``/``revising_cl`` job whose latest
    Document already carries a ``.structured`` payload — which any job that
    completed a REAL ``run_stage(..., Stage.cv_adjust`` or ``Stage.cover_letter, ...)``
    call does (as opposed to a hand-inserted ``Document`` row with no ``structured``
    field, which skips the tool loop entirely). A scripted backend with the default
    ``supports_native_tools=False`` therefore enters the PROMPT rung, which consumes
    ONE scripted reply attempting to parse it as a tool call; this reply always fails
    that parse, so the loop gives up and `run_stage` falls through to rung 3 (today's
    unmodified full-document-rewrite path), which then consumes the NEXT scripted
    reply. Any test scripting a revision turn against a job with a real prior
    ``structured`` Document must prepend one of these before the "real" reply.
    """
    return cv_final("tool-loop miss — ignored, rung 3 follows")
