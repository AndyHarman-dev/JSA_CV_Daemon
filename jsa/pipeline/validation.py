"""FINAL-payload parsing and stage-content validation.

Extracted from ``jsa/pipeline/stages.py`` (revision-tool-use plan, Phase 1) so that
``jsa/schema/patch.py``'s ``finalize()`` and ``jsa/pipeline/tool_loop.py`` can both
reach this gate without an import cycle: ``tool_loop.py`` needs it from the pipeline
layer, and ``stages.py`` re-exports every name below unchanged so existing call
sites and tests (``from jsa.pipeline.stages import FinalContentError, ...``) keep
working without modification.

This is the single, authoritative content gate for a stage's FINAL payload — used
by both the self-heal loop (``stages.py::_self_heal_final``, to decide whether to
re-prompt) and ``stages.py::_handle_final`` (the authoritative gate before any DB
write), and now also by the tool-patching loop's ``finalize`` op (``jsa/schema/
patch.py``), so a patched revision and a full-rewrite revision are held to exactly
the same schema.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from jsa.db.models import Job, Stage
# Submodule imports, not `from jsa.schema import ...` — jsa/schema/patch.py imports this
# module, and if jsa/schema/__init__.py ever imports patch.py, a package-level import
# here would risk a partial-import cycle depending on __init__.py's line order. A
# submodule import can't form that cycle regardless of ordering.
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument

logger = logging.getLogger(__name__)


class FinalContentError(ValueError):
    """Raised when a FINAL payload fails stage content validation.

    Subclasses ValueError so the existing _run_one handler still marks the job
    ``failed`` when correction attempts are exhausted. The self-heal loop catches
    it specifically to re-prompt the agent before giving up.
    """


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


def _capture_failed_payload(label: str, job_id: str | None, content: str, reason: str) -> None:
    """Best-effort dump of a FINAL payload that failed validation, for offline debugging.

    Pydantic truncates ``input_value`` in its error text, so the ``error`` column never
    holds the full payload. This writes the raw pre-validation FINAL to
    ``~/.jsa/debug/`` so a failure can be reproduced and the schema/serializer iterated
    offline. Never raises — debugging aid only.
    """
    if not job_id:
        return
    import os
    if "PYTEST_CURRENT_TEST" in os.environ:  # don't litter ~/.jsa during the test suite
        return
    try:
        debug_dir = Path.home() / ".jsa" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        path = debug_dir / f"{label}_{job_id}_{ts}.txt"
        path.write_text(f"# reason: {reason}\n\n{content}", encoding="utf-8")
    except Exception:  # pragma: no cover - diagnostics must never break the pipeline
        logger.debug("failed to capture FINAL payload for %s/%s", label, job_id, exc_info=True)


def _parse_structured(
    content: str,
    model: type[BaseModel],
    label: str,
    job_id: str | None = None,
    language: str = "en",
    *,
    structured: bool = False,
) -> BaseModel:
    """Parse a FINAL payload as JSON and validate it against ``model``.

    Raises ``FinalContentError`` (→ self-heal re-prompt, then job ``failed``) on invalid
    JSON or schema violation; returns the validated model instance otherwise. This
    replaces the old regex heuristics: a change-log, summary, mixed CV/CL payload, or
    third-person description cannot satisfy the schema, so it is rejected *structurally*
    rather than by pattern-matching.

    ``language`` is passed through as validation context (``{"language": language}``);
    only ``CVDocument``'s content-kind guard reads it (to pick the right per-language
    letter-formula pattern) — other models ignore the unused context key harmlessly.

    ``structured`` (default ``False``) selects only the trailing re-emit sentence's
    wording — the sentinel-mode text (``structured=False``) is byte-identical to before
    this parameter existed, since it is also the job's persisted ``error`` column on a
    hard fail that existing tests may pin. A structured-mode session never sees a
    sentinel-block instruction; the schema is enforced on the wire (forced tool-use /
    ``response_format``), not by prompt wording, so the trailing sentence only needs to
    tell the model to re-emit via its structured reply shape instead.
    """
    text = _strip_code_fence(content)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        _capture_failed_payload(label, job_id, content, f"invalid JSON: {exc}")
        reemit = (
            "Re-emit ONLY a single JSON object conforming to the schema as your "
            "structured reply's `payload`."
            if structured
            else "Re-emit ONLY a single JSON object conforming to the schema inside "
            "<<<FINAL>>>...<<<END>>>."
        )
        raise FinalContentError(
            f"{label} FINAL block was not valid JSON ({exc}). {reemit}"
        ) from exc
    try:
        return model.model_validate(data, context={"language": language})
    except ValidationError as exc:
        _capture_failed_payload(label, job_id, content, f"schema violation: {exc}")
        # Build a concise, model-facing reason from the validators' own messages. Never use
        # str(exc) here: pydantic embeds the entire input_value (the whole CV payload) per
        # error, which would echo the document back at the model in the self-heal correction.
        reasons = "; ".join(
            e.get("msg", "").removeprefix("Value error, ") for e in exc.errors()
        ) or "the payload did not conform to the schema"
        reemit = (
            "Re-emit a corrected JSON object as your structured reply's `payload`."
            if structured
            else "Re-emit a corrected JSON object inside <<<FINAL>>>...<<<END>>>."
        )
        raise FinalContentError(
            f"{label} JSON did not match the required schema: {reasons}. {reemit}"
        ) from exc


def _validate_final_content(
    stage: Stage, content: str, job: Job | None, language: str = "en", *, structured: bool = False
) -> BaseModel | None:
    """Parse + validate a FINAL payload for the given stage.

    Returns the validated structured object (``CVDocument`` / ``CoverLetter``) for the
    document stages, or ``None`` for stages without structured output (e.g.
    fit_assessment). Raises ``FinalContentError`` on invalid JSON / schema violation.
    Single source of truth used by the self-heal loop (to decide whether to re-prompt),
    ``_handle_final`` (the authoritative gate before any DB write), and the tool-patching
    loop's ``finalize`` op (``jsa/schema/patch.py``).

    ``job`` may be ``None`` — the tool-patching loop's applier validates a working copy
    outside any job/session context; only ``job.id`` (for the debug-dump filename) is
    read, and that read is itself ``None``-tolerant.

    ``language`` (default ``"en"``) is threaded through to ``_parse_structured`` for the
    ``CVDocument`` content-kind guard's per-language letter-formula matching.

    ``structured`` (default ``False``) is threaded through to ``_parse_structured`` for
    its mode-aware re-emit wording — both call sites (``_self_heal_final`` and
    ``_handle_final``) must pass the SAME value for a given stage invocation, or the
    self-heal loop's correction and the authoritative gate's hard-fail error would
    describe two different reply shapes.
    """
    job_id = job.id if job is not None else None
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        return _parse_structured(
            content, CVDocument, "cv_adjust", job_id, language, structured=structured
        )
    if stage in (Stage.cover_letter, Stage.revising_cl):
        return _parse_structured(
            content, CoverLetter, "cover_letter", job_id, language, structured=structured
        )
    return None


__all__ = [
    "FinalContentError",
    "_capture_failed_payload",
    "_parse_structured",
    "_strip_code_fence",
    "_validate_final_content",
]
