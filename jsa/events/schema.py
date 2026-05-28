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


def event_to_dict(event) -> dict:
    """Convert any event dataclass to a JSON-serialisable dict."""
    return dataclasses.asdict(event)
