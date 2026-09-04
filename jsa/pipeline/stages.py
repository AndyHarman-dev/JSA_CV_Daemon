"""Pipeline stage runners: run_stage.

Handles both fresh sessions (pending/cv_done) and resumed sessions
(awaiting_input, revising_cv, revising_cl).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jsa.agents.base import AgentBackend, AgentReply, HistoryTurn, OnChunk, OnRetry, SessionHandle
from jsa.agents.protocol import ProtocolError
from jsa.agents.tool_spec import ToolSpec
from jsa.agents.tool_spec import tools_for as _tool_specs_for_stage
from jsa.pipeline.tool_loop import ToolLoopResult, run_tool_loop
from jsa.render.registry import renderer_for
from jsa.render.serialize import cover_letter_to_markdown, cv_to_markdown
from jsa.schema import CVDocument, CoverLetter, cv_has_summary
from jsa.db import repo
from jsa.util import slugify as _slugify
from jsa.db.models import (
    Document,
    FollowUp,
    Job,
    JobState,
    Message,
    RevisionRequest,
    Stage,
)
from jsa.events.bus import bus
from jsa.events.schema import (
    FollowUpNeededEvent,
    LogEvent,
    StageCompleteEvent,
    StatusChangedEvent,
    TranscriptChangedEvent,
    event_to_dict,
)
from jsa.pipeline.checkpoints import checkpoint
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.pipeline.streaming import ChunkAccumulator
# Content-validation gate for a stage's FINAL payload. Extracted to
# jsa/pipeline/validation.py (revision-tool-use plan, Phase 1) so jsa/schema/patch.py's
# finalize() and jsa/pipeline/tool_loop.py can reach it without an import cycle
# (tool_loop.py importing stages.py while stages.py imports tool_loop.py). Re-exported
# here unchanged so every existing call site and test in this module keeps working.
from jsa.pipeline.validation import (
    FinalContentError,
    _capture_failed_payload,  # noqa: F401 - re-exported, not called in this module
    _parse_structured,  # noqa: F401 - re-exported, not called in this module
    _strip_code_fence,  # noqa: F401 - re-exported, not called in this module
    _validate_final_content,
)
from jsa.prompts import loader
from jsa.schema.turn_models import adapt_history, json_schema_for
from jsa.store import cv_structure as cv_structure_store
from jsa.store import preferences as preferences_store

logger = logging.getLogger(__name__)


class PausedForInput(Exception):
    """Raised when run_stage parks a job to awaiting_input.

    The job's state has already been checkpointed before this is raised.
    The orchestrator catches this silently — it is control flow, not an error.
    """


class StaleJobResult(Exception):
    """Raised when the job's DB state changed (dismiss/cancel/delete) while the
    agent was working, so the just-produced reply is stale and must be
    discarded rather than checkpointed.

    The orchestrator worker holds its Job ORM object in memory for the whole,
    potentially slow, agent turn. If another request (e.g. dismiss) commits a
    state change on a separate session in the meantime, writing this reply's
    checkpoint would resurrect the job — see checkpoint()'s in-memory-only
    transition() guard in jsa/db/repo.py. No rows are written when this fires;
    the orchestrator catches it silently, like PausedForInput.
    """


_RENDER_FILENAMES = {Stage.cv_adjust: "cv", Stage.cover_letter: "cover_letter"}


async def _render(
    session: AsyncSession,
    job: Job,
    output_dir: Path,
    stages: tuple[Stage, ...] = (Stage.cv_adjust, Stage.cover_letter),
) -> None:
    """Render PDF+DOCX for the latest Document of each given stage, persisting paths.

    A stage with no Document yet is skipped rather than erroring — e.g. at the CV gate
    only ``cv_adjust`` has one; at final review both do.
    """
    docs: dict[Stage, Document] = {}
    for st in stages:
        st_docs = await repo.get_documents(session, job.id, st)
        if st_docs:
            docs[st] = st_docs[0]  # version-desc, so [0] is latest
    if not docs:
        return

    slug = f"{_slugify(job.company)}_{_slugify(job.role)}_{job.id[:8]}"
    out = output_dir / slug
    out.mkdir(parents=True, exist_ok=True)

    pdf_r  = renderer_for("weasyprint")
    docx_r = renderer_for("docx")

    render_tasks = []
    doc_paths: dict[Stage, tuple[Path, Path]] = {}
    for st, doc in docs.items():
        name = _RENDER_FILENAMES[st]
        pdf_path = out / f"{name}.pdf"
        docx_path = out / f"{name}.docx"
        render_tasks.append(pdf_r.render(doc.markdown, pdf_path))
        render_tasks.append(docx_r.render(doc.markdown, docx_path))
        doc_paths[st] = (pdf_path, docx_path)

    await asyncio.gather(*render_tasks)

    for st, doc in docs.items():
        pdf_path, docx_path = doc_paths[st]
        doc.pdf_path = str(pdf_path)
        doc.docx_path = str(docx_path)
        session.add(doc)
    await session.commit()


async def _render_for_review(session: AsyncSession, job: Job, output_dir: Path) -> None:
    """Render both PDF and DOCX for the CV and cover-letter documents and persist paths."""
    await _render(session, job, output_dir, stages=(Stage.cv_adjust, Stage.cover_letter))


async def _render_cv(session: AsyncSession, job: Job, output_dir: Path) -> None:
    """Render PDF and DOCX for the cv_adjust document only — the CV gate."""
    await _render(session, job, output_dir, stages=(Stage.cv_adjust,))


def _schema_kwargs(schema: dict | None) -> dict:
    """The ``structured_schema=`` kwarg dict for a start_session/restore_session call.

    Empty when ``schema`` is ``None`` (sentinel mode) so CLI backends — which accept no
    such parameter, per the contract on ``AgentBackend.supports_structured_output`` —
    are never handed a kwarg they don't declare. Centralizes the
    ``{"structured_schema": schema} if schema is not None else {}`` idiom that was
    previously repeated at every call site.
    """
    return {"structured_schema": schema} if schema is not None else {}


def _streaming_kwargs(
    backend: AgentBackend, on_chunk: OnChunk | None, on_retry: OnRetry | None = None
) -> dict:
    """The ``on_chunk=``/``on_retry=`` kwarg dict for a start_session/send_message
    call.

    Empty unless the backend declares ``supports_streaming`` AND a callback is
    actually in hand — mirrors ``_schema_kwargs``'s conditional-kwarg idiom, so a
    non-streaming backend (e.g. google-cli) is never handed a kwarg it doesn't
    declare. ``on_retry`` rides along with ``on_chunk`` unconditionally (never
    omitted on its own) — every backend that accepts ``on_chunk`` must also
    accept ``on_retry`` per the ``supports_streaming`` contract (see
    ``jsa/agents/base.py``'s ``OnRetry`` docstring), even if it never calls it.
    """
    if on_chunk is None or not getattr(backend, "supports_streaming", False):
        return {}
    return {"on_chunk": on_chunk, "on_retry": on_retry}


def _on_retry_for(accumulator: ChunkAccumulator | None) -> OnRetry | None:
    """Build the ``on_retry`` callback a backend calls right before replaying a
    whole turn on the SAME logical request (a sentinel-nudge retry, or an
    in-backend transient-HTTP retry) — see ``jsa/agents/base.py``'s ``OnRetry``
    docstring. Bound to ``ChunkAccumulator.end_turn(superseded=True)``, which
    force-flushes (so any already-streamed partial reaches the frontend) AND
    marks it discardable AND clears the buffer for the next attempt — a single
    call satisfies both halves of the plan's "reset hook" and "superseded
    signal" requirements at once.
    """
    if accumulator is None:
        return None

    async def _supersede() -> None:
        await accumulator.end_turn(superseded=True)

    return _supersede


def _structured_schema_for(backend: AgentBackend, stage: Stage) -> dict | None:
    """The JSON schema to enforce for ``stage`` on ``backend``, or ``None``.

    ``None`` covers both "this backend cannot enforce structured output" (CLI backends;
    ``supports_structured_output`` is a hard-coded per-backend ClassVar, see
    ``jsa/agents/base.py``) and, by construction, "sentinel mode" everywhere downstream
    — every call site in this module treats ``schema is not None`` as the single
    destination-mode boolean (the structured-output plan's advisor-locked invariant:
    one boolean drives the ``structured_schema`` kwarg, ``adapt_history``, the self-heal
    wording, and the wire-level retry budget alike).
    """
    return json_schema_for(stage) if backend.supports_structured_output else None


def _tools_for(backend: AgentBackend, stage: Stage) -> tuple[ToolSpec, ...] | None:
    """The tool vocabulary to offer for ``stage``, or ``None``.

    Only ``Stage.revising_cv``/``Stage.revising_cl`` ever get tools (the
    revision-tool-use plan's D3) — this is the single mechanical enforcement that
    ``cv_adjust``, ``cover_letter``, and ``fit_assessment`` NEVER get tools, mirroring
    how ``_structured_schema_for`` above is the single mechanical source for its own
    destination-mode decision.

    ``backend`` gates the SECOND, independent precondition: at least one of the two tool
    rungs has to be reachable at all. WHICH rung actually runs, and any native -> prompt
    downgrade between them, stays ``jsa/pipeline/tool_loop.py``'s decision — this
    function only rules out a backend where NEITHER can work:

    * ``supports_native_tools`` -> rung 1 is available. Read off the INSTANCE, never the
      class — see ``_tools_kwargs`` below for the ``OpenCodeGoBackend`` trap.
    * ``restore_applies_system_prompt`` -> rung 2 is available. The prompt rung's ONLY
      transport is a system prompt carrying the tool contract, and ``claude-cli`` /
      ``google-cli`` resume a provider-held conversation by id, discarding the
      ``system_prompt`` and ``history`` handed to ``restore_session`` outright (and
      passing no ``--system-prompt`` on the resume). Entering the loop there is not
      merely useless, it is harmful: the model never sees the contract, answers an
      ordinary ``<<<FINAL>>>`` that the loop discards as unparseable, and rung 3 then
      re-sends the SAME instruction into a persisted conversation that now contains it
      twice.

    Both flags False therefore means "skip the loop entirely, go straight to rung 3",
    which is byte-for-byte the pre-tool-use behavior for those two backends. Do not
    "simplify" this back to a stage-only check — the plan named ``claude-cli`` as its
    prompt-rung verification target before this precondition was discovered.
    """
    if stage not in (Stage.revising_cv, Stage.revising_cl):
        return None
    native = getattr(backend, "supports_native_tools", False)
    prompt_rung = getattr(backend, "restore_applies_system_prompt", True)
    if not native and not prompt_rung:
        return None
    return _tool_specs_for_stage(stage)


def _tools_kwargs(backend: AgentBackend, tools: tuple[ToolSpec, ...] | None) -> dict:
    """The ``tools=`` kwarg dict for a native-tool-mode backend call, or ``{}``.

    Same conditional-kwarg idiom as ``_schema_kwargs``: empty unless tools were
    actually requested (``tools is not None``) AND the backend can speak native
    tool-calling.

    ``getattr(backend, "supports_native_tools", False)`` MUST read off the backend
    INSTANCE (the ``backend`` parameter), never off the class — ``OpenCodeGoBackend``
    proxies two different wire protocols under one backend name and sets
    ``supports_native_tools`` as an INSTANCE attribute in ``__init__`` (forced tool-use
    does not take on its ``/messages`` protocol path). A class-level read
    (``type(backend).supports_native_tools`` / ``OpenCodeGoBackend.supports_native_tools``)
    would silently see the inherited ``AgentBackend`` ClassVar default and be WRONG for
    half its models — see CLAUDE.md's identical trap documented for the sibling flag
    ``supports_structured_output``. ``backend`` here is always a real instance, so the
    plain ``getattr`` below is correct as written; do not "simplify" it to a class read.
    """
    if tools is None or not getattr(backend, "supports_native_tools", False):
        return {}
    return {"tools": tools}


async def _log_session_mode(
    job: Job,
    stage: Stage,
    schema: dict | None,
    handle: SessionHandle,
    *,
    tools: tuple[ToolSpec, ...] | None = None,
    tool_mode: str | None = None,
) -> None:
    """Log the session's actual mode once, right after it is established.

    Logged AFTER the call that establishes it (start_session, or restore_session +
    its first send_message) rather than from ``schema`` alone — OpenCode Zen's
    per-session downgrade means a schema was requested but the session may already be
    sentinel-only by the time this runs, and only the handle (not the schema argument)
    reflects that outcome. ``getattr(..., True)`` defaults a handle with no
    ``structured_enabled`` attribute (AnthropicSessionHandle, any CLI SessionHandle) to
    "not downgraded" — the only backend that can downgrade is OpenCode Zen, and it
    always sets the attribute.

    ``tools``/``tool_mode`` (both default ``None``, so every pre-existing call site is
    unaffected) name the tool-patching ladder's outcome for a revision stage:
    ``tool_mode`` is the ACTUAL rung ``run_tool_loop`` used (``"native"``/``"prompt"``,
    from ``ToolLoopResult.mode``) when the loop produced a terminal reply, or ``None``
    when the loop gave up and rung 3 (full-document rewrite) ran instead. This is
    deliberately NOT derived from ``_tools_kwargs``/``backend.supports_native_tools``
    here — the ladder can downgrade native → prompt mid-turn, so only the loop's own
    reported outcome is truthful about which rung actually ran.

    When ``tool_mode`` is given (the loop succeeded), the base sentinel/structured/
    downgraded computation below is skipped: a tool-mode ``restore_session`` call never
    receives a ``structured_schema`` (tool mode and structured mode are mutually
    exclusive per request — see CLAUDE.md), so a tool handle's own
    ``structured_enabled`` reflects nothing about the opencode-zen downgrade this
    check exists for. The stage's underlying structured capability (``schema is not
    None``) still names the base label.
    """
    if tool_mode is not None:
        base = "structured" if schema is not None else "sentinel"
        mode = f"{base} + tools ({tool_mode})"
    else:
        if schema is None:
            mode = "sentinel"
        elif getattr(handle, "structured_enabled", True) is False:
            mode = "sentinel (downgraded)"
        else:
            mode = "structured"
        if tools is not None:
            # Tools were offered for this stage but the loop gave up on both rungs —
            # rung 3 (the ACTUAL session `handle` reflects) ran instead.
            mode = f"{mode} (tools downgraded)"
    await bus.publish(
        event_to_dict(
            LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: session mode = {mode}")
        )
    )


async def _publish_transcript_changed(job: Job) -> None:
    """Invalidation-only hint: Messages/FollowUps/Documents/RevisionRequests changed
    for this job. Emit after every checkpoint() call that writes any of those rows
    (see CLAUDE.md / the transcript-projection plan for the full call-site list)."""
    await bus.publish(event_to_dict(TranscriptChangedEvent(job_id=job.id)))


# How many times to re-prompt the same session when a FINAL block fails content
# validation before giving up and letting the job fail.
MAX_FINAL_CORRECTIONS = 2

_CV_CORRECTION = (
    "Your previous <<<FINAL>>> block was not a valid CV JSON object. Re-emit now: put ONLY "
    "a single JSON object conforming to the CV schema inside one <<<FINAL>>>...<<<END>>> "
    "block — a `contact` object (name plus email and/or phone) and an ordered `sections` "
    "array mirroring the base CV's sections. No Markdown, no prose, no change-log, no "
    "commentary inside the block, and do not wrap the JSON in code fences."
)
_CL_CORRECTION = (
    "Your previous <<<FINAL>>> block was not a valid cover-letter JSON object. Re-emit now: "
    "put ONLY a single JSON object conforming to the cover-letter schema inside one "
    "<<<FINAL>>>...<<<END>>> block — an optional `salutation`, a `paragraphs` array holding "
    "the letter's body paragraphs, and an optional `signoff`. No Markdown, no commentary, "
    "and do not wrap the JSON in code fences."
)

# Soft nudge (not a hard failure): the CV validated fine but has no summary/profile section.
# The user wants a summary to always appear; the model can always write one from the CV's own
# content, so we re-prompt for it — but a CV that still lacks one ships as a slightly-thin CV
# rather than failing the job (a missing summary is thinness, not corruption).
_CV_SUMMARY_NUDGE = (
    "Your CV JSON is valid but is missing a Summary section. Re-emit the SAME CV with one "
    "change: add a section named \"Summary\" to `sections`, with a `text` value holding a "
    "2–3 sentence professional summary tailored to this role and drawn from the candidate's "
    "own experience (do not invent facts). If a base CV structure was provided, insert it "
    "wherever that structure placed (or would place) a Summary; otherwise put it first. Keep "
    "everything else, including the order of every other section, identical. Emit the "
    "complete CV JSON object inside one <<<FINAL>>>...<<<END>>> block — no Markdown, no "
    "commentary, no code fences."
)

# Structured-mode counterparts of the three sentinel-worded texts above. A structured
# session's shape is enforced on the wire (forced tool-use / response_format), not by
# prompt text, so these describe the {kind, payload} reply shape instead of a sentinel
# block — never mention <<<FINAL>>>/<<<END>>> to a session that was told those markers
# "don't apply this session" (see prompt_assembly.py's structured contract).
_CV_CORRECTION_STRUCTURED = (
    "Your previous reply's `payload` was not a valid CV object. Re-emit now: a JSON object "
    "with `kind: \"final\"` and a `payload` conforming to the CV schema — a `contact` "
    "object (name plus email and/or phone) and an ordered `sections` array mirroring the "
    "base CV's sections. Leave `question` null. No prose or commentary outside the JSON "
    "object."
)
_CL_CORRECTION_STRUCTURED = (
    "Your previous reply's `payload` was not a valid cover-letter object. Re-emit now: a "
    "JSON object with `kind: \"final\"` and a `payload` conforming to the cover-letter "
    "schema — an optional `salutation`, a `paragraphs` array holding the letter's body "
    "paragraphs, and an optional `signoff`. Leave `question` null."
)
_CV_SUMMARY_NUDGE_STRUCTURED = (
    "Your CV `payload` is valid but is missing a Summary section. Re-emit the SAME CV with "
    "one change: add a section named \"Summary\" to `sections`, with a `text` value holding "
    "a 2–3 sentence professional summary tailored to this role and drawn from the "
    "candidate's own experience (do not invent facts). If a base CV structure was provided, "
    "insert it wherever that structure placed (or would place) a Summary; otherwise put it "
    "first. Keep everything else, including the order of every other section, identical. "
    "Emit a JSON object with `kind: \"final\"` and the complete CV as `payload`; leave "
    "`question` null."
)

# Wire-level correction: a structured backend's reply couldn't even be parsed into the
# {kind, question, payload} union (invalid JSON, missing/invalid `kind`) — distinct from
# the semantic corrections above, which fire on a well-formed-but-invalid `payload`. Used
# only by _send_message_with_wire_retry, never nested inside the semantic self-heal loop
# (see that function's docstring for why nesting the two budgets is deliberately avoided).
_STRUCTURED_WIRE_CORRECTION = (
    "Your previous reply could not be parsed as a valid structured object — it must be a "
    "single JSON object with a `kind` field (`\"question\"` or `\"final\"`) plus "
    "`question`/`payload` set accordingly. Re-emit a corrected reply now, answering the "
    "same request as before:\n\n{original}"
)


async def _self_heal_final(
    *,
    backend: AgentBackend,
    handle: SessionHandle,
    stage: Stage,
    job: Job,
    reply: AgentReply,
    accumulated_messages: list[dict],
    language: str = "en",
    structured: bool = False,
    on_chunk: OnChunk | None = None,
    accumulator: ChunkAccumulator | None = None,
) -> tuple[AgentReply, list[dict]]:
    """Re-prompt the open session when a FINAL block fails content validation.

    Robustness layer for when the model ignores the prompt and emits a change-log,
    "task complete" summary, or file-write announcement instead of the artifact. On
    each failure we send a precise correction on the *same* session (preserving full
    context) and re-validate, up to MAX_FINAL_CORRECTIONS times.

    ``structured`` (default ``False``) selects the sentinel- vs structured-worded
    correction texts and is threaded into ``_validate_final_content`` so its
    ``FinalContentError`` message matches. This is a purely SEMANTIC budget — a
    well-formed reply whose `payload`/content fails CV/CL schema validation. A
    wire-level failure (the reply couldn't even be parsed into {kind, payload} at all)
    is a different budget, handled before this function ever runs — see
    _send_message_with_wire_retry / _start_session_with_retry. If ``backend.send_message``
    below raises ProtocolError (a wire-level failure surfacing mid-correction), it is
    deliberately NOT caught here and propagates — nesting the wire budget inside the
    semantic budget is the "self-heal double-application" this plan's Problems/Bugs
    section calls out; one budget per turn.

    Returns the (possibly updated) reply and accumulated messages. The caller then
    dispatches normally: a recovered FINAL proceeds; a correction that turns into
    NEED_INPUT parks for the user; a still-invalid FINAL falls through to
    _handle_final, whose validation raises and fails the job (after auto-retries).
    """
    if stage not in (Stage.cv_adjust, Stage.revising_cv, Stage.cover_letter, Stage.revising_cl):
        return reply, accumulated_messages

    is_cv_stage = stage in (Stage.cv_adjust, Stage.revising_cv)
    if structured:
        correction = _CV_CORRECTION_STRUCTURED if is_cv_stage else _CL_CORRECTION_STRUCTURED
    else:
        correction = _CV_CORRECTION if is_cv_stage else _CL_CORRECTION

    async def _reprompt(message: str, label: str) -> None:
        nonlocal reply
        await bus.publish(
            event_to_dict(
                LogEvent(
                    job_id=job.id,
                    level="warning",
                    text=(
                        f"Stage {stage.value}: {label}; "
                        f"re-prompting agent (attempt {attempts}/{MAX_FINAL_CORRECTIONS})"
                    ),
                )
            )
        )
        if accumulator is not None:
            # The previous attempt's streamed buffer (if any) is being replaced by
            # this correction turn -- tell the frontend to discard it.
            await accumulator.end_turn(superseded=True)
        reply = await backend.send_message(
            handle, message, **_streaming_kwargs(backend, on_chunk, _on_retry_for(accumulator))
        )
        assistant_msg = {"role": "assistant", "content": reply.raw}
        if accumulator is not None:
            assistant_msg["reasoning"] = accumulator.take_reasoning()
        accumulated_messages.append({"role": "user", "content": message})
        accumulated_messages.append(assistant_msg)

    summary_nudge = _CV_SUMMARY_NUDGE_STRUCTURED if structured else _CV_SUMMARY_NUDGE

    attempts = 0
    while reply.kind == "final":
        try:
            obj = _validate_final_content(stage, reply.content, job, language, structured=structured)
        except FinalContentError as exc:
            if attempts >= MAX_FINAL_CORRECTIONS:
                break  # budget exhausted; _handle_final re-validates, raises, fails the job
            attempts += 1
            # Forward the concise validator feedback so the model knows *what* to fix.
            await _reprompt(
                f"{correction}\n\nValidation feedback on your last attempt: {exc}",
                f"FINAL failed validation ({exc})",
            )
            continue
        # Valid payload. Soft, non-fatal nudge: a CV with no summary section gets one
        # re-prompt to add it (if budget remains); an unfixed summary still ships.
        if is_cv_stage and isinstance(obj, CVDocument) and not cv_has_summary(obj) \
                and attempts < MAX_FINAL_CORRECTIONS:
            attempts += 1
            await _reprompt(summary_nudge, "valid CV missing a Summary section")
            continue
        return reply, accumulated_messages

    return reply, accumulated_messages


async def _start_session_with_retry(
    backend: AgentBackend,
    system_prompt: str,
    initial_user_msg: str,
    schema: dict | None,
    stage: Stage,
    job: Job,
    on_chunk: OnChunk | None = None,
    accumulator: ChunkAccumulator | None = None,
) -> tuple[SessionHandle, AgentReply]:
    """Start a fresh session, retrying a structured-mode wire-level ProtocolError.

    Fresh-session recovery shape #1 (see the plan's Phase 3 carry-forwards): there is
    no handle yet on failure, so a corrective follow-up message cannot be sent — the
    only lever is to re-issue the IDENTICAL start_session call (same args) up to
    MAX_FINAL_CORRECTIONS times. Deliberately not amended per attempt: this call's
    `initial_user_msg` is exactly what the caller persists into `accumulated_messages`
    on success, and amending it on a retry would either desync the persisted row from
    what was actually sent, or leak the amendment into every later replay of this
    session's first turn.

    Sentinel mode (``schema is None``) never retries here — a CLI backend's own
    internal nudge already covers a malformed reply, so re-raising immediately
    preserves the pre-existing hard-fail behavior byte-for-byte.
    """
    kwargs = _schema_kwargs(schema) | _streaming_kwargs(backend, on_chunk, _on_retry_for(accumulator))
    attempts = 0
    while True:
        try:
            return await backend.start_session(system_prompt, initial_user_msg, **kwargs)
        except ProtocolError:
            if schema is None or attempts >= MAX_FINAL_CORRECTIONS:
                raise
            attempts += 1
            if accumulator is not None:
                await accumulator.end_turn(superseded=True)
            await bus.publish(
                event_to_dict(
                    LogEvent(
                        job_id=job.id,
                        level="warning",
                        text=(
                            f"Stage {stage.value}: structured reply unparseable on session "
                            f"start; retrying (attempt {attempts}/{MAX_FINAL_CORRECTIONS})"
                        ),
                    )
                )
            )


async def _send_message_with_wire_retry(
    backend: AgentBackend,
    handle: SessionHandle,
    text: str,
    schema: dict | None,
    stage: Stage,
    job: Job,
    on_chunk: OnChunk | None = None,
    accumulator: ChunkAccumulator | None = None,
) -> tuple[AgentReply, list[dict]]:
    """Send ``text``, retrying a structured-mode wire-level ProtocolError with a
    corrective re-send.

    Fresh-session recovery shape #2: unlike _start_session_with_retry, a handle
    exists here, so the correction is a real follow-up turn (_STRUCTURED_WIRE_CORRECTION,
    embedding the original ``text`` so the model still answers the original request).

    Both backends that can raise this (anthropic, opencode-zen) mutate their own
    ``handle.messages`` only AFTER a successful call — a failed attempt is never
    persisted there. This function mirrors that: it returns exactly one
    {"user", "assistant"} pair, for whichever text (the original or a correction)
    actually succeeded, so the caller's ``accumulated_messages`` never carries an
    orphaned user turn with no assistant reply after it.

    Sentinel mode (``schema is None``) never retries here, matching
    _start_session_with_retry.
    """
    sent_text = text
    attempts = 0
    kwargs = _streaming_kwargs(backend, on_chunk, _on_retry_for(accumulator))
    while True:
        try:
            reply = await backend.send_message(handle, sent_text, **kwargs)
            assistant_msg = {"role": "assistant", "content": reply.raw}
            if accumulator is not None:
                assistant_msg["reasoning"] = accumulator.take_reasoning()
            return reply, [
                {"role": "user", "content": sent_text},
                assistant_msg,
            ]
        except ProtocolError:
            if schema is None or attempts >= MAX_FINAL_CORRECTIONS:
                raise
            attempts += 1
            if accumulator is not None:
                await accumulator.end_turn(superseded=True)
            await bus.publish(
                event_to_dict(
                    LogEvent(
                        job_id=job.id,
                        level="warning",
                        text=(
                            f"Stage {stage.value}: structured reply unparseable; "
                            f"re-prompting agent (attempt {attempts}/{MAX_FINAL_CORRECTIONS})"
                        ),
                    )
                )
            )
            sent_text = _STRUCTURED_WIRE_CORRECTION.format(original=text)


async def _read_base_structure(cv_structure_path: Path | None) -> CVDocument | None:
    """Read the standalone base-CV structure, tolerating a corrupt/invalid file.

    ``None`` covers both "no path given" and "file missing" (see
    ``cv_structure_store.read``'s own contract) as well as "file present but not valid
    JSON / doesn't pass the CVDocument schema" — a hand-edited or partially-written file.
    Orchestrator.run()'s gate normally keeps jobs pending until the file is loadable, but
    a job already past that gate could still race a concurrent edit; treating a corrupt
    read the same as "absent" here means that race degrades to a missing-CV prompt rather
    than crashing the job (caught by run_one's generic except → mark_failed).
    """
    if cv_structure_path is None:
        return None
    try:
        return await cv_structure_store.read(cv_structure_path)
    except (json.JSONDecodeError, ValidationError):
        logger.warning(
            "base-CV structure at %s is present but invalid — treating as absent",
            cv_structure_path,
        )
        return None


async def run_stage(
    job: Job,
    general_purpose_backend: AgentBackend,
    stage: Stage,
    session: AsyncSession,
    fit_backend: AgentBackend | None = None,
    output_dir: Path | None = None,
    cv_structure_path: Path | None = None,
    preferences_path: Path | None = None,
) -> None:
    """Run one pipeline stage to completion or park.

    `fit_backend` is an optional pre-built backend used *only* for the
    `fit_assessment` stage, so the cheap one-shot fit gate can run on a different
    model than the rest of the pipeline (see Settings.fit_model). It is injected by
    the caller rather than constructed here — the pipeline must not reach back into
    `jsa.server` for a factory. When None, `general_purpose_backend` runs every stage.

    Handles:
    - Fresh sessions: cv_adjust (from pending) and cover_letter (from cv_done)
    - Resumed sessions: awaiting_input resume (send answer) — cv_adjust, cover_letter, and revision stages
    - Revision sessions: revising_cv and revising_cl (fresh revision from review state)

    On FINAL: writes Document + Messages + transitions to next state atomically.
    On NEED_INPUT: writes FollowUp + Messages + transitions to awaiting_input, raises PausedForInput.
    """
    system_prompt = _get_system_prompt(stage)

    # A launched job snapshots the global language preference at LAUNCH time
    # (job.language — see routes_jobs.py::launch_job) so its whole pipeline stays
    # in one language even if the global preference changes mid-run. Jobs launched
    # before this snapshot existed (job.language is None) fall back to reading the
    # live global preference fresh at stage time (no cache) — mirrors
    # cv_structure_path's read-at-stage-time rationale. Only used to steer NEW
    # sessions (start_session); resumed sessions (restore_session) never see this
    # — see assemble_system_prompt's docstring (jsa/pipeline/prompt_assembly.py).
    if job.language is not None:
        language_code = job.language
    else:
        language_code = "en"
        if preferences_path is not None:
            prefs = await preferences_store.read(preferences_path)
            language_code = prefs.language

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Starting stage: {stage.value}"))
    )

    if stage == Stage.fit_assessment:
        # One-shot pre-check: no resume path, no NEED_INPUT, no research. Handled
        # entirely here (FIT → fit_done, anything else → unfit) and returns early.
        base_structure = await _read_base_structure(cv_structure_path)
        await _run_fit_assessment(
            job,
            fit_backend if fit_backend is not None else general_purpose_backend,
            session,
            system_prompt,
            language_code,
            base_structure,
        )
        return

    # Single destination-mode decision for this whole stage invocation, computed once
    # and reused everywhere: the assemble_system_prompt/start_session structured_model
    # kwarg, adapt_history's destination flag, the wire-level retry budget, and the
    # self-heal/validation wording all key off this one `schema`/`structured` pair —
    # never two independently-computed expressions that happen to agree today (see the
    # structured-output plan's Phase 5 advisor carry-forward #2).
    schema = _structured_schema_for(general_purpose_backend, stage)
    structured = schema is not None

    # Same single-decision idiom as `schema`/`structured` above, computed once and
    # reused everywhere a revision stage needs to know whether tools are in play.
    # Non-None ONLY for revising_cv/revising_cl (see `_tools_for`'s docstring) — this
    # is what mechanically keeps cv_adjust/cover_letter/fit_assessment tool-free.
    tools = _tools_for(general_purpose_backend, stage)
    # Set to the ACTUAL rung run_tool_loop used ("native"/"prompt") only when the tool
    # loop produces a terminal reply this invocation; stays None for every non-revision
    # stage and for a revision that fell through to rung 3 — see `_log_session_mode`.
    tool_mode: str | None = None

    # One accumulator per stage invocation; None when the backend can't stream, so
    # every downstream _streaming_kwargs() call degrades to "no on_chunk kwarg"
    # cleanly (same conditional-kwarg pattern as structured_schema).
    streaming_enabled = getattr(general_purpose_backend, "supports_streaming", False)
    accumulator = ChunkAccumulator(job.id, stage.value) if streaming_enabled else None
    on_chunk = accumulator.add_chunk if accumulator is not None else None

    # Every restore_session call below must use THIS, not the bare `system_prompt` —
    # see assemble_system_prompt's docstring: structured-capable backends are
    # wire-stateless and resend the system prompt on every call, so the structured
    # contract must be re-appended on resume or the model silently loses the
    # kind/question/payload explanation and can loop forever re-asking its opening
    # question. Sentinel-mode (structured=False) resumes unchanged (real CLI session).
    resume_system_prompt = assemble_system_prompt(
        system_prompt,
        language=language_code,
        structured_model=schema,
        for_resume=True,
        now=datetime.utcnow(),
    )

    if stage in (Stage.revising_cv, Stage.revising_cl):
        original_stage = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
        revision_session_id = job.cv_session_id if stage == Stage.revising_cv else job.cl_session_id
        # revision_session_id may be None after a backend switch (BF-19): the new backend
        # will restore from history only (AnthropicAPIBackend ignores external_id; CLI
        # backends receive None and start a new session backed by the history array).
        # Only raise if both session ID and Message history are absent, which indicates
        # a job that predates BF-9 (not a backend switch).
        if revision_session_id is None:
            original_stage_check = Stage.cv_adjust if stage == Stage.revising_cv else Stage.cover_letter
            history_check = await _load_history(session, job.id, original_stage_check)
            if not history_check:
                raise ValueError(
                    f"Cannot resume revision for {stage.value}: per-stage session ID was not "
                    f"recorded and no history available (job predates BF-9 fix). "
                    f"Reset the job to re-run from scratch."
                )

        # Fetch the unconsumed RevisionRequest — needed for its instruction (fresh path)
        # and its created_at (discriminator).
        rev_req = await _get_revision_request(session, job.id)

        # Discriminate fresh revision vs resume after awaiting_input.
        #
        # The spec's original discriminator (_load_history non-empty) is insufficient
        # because Message rows from prior completed revisions persist under the same
        # stage enum value. The correct discriminator is:
        #
        #   Resume iff a FollowUp for this revision stage was answered AFTER the
        #   current (unconsumed) RevisionRequest was created.
        #
        # This handles all cases correctly:
        #   - Fresh 1st revision: no FollowUp at all → fresh
        #   - Mid-revision park then resume: FollowUp.answered_at > RevReq.created_at → resume
        #   - Fresh 2nd revision: any old FollowUp.answered_at < RevReq.created_at → fresh
        #   - 2nd revision parks then resumes: new FollowUp.answered_at > new RevReq.created_at → resume
        is_resume = await _is_revision_resume(session, job.id, stage, rev_req.created_at)

        # Both sub-branches below only need to disagree on WHICH raw (unadapted)
        # history + instruction to use — everything downstream (the tool-loop
        # attempt, and rung 3's fallback) is identical from here on, so compute those
        # two once rather than duplicating the tool-loop/rung-3 dispatch per branch.
        if is_resume:
            # Resume: the user answered a follow-up question mid-revision (this is
            # also the ``ask_user`` tool's resume target — a fresh CvWorkingCopy/
            # ClWorkingCopy is built below and the model re-establishes read state by
            # calling get_cv/get_letter again; ask_user discards any edits from the
            # parked turn, so there is nothing to replay there).
            revision_turns = await _load_history(session, job.id, stage)
            answer_text = await _get_latest_answer(session, job.id, stage)
            original_history = await _load_history(session, job.id, original_stage)
            raw_history = original_history + revision_turns
            instruction = answer_text
        else:
            # Fresh revision: restore the original stage's session and send the
            # revision instruction.
            raw_history = await _load_history(session, job.id, original_stage)
            instruction = rev_req.instruction

        tool_result: ToolLoopResult | None = None
        if tools is not None:
            document_obj = await _latest_document_object(session, job.id, original_stage)
            if document_obj is not None:
                # The tool session is NON-structured by construction (tool mode and
                # structured mode are mutually exclusive per request — the terminal
                # tool's arguments ARE the structured output, so no response_format is
                # ever sent here) — this `adapt_history(..., structured=False)` is
                # computed SEPARATELY from rung 3's `adapt_history(..., structured=
                # structured)` below; they must never share one variable (see
                # CLAUDE.md's canonical-form invariant).
                tool_history = adapt_history(raw_history, structured=False)

                def _tool_system_prompt(
                    mode: str, _prompt: str = system_prompt, _tools: tuple[ToolSpec, ...] = tools
                ) -> str:
                    return assemble_system_prompt(
                        _prompt, language=language_code, tool_model=_tools,
                        native_tools=(mode == "native"),
                    )

                tool_result = await run_tool_loop(
                    backend=general_purpose_backend,
                    stage=stage,
                    job=job,
                    document=document_obj,
                    instruction=instruction,
                    history=tool_history,
                    external_id=revision_session_id,
                    build_system_prompt=_tool_system_prompt,
                    language=language_code,
                )

        if tool_result is not None:
            handle = tool_result.handle
            reply = tool_result.reply
            tool_mode = tool_result.mode
            # Persist the loop's execution log as role="tool" Message rows, ordered,
            # one per attempted call (including not_executed/budget_exhausted entries)
            # — jsa/pipeline/tool_loop.py's own docstring names this as this phase's
            # job. `_load_history`'s role.in_(["user", "assistant"]) filter already
            # excludes "tool" rows from every future replay with zero changes there —
            # do NOT widen that one.
            #
            # jsa/api/transcript.py does NOT render these as turns of their own: it
            # folds them into the FOLLOWING assistant turn's `tools` field
            # (`_tool_mark`), which is what makes a settled REASONING card show the
            # same rows the live one did. That fold relies on this list's ORDER —
            # [user instruction, *tool rows, assistant reply], written as one atomic
            # repo.checkpoint — so a tool row always precedes the assistant row it
            # belongs to and can never dangle past the end of the transcript. Keep
            # the assistant row last here.
            tool_rows = [
                {"role": "tool", "content": json.dumps(call, sort_keys=True)}
                for call in tool_result.tool_calls
            ]
            assistant_msg = {"role": "assistant", "content": reply.raw}
            accumulated_messages = [
                {"role": "user", "content": instruction},
                *tool_rows,
                assistant_msg,
            ]
        else:
            # Rung 3: no tools offered (unreachable for these two stages — see
            # `_tools_for`), no seed document, or the loop gave up on both rungs —
            # give up on tools and do today's full-document rewrite, UNCHANGED: the
            # CV/cover-letter content is already part of `raw_history` (the original
            # stage's session), so nothing new is injected here.
            history = adapt_history(raw_history, structured=structured)
            restore_kwargs = _schema_kwargs(schema)
            handle = await general_purpose_backend.restore_session(
                resume_system_prompt, history, revision_session_id, **restore_kwargs
            )
            reply, accumulated_messages = await _send_message_with_wire_retry(
                general_purpose_backend, handle, instruction, schema, stage, job,
                on_chunk=on_chunk, accumulator=accumulator,
            )
    elif stage in (Stage.cv_adjust, Stage.cover_letter):
        # Determine fresh vs resume by checking whether Message rows exist for
        # this job+stage.  The orchestrator already transitioned the job to
        # `running` before calling us, so we cannot discriminate on job.state.
        history = await _load_history(session, job.id, stage)
        if history:
            # Resume after awaiting_input — send the user's answer as the next turn.
            answer_text = await _get_latest_answer(session, job.id, stage)
            history = adapt_history(history, structured=structured)
            restore_kwargs = _schema_kwargs(schema)
            handle = await general_purpose_backend.restore_session(
                resume_system_prompt, history, job.session_external_id, **restore_kwargs
            )
            # Only the new turns are new; prior messages already persisted.
            reply, accumulated_messages = await _send_message_with_wire_retry(
                general_purpose_backend, handle, answer_text, schema, stage, job,
                on_chunk=on_chunk, accumulator=accumulator,
            )
        else:
            # Fresh session. cover_letter gets a company-research brief and the approved
            # tailored CV (the two-lane split's whole point: write the letter against what
            # will actually be submitted, not the base structure). cv_adjust gets neither —
            # research is unwired for it (see CLAUDE.md → "CV structure — single source of
            # truth") and it works from the base structure directly.
            if stage is Stage.cover_letter:
                brief = await _gather_research(job, general_purpose_backend, stage)
                cv_docs = await repo.get_documents(session, job.id, stage=Stage.cv_adjust)
                if cv_docs:
                    cv_block = (
                        "TAILORED CV (approved by the user — write the letter against this)",
                        cv_docs[0].markdown,
                    )
                else:
                    logger.warning(
                        "cover_letter fresh session for job %s has no cv_adjust Document "
                        "yet; falling back to the base CV structure (should be unreachable "
                        "past the CV gate)",
                        job.id,
                    )
                    cv_block = await _base_structure_cv_block(cv_structure_path)
            else:
                brief = None
                cv_block = await _base_structure_cv_block(cv_structure_path)
            initial_user_msg = _build_initial_user_msg(job, brief, cv_block)
            fresh_system_prompt = assemble_system_prompt(
                system_prompt,
                language=language_code,
                structured_model=schema,
                now=datetime.utcnow(),
            )
            handle, reply = await _start_session_with_retry(
                general_purpose_backend, fresh_system_prompt, initial_user_msg, schema, stage, job,
                on_chunk=on_chunk, accumulator=accumulator,
            )
            # Accumulate all messages for this session (system, user, assistant reply)
            fresh_assistant_msg = {"role": "assistant", "content": reply.raw}
            if accumulator is not None:
                fresh_assistant_msg["reasoning"] = accumulator.take_reasoning()
            accumulated_messages = [
                {"role": "system", "content": fresh_system_prompt},
                {"role": "user", "content": initial_user_msg},
                fresh_assistant_msg,
            ]
    else:
        raise ValueError(f"Unexpected stage: {stage}")

    # Persist session_external_id while we have the handle in case we need to park
    job.session_external_id = handle.external_id

    # Keep per-stage session IDs so revision can resume the correct conversation.
    # Only set for the primary stages; do NOT overwrite during revising_* branches
    # (revisions continue the original session, so the UUID stays the same).
    if stage == Stage.cv_adjust:
        job.cv_session_id = handle.external_id
    elif stage == Stage.cover_letter:
        job.cl_session_id = handle.external_id

    await _log_session_mode(job, stage, schema, handle, tools=tools, tool_mode=tool_mode)

    # Self-heal: if a FINAL block fails content validation (e.g. the agent emitted a
    # change-log or "done" summary instead of the artifact), re-prompt the SAME session
    # to re-emit a clean artifact before the reply is dispatched below. Recovered →
    # proceeds as FINAL; turned into NEED_INPUT → parks; still invalid → _handle_final
    # raises and fails the job.
    #
    # Skipped entirely when a tool-loop rung produced this reply (tool_mode is not
    # None): jsa/schema/patch.py's finalize() already ran this exact content gate
    # (_validate_final_content) before the loop ever returned, so a FINAL reply here
    # is already known-valid — re-validating is a harmless no-op, but the soft
    # "CV missing a Summary section" nudge below it is NOT a no-op. That nudge sends a
    # plain correction message via backend.send_message on the tool-mode handle, whose
    # session has no sentinel/structured contract at all (tool and structured/sentinel
    # modes are mutually exclusive per request) — a native-tool session may answer
    # with another tool call instead of a bare "final" reply, which `run_stage`'s
    # `reply.kind == "tool_calls"` guard below would then hard-fail on. The tool
    # loop's own reemit_hint retry (inside finalize()) is this reply's self-heal
    # equivalent; nesting the two here is exactly the "self-heal double-application"
    # this function's own docstring already warns against for the wire-level budget.
    if tool_mode is None:
        reply, accumulated_messages = await _self_heal_final(
            backend=general_purpose_backend,
            handle=handle,
            stage=stage,
            job=job,
            reply=reply,
            accumulated_messages=accumulated_messages,
            language=language_code,
            structured=structured,
            on_chunk=on_chunk,
            accumulator=accumulator,
        )

    # Guard against a stale result: the agent turn above may have run for a long
    # time, during which the job could have been dismissed/cancelled/deleted on
    # a separate session. checkpoint()'s transition() guard only validates
    # against this in-memory `job` object (still `running`), so without this
    # re-check a stale NEED_INPUT/FINAL would silently overwrite the real DB
    # state (e.g. resurrect a dismissed job — see StaleJobResult docstring).
    # Read via a fresh session: this session may hold a stale snapshot.
    #
    # accumulator.end_turn(superseded=False) -- telling the frontend the streamed
    # turn is final -- deliberately runs AFTER this guard, not before: firing it
    # first would tell a connected client "turn complete, keep the streamed
    # content" even when the job turns out to be stale and no Message/Document
    # ever gets persisted for this turn, leaving the client's view permanently
    # out of sync with the DB until reload.
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

    if accumulator is not None:
        await accumulator.end_turn(superseded=False)

    # Handle the reply
    if reply.kind == "needs_input":
        await _handle_needs_input(
            session=session,
            job=job,
            handle=handle,
            stage=stage,
            reply=reply,
            accumulated_messages=accumulated_messages,
        )
        # Fetch the follow-up row we just inserted so we can include its ID
        # in the WS event.  The checkpoint above already committed, so the row
        # is visible to the same session.
        fu_result = await session.execute(
            select(FollowUp).where(
                FollowUp.job_id == job.id,
                FollowUp.stage == stage,
                FollowUp.answered_at.is_(None),
            )
        )
        fu = fu_result.scalar_one()
        await bus.publish(
            event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: NEED_INPUT — agent is asking for input"))
        )
        await bus.publish(
            event_to_dict(
                FollowUpNeededEvent(
                    job_id=job.id,
                    follow_up_id=fu.id,
                    question=reply.question or "",
                    stage=stage.value,
                )
            )
        )
        raise PausedForInput()

    if reply.kind == "tool_calls":
        # This is unreachable for a genuinely successful tool-loop turn: run_tool_loop
        # (jsa/pipeline/tool_loop.py) only ever returns a ToolLoopResult (handled above,
        # BEFORE this point — see the revising_cv/revising_cl branch) when a terminal
        # tool (finalize/ask_user) fired, synthesizing an ordinary "final"/"needs_input"
        # AgentReply; it never hands back a bare "tool_calls" reply. So by the time
        # execution reaches here, `reply` is either a non-revision stage's reply, or a
        # revision stage's rung-3 (full-rewrite) reply — in BOTH cases no session
        # dispatched to produce this `reply` was a tool session, so a `<<<TOOL_CALLS>>>`
        # block (protocol.py's TOOL_CALLS verb) can only mean the model hallucinated
        # one unprompted. Without this guard the reply would fall through to
        # `_handle_final` below: `_self_heal_final`'s `while reply.kind == "final"` is
        # false for "tool_calls" (skipped, no correction budget), and
        # `_validate_final_content` would `json.loads` the tool-call array successfully
        # (it's valid JSON) and then fail `CVDocument`/`CoverLetter.model_validate` on a
        # list — a confusing schema error instead of a diagnosable one. Raising here
        # also means this does NOT get the sentinel-nudge retry
        # (`_parse_with_nudge`/OpenCode Zen's downgrade) a genuine "no sentinel block"
        # gets — the block DID parse, just unexpectedly — so it hard-fails the job in
        # one shot.
        raise ProtocolError("unexpected tool-call block outside a tool session")

    # reply.kind == "final"
    await _handle_final(
        session=session,
        job=job,
        backend=general_purpose_backend,
        handle=handle,
        stage=stage,
        reply=reply,
        accumulated_messages=accumulated_messages,
        output_dir=output_dir,
        language=language_code,
        structured=structured,
    )

    await bus.publish(
        event_to_dict(LogEvent(job_id=job.id, level="info", text=f"Stage {stage.value}: FINAL received"))
    )

    # Publish stage-completion events after a successful FINAL checkpoint.
    # `job.state` has been mutated by transition() inside _handle_final — read it rather
    # than hardcoding a destination. cv_adjust always lands on cv_review (cv_done is only
    # reached later, via the approve-cv endpoint); revising_cv lands on one of two states
    # depending on RevisionRequest.origin_state (cv_review or review).
    if stage == Stage.cv_adjust:
        await bus.publish(
            event_to_dict(StageCompleteEvent(job_id=job.id, stage="cv_adjust"))
        )
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=job.state.value,
                )
            )
        )
    elif stage == Stage.cover_letter:
        await bus.publish(
            event_to_dict(StageCompleteEvent(job_id=job.id, stage="cover_letter"))
        )
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=JobState.review.value,
                )
            )
        )
    elif stage in (Stage.revising_cv, Stage.revising_cl):
        await bus.publish(
            event_to_dict(
                StatusChangedEvent(
                    job_id=job.id,
                    from_state=JobState.running.value,
                    to_state=job.state.value,
                )
            )
        )


async def _handle_needs_input(
    *,
    session: AsyncSession,
    job: Job,
    handle: SessionHandle,
    stage: Stage,
    reply: AgentReply,
    accumulated_messages: list[dict],
) -> None:
    """Park the job to awaiting_input, write FollowUp + messages atomically."""
    # Build a display question that includes any pre-sentinel context
    # (e.g. the Intel Brief Claude wrote before <<<NEED_INPUT>>>).
    sentinel_marker = "<<<NEED_INPUT>>>"
    raw_text = reply.raw
    sentinel_pos = raw_text.find(sentinel_marker)
    if sentinel_pos > 0:
        context = raw_text[:sentinel_pos].strip()
        display_question = f"{context}\n\n---\n\n{reply.question}" if context else reply.question
    else:
        display_question = reply.question

    follow_up_data = {
        "stage": stage,
        "question": display_question,
        "suggested_replies": reply.suggested_replies,
    }
    await checkpoint(
        session,
        job,
        JobState.awaiting_input,
        stage,  # preserve current_stage
        messages=accumulated_messages,
        follow_up=follow_up_data,
    )
    await _publish_transcript_changed(job)


# ---------------------------------------------------------------------------
# Fit assessment (one-shot pre-check before any CV work)
# ---------------------------------------------------------------------------

# Shown in the modal when the agent's verdict cannot be parsed (fail-to-modal).
_FIT_FALLBACK_REASON = (
    "The fit assessment did not return a clear verdict. Review this job manually "
    "before continuing."
)

def _build_fit_user_msg(job: Job, base_structure: CVDocument | None) -> str:
    """Build the (single) user message for the fit-assessment stage.

    Deliberately minimal — no research brief — so the pre-check stays cheap.
    ``base_structure`` is the standalone base-CV structure (Structure Editor); rendered
    to markdown for readability. Absent (no structure saved yet) → no CV block — the
    gate in Orchestrator.run() keeps jobs pending until a structure exists, so this
    should not normally happen in production.
    """
    cv_block = f"CV:\n{cv_to_markdown(base_structure)}\n\n" if base_structure is not None else ""
    return (
        f"COMPANY: {job.company}\n"
        f"ROLE: {job.role}\n\n"
        f"{cv_block}"
        f"JOB DESCRIPTION:\n{job.jd}"
    )


def _parse_fit_verdict(reply: AgentReply) -> tuple[bool, str | None]:
    """Parse a fit-assessment reply into ``(is_fit, reason)``.

    Contract: the FINAL payload's first line carries the verdict ``FIT`` or ``UNFIT``;
    the rest is the reason. We match the verdict as a substring of the (uppercased)
    first line so common decoration is tolerated — ``**FIT**``, ``Verdict: FIT``,
    ``## UNFIT``, ``FIT ✅`` all classify correctly. ``UNFIT`` is checked first because
    it contains ``FIT``.

    Fail-to-modal — anything that is not a clear ``FIT`` returns ``is_fit=False`` with a
    best-effort reason for the modal:
    - ``UNFIT`` → the agent's reason (or a fallback if none given)
    - a ``needs_input`` reply → the agent's question text
    - no recognizable verdict → the raw content (so nothing is hidden from the user)
    """
    if reply.kind == "needs_input":
        return False, (reply.question or "").strip() or _FIT_FALLBACK_REASON

    content = (reply.content or "").strip()
    lines = content.split("\n", 1)
    first_line_upper = lines[0].upper()
    rest = lines[1].strip() if len(lines) > 1 else ""

    if "UNFIT" in first_line_upper:
        # Reason = following lines, else the first line minus everything up to "UNFIT".
        reason = rest or re.sub(r"(?is).*unfit[\s:.\-—*]*", "", lines[0]).strip()
        return False, reason or _FIT_FALLBACK_REASON

    if "FIT" in first_line_upper:
        return True, None

    # No recognizable verdict → fail to modal, surfacing whatever the agent produced.
    return False, content or _FIT_FALLBACK_REASON


async def _run_fit_assessment(
    job: Job,
    backend: AgentBackend,
    session: AsyncSession,
    system_prompt: str,
    language_code: str = "en",
    base_structure: CVDocument | None = None,
) -> None:
    """Run the one-shot fit-assessment stage and checkpoint the outcome.

    Always a fresh session expecting a single FINAL with a FIT/UNFIT verdict; there
    is no resume / awaiting_input path for this stage. FIT → ``fit_done`` (pipeline
    continues to cv_adjust). Anything else → ``unfit`` (parked), storing the agent's
    reason in ``job.fit_reason`` for the frontend modal.
    """
    initial_user_msg = _build_fit_user_msg(job, base_structure)
    job.retry_count = 0
    # The fit gate may run on a DIFFERENT backend instance than the rest of the
    # pipeline (Settings.fit_model — see CLAUDE.md → "Separate fit-assessment model"),
    # so its structured-mode decision must be derived from THIS `backend` param, never
    # from the caller's general_purpose_backend — computing it here, where the resolved
    # instance already lives, makes that structurally guaranteed rather than a call-site
    # convention someone could get wrong (the fit-capability trap the plan's Phase 5
    # Problems/Bugs section names explicitly).
    schema = _structured_schema_for(backend, Stage.fit_assessment)
    # Always a fresh start_session (no resume path for this stage) — safe to inject here.
    system_prompt = assemble_system_prompt(
        system_prompt,
        language=language_code,
        structured_model=schema,
        fit_verdict=True,
        now=datetime.utcnow(),
    )

    # Same conditional-kwarg pattern run_stage uses (see _streaming_kwargs) — this
    # stage was missed when Phase 6-8 wired streaming into run_stage, so a
    # streaming-capable backend (e.g. claude-cli) never streamed its fit-assessment
    # turn even though it's the very first stage every job runs.
    streaming_enabled = getattr(backend, "supports_streaming", False)
    accumulator = ChunkAccumulator(job.id, Stage.fit_assessment.value) if streaming_enabled else None
    on_chunk = accumulator.add_chunk if accumulator is not None else None

    start_kwargs = _schema_kwargs(schema) | _streaming_kwargs(backend, on_chunk, _on_retry_for(accumulator))
    try:
        handle, reply = await backend.start_session(system_prompt, initial_user_msg, **start_kwargs)
    except ProtocolError as exc:
        # A malformed / sentinel-less reply is "unparseable" → fail to the modal
        # (closed), consistent with the verdict contract, rather than failing the
        # job outright.
        #
        # AgentTimeout and AgentLimitReached are deliberately NOT caught here — a
        # timeout or a quota signal means the backend didn't answer, not that it
        # "answered no". Those must propagate past run_stage to the orchestrator's
        # BF-19 fallback chain (Orchestrator._handle_backend_timeout /
        # _handle_limit_reached) so a timing-out or rate-limited fit backend tries
        # the next configured backend instead of producing a false "not a fit"
        # verdict. (Before this, AgentTimeout was caught here too — the fit gate is
        # the very first stage every job hits, so on a chain like `--backends
        # opencode-zen,claude-cli` a slow opencode-zen response silently parked
        # every job as unfit instead of ever trying claude-cli.)
        #
        # Same stale-result guard as run_stage's post-reply check (see StaleJobResult):
        # fit_assessment is the FIRST stage and runs before run_stage's guard is ever
        # reached (that check sits after this whole function returns), so it needs its
        # own re-check here — this is the exact "dismissed before it ever outputs
        # anything" window from the reported bug.
        current_state = await repo.get_state_fresh(session, job.id)
        if current_state != JobState.running:
            raise StaleJobResult(job.id, current_state)
        # No clean turn to show — discard whatever streamed before the parse failed
        # so the frontend's live bubble doesn't linger past the unfit modal.
        if accumulator is not None:
            await accumulator.end_turn(superseded=True)
        job.fit_reason = _FIT_FALLBACK_REASON
        await checkpoint(session, job, JobState.unfit, None)
        await _publish_transcript_changed(job)
        await _publish_fit_outcome(job, is_fit=False)
        return

    # Same guard for the normal (non-ProtocolError) path — see comment above.
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

    # Deliberately after the stale-job guard, before checkpoint — mirrors run_stage's
    # ordering (see its comment): telling the frontend "turn complete, keep the
    # streamed content" before confirming the job is still live would let a stale
    # result announce completion with nothing actually persisted.
    if accumulator is not None:
        await accumulator.end_turn(superseded=False)

    await _log_session_mode(job, Stage.fit_assessment, schema, handle)

    assistant_msg = {"role": "assistant", "content": reply.raw}
    if accumulator is not None:
        assistant_msg["reasoning"] = accumulator.take_reasoning()
    accumulated_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": initial_user_msg},
        assistant_msg,
    ]
    job.session_external_id = handle.external_id

    is_fit, reason = _parse_fit_verdict(reply)
    target_state = JobState.fit_done if is_fit else JobState.unfit
    job.fit_reason = None if is_fit else reason

    await checkpoint(
        session,
        job,
        target_state,
        None,
        messages=accumulated_messages,
    )
    await _publish_transcript_changed(job)
    await backend.end_session(handle)
    await _publish_fit_outcome(job, is_fit=is_fit)


async def _publish_fit_outcome(job: Job, *, is_fit: bool) -> None:
    """Publish the log + stage-complete + status-changed events for a fit outcome."""
    target_state = JobState.fit_done if is_fit else JobState.unfit
    await bus.publish(
        event_to_dict(LogEvent(
            job_id=job.id, level="info",
            text=f"Stage fit_assessment: {'FIT' if is_fit else 'UNFIT'} → {target_state.value}",
        ))
    )
    await bus.publish(
        event_to_dict(StageCompleteEvent(job_id=job.id, stage="fit_assessment"))
    )
    await bus.publish(
        event_to_dict(
            StatusChangedEvent(
                job_id=job.id,
                from_state=JobState.running.value,
                to_state=target_state.value,
            )
        )
    )


async def _handle_final(
    *,
    session: AsyncSession,
    job: Job,
    backend: AgentBackend,
    handle: SessionHandle,
    stage: Stage,
    reply: AgentReply,
    accumulated_messages: list[dict],
    output_dir: Path | None = None,
    language: str = "en",
    structured: bool = False,
) -> None:
    """Finalize the stage: compute next state, version document, write checkpoint.

    ``structured`` must be the SAME value ``run_stage`` passed to ``_self_heal_final``
    for this invocation — both feed ``_validate_final_content``, and a mismatch would
    mean the self-heal loop and this authoritative gate disagree about which reply
    shape is expected.
    """
    # A successful FINAL means any soft retry worked — reset the retry counter.
    # checkpoint() calls session.add(job) + commit, so this persists atomically.
    job.retry_count = 0

    # Authoritative content gate before writing anything to the DB. If the model
    # emitted a change-log/summary instead of the artifact (and self-heal could not
    # recover it), this raises FinalContentError → propagates to _run_one → job failed.
    structured_obj = _validate_final_content(stage, reply.content, job, language, structured=structured)

    # Determine document stage (revision docs stored under original stage)
    if stage == Stage.revising_cv:
        doc_stage = Stage.cv_adjust
    elif stage == Stage.revising_cl:
        doc_stage = Stage.cover_letter
    else:
        doc_stage = stage

    # Compute next version: count existing documents for this job+doc_stage, +1
    existing_docs = await repo.get_documents(session, job.id, doc_stage)
    next_version = len(existing_docs) + 1

    # Serialize the validated structured object to canonical Markdown — the program owns
    # 100% of layout. Persist both the JSON source-of-truth (`structured`) and the
    # rendered Markdown; downstream renderers consume `markdown` unchanged.
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        canonical_md = cv_to_markdown(structured_obj)
    else:  # cover_letter / revising_cl
        canonical_md = cover_letter_to_markdown(structured_obj)

    document_data = {
        "stage": doc_stage,
        "version": next_version,
        "markdown": canonical_md,
        "structured": structured_obj.model_dump_json(),
    }

    # Second stale-result guard: get_documents() above was a real DB round-trip
    # (an await point) since run_stage's pre-check, during which a dismiss/cancel
    # on another session could have landed. Re-check immediately before the
    # checkpoint write that would otherwise resurrect the job (see StaleJobResult
    # docstring / run_stage's guard for the general rationale).
    current_state = await repo.get_state_fresh(session, job.id)
    if current_state != JobState.running:
        raise StaleJobResult(job.id, current_state)

    if stage == Stage.cv_adjust:
        # cv_adjust → cv_review: the CV lane parks for user approve/revise. cv_done is
        # reached only via the approve-cv endpoint (cv_review → cv_done), never directly
        # from here — see CLAUDE.md → "Two-lane pipeline / CV gate".
        await checkpoint(
            session,
            job,
            JobState.cv_review,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        await _publish_transcript_changed(job)
        if output_dir is not None:
            await _render_cv(session, job, output_dir)
    elif stage == Stage.cover_letter:
        # cover_letter → review directly: write messages + document in a single transaction.
        # cl_done is not an observable intermediate state in normal flow; it exists only
        # as a crash-recovery landing state (the startup sweep uses it when it finds a
        # cover_letter Document on a job that crashed mid-run).
        await checkpoint(
            session,
            job,
            JobState.review,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        await _publish_transcript_changed(job)
        if output_dir is not None:
            await _render_for_review(session, job, output_dir)
    elif stage in (Stage.revising_cv, Stage.revising_cl):
        # A CV revision can be requested from either the CV gate or final review (both are
        # revisable there — see CLAUDE.md); revising_cl only ever returns to review (no
        # cl_review state exists). Read origin_state BEFORE marking the RevisionRequest
        # consumed. NULL (legacy rows) reads as "review".
        dest_state = JobState.review
        if stage == Stage.revising_cv:
            origin_state = await repo.get_unconsumed_revision_origin(session, job.id)
            if origin_state == JobState.cv_review.value:
                dest_state = JobState.cv_review

        # Mark the RevisionRequest consumed (within same transaction as the checkpoint commit)
        await session.execute(
            update(RevisionRequest)
            .where(
                RevisionRequest.job_id == job.id,
                RevisionRequest.consumed_at.is_(None),
            )
            .values(consumed_at=datetime.utcnow())
        )
        await checkpoint(
            session,
            job,
            dest_state,
            None,
            messages=accumulated_messages,
            document=document_data,
        )
        await _publish_transcript_changed(job)
        if output_dir is not None:
            if dest_state == JobState.cv_review:
                await _render_cv(session, job, output_dir)
            else:
                await _render_for_review(session, job, output_dir)

    await backend.end_session(handle)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_system_prompt(stage: Stage) -> str:
    """Return the system prompt for the given stage."""
    if stage == Stage.fit_assessment:
        return loader.read_prompt("fit_assessment")
    if stage in (Stage.cv_adjust, Stage.revising_cv):
        return loader.read_prompt("cv_adjust")
    else:  # cover_letter or revising_cl
        return loader.read_prompt("cover_letter")


# NOTE: the language directive used to live here as `_language_directive` /
# `_with_language_directive`. It moved verbatim to
# `jsa/pipeline/prompt_assembly.py::assemble_system_prompt` (the structured-output
# plan's single composition root for runtime system-prompt mutation) — see that
# module's docstring and `tests/backend/fixtures/language_directive_golden.json`
# (captured from this original implementation) for the parity gate that proved the
# move is byte-identical for the sentinel-mode path every CLI backend depends on.


def _build_initial_user_msg(
    job: Job, brief: str | None, cv_block: tuple[str, str] | None = None
) -> str:
    """Build the initial user message for a fresh session.

    ``brief`` is either a populated research block ([INTEL_BRIEF] or [COMPANY_BRIEF]) or
    the NONE placeholder produced by ``_research_placeholder`` — or ``None`` when the
    stage has no research (cv_adjust; research is unwired for it, see CLAUDE.md → "CV
    structure — single source of truth"). When present it is injected first so the main
    agent sees it immediately and the resumed Message history replays it verbatim.

    ``cv_block`` is an optional ``(label, content)`` pair supplying the CV content —
    cv_adjust gets the base structure's JSON skeleton; cover_letter gets the approved
    tailored CV's markdown (or, absent one, the base structure as a fallback). This block
    only supplies the data; stage-specific instructions for *how* to use it live in each
    stage's own system prompt, not here. Injected here (rather than after research) so it
    is part of the persisted initial message and replays verbatim on an awaiting_input
    resume.
    """
    brief_part = f"{brief}\n\n" if brief is not None else ""
    skeleton = ""
    if cv_block is not None:
        label, content = cv_block
        skeleton = f"{label}:\n{content}\n\n"
    return (
        f"{brief_part}"
        f"{skeleton}"
        f"JOB DESCRIPTION:\n{job.jd}\n\n"
        f"TIER: {job.tier}"
    )


async def _base_structure_cv_block(cv_structure_path: Path | None) -> tuple[str, str] | None:
    """Return the base-CV structure as a ``(label, content)`` cv_block, or None if absent."""
    base_structure = await _read_base_structure(cv_structure_path)
    if base_structure is None:
        return None
    return (
        "BASE CV STRUCTURE (the candidate's base CV, curated in the Structure Editor)",
        base_structure.model_dump_json(indent=2),
    )


async def _latest_document_object(
    session: AsyncSession, job_id: str, doc_stage: Stage
) -> CVDocument | CoverLetter | None:
    """The latest ``Document.structured`` for ``job_id``/``doc_stage``, parsed into a
    ``CVDocument``/``CoverLetter`` — the working-copy seed for
    ``jsa/pipeline/tool_loop.py``'s ``run_tool_loop``.

    ``None`` when no Document exists yet for this stage, or its ``structured`` column
    is empty (a legacy row predating the structured-output plan's JSON
    source-of-truth) — both should be unreachable for a real revision (a revision
    always targets an already-produced, already-structured document), but are
    tolerated the same way ``_read_base_structure``/``_base_structure_cv_block``
    tolerate absence: the caller falls back to skipping the tool loop and running rung
    3 (the full-document rewrite), which needs no working-copy seed at all.
    """
    docs = await repo.get_documents(session, job_id, doc_stage)
    if not docs or not docs[0].structured:
        return None
    model = CVDocument if doc_stage == Stage.cv_adjust else CoverLetter
    return model.model_validate_json(docs[0].structured)


async def _load_history(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
) -> list[HistoryTurn]:
    """Load Message rows for the given job+stage as HistoryTurn list.

    Excludes system messages — only user and assistant turns are included,
    ordered by id (creation order).
    """
    stmt = (
        select(Message)
        .where(
            Message.job_id == job_id,
            Message.stage == stage,
            Message.role.in_(["user", "assistant"]),
        )
        .order_by(Message.id.asc())
    )
    result = await session.execute(stmt)
    messages = result.scalars().all()
    return [HistoryTurn(role=m.role, content=m.content) for m in messages]  # type: ignore[arg-type]



async def _get_latest_answer(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
) -> str:
    """Return the answer from the most recently answered FollowUp for this job+stage."""
    stmt = (
        select(FollowUp)
        .where(
            FollowUp.job_id == job_id,
            FollowUp.stage == stage,
            FollowUp.answered_at.is_not(None),
        )
        .order_by(FollowUp.answered_at.desc())
    )
    result = await session.execute(stmt)
    fu = result.scalars().first()
    if fu is None:
        raise ValueError(
            f"No answered FollowUp found for job {job_id}, stage {stage}"
        )
    if fu.answer is None:
        raise ValueError(
            f"FollowUp for job {job_id}, stage {stage} has no answer text"
        )
    return fu.answer


async def _get_revision_request(
    session: AsyncSession,
    job_id: str,
) -> RevisionRequest:
    """Return the unconsumed RevisionRequest for this job, raising if absent."""
    stmt = (
        select(RevisionRequest)
        .where(
            RevisionRequest.job_id == job_id,
            RevisionRequest.consumed_at.is_(None),
        )
    )
    result = await session.execute(stmt)
    rev = result.scalar_one_or_none()
    if rev is None:
        raise ValueError(f"No unconsumed RevisionRequest found for job {job_id}")
    return rev


async def _is_revision_resume(
    session: AsyncSession,
    job_id: str,
    stage: Stage,
    revision_created_at: datetime,
) -> bool:
    """Return True if this revision invocation is a resume after a mid-revision park.

    A revision is a resume iff a FollowUp for this revision stage was answered
    AFTER the current (unconsumed) RevisionRequest was created. This correctly
    handles multiple sequential revisions: old FollowUp rows from completed
    prior revisions have answered_at < current RevisionRequest.created_at and
    therefore do not trigger the resume path.
    """
    stmt = (
        select(FollowUp)
        .where(
            FollowUp.job_id == job_id,
            FollowUp.stage == stage,
            FollowUp.answered_at.is_not(None),
            FollowUp.answered_at > revision_created_at,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


def _research_placeholder(stage: Stage) -> str:
    """Return a NONE placeholder brief for non-claude-cli backends or research failures."""
    tag = "INTEL_BRIEF" if stage in (Stage.cv_adjust, Stage.revising_cv) else "COMPANY_BRIEF"
    return (
        f"[{tag}]\n"
        f"NONE — no automated research available for this backend. "
        f"Ask the user for company context; do NOT attempt to browse the web.\n"
        f"[/{tag}]"
    )


def _research_spec(job: Job, stage: Stage) -> tuple[str, str, str]:
    """Return (agent_name, query, open_tag) for the given stage.

    Only ``cover_letter`` is a valid input. ``cv-research``/``cv_adjust`` is unwired
    (see CLAUDE.md → "CV structure — single source of truth"): ``cv-research.md`` and
    ``GEMINI_CV_RESEARCH.md`` are deliberately left on disk, but nothing calls this with
    ``Stage.cv_adjust`` anymore. ``_gather_research`` is never called for revision stages
    either, but this guard makes that contract explicit.
    """
    if stage == Stage.cover_letter:
        agent_name = "cl-research"
        open_tag = "[COMPANY_BRIEF]"
        query = (
            f"Company: {job.company}\n"
            f"Role: {job.role}\n"
            f"Job posting link: {job.link}"
        )
    else:
        raise ValueError(
            f"_research_spec called with unexpected stage {stage!r}; "
            "only cover_letter is supported."
        )
    return agent_name, query, open_tag


async def _gather_research(job: Job, backend: AgentBackend, stage: Stage) -> str:
    """Return a research brief block to inject into the initial user message.

    Any backend that implements ``run_research`` (e.g. ClaudeCliBackend,
    GoogleCliBackend) will have it invoked here.  Any backend without
    ``run_research``, or any research failure, yields the NONE placeholder so
    the main prompt's single code path falls back to asking the user directly.
    Research is best-effort and never fails the job.
    """
    if not hasattr(backend, "run_research"):
        return _research_placeholder(stage)

    agent_name, query, open_tag = _research_spec(job, stage)
    try:
        await bus.publish(
            event_to_dict(LogEvent(
                job_id=job.id, level="info", text=f"Researching ({agent_name})…"
            ))
        )
        text = await backend.run_research(agent_name, query)
        text = text.strip()
        if open_tag in text:
            return text
        logger.warning(
            "run_research for job %s (%s) returned output missing expected tag %r; "
            "falling back to research placeholder",
            job.id, agent_name, open_tag,
        )
        return _research_placeholder(stage)
    except Exception as exc:  # research is best-effort; never fail the job
        logger.warning(
            "research failed for job %s (%s): %s", job.id, agent_name, exc
        )
        await bus.publish(
            event_to_dict(LogEvent(
                job_id=job.id, level="warn",
                text="Research unavailable; proceeding without brief."
            ))
        )
        return _research_placeholder(stage)
