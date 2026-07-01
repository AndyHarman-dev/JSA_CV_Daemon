---
status: Done
---

# JSA — Cyberpunk Daemon Redesign

## Context

The current JSA frontend is a plain, warm/editorial Tailwind UI (`bg-gray-50` shell, blue/paper `cv-*` palette, Geist/Newsreader fonts). The design team produced a full high-fidelity **"serious daemon" cyberpunk** redesign covering *every* screen — dark HUD surfaces, glowing accents, chamfered panels, and system/process-flavored copy. The goal is that the user feels like they're **directing an automated daemon running jobs in parallel**, not filling out a form.

The handoff (`.claude/designs/design_handoff_cyberpunk_redesign/`) ships two interactive `.dc.html` references built on an in-house runtime that **does not exist in our stack** (`support.js`, `<x-dc>`) and must not be ported. This is an almost-entirely **visual/structural** pass: we keep all existing data flow, state machine, API calls, and store logic, and restyle + lightly restructure the React components to match the references pixel-for-pixel.

This redesign is intentionally **not** a behavior rewrite — every Zustand read, `api.*` call, and `refetchAll()` site stays. Three small *new* interaction affordances are added (logo-as-switch nav, backend failover cluster, download menu — the last already exists).

### Confirmed design decisions
- **Two accents:** Amber `#F4CE4A` for the **Jobs shell** (`JSA // DAEMON`); Red `#FF4655` for the **CV editor** (`CV // DAEMON`). Accent-2 cyan `#33E6E6` is fixed for "system/live" signals in both (uplink dot, active backend, JSON live tag, pipeline glow). Note: in the editor, the red accent shares its hue with the danger color — primary (filled) vs danger (outlined) buttons stay distinguishable by fill, which is acceptable.
- **Skin: `hud`** everywhere — one corner-chamfer per panel via `clip-path` (8–18px), 7px button radius, **no** L-corner brackets. Used consistently; never mixed with `terminal`.
- **Ambient telemetry: on** — faint cyan grid, slow ~16s scanline sweep, corner mono session/sync readouts. Decorative only.
- **Density `comfortable`, `paperSerif` on** (the latter already exists in `editorStore`).

## Why this approach (architecture)

Tailwind here is **v3.4.4**, which *cannot evaluate `color-mix()` at build time* (the existing config even comments on this). The design uses dynamic, accent-derived `color-mix(...)` fills/borders and chamfer `clip-path` polygons *everywhere*. Fighting that through Tailwind arbitrary values would be lossy and unmaintainable.

Instead we mirror the design's own architecture: a **shared TypeScript theme module** that ports the reference's `T` token object + helper functions verbatim, and components render with **inline styles** off those tokens (exactly as the `.dc.html` files do). Tailwind stays for trivial layout utilities but is not the styling backbone for chrome. The README explicitly blesses "a small custom theme extension." Inline styles can't express hover/focus/placeholder/keyframes, so the design's **global CSS classes** (`.jbtn:hover`, `.cvf-card:focus`, `jsblink`/`jsspin`/`jsscan`/`cvpulse` keyframes) are ported into `index.css`.

We **unify** the two near-identical design files into **one** theme module + **one** icon set (superset of both) + **one** global CSS injection, parameterized by `makeTheme(accent, skin, density)` and instantiated once per surface (amber for shell, red for editor).

---

## Phase 1 — Foundation (theme + icons + global CSS + fonts)

New shared module `frontend/src/theme/` (no new store state — these are module constants):

