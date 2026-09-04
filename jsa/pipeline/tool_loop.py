"""Bounded tool-calling loop for revision patching (revision-tool-use plan, Phase 2).

Transport-agnostic — lives in the pipeline layer, not in any backend, per the plan's
Phase 2 rationale (``_openai_compat.py``'s "batching lives in the pipeline, NOT in any
backend" precedent) and because the applier needs ``CVDocument``/``CoverLetter``, which
``jsa/agents/`` must not import.

``run_tool_loop`` owns session establishment for whichever rung it enters or downgrades
to (always via ``backend.restore_session`` — revisions always restore, never
``start_session``), executes the model's tool calls against a ``CvWorkingCopy``/
``ClWorkingCopy`` (``jsa/schema/patch.py``) under a hard 10-call budget (D1), and returns
either a synthesized terminal ``AgentReply`` (``finalize``/``ask_user`` succeeded) or
``None``. ``None`` means "give up on tool mode for this turn" — the caller
(``jsa/pipeline/stages.py``, Phase 5) falls through to the existing, unmodified
full-rewrite path (rung 3) with the CV injected, exactly as today. This module never
implements rung 3 itself.

The ladder (``ToolMode``: native → prompt → give up) is a per-turn local, never
persisted — see ``_enter_rung``. Argument-validation failures (a bad ``entry_id``, a
malformed ``bullets`` list, ...) never trigger a downgrade (D5): they come back as an
ordinary tool result and the model gets to try again within the same budget.

Tool mode never streams (an on_chunk-driven backend call degrades to `use_stream=False`
by construction when `on_chunk` is omitted — see the plan's Phase 2 finding #1), so this
module never passes `on_chunk`/`on_retry` to any backend call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from jsa.agents.base import (
    AgentBackend,
    AgentReply,
    HistoryTurn,
    SessionHandle,
    ToolCall,
    ToolResult,
    ToolsUnsupported,
)
from jsa.agents.tool_spec import ToolSpec, tools_for
from jsa.db.models import Job, Stage
from jsa.events.bus import bus
from jsa.events.schema import AgentToolEvent, LogEvent, event_to_dict
from jsa.schema.cover_letter import CoverLetter
from jsa.schema.cv import CVDocument
from jsa.schema.patch import ClWorkingCopy, CvWorkingCopy

logger = logging.getLogger(__name__)

# D1: hard cap on individual tool calls per turn (counts calls, not round-trips —
# see the plan's "two rules D1 leaves open").
TOOL_BUDGET = 10

ToolMode = Literal["native", "prompt"]

# D2: the two tools shared by both CV and CL vocabularies that end a turn.
_TERMINAL_TOOLS = frozenset({"finalize", "ask_user"})

# Tools that don't mutate the working copy — excluded from the "N edits discarded"
# count published when ask_user fires (D8).
_NON_MUTATING_TOOLS = frozenset({"get_cv", "get_letter", "finalize", "ask_user"})

_TOOL_REEMIT_HINT = (
    "That did not finalize. Fix the issue using the available tools (call get_cv or "
    "get_letter again first if you need fresh ids), then call finalize again."
)

_NOT_EXECUTED = {
    "ok": False,
    "error": {
        "code": "not_executed",
        "message": "a terminal tool (finalize/ask_user) already ran earlier this turn",
    },
}
_BUDGET_EXHAUSTED = {
    "ok": False,
    "error": {
        "code": "budget_exhausted",
        "message": "the tool-call budget for this turn is exhausted",
    },
}


def _bad_argument(message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": "bad_argument", "message": message}}


@dataclass
class ToolLoopResult:
    """What ``run_tool_loop`` hands back on success (a terminal tool fired)."""

    reply: AgentReply
    handle: SessionHandle
    mode: ToolMode
    # Execution log for this whole loop invocation, in order:
    # {"call_id", "name", "arguments", "result"} per tool call actually attempted
    # (including not_executed/budget_exhausted entries). Persistence into
    # role="tool" Message rows is Phase 5's job (jsa/pipeline/stages.py) — this is
    # the raw material for it, not a persisted shape itself.
    tool_calls: list[dict[str, Any]]


async def _enter_rung(
    backend: AgentBackend,
    mode: ToolMode,
    specs: tuple[ToolSpec, ...],
    history: list[HistoryTurn],
    external_id: str | None,
    build_system_prompt: Callable[[ToolMode], str],
    instruction: str,
) -> tuple[SessionHandle, AgentReply]:
    """Rebuild the session for ``mode`` and send the (verbatim) instruction as the
    first turn. Used both for the initial rung and for the one-shot native→prompt
    downgrade — a pure local restore_session + one send_message, no replay of any
    prior rung's turns into the new session (see the module docstring)."""
    system_prompt = build_system_prompt(mode)
    tools_kwarg = {"tools": specs} if mode == "native" else {}
    handle = await backend.restore_session(system_prompt, history, external_id, **tools_kwarg)
    reply = await backend.send_message(handle, instruction)
    return handle, reply


