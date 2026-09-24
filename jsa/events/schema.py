"""Event envelope dataclasses and Literal types for the WebSocket event bus."""

import dataclasses
from dataclasses import dataclass
from typing import Literal


@dataclass
class StatusChangedEvent:
    type: Literal["status_changed"] = "status_changed"
    job_id: str = ""
    from_state: str = ""
    to_state: str = ""


@dataclass
class StageCompleteEvent:
    type: Literal["stage_complete"] = "stage_complete"
    job_id: str = ""
    stage: str = ""


@dataclass
class FollowUpNeededEvent:
    type: Literal["follow_up_needed"] = "follow_up_needed"
    job_id: str = ""
    follow_up_id: int = 0
    question: str = ""
    stage: str = ""


@dataclass
class LogEvent:
    type: Literal["log"] = "log"
    job_id: str = ""
    level: Literal["info", "warn", "error"] = "info"
    text: str = ""


@dataclass
class ErrorEvent:
    type: Literal["error"] = "error"
    job_id: str = ""
    message: str = ""


@dataclass
class ApprovedEvent:
    type: Literal["approved"] = "approved"
    job_id: str = ""
    cv_pdf_path: str = ""
    cl_pdf_path: str = ""


@dataclass
class JobRemovedEvent:
    type: Literal["job_removed"] = "job_removed"
    job_id: str = ""


@dataclass
class BackendSwitchedEvent:
    """Emitted when a job's backend is switched due to AgentLimitReached (BF-19)."""
    type: Literal["backend_switched"] = "backend_switched"
    job_id: str = ""
    from_backend: str = ""
    to_backend: str = ""


@dataclass
class ModelSwitchedEvent:
    """Emitted when a job hops to the next model rung on the SAME backend
    (model-first fallback ladder, tried before the BF-19 backend advance)."""
    type: Literal["model_switched"] = "model_switched"
    job_id: str = ""
    backend: str = ""
    from_model: str = ""
    to_model: str = ""


@dataclass
class TranscriptChangedEvent:
    """Invalidation hint only — no content. Emitted whenever a job's Message,
    FollowUp, Document, or RevisionRequest rows change (including reset paths that
    delete them), so clients know to refetch GET /api/jobs/{id}/transcript."""
    type: Literal["transcript_changed"] = "transcript_changed"
    job_id: str = ""


@dataclass
class InferProgressEvent:
    """Progress for a standalone CV-structure inference task (no job).

    Broadcast as each of the five inference steps becomes active; a final event with
    ``status="done"`` (or ``status="error"`` + ``message``) marks completion. The structured
    result is NOT carried here — it is returned in the HTTP response of the infer endpoint."""
    type: Literal["infer_progress"] = "infer_progress"
    task_id: str = ""
    step: int = 0
    total: int = 5
    label: str = ""
    status: Literal["active", "done", "error"] = "active"
    message: str = ""


@dataclass
class AgentChunkEvent:
    """A batched slice of streamed model output. Never the raw per-token
    stream — jsa/pipeline/stages.py's accumulator coalesces before publishing."""
    type: Literal["agent_chunk"] = "agent_chunk"
    job_id: str = ""
    stage: str = ""
    kind: Literal["content", "reasoning"] = "content"
    text: str = ""


@dataclass
class AgentToolEvent:
    """One tool call's execution outcome, part of jsa/pipeline/tool_loop.py's bounded
    revision-patching loop. Published AFTER execution (never before) so `status` is
    already known — a discrete record, not a text delta, so it is never routed through
    ChunkAccumulator's 75ms/200-char batching the way AgentChunkEvent is."""
    type: Literal["agent_tool"] = "agent_tool"
    job_id: str = ""
    stage: str = ""
    seq: int = 0
    call_id: str = ""
    name: str = ""
    summary: str = ""
    status: Literal["ok", "error", "not_executed", "budget_exhausted"] = "ok"
    detail: str = ""


@dataclass
class AgentTurnEndEvent:
    """Marks the end of one streamed turn's chunk sequence. superseded=True
    tells clients to discard the streamed buffer entirely — emitted by any of
    the retry/nudge/self-heal paths that replay a whole turn and produce a
    second assistant turn."""
    type: Literal["agent_turn_end"] = "agent_turn_end"
    job_id: str = ""
    stage: str = ""
    superseded: bool = False


@dataclass
class ChatChunkEvent:
    """One streamed delta for a CV-editor chat turn (job-less — see
    jsa/pipeline/cv_chat.py). Keyed by ``task_id``, mirroring InferProgressEvent,
    since there is no ``job_id`` here. No tool events: a chat turn is a one-shot
    reply, nothing executes mid-turn."""
    type: Literal["chat_chunk"] = "chat_chunk"
    task_id: str = ""
    kind: Literal["content", "reasoning"] = "content"
    text: str = ""


@dataclass
class ChatTurnEndEvent:
    """Marks the end of one CV-editor chat turn's streamed chunk sequence."""
    type: Literal["chat_turn_end"] = "chat_turn_end"
    task_id: str = ""
    superseded: bool = False


def event_to_dict(event) -> dict:
    """Convert any event dataclass to a JSON-serialisable dict."""
    return dataclasses.asdict(event)