- `tokens.ts` — `makeTheme(accent, skin='hud', density='comfortable')` returning the `T` object ported from the design refs (`canvas/surface/subtle/sunk`, `bd/bd2`, `ink/ink2/ink3`, `accent2`, `danger`, `violet`, `green`, `aSoft/aSoft2/aBorder` via `color-mix`, `pad/gap/secGap`, `chamfer/radius/btnRadius`, shadows, font stacks). Plus `paperT` (the fixed light/paper palette). Export two ready instances: `SHELL_THEME` (amber) and `EDITOR_THEME` (red).
- `chrome.tsx` — `chamferPath(c)`, `panelBase(T, opts)`, `cornerMarks(T,...)` (returns `[]` under `hud`, kept for completeness), and a `<Badge state>` helper (the `STATE_META` map: label/code/color/spin per `JobState`).
- `Icon.tsx` — single `<Icon name size color/>` component porting the **superset** of both icon maps (shell: `bolt, server, refresh, send, download, chevron, alert, inbox, doc, work, x, plus, check, trash, mail, phone, pin, link, back`; editor adds: `up, down, undo, redo, braces, spark, eye, copy, text, list, tag, cap, blocks, cols`), plus a `<Grip>` glyph. 16-viewBox, stroke 1.55. This replaces the per-instance inline icons and the `ui.tsx` `svg()` factory's icon set.
- `Ambient.tsx` — the grid + scanline + corner-readout layer, gated by a `telemetry` prop (default on); `clockTick` via a 1s interval and a per-mount session hex.

Global CSS — append to `frontend/src/index.css`: keyframes (`jsfade, jsspin, jsblink, jsscan, cvpulse, cvbar`), interaction classes (`.jbtn, .jghost, .jprimary, .jdanger, .jta, .jtab, .jrow, .cvf-card, .cvf-paper, .cvbtn, .cvbtn-light, .cvprimary, .cvghost, .cvsec/.cvtools, .cvchip/.cvx, .cvitem/.cvih, .cvgap/.cvadd, .cvkindbtn`), `::selection`, scrollbar styling, and `:root{--a;--pa}` CSS vars used by the focus-ring classes. The `var(--a)` is set per-surface via a small `<style>` injected by each root (shell vs editor) so the same `.cvf-card:focus` class glows amber in the shell and red in the editor.

Fonts — replace the Geist/Newsreader `@import` in `index.css` with the design's set: **Chakra Petch** (display/labels/buttons), **IBM Plex Sans** (body), **Share Tech Mono** (mono/telemetry/code), **Newsreader** (paper serif). Update `tailwind.config.js` `fontFamily` keys accordingly and retire the warm `cv-*` color palette (or repoint it; the theme module is now the source of truth).

## Phase 2 — App shell restyle (amber `SHELL_THEME`)

