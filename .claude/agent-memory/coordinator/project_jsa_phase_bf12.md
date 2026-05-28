---
name: project-jsa-phase-bf12
description: BF-12 hard-delete Cancel feature — patterns and gotchas for DELETE endpoint, WS job_removed event, and Zustand store removal
metadata:
  type: project
---

## Phase BF-12: Hard-delete Cancel button

**What was built:** `DELETE /api/jobs/{id}` endpoint + `JobRemovedEvent` WS event + Zustand `removeJob()` action + Cancel button visible at all non-approved states.

### Key patterns

**ORM cascade delete requires selectinload first.**
SQLAlchemy `cascade="all, delete-orphan"` works at the ORM layer, not the DB layer (no `ON DELETE CASCADE` in FK DDL, SQLite FK enforcement is off). Must eagerly load all 4 relationships (`messages`, `documents`, `follow_ups`, `revision_requests`) via `selectinload` before calling `session.delete(job)`, otherwise child rows become orphans.

**refetchAll() cannot remove rows from the Zustand store.**
`refetchAll()` only merges incoming jobs into the map (`updated[j.id] = j`) — it can never delete a key. For hard-delete actions, call `removeJob(id)` directly on success instead of `refetchAll()`. The WS `job_removed` event arrives later and is a no-op (already removed). This pattern is important: any future feature that removes a DB row must call `removeJob()` (or equivalent) directly, not rely on a refetch.

**Publish WS event after session closes, not inside `async with`.**
`bus.publish(...)` must be called OUTSIDE the `async with sf() as session:` block, after `await session.commit()`. Same pattern as all other event-emitting routes.

**DELETE endpoint bypasses state machine intentionally.**
Hard deletion is not a state transition — don't use `repo.checkpoint()` or `transition()`. Just `session.delete(job)` + `session.commit()`.

**Why:** user wanted a "remove this job from the DB and queue" action at every stage, as opposed to the existing soft-cancel (running→pending) and soft-dismiss (→dismissed).
**How to apply:** follow the same pattern for any future "permanent removal" endpoints.
