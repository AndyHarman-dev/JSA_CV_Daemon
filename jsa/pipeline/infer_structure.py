"""Standalone (job-less) CV-structure inference for the CV Structure Editor.

Extracts text from an uploaded CV and asks the configured agent backend, in one shot, to
mirror it into a ``CVDocument`` JSON (prompt: ``PROMPT_INFER_STRUCTURE.md``). Progress for the
five UI steps is broadcast as ``infer_progress`` events; the validated ``CVDocument`` is
returned to the caller (the editor persists it on the user's "Done", not here).

This deliberately mirrors the one-shot shape of the ``fit_assessment`` stage
(``jsa/pipeline/stages.py::_run_fit_assessment``): a fresh ``start_session`` expecting a single
FINAL, no resume / awaiting-input path. It does NOT touch any Job, DB row, or job state.
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
from jsa.prompts import loader
from jsa.schema import CVDocument

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
    system_prompt = loader.read_prompt("infer_structure")
    user_msg = f"CV TEXT:\n{cv_text}"
    try:
        handle, reply = await backend.start_session(system_prompt, user_msg)
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
