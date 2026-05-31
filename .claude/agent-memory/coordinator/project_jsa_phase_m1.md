---
name: project-jsa-phase-m1
description: Phase M-1 mobile-responsive UI — single-panel nav, compact header, store selectJob widening
metadata:
  type: project
---

## Phase M-1: Mobile-responsive UI (complete, 2026-05-30)

**Why:** Fixed `w-80` sidebar took 82% of a 390px iPhone screen; detail panel was invisible.

**Pattern used:** Two-panel desktop / single-panel mobile (no router, no extra state):
- `App.tsx` reads `selectedId`; on mobile hides aside when job selected, hides main when nothing selected
- `JobDetail.tsx` gets a `← Back` button (`md:hidden`) calling `selectJob(undefined)`
- `Header.tsx` hides count badges on mobile (`hidden md:flex`), short title on mobile

**Store change:** `selectJob(id: string | undefined)` — widened from `string` to allow clearing selection. All existing callers still valid.

**Gotcha (caught in review):** The Back button must also be in the placeholder return (`selectedId set, job missing`) not just the normal return — otherwise mobile users get stuck with no escape route.

**Stale tests fixed:** `cancel_bf6.test.tsx` was asserting old "Cancel → api.cancel" behaviour; BF-12 had renamed it to "Delete → api.deleteJob". Updated assertions + added `window.confirm` spy (jsdom returns false by default, blocking the handler).

**Final test count:** 170/170 passed.
