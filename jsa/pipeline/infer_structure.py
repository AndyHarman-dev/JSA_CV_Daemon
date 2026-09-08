"""Standalone (job-less) CV-structure inference for the CV Structure Editor.

Extracts text from an uploaded CV and asks the configured agent backend, in one shot, to
mirror it into a ``CVDocument`` JSON (prompt: ``PROMPT_INFER_STRUCTURE.md``). Progress for the
five UI steps is broadcast as ``infer_progress`` events; the validated ``CVDocument`` is
returned to the caller (the editor persists it on the user's "Done", not here).

This deliberately mirrors the one-shot shape of the ``fit_assessment`` stage
(``jsa/pipeline/stages.py::_run_fit_assessment``): a fresh ``start_session`` expecting a single
FINAL, no resume / awaiting-input path. It does NOT touch any Job, DB row, or job state.

Like every pipeline stage, this call runs in **structured mode** on a backend that can
enforce it (``jsa.schema.turn_models.InferTurn`` / ``json_schema_for_infer``) and falls back
to the sentinel grammar otherwise. The reply-handling below is mode-agnostic on purpose:
``AgentReply.content`` is the bare ``CVDocument`` JSON in both modes — the FINAL block's body
in sentinel mode, ``InferTurn.payload`` re-serialized in structured mode — so this function
needs no branch after ``start_session`` returns, and an OpenCode-Zen-style mid-session
structured→sentinel downgrade lands here as an ordinary reply rather than a parse failure.
Unlike the job pipeline there is no self-heal correction budget: a malformed reply is a
terminal ``InferError`` (HTTP 422) on the first try, in both modes, exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import ValidationError

from jsa.agents.base import AgentBackend, AgentLimitReached, AgentTimeout
from jsa.agents.protocol import ProtocolError, parse_reply
from jsa.events.schema import InferProgressEvent, event_to_dict
from jsa.ingest.cv_loader import load_cv
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.prompts import loader
from jsa.schema import CVDocument
from jsa.schema.turn_models import json_schema_for_infer

# The five steps surfaced in the editor's "inferring" checklist. The genuine work boundaries
# are step 1 (text extraction) and step 5 (schema validation); the single LLM call spans the
# middle steps, with step 4 ("Structuring JSON") active while the model is generating.
INFER_STEPS = (
    "Reading document",
    "Detecting section breaks",
    "Extracting entries & dates",
    "Structuring JSON",
    "Validating against schema",
)

Publish = Callable[[dict], Awaitable[None]]


class InferError(Exception):
    """The model output could not be parsed/validated into a CVDocument (→ HTTP 422)."""


def _strip_code_fence(text: str) -> str:
    """Remove a surrounding ```json ...``` (or plain ```) fence if the model added one."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


async def run_infer(
    backend: AgentBackend,
    cv_path: Path,
    *,
    task_id: str,
    publish: Publish,
) -> CVDocument:
    """Infer a ``CVDocument`` from the CV file at ``cv_path``. Raises ``InferError`` on bad
    model output (after emitting a terminal ``status="error"`` progress event)."""

    async def emit(step: int, status: str = "active", message: str = "") -> None:
        await publish(event_to_dict(InferProgressEvent(
            task_id=task_id, step=step, total=len(INFER_STEPS),
            label=INFER_STEPS[step - 1], status=status, message=message,
        )))

    # Step 1 — extract text (blocking pypdf/python-docx → off the event loop).
    await emit(1)
    try:
        cv_text = await asyncio.to_thread(load_cv, cv_path)
    except Exception as exc:
        await emit(1, status="error", message="could not read the file")
        raise InferError(f"could not read CV file: {exc}") from exc

    # Steps 2–4 — the single structuring call (step 4 is active during generation).
    await emit(2)
    await emit(3)
    await emit(4)
    # Deliberately NOT steered by the language preference: this structure is a skeleton
    # (headings/shape), not final deliverable prose, and it feeds cv_adjust — which DOES
    # apply the language directive (jsa/pipeline/stages.py) — so re-languaging it here
    # would be redundant. See the language-preference handoff, "Pipeline Integration" §4.
    #
    # Structured mode when the backend can enforce it, sentinel otherwise — the same
    # single-boolean shape `stages.py::_structured_schema_for` uses, deliberately
    # duplicated in three lines rather than imported: `stages.py` drags the repo, the
    # state machine and the renderers in with it, none of which belong on this job-less
    # path. `supports_structured_output` is read off the backend INSTANCE, never the
    # class — `OpenCodeGoBackend` sets it per-instance (its `/messages` models are
    # sentinel-only) and a class-level read would see `OpenAICompatBackend`'s inherited
    # `True` and be wrong for them. See CLAUDE.md → "Structured output (API backends)".
    schema = json_schema_for_infer() if backend.supports_structured_output else None
    # `language` is unused here: `document_only=True` suppresses the language directive
    # outright (this stage is deliberately not language-steered, see the note above), so
    # the value passed is inert. With `schema is None` this returns the prompt file's
    # text byte-for-byte, which is what keeps the sentinel path unchanged.
    system_prompt = assemble_system_prompt(
        loader.read_prompt("infer_structure"),
        language="en",
        structured_model=schema,
        document_only=True,
    )
    user_msg = f"CV TEXT:\n{cv_text}"
    # Conditional kwarg, mirroring `stages.py::_schema_kwargs`: a CLI backend accepts no
    # `structured_schema` parameter at all, so it must be omitted, not passed as None.
    start_kwargs = {"structured_schema": schema} if schema is not None else {}
    try:
        handle, reply = await backend.start_session(system_prompt, user_msg, **start_kwargs)
    except ProtocolError as exc:
        await emit(4, status="error", message="the model did not return a valid reply")
        raise InferError(f"agent returned no usable reply: {exc}") from exc
    except AgentTimeout as exc:
        await emit(4, status="error", message="request timed out — try again")
        raise InferError(str(exc)) from exc
    except AgentLimitReached as exc:
        await emit(4, status="error", message="rate limit reached — try again later")
        raise InferError(str(exc)) from exc
    except Exception as exc:
        await emit(4, status="error", message="backend error — try again")
        raise InferError(f"backend error: {exc}") from exc
    await backend.end_session(handle)

    # Step 5 — parse + schema-validate (the CVDocument hard gates apply here).
    await emit(5)
    text = _strip_code_fence(reply.content)
    try:
        data = json.loads(text)
        cv = CVDocument.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        reason = _concise_reason(exc)
        await emit(5, status="error", message=reason)
        raise InferError(reason) from exc

    await emit(len(INFER_STEPS), status="done")
    return cv


def _concise_reason(exc: Exception) -> str:
    """A short, user-facing reason — never the whole pydantic dump (it embeds the payload)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            e.get("msg", "").removeprefix("Value error, ") for e in exc.errors()
        ) or "the inferred structure did not match the CV schema"
    return f"the inferred structure was not valid JSON ({exc})"
