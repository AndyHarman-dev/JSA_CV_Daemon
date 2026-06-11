"""DevAutoResponder — background task that auto-answers NEED_INPUT gates in dev mode.

Only started when Settings.dev_autoanswer is True. Has zero effect on production paths.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jsa.db.models import FollowUp, Job, JobState
from jsa.dev.answers import load_rules, match_answer
from jsa.events.bus import bus

logger = logging.getLogger(__name__)

_AUTO_ANSWER_CAP = 8  # max auto-answers per (job_id, stage) to prevent runaway loops


class DevAutoResponder:
    """Listens on the event bus and auto-answers FollowUp gates with pattern-matched replies.

    Answers only 'follow_up_needed' events. Does NOT touch 'review' or 'unfit' states.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        orchestrator,
        rules_path: Path,
        cap: int = _AUTO_ANSWER_CAP,
    ) -> None:
        self._sf = session_factory
        self._orchestrator = orchestrator
        self._rules_path = rules_path
        self._cap = cap
        self._stopping = False
        # Per-(job_id, stage) counter to enforce cap
        self._counts: dict[tuple[str, str], int] = defaultdict(int)

    async def run(self) -> None:
        """Subscribe to the event bus and auto-answer follow-up events."""
        logger.warning(
            "[DEV] DevAutoResponder active — NEED_INPUT gates will be auto-answered. "
            "Rules file: %s",
            self._rules_path,
        )

        # Subscribe FIRST so events published during the startup scan land in the queue.
        q = bus.subscribe()
        try:
            # One-shot startup scan: clear jobs already parked before the server started.
            await self._startup_scan()

            while not self._stopping:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if event.get("type") == "follow_up_needed":
                    await self._handle_event(event)
        finally:
            bus.unsubscribe(q)

    async def _startup_scan(self) -> None:
        """Clear any jobs that were already parked in awaiting_input before startup."""
        async with self._sf() as session:
            result = await session.execute(
                select(FollowUp)
                .join(Job, FollowUp.job_id == Job.id)
                .where(
                    Job.state == JobState.awaiting_input,
                    FollowUp.answered_at.is_(None),
                )
            )
            pending_fus = list(result.scalars().all())

        for fu in pending_fus:
            await self._answer_followup(fu.job_id, fu.id, fu.question, str(fu.stage.value))

        if pending_fus:
            self._orchestrator.kick()

    async def _handle_event(self, event: dict) -> None:
        job_id: str = event["job_id"]
        follow_up_id: int = event["follow_up_id"]
        question: str = event["question"]
        stage: str = event["stage"]

        answered = await self._answer_followup(job_id, follow_up_id, question, stage)
        if answered:
            self._orchestrator.kick()

    async def _answer_followup(
        self, job_id: str, follow_up_id: int, question: str, stage: str
    ) -> bool:
        """Write answer to the FollowUp row. Returns True if answered, False if skipped."""
        key = (job_id, stage)
        if self._counts[key] >= self._cap:
            logger.warning(
                "[DEV] Auto-answer cap (%d) reached for job=%s stage=%s — stopping auto-answer for this stage",
                _AUTO_ANSWER_CAP,
                job_id,
                stage,
            )
            return False

        rules = load_rules(self._rules_path)
        answer = match_answer(question, rules)

        async with self._sf() as session:
            result = await session.execute(
                select(FollowUp).where(FollowUp.id == follow_up_id)
            )
            fu = result.scalar_one_or_none()
            if fu is None or fu.answered_at is not None:
                return False  # already answered or gone

            fu.answer = answer
            fu.answered_at = datetime.utcnow()
            session.add(fu)
            await session.commit()

        self._counts[key] += 1
        logger.warning(
            "[DEV] Auto-answered job=%s stage=%s follow_up_id=%d\n  Q: %r\n  A: %r",
            job_id,
            stage,
            follow_up_id,
            question,
            answer,
        )
        return True