- `App.tsx` — swap `bg-gray-50`/`bg-white`/`border-gray-200` for `T.canvas` + the column→header→(rail|main) structure with `Ambient` underlay. **Keep** the responsive master-detail logic (`hidden md:block` list/detail swap — it's behavior guarded by `mobile_responsive.test.tsx`). Editor still mounts via `{editorOpen && <CvEditor/>}` as a full-screen overlay.
- `Header.tsx` — full restyle per ref `renderHeader`: left logo button (`bolt` tile + `JSA // DAEMON` + subtitle) → **now opens the editor** (`setEditorOpen(true)`), replacing and removing the separate "Structure Editor" button; center cluster = **backend cluster** (Phase 4) + **Workers meter** (count of `running/pending/cv_done/cl_done` capped at 5) + glowing mono count chips (Inbox/Review/Done/Failed, hidden when 0); right = **UPLINK** status (map `wsStatus`: open→`SYNCED`/cyan, connecting→`RECONNECTING`/amber, closed→`LOST`/red) + restyled hard-reload (`nuclearReload` preserved). Preserve `RUNNING_STATES`/`countByStates`/`api.config()` logic.
- `StatusBadge.tsx` — reduce to the `<Badge>` helper: mono pill + colored dot (solid for static, spinning ring for `running`). Preserve `STATE_CONFIG` label + spinner flag; colors come from `STATE_META`.
- `JobList.tsx` — restyle to `PROCESS_QUEUE` rail (290px), grouped exactly per existing `GROUPS` (unchanged order/membership, incl. `fit_done` in Running). Rows: company + `T{A/B/C}` tier code, role, badge; selected row gets accent left-bar + glow + border. Empty groups still skipped.
- `JobDetail.tsx` — restyle title row (`{Company} — {Role}` + tier pill + badge + right-aligned actions), **preserving** `showCancel/showDismiss/showRequeue/showRetry` rules and the nuclear-reset confirm (`retry_count > 0`). Replace JD `▶/▼` with chevron-rotate disclosure. Error box → bracketed **EXCEPTION** panel (shown on `job.error || state==='failed'`). Pipeline panel wraps `StageTimeline`. All handlers (`handleDelete/Dismiss/Requeue/Retry/NuclearConfirm`) unchanged.
- `StageTimeline.tsx` — visual replacement only: 7 glowing thread-nodes (`PENDING/CV_ADJUST/CV_DONE/COVER_LETTER/CL_DONE/REVIEW/APPROVED`) + connecting line glowing cyan up to active. Preserve `getActiveStepIndex` exactly.
- `FollowUpPane.tsx` + `ChatBox.tsx` — relabel section `AGENT_QUERY · BLOCKING`, accent-tinted question card, restyled textarea + `SUBMIT_ANSWER`/`REQUEST_REVISION` button. Preserve fetch + both submit (`answer`/`revise`) flows.
- `ReviewPane.tsx` — restyle tabs (`CV / RESUME` ↔ `COVER_LETTER`), version label, the **real PDF `<iframe>`** preview inside the dark `EXPORT_PREVIEW` frame (not the mock paper placeholder). The PDF/DOCX **download menu already exists** — restyle to the dropdown UI, keep `toFileUrl`/`downloadFormat`/path-fetch effect. Approve → green glowing `APPROVE & EXPORT`; approved → success strip. `handleApprove` unchanged.
- `UnfitModal.tsx` — corner-chamfered centered 460px panel, `FIT_ASSESSMENT: MISMATCH` heading, accent-tinted reason card, `Ignore & Continue`/`Dismiss Job`. Preserve `run('dismiss'|'ignore')`.

## Phase 3 — CV Structure Editor restyle (red `EDITOR_THEME`)

- `CvEditor.tsx` — restyle topbar per ref `renderTopbar`: left **logo-as-switch** (`CV // DAEMON`, `blocks` tile) → clicking it calls the existing `onDone()` (save→close); when CV loaded, `OPERATOR` readout (contact name + `N MODULES` pill). Right: live clock + session hex, the **BLOCKS/DOCUMENT/SPLIT** segmented control (numbered `01/02/03`, drives `st.setView`), undo/redo group, `SRC.JSON` toggle, `RUN INFERENCE`/`RE-RUN` (the existing `FileButton`/`inferFromFile`), and **COMMIT** (primary glowing) → also `onDone()`. Both logo and COMMIT are the only exits (no floating close). Preserve mount-load, keyboard undo/redo, and the save-blocks-exit semantics.
- `InferringState()` — render the 5-row `THREAD_01..05` panel (`PARSE_DOCUMENT…VALIDATE_SCHEMA`) with spinner/check + thin glowing bars. **Wire to the real sequential `infer_progress` step events** (`inferStep/inferTotal=5/inferLabel/inferError`): rows ≤ current step show done/active. (See "Known deviations" — true parallel overlap is mock-only.)
- `EmptyState()` — bracketed `NO STRUCTURE DETECTED` + `RUN INFERENCE` (primary) / `INIT BLANK` (`startBlank`).
- `BlocksView.tsx` — restyle `ContactCard` (`IDENTITY`), per-section `SectionCard` (`MOD_0N`, drag handle, kind badge, editable title, hover move/delete tools), kind-specific bodies (summary textarea / bullets / skills tag-editor / experience-projects-education entry cards), and the `INJECT MODULE` popover (`AddSectionMenu`, the 6 `ADD_KINDS`). **Preserve** HTML5 drag-arm-from-grip reorder, all `st.*` mutation calls, and the `applyEdit` coalesce/commit timing.
- `PaperSheet.tsx` / `SplitView.tsx` — keep Document/Split **light/paper** (intentionally not reskinned dark), but wrap in the dark `EXPORT_PREVIEW · READ-ONLY LAYOUT` viewport frame (dashed border + chamfer + pulsing cyan dot). Split = 280px `INDEX` rail (icon/name/`code · count`, selection ring) + paper. Repoint the two hardcoded colors (paper selection ring `#3B5BD9/#E2E6F9` → `paperT.a`; replace as needed) to `paperT`.
- `JsonDrawer.tsx` — restyle to `SOURCE.JSON` (400px) with `LIVE · READ-ONLY · SCHEMA-VALID` pulsing tag, copy/close, syntax-highlighted `<pre>` (string green / key cyan / number-bool pink-amber). Preserve `exportJson`-derived text + highlight regex; fix the hardcoded live-dot color to `T.accent2`.
- `ui.tsx` — retire its private icon `svg()` set in favor of the shared `Icon`; keep `AutoTextarea`, `ContentInput`, `useRefit`, and `KIND_ICON`/`KIND_LABEL` (repointed to shared icons).

## Phase 4 — Backend failover cluster (client-side, no backend change)

`/api/config` already returns `backend` (active) **and** `backends` (ordered chain; `backends[0]` primary). Build the cluster in `Header.tsx`:
- Derive `queue = backends.map((id,i) => ({ id, label: LABELS[id], status: i===0 ? 'active' : 'standby' }))` from the existing `api.config()` call (extend Header's `backendState` to hold the list, not just the active string).
- Render the button (`server` icon + `BACKEND` + active label + `+N` standby) and the dropdown (`BACKEND FAILOVER QUEUE`, numbered rows with ACTIVE/STANDBY + the failover explainer). Add outside-click close.
- On the existing `backend_switched` WS event (already in `types.ts`/`applyEvent`), **shift the active marker** to `to_backend` client-side so the indicator doesn't go stale after a runtime failover (the small new piece of state the README sanctions). No new endpoint, no schema change.

## Phase 5 — Test updates (explicit, scoped)

The restyle changes labels/markup that existing tests assert on. Update — kept separate from the pre-existing BF-23/ReviewPane mock failures already documented in memory:
- `Header.test.tsx` — removed "Structure Editor" button (now the logo), new UPLINK/backend-cluster text, count-chip changes.
- `StatusBadge.test.tsx` — label casing (`Pending`→`PENDING`, etc.) and spinner markup.
- `mobile_responsive.test.tsx` — confirm the master-detail swap still passes after layout restyle (behavior preserved, assertions may need selector tweaks).
- `JobList.test.tsx`, `JobDetail.test.tsx`, `StageTimeline.test.tsx`, `FollowUpPane.test.tsx`, `ChatBox.test.tsx`, `UnfitModal.test.tsx` — re-point any text/role/class assertions to new copy. Keep all behavioral assertions (actions fire the same `api.*`).

## Critical files

- New: `frontend/src/theme/tokens.ts`, `frontend/src/theme/chrome.tsx`, `frontend/src/theme/Icon.tsx`, `frontend/src/theme/Ambient.tsx`.
- Restyle: `frontend/src/index.css`, `frontend/tailwind.config.js`, `frontend/src/App.tsx`, and all of `frontend/src/components/*.tsx` + `frontend/src/components/cv-editor/*.tsx`.
- Reference (do not port): `.claude/designs/design_handoff_cyberpunk_redesign/JSA App Shell.dc.html`, `CV Structure Editor.dc.html`.

## Behavior to preserve (do not touch)

Zustand store shape + `applyEvent`; `editorOpen` gating; all `api.*` calls and the universal `refetchAll()` after mutations; `getActiveStepIndex`; `GROUPS`/`RUNNING_STATES`/`STATE_CONFIG` state→meaning maps; JobDetail action-visibility rules + nuclear confirm; ReviewPane `toFileUrl`/path-fetch/`downloadFormat`; editor `applyEdit` coalesce/commit + undo/redo + all mutation signatures; responsive master-detail.

## Known deviations (intentional)

1. **Infer "parallel threads" are sequential in production.** The real backend emits step-by-step `infer_progress` events (step N of 5); the mock's overlapping `performance.now()` threads are fiction. We render 5 thread rows mapped to real step state (done/active/pending). True visual overlap would need backend changes — out of scope.
2. **Editor red accent shares the danger hue.** User-chosen; primary (filled) vs danger (outlined) buttons remain distinguishable.

## Verification

1. `cd frontend && npm install && npm run dev` → open `http://localhost:5173`. Visually compare against both `.dc.html` files opened directly in a browser (HUD skin, amber shell / red editor, telemetry on).
2. Walk every surface: header (backend cluster dropdown, workers meter, uplink, logo opens editor), job rail groups, job detail (pipeline, JD disclosure, EXCEPTION panel, actions + nuclear confirm), follow-up Q&A, review (tabs, real PDF iframe, download PDF/DOCX, approve), unfit overlay; editor (empty → run inference → blocks/document/split, inject module, drag reorder, undo/redo, JSON drawer, COMMIT/logo exit back to jobs).
3. `cd frontend && npm test` → all updated tests pass (excluding the pre-existing BF-23 failures noted in memory).
4. Run the full app end-to-end: `jsa --csv <jobs.csv> --cv <resume.pdf>` (the standard test command) and confirm real WS events (`status_changed`, `backend_switched`, `infer_progress`) drive the new UI live.
5. `graphify update .` after implementation to refresh the graph.

## Execution & parallelization

The Jobs shell and the CV editor are **independent surfaces** — disjoint component files, no shared store/type/api edits in this pass. Their *only* shared dependency is the Phase 1 foundation. So the build splits into a sequential foundation followed by two parallel tracks:

1. **Foundation (sequential, blocks everything).** Implement Phase 1 on `feat/cyberpunk-daemon-redesign` and **commit**. Phase 1 owns *all* edits to the shared files — `frontend/src/index.css`, `frontend/tailwind.config.js`, and `frontend/src/theme/*` (incl. the **complete** icon superset enumerated in Phase 1). These files are then **frozen**.

2. **Two parallel coder agents in git worktrees**, each branching from the committed foundation:
   - **Agent A — Shell:** Phase 2 + Phase 4 (backend cluster) + the shell test updates from Phase 5. Files: `App.tsx`, `Header.tsx`, `JobList.tsx`, `StatusBadge.tsx`, `JobDetail.tsx`, `StageTimeline.tsx`, `FollowUpPane.tsx`, `ChatBox.tsx`, `ReviewPane.tsx`, `UnfitModal.tsx` + their tests.
   - **Agent B — Editor:** Phase 3 + the editor test updates from Phase 5. Files: `components/cv-editor/*.tsx`, `ui.tsx` + editor tests.
   - **Hard boundary:** neither agent edits `index.css`, `tailwind.config.js`, or `theme/*`. If either finds a genuinely missing token/icon/class, it **reports back** rather than editing the frozen foundation — I reconcile in a short follow-up pass and the agents consume the update. This keeps the two branches conflict-free (disjoint files ⇒ clean merge).

3. **Merge both worktree branches** back into `feat/cyberpunk-daemon-redesign` (clean, disjoint files), then I run the full `npm test` + dev smoke + `graphify update .` reconciliation.

(If you'd rather not use worktrees, the same A/B split can run as two sequential agents — parallelism is an optimization, not a correctness requirement.)

## Branch

Per policy, branch from `main`: `git checkout -b feat/cyberpunk-daemon-redesign`. Parallel work happens in worktrees off that branch. No merge to `main` without explicit approval.