def _no_parseable_call(reply: AgentReply) -> bool:
    return reply.kind != "tool_calls" or not reply.tool_calls


def _dispatch_cv(copy: CvWorkingCopy, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_cv":
        return copy.get_cv()
    if name == "replace_summary":
        return copy.replace_summary(args.get("text"))
    if name == "replace_section":
        return copy.replace_section(args.get("section_id"), args.get("section"))
    if name == "replace_entry":
        return copy.replace_entry(args.get("entry_id"), args.get("entry"))
    if name == "edit_entry_bullets":
        return copy.edit_entry_bullets(args.get("entry_id"), args.get("bullets"))
    if name == "add_entry":
        return copy.add_entry(args.get("section_id"), args.get("entry"), args.get("position"))
    if name == "remove_entry":
        return copy.remove_entry(args.get("entry_id"))
    if name == "reorder_entries":
        return copy.reorder_entries(args.get("section_id"), args.get("order"))
    return _bad_argument(f"unknown tool {name!r}")


def _dispatch_cl(copy: ClWorkingCopy, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_letter":
        return copy.get_letter()
    if name == "replace_paragraph":
        return copy.replace_paragraph(args.get("paragraph_id"), args.get("text"))
    if name == "add_paragraph":
        return copy.add_paragraph(args.get("text"), args.get("position"))
    if name == "remove_paragraph":
        return copy.remove_paragraph(args.get("paragraph_id"))
    if name == "reorder_paragraphs":
        return copy.reorder_paragraphs(args.get("order"))
    if name == "set_salutation":
        return copy.set_salutation(args.get("text"))
    if name == "set_signoff":
        return copy.set_signoff(args.get("text"))
    return _bad_argument(f"unknown tool {name!r}")


def _dispatch_ask_user(args: dict[str, Any]) -> tuple[dict[str, Any], AgentReply | None]:
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return _bad_argument("question must be a non-empty string"), None
    q = question.strip()
    suggested = args.get("suggested_replies")
    if not (
        isinstance(suggested, list) and suggested and all(isinstance(s, str) for s in suggested)
    ):
        suggested = None
    payload: dict[str, Any] = {"kind": "question", "question": q, "payload": None}
    if suggested:
        payload["suggested_replies"] = suggested
    final = AgentReply(
        raw=json.dumps(payload), content=q, kind="needs_input",
        question=q, suggested_replies=suggested,
    )
    return {"ok": True}, final


def _dispatch_finalize(
    copy: CvWorkingCopy | ClWorkingCopy, language: str
) -> tuple[dict[str, Any], AgentReply | None]:
    result = copy.finalize(language=language, reemit_hint=_TOOL_REEMIT_HINT)
    if not result.get("ok"):
        return result, None
    document = result["document"]
    doc_payload = document.model_dump(mode="json")
    raw = json.dumps({"kind": "final", "question": None, "payload": doc_payload})
    content = json.dumps(doc_payload)
    final = AgentReply(raw=raw, content=content, kind="final")
    # The public tool-result content stays JSON-safe and small — the model already
    # knows what it wrote; it doesn't need the whole document echoed back.
    return {"ok": True}, final


def _dispatch(
    copy: CvWorkingCopy | ClWorkingCopy, stage: Stage, call: ToolCall, language: str
) -> tuple[dict[str, Any], AgentReply | None, bool]:
    """Execute one tool call. Returns ``(result, final_reply, is_genuinely_terminal)``.

    ``is_genuinely_terminal`` is True only for a SUCCESSFUL finalize/ask_user — a
    finalize that fails validation returns an ordinary error result and the loop
    keeps going (the model can fix it and call finalize again within the same
    budget); D1's "stop at the first terminal tool" governs array-order execution
    regardless of success (see ``run_tool_loop``), but only success ends the loop.
    """
    args = call.arguments if isinstance(call.arguments, dict) else {}
    if call.name == "finalize":
        result, final = _dispatch_finalize(copy, language)
        return result, final, final is not None
    if call.name == "ask_user":
        result, final = _dispatch_ask_user(args)
        return result, final, final is not None
    if stage is Stage.revising_cv:
        assert isinstance(copy, CvWorkingCopy)
        return _dispatch_cv(copy, call.name, args), None, False
    assert isinstance(copy, ClWorkingCopy)
    return _dispatch_cl(copy, call.name, args), None, False


def _summarize(name: str, result: dict[str, Any]) -> str:
    if result.get("ok"):
        return name
    error = result.get("error") or {}
    return f"{name} failed: {error.get('code', 'error')}"


async def run_tool_loop(
    *,
    backend: AgentBackend,
    stage: Stage,
    job: Job,
    document: CVDocument | CoverLetter,
    instruction: str,
    history: list[HistoryTurn],
    external_id: str | None,
    build_system_prompt: Callable[[ToolMode], str],
    language: str = "en",
) -> ToolLoopResult | None:
    """Run the bounded tool-patching loop for one ``revising_cv``/``revising_cl`` turn.

    ``document`` seeds the working copy (the latest ``Document.structured`` for this
    job+stage). ``instruction`` is ``rev_req.instruction`` (or the resume answer text)
    sent VERBATIM as the first turn on rungs 1–2 — never inject the CV here, that would
    defeat the entire token saving, since ``get_cv``/``get_letter`` provides it (only
    rung 3, owned by the caller, injects the CV).

    ``history``/``external_id`` are what the caller would otherwise have passed to its
    own ``restore_session`` call — this function owns that call instead (for both the
    entry rung and the one native→prompt downgrade attempt), so entry and downgrade
    share one code path. ``build_system_prompt(mode)`` supplies the mode-appropriate
    system prompt (native: short contract; prompt: full contract with inlined schemas
    and the ``<<<TOOL_CALLS>>>`` grammar — Phase 4's ``_tool_contract``); this module
    stays prompt-assembly-agnostic on purpose.

    Returns ``None`` when even the prompt rung fails to produce a parseable tool call,
    or when the budget is exhausted without a successful terminal tool — the caller then
    falls through to the existing rung-3 path. Never raises for a normal tool-usage
    failure (bad argument, stale id, failed validation) — those become ordinary tool
    results the model can react to within the same budget (D5: argument-validation
    failures never downgrade the rung).
    """
    specs = tools_for(stage)  # ValueError outside revising_cv/revising_cl — D3
    is_cv = stage is Stage.revising_cv
    copy: CvWorkingCopy | ClWorkingCopy = (
        CvWorkingCopy(document) if is_cv else ClWorkingCopy(document)  # type: ignore[arg-type]
    )

    mode: ToolMode = "native" if getattr(backend, "supports_native_tools", False) else "prompt"

    try:
        handle, reply = await _enter_rung(
            backend, mode, specs, history, external_id, build_system_prompt, instruction
        )
        unparseable = _no_parseable_call(reply)
    except ToolsUnsupported:
        handle, reply, unparseable = None, None, True  # type: ignore[assignment]

    if mode == "native" and unparseable:
        logger.info(
            "tool_loop: stage %s job %s downgrading native -> prompt rung",
            stage.value, job.id,
        )
        mode = "prompt"
        try:
            handle, reply = await _enter_rung(
                backend, mode, specs, history, external_id, build_system_prompt, instruction
            )
            unparseable = _no_parseable_call(reply)
        except ToolsUnsupported:
            unparseable = True

    if unparseable:
        logger.info(
            "tool_loop: stage %s job %s giving up on tool mode (%s rung unparseable); "
            "falling back to full-rewrite",
            stage.value, job.id, mode,
        )
        return None  # rung 3 is the caller's job

    assert handle is not None and reply is not None

    budget = TOOL_BUDGET
    seq = 0
    executed: list[dict[str, Any]] = []

    while reply.kind == "tool_calls":
        if not reply.tool_calls:
            return None  # defensive; parse-level guarantees make this unreachable
        results: list[ToolResult] = []
        stop_calls = False
        final_reply: AgentReply | None = None
        finalize_change_log: str | None = None
        for call in reply.tool_calls:
            seq += 1
            if stop_calls:
                outcome, status = dict(_NOT_EXECUTED), "not_executed"
            elif budget <= 0:
                outcome, status = dict(_BUDGET_EXHAUSTED), "budget_exhausted"
            else:
                budget -= 1
                outcome, candidate, is_terminal = _dispatch(copy, stage, call, language)
                status = "ok" if outcome.get("ok") else "error"
                if call.name in _TERMINAL_TOOLS:
                    stop_calls = True
                    if is_terminal:
                        final_reply = candidate
                        if call.name == "finalize":
                            finalize_change_log = call.arguments.get("change_log") \
                                if isinstance(call.arguments, dict) else None

            results.append(
                ToolResult(call_id=call.id, name=call.name, ok=bool(outcome.get("ok")), content=outcome)
            )
            executed.append(
                {"call_id": call.id, "name": call.name, "arguments": call.arguments, "result": outcome}
            )
            await bus.publish(
                event_to_dict(
                    AgentToolEvent(
                        job_id=job.id, stage=stage.value, seq=seq, call_id=call.id,
                        name=call.name, summary=_summarize(call.name, outcome),
                        status=status, detail=json.dumps(outcome),
                    )
                )
            )

        if final_reply is not None:
            if finalize_change_log is not None:
                # A schema-valid but empty change_log ("" — the tool's `_STRING` param
                # has no minLength) must still log that finalize succeeded; only a
                # genuinely absent change_log (finalize_change_log is None, i.e. this
                # wasn't a successful finalize call) skips this branch.
                summary = finalize_change_log or "(no summary provided)"
                await bus.publish(
                    event_to_dict(
                        LogEvent(
                            job_id=job.id, level="info",
                            text=f"Stage {stage.value}: revision finalized — {summary}",
                        )
                    )
                )
            elif final_reply.kind == "needs_input":
                dropped = sum(
                    1 for e in executed
                    if e["name"] not in _NON_MUTATING_TOOLS and e["result"].get("ok")
                )
                await bus.publish(
                    event_to_dict(
                        LogEvent(
                            job_id=job.id, level="info",
                            text=(
                                f"Stage {stage.value}: revision paused for a clarifying "
                                f"question — {dropped} edit(s) made this turn were discarded"
                            ),
                        )
                    )
                )
            return ToolLoopResult(reply=final_reply, handle=handle, mode=mode, tool_calls=executed)

        if budget <= 0:
            logger.info(
                "tool_loop: stage %s job %s exhausted the %d-call budget without a "
                "terminal tool; falling back to full-rewrite",
                stage.value, job.id, TOOL_BUDGET,
            )
            return None  # exhausted without a terminal — rung 3 is the caller's job

        reply = await backend.send_tool_results(handle, results)

    # A mid-loop reply that isn't kind=="tool_calls" and never went through a terminal
    # tool is a broken tool-mode turn (native/prompt tool_choice is meant to force one
    # or the other every round) — abandon to rung 3 rather than guess.
    logger.info(
        "tool_loop: stage %s job %s got a non-tool_calls reply mid-loop; "
        "falling back to full-rewrite",
        stage.value, job.id,
    )
    return None
