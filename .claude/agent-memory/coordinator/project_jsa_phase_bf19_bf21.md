---
name: project-jsa-phase-bf19-bf21
description: Gotchas and decisions from Phase BF-19 (backend chain) and BF-21 (export endpoint + frontend toggle)
metadata:
  type: project
---

## BF-19: Backend chain + per-job backend + mid-stage switch

**Fact:** `_wrap_factory` shim in `orchestrator.py` uses `inspect.signature()` to handle both 0-arg (legacy) and 1-arg (name-parameterized) backend factories. New tests should use the 1-arg `factory(name)` style.
**Why:** Avoids breaking existing tests while the API evolves.
**How to apply:** If adding new backends or factories, always use the 1-arg signature.

**Fact:** `backend_switch_reset` deletes ALL FollowUps (including answered) when rewinding revision stages, not just unanswered ones.
**Why:** Answered FollowUps cause `_is_revision_resume` to return True with empty history (Messages deleted), leading to silent divergence on the new backend.
**How to apply:** This is intentional — don't change it back to "unanswered only."

**Fact:** State machine additions (`running → pending`, `running → cv_done`, `running → review`) are exclusively for backend-switch restarts and are all tagged `# backend-switch restart` in the ALLOWED table.
**Why:** Prevents accidental misuse outside the switch path.

**Fact:** Missing visual "⚡ Switched backend" log in the UI. `BackendSwitchedEvent` is plumbed (emitted, typed, WS handler, `refetchAll`) but surfaces only as `console.info`. No LogTail/activity-log component exists yet.
**Why:** Known gap — no LogTail component in the UI. Track as future work.
**How to apply:** If adding a LogTail or activity log component later, `BackendSwitchedEvent` is already fully wired on the backend — just add the frontend rendering.

**Fact:** `stages.py` now allows `revision_session_id=None` if history (Message rows) is available — backend switch clears session ID but preserves history. Error only if BOTH are missing.

## BF-21: Export endpoint + frontend toggle

**Fact:** `GET /api/files/{relpath:path}` is a new route not in ARCH.md's REST table, added to serve output_dir files for browser download.
**Why:** No other mechanism exists to serve rendered output files.

**Fact:** `toRel` helper in `ReviewPane.tsx` extracts last two path segments (slug/filename) from absolute DB path to form `/api/files/<relpath>`. Must be defined OUTSIDE both if-blocks so both PDF and DOCX seeding can use it.
**Why:** Had a post-review fix to hoist the function — duplicate definitions caused a TS compile error.
**How to apply:** Keep `toRel` at the enclosing scope of the `initialLinks` block.

**Fact:** Export endpoint uses renderer key `"weasyprint"` (not `"pdf"`) for the PDF renderer — the registry key is `"weasyprint"`, not `"pdf"`.
**How to apply:** When calling `renderer_for("pdf")` in tests or new code, use `"weasyprint"` as the key.

**Fact:** `_doc_to_dict` was stale (missing `docx_path`) until BF-21 fixed it. BF-20 added the DB column but left the serializer incomplete.
