"""Pure projection from raw DB rows (Message/FollowUp/Document/RevisionRequest) into
a display-ready, ordered list of transcript "turns" for GET /api/jobs/{id}/transcript.

See CLAUDE.md / the transcript-projection plan for the full derivation rules. This
module is intentionally free of any DB/session access — the caller (routes_jobs.py)
loads all four row lists and hands them in, which is what makes this unit-testable
without a database.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from jsa.db.models import Document, FollowUp, Job, Message, RevisionRequest, Stage
from jsa.schema.turn_models import unwrap_sentinel_to_canonical

# Truncation length for plumbing turns — these are hidden-by-default machine
# internals (raw assistant JSON envelopes, self-heal/nudge turns), not conversation;
# they only need to be legible enough for debugging, not fully reproduced.
_PLUMBING_MAX_LEN = 2000

# Fixed rank per turn kind, used as a sort tiebreaker (see build_transcript's
# docstring for the full sort key).
_KIND_RANK = {
    "verdict": 0,
    "question": 1,
    "answer": 2,
    "plumbing": 3,
    "delivery": 4,
}

# Coarse pipeline-progress ordering used as the PRIMARY sort key for every kind
# except "delivery" (see build_transcript). revising_cv/revising_cl share their
# anchor stage's index — they are a redo of that stage, not a later one.
_STAGE_INDEX = {
    Stage.fit_assessment: 0,
    Stage.cv_adjust: 1,
    Stage.revising_cv: 1,
    Stage.cover_letter: 2,
    Stage.revising_cl: 2,
}

_DOC_LABEL = {
    Stage.cv_adjust: "CV",
    Stage.cover_letter: "Cover Letter",
}

# Below this length, a resent-text containment check falls back to exact equality
# — a short/common answer (e.g. "yes", "None") is otherwise likely to appear as a
# spurious substring of unrelated plumbing content.
_MIN_CONTAINMENT_LEN = 12


def _matches_resent_text(original_stripped: str, content_stripped: str) -> bool:
    if len(original_stripped) < _MIN_CONTAINMENT_LEN:
        return original_stripped == content_stripped
    return original_stripped in content_stripped


def _stage_index(stage: Stage | None) -> int:
    if stage is None:
        return 0
    return _STAGE_INDEX.get(stage, 0)


def _truncate(text: str, limit: int = _PLUMBING_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _unwrap_for_display(content: str) -> str:
    """Best-effort readable projection of a raw Message.content for a plumbing turn.

    Unwraps a sentinel block into canonical JSON text (no-op for already-canonical or
    unrecognized content — see unwrap_sentinel_to_canonical's docstring), then tries
    json.loads + a compact re-dump for readability; falls back to the raw string on
    any failure. Never raises.
    """
    unwrapped = unwrap_sentinel_to_canonical(content)
    try:
        data = json.loads(unwrapped)
    except (json.JSONDecodeError, TypeError):
        return _truncate(content)
    try:
        return _truncate(json.dumps(data, ensure_ascii=False))
    except TypeError:
        return _truncate(content)


def _make_turn(
    *,
    kind: str,
    role: str,
    stage: Stage | None,
    text: str,
    created_at: datetime,
    source_id: int,
    follow_up_id: int | None = None,
    suggested_replies: list[str] | None = None,
    reasoning: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "role": role,
        "stage": stage.value if stage is not None else None,
        "text": text,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "follow_up_id": follow_up_id,
        "suggested_replies": suggested_replies,
        "reasoning": reasoning,
        # sort-only fields — stripped before returning to the caller
        "_stage_index": _stage_index(stage),
        "_timestamp": created_at,
        "_kind_rank": _KIND_RANK[kind],
        "_source_id": source_id,
    }


def build_transcript(
    job: Job,
    messages: list[Message],
    follow_ups: list[FollowUp],
    documents: list[Document],
    revision_requests: list[RevisionRequest],
) -> list[dict[str, Any]]:
    """Project raw rows into an ordered list of display-ready turns.

    Each turn: ``{seq, kind, role, stage, text, created_at, follow_up_id,
    suggested_replies, reasoning}`` where ``kind`` is one of ``question``,
    ``answer``, ``delivery``, ``verdict``, ``plumbing``.

    Sort order is total, with ``timestamp`` always the primary key: every kind except
    ``delivery`` sorts ``(timestamp, stage_index, kind_rank, source_id)``; ``delivery``
    sorts ``(timestamp, kind_rank, stage_index, source_id)`` instead — a revision's
    Document is stored under its *anchor* stage (``revising_cv -> cv_adjust``, see
    ``stages.py::_handle_final``), so a stage-first tiebreak would park a revised-CV
    delivery before the cover-letter conversation that actually preceded it in real
    time. ``stage_index``/``kind_rank`` only ever tiebreak near-identical timestamps
    within the same checkpoint — see ``_sort_key``.
    """
    turns: list[dict[str, Any]] = []

    # --- verdict --------------------------------------------------------
    if job.fit_reason:
        # Fixed timestamp source — never job.updated_at, which drifts on every
        # unrelated update to the job row. Scoped to the fit_assessment stage
        # specifically (not job-wide) -- a BF-19 rewind can re-enter
        # fit_assessment after a later stage (e.g. cv_adjust) already asked and
        # got answered a FollowUp; a job-wide min() would then anchor the verdict
        # at that earlier, unrelated FollowUp's timestamp and render the fit
        # verdict before the Q&A that actually preceded it in real time.
        fit_follow_ups = [fu for fu in follow_ups if fu.stage == Stage.fit_assessment]
        fit_messages = [m for m in messages if m.stage == Stage.fit_assessment]
        if fit_follow_ups:
            verdict_ts = min(fu.asked_at for fu in fit_follow_ups)
        elif fit_messages:
            verdict_ts = min(m.created_at for m in fit_messages)
        else:
            verdict_ts = job.created_at
        turns.append(
            _make_turn(
                kind="verdict",
                role="assistant",
                stage=Stage.fit_assessment,
                text=job.fit_reason,
                created_at=verdict_ts,
                source_id=0,
            )
        )

    # --- question / answer (from FollowUp rows, not Message rows) ------
    answered_answers_stripped: set[str] = set()
    for fu in sorted(follow_ups, key=lambda f: (f.asked_at, f.id)):
        try:
            suggested_replies = json.loads(fu.suggested_replies) if fu.suggested_replies else None
        except (json.JSONDecodeError, TypeError):
            suggested_replies = None
        turns.append(
            _make_turn(
                kind="question",
                role="assistant",
                stage=fu.stage,
                text=fu.question,
                created_at=fu.asked_at,
                source_id=fu.id,
                follow_up_id=fu.id,
                suggested_replies=suggested_replies,
            )
        )
        if fu.answered_at is not None and fu.answer is not None:
            turns.append(
                _make_turn(
                    kind="answer",
                    role="user",
                    stage=fu.stage,
                    text=fu.answer,
                    created_at=fu.answered_at,
                    source_id=fu.id,
                    follow_up_id=fu.id,
                )
            )
            stripped = fu.answer.strip()
            if stripped:
                answered_answers_stripped.add(stripped)

    # --- delivery (one per Document) ------------------------------------
    for doc in documents:
        label = _DOC_LABEL.get(doc.stage, doc.stage.value if doc.stage else "Document")
        turns.append(
            _make_turn(
                kind="delivery",
                role="assistant",
                stage=doc.stage,
                text=f"Delivered {label} v{doc.version}",
                created_at=doc.created_at,
                source_id=doc.id,
            )
        )

    # --- revision instructions (containment match against Message rows) --
    revision_instructions_stripped = [
        rr.instruction.strip() for rr in revision_requests if rr.instruction and rr.instruction.strip()
    ]

    # --- plumbing (assistant rows + un-folded user rows) -----------------
    for msg in messages:
        if msg.role == "system":
            continue
        content_stripped = msg.content.strip()

        if msg.role == "user":
            # Rule (b) is checked BEFORE rule (a): a RevisionRequest instruction,
            # possibly wire-retry-wrapped — this IS a real user turn, classify as
            # "answer" (never "plumbing" and never silently dropped), or the user's
            # own revision request hides behind the plumbing toggle. Checking this
            # first matters — an instruction that happens to contain a previously
            # answered FollowUp's answer text as a substring (e.g. the user reuses
            # the same phrasing in both) would otherwise match rule (a) below and be
            # dropped entirely before ever reaching this check. Same short-string
            # guard as rule (a).
            matched_instruction = next(
                (
                    instr
                    for instr in revision_instructions_stripped
                    if _matches_resent_text(instr, content_stripped)
                ),
                None,
            )
            if matched_instruction is not None:
                turns.append(
                    _make_turn(
                        kind="answer",
                        role="user",
                        stage=msg.stage,
                        text=matched_instruction,
                        created_at=msg.created_at,
                        source_id=msg.id,
                    )
                )
                continue

            # Rule (a): the user's own answer, re-sent verbatim (or wrapped in a
            # structured-mode wire-retry correction) at session resume — fold into
            # the answer turn already emitted above; do not render twice.
            #
            # Containment (not equality) is required because
            # `_STRUCTURED_WIRE_CORRECTION.format(original=...)` wraps the original
            # text in a preamble on a wire retry — but a short/common answer (e.g.
            # "yes", "None") can then spuriously match unrelated content. Guard
            # against that: below _MIN_CONTAINMENT_LEN, require exact equality
            # instead of substring containment.
            if any(_matches_resent_text(ans, content_stripped) for ans in answered_answers_stripped):
                continue

        # Neither fold applies (or role == assistant) — machine plumbing.
        turns.append(
            _make_turn(
                kind="plumbing",
                role=msg.role,
                stage=msg.stage,
                text=_unwrap_for_display(msg.content),
                created_at=msg.created_at,
                source_id=msg.id,
                reasoning=msg.reasoning,
            )
        )

    def _sort_key(turn: dict[str, Any]):
        # Timestamp is the primary key for EVERY turn, delivery or not — real events
        # are genuinely time-ordered, and stage_index is only ever a tiebreak for
        # near-identical timestamps within one checkpoint (its own justification for
        # why delivery reorders it to the tail). Mixing an absolute epoch float with
        # a small stage_index int in the SAME tuple position (as a literal reading of
        # "(stage_index, timestamp, ...)" for non-delivery would require) is not
        # comparable across delivery/non-delivery turns — a revision's delivery can
        # be timestamped minutes after a same-checkpoint stage_index tiebreak would
        # ever need to matter, so anchoring both key shapes on epoch-timestamp first
        # keeps the whole list correctly time-ordered while still using stage_index
        # (and, for delivery, kind_rank before stage_index) as the intended tiebreak.
        ts = turn["_timestamp"].timestamp()
        if turn["kind"] == "delivery":
            return (ts, turn["_kind_rank"], turn["_stage_index"], turn["_source_id"])
        return (ts, turn["_stage_index"], turn["_kind_rank"], turn["_source_id"])

    turns.sort(key=_sort_key)

    result: list[dict[str, Any]] = []
    for seq, turn in enumerate(turns):
        turn.pop("_stage_index")
        turn.pop("_timestamp")
        turn.pop("_kind_rank")
        turn.pop("_source_id")
        turn["seq"] = seq
        result.append(turn)
    return result
