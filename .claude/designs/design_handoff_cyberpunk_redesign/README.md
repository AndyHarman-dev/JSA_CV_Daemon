# Handoff: JSA — Cyberpunk Daemon Redesign

## Overview
A full visual + interaction redesign of the JSA (Job Search Assistant) web app in a restrained, "serious daemon" cyberpunk style — dark HUD surfaces, glowing accents, corner-bracket/chamfer framing, and system/process-flavored copy ("PROCESS_QUEUE", "THREAD_0N", "BACKEND FAILOVER QUEUE"). The goal: the user should feel like they're directing an automated daemon that runs several jobs in parallel, not filling out a form.

This redesign covers **every screen in the existing frontend**:
- The main app shell: header, job queue rail, job detail (pipeline progress, job description, error states)
- All job-state surfaces: Inbox/follow-up Q&A, Review (CV/cover-letter tabs + approve/revise), the "not a fit" overlay
- The CV Structure Editor (already existed as a separate modal): empty state, parallel inference, Blocks/Document/Split views, JSON drawer

## About the Design Files
The two `.dc.html` files in this folder (`JSA App Shell.dc.html`, `CV Structure Editor.dc.html`) are **interactive HTML/React design references**, built and run inside our design tool — not production code to copy directly. They use a custom in-house component runtime (`support.js`, `<x-dc>`, `<dc-import>`, template holes) that **does not exist in your stack** and should not be ported.

The task is to **recreate these designs in the existing JSA frontend** (`frontend/` — React + TypeScript + Vite + Tailwind), restyling and extending the existing component files listed in "Component Mapping" below. Where the existing components already implement the right behavior (data fetching, state machines, API calls), keep that logic — this handoff is almost entirely visual/structural, plus a small number of new interaction affordances called out below.

Open `JSA App Shell.dc.html` directly in a browser to explore the whole shell live (it mock-simulates 8 jobs across every state, and the header's daemon-logo button opens the live CV Structure Editor as a full-screen overlay, exactly like the real "Structure Editor" flow). Open `CV Structure Editor.dc.html` standalone to explore just the editor.

## Fidelity
**High-fidelity.** Colors, type, spacing, radii/chamfers, glow values, and copy are final as shown. Implement pixel-for-pixel using Tailwind's arbitrary-value utilities or a small custom theme extension (see Design Tokens) — don't substitute your own interpretation of "cyberpunk."

## Component Mapping (existing frontend → new design)
| Existing file | What changes |
|---|---|
| `src/components/Header.tsx` | Full restyle. App name/logo becomes a clickable button (logo-as-switch, see Interactions). Status badge counts become glowing mono chips. Add the new **Backend Queue** cluster (not in current code — see below). Add **Workers** meter (derived from count of jobs in active-processing states, capped at 5 — mirrors the "up to 5 jobs processed concurrently" semaphore in the README). WS status dot becomes "UPLINK: SYNCED/RECONNECTING/LOST". Hard-reload icon kept, restyled. |
| `src/components/JobList.tsx` | Restyle only — same `GROUPS`/state logic. Rows get tier color, glowing status badge, accent left-bar + glow on selection. |
| `src/components/StatusBadge.tsx` | Restyle to mono pill w/ colored dot (solid = static state, spinning ring = `running`). Color mapping in Design Tokens. |
| `src/components/JobDetail.tsx` | Restyle header/actions row. Replace the `▶/▼ Job Description` disclosure with the chevron-rotate version shown. Error box restyled as an "EXCEPTION" panel. Same conditional logic (`showCancel`/`showDismiss`/`showRequeue`/`showRetry`, nuclear-reset confirm). |
| `src/components/StageTimeline.tsx` | Visual replacement only — same 7-step `getActiveStepIndex` logic, redrawn as glowing thread-nodes with a connecting line (see "Pipeline Progress" below). Step labels become `PENDING / CV_ADJUST / CV_DONE / COVER_LETTER / CL_DONE / REVIEW / APPROVED`. |
| `src/components/FollowUpPane.tsx` | Restyle the question card + relabel section "AGENT_QUERY · BLOCKING". Same data flow. |
| `src/components/ReviewPane.tsx` | Restyle tabs, version label, document preview frame. **New:** the download button must become a dropdown with PDF / DOCX choices (this was a flat button in the design pass that started this — confirm your current impl already has this menu; if not, add it using the existing `toFileUrl`/`downloadFormat` logic, just with the new menu UI). Approve button restyled green/glowing. |
| `src/components/ChatBox.tsx` | Restyle textarea + submit button only. Same submit logic for both `answer` and `revise` kinds. |
| `src/components/UnfitModal.tsx` | Restyle as a corner-bracketed centered panel, "FIT_ASSESSMENT: MISMATCH" heading. Same Ignore/Dismiss actions. |
| `src/components/cv-editor/CvEditor.tsx` | Full restyle per `CV Structure Editor.dc.html` — topbar, Blocks/Document/Split views, empty/infer state, JSON drawer. See that file's structure below. |

### New behavior not in the current frontend
1. **Logo-as-switch navigation.** In the redesign, the app's logo/wordmark (top-left of both the main header and the CV Editor's topbar) doubles as the only way to open/close the editor:
   - Clicking the **JSA // DAEMON** logo in the main header opens the CV Editor full-screen (replaces the current "Structure Editor" header button — remove that separate button).
   - Clicking the **CV // DAEMON** logo inside the editor (or clicking **COMMIT**) returns to the Jobs page.
   - Implementation note: in the prototype this is a callback prop (`onExit`) passed into the editor component; wire the equivalent in React (e.g. the editor calls a `onClose`/`onExit` prop, or you navigate via your router/`editorOpen` store boolean — `editorStore.ts` already exists for this).
   - There is intentionally **no separate floating "close" button** — the logo and COMMIT are the only exits, matching the real workflow (open editor → run inference → edit → commit → land back on the jobs page).
2. **Backend failover queue indicator.** The header needs a new cluster (it doesn't exist in the current `Header.tsx`) showing which AI backend is currently active (`claude-cli` / `google-cli` / `anthropic`, per the `--backend` CLI flag and the three `AgentBackend` implementations) and the standby order it would fail over to. Click to open a dropdown listing the full priority queue with ACTIVE/STANDBY status. This is read-only in the mock; wire it to whatever endpoint/event reports the active backend and failover history (the existing `/api/config` call already returns `backend` — extend it, or add a WS event, to report the full queue + any `backend_switched` events from `types.ts`).
3. **Download format menu.** The Review pane's download action opens a small dropdown (PDF / DOCX) instead of being a single button — see ReviewPane row above.

## Screens / Views

### 1. App Shell — Header
- Full-width bar, `padding: 10px 18px`, translucent dark background (`rgba(10,14,19,.85)`) with `backdrop-filter: blur(10px)`, bottom border, drop shadow.
- **Left:** logo button — 32×32 chamfered/cornered square in accent color with a bolt icon (glow: `0 0 16px <accent>66`), wordmark "JSA // DAEMON" (Chakra Petch 700 14.5px) + subtitle "JOB_SEARCH_AUTOMATION · LOCAL ⇄ EDITOR" (Share Tech Mono 500 10px, the "⇄ EDITOR" portion in accent color).
- **Center (wraps on narrow widths):** Backend cluster → Workers meter → status chips (Inbox/Review/Done/Failed; pill shows only if count > 0).
- **Right:** Uplink status (pulsing cyan dot + "UPLINK: SYNCED"), hard-reload icon button.

### 2. App Shell — Job Queue Rail (left, 290px fixed)
- Background `T.subtle` (#0E141B), right border.
- Header label "PROCESS_QUEUE" (mono, uppercase, letter-spacing .16em).
- Groups in fixed order: Inbox → Needs Review → Running → Review → Done → Failed → Dismissed. Each group only renders if it has jobs; header row = group label + thin rule + count.
- Job row: company name + "T{A/B/C}" tier code (top), role (below, muted), status badge (bottom). Selected row gets an accent left-bar with glow + accent border + lighter background.

### 3. App Shell — Job Detail (main, flexible width)
- Title row: `{Company} — {Role}` (Chakra Petch 600 19px) + tier badge (bordered pill) + status badge + right-aligned contextual actions (Retry/Re-queue/Dismiss/Delete — same visibility rules as current `JobDetail.tsx`).
- Nuclear-reset confirm: inline danger-tinted panel with Yes/Cancel, shown only when retrying a job that already has `retry_count > 0`.
- **Pipeline Progress:** bracketed panel containing a horizontal thread tracker — 7 steps (`PENDING/CV_ADJUST/CV_DONE/COVER_LETTER/CL_DONE/REVIEW/APPROVED`), each a small node (filled+check if done, pulsing dot if active, dim ring if pending) connected by a line that glows cyan up to the active step.
- **Job Description:** collapsible, chevron rotates 90° open, monospace block on a sunk background, max-height 220px scroll.
- **Exception panel:** only shown when `job.error` or state is `failed` — alert icon + "EXCEPTION" label + message, in a danger-tinted bracketed panel.
- State-specific surface below, one of:
  - **Inbox / Follow-up** (state `awaiting_input`): "AGENT_QUERY · BLOCKING" label, question in an accent-tinted bracketed card, then the chat box (textarea + SUBMIT_ANSWER button).
  - **Review** (state `review` or `approved`): CV/RESUME ↔ COVER_LETTER tabs, version label, a stylized light-paper preview placeholder (since there's no live PDF in the mock — wire to the real PDF iframe in production), a DOWNLOAD button opening a PDF/DOCX dropdown, then either an "APPROVED" success strip (if approved) or an APPROVE & EXPORT button + revision chat box.
  - Neither (e.g. `pending`, `running`, `cv_done`): no extra surface — just pipeline + JD + (optional) error.

### 4. App Shell — "Not a Fit" Overlay
- Full-screen dim+blur backdrop, centered 460px bracketed panel.
- Alert icon + "FIT_ASSESSMENT: MISMATCH" heading, company/role subline, the fit-assessment reason in an accent-tinted card, explanatory copy, then **Ignore & Continue** / **Dismiss Job** buttons (right-aligned).

### 5. CV Structure Editor — Topbar
- Same translucent/blurred bar as the main header.
- **Left:** logo button (same pattern — clicking it, when embedded, exits back to Jobs), then (once a CV is loaded) an "OPERATOR" readout: contact name + module count pill, divided by a vertical rule.
- **Right:** live clock + session hex id, the **view switcher** (BLOCKS/DOCUMENT/SPLIT segmented control, numbered 01/02/03), undo/redo icon buttons, SRC.JSON toggle, RUN INFERENCE/RE-RUN button, COMMIT button (primary, glowing) — COMMIT exits back to Jobs.

### 6. CV Structure Editor — Empty State
- Centered, no CV loaded: bracketed icon tile, "NO STRUCTURE DETECTED" heading, explanatory copy, **RUN INFERENCE** (primary) and **INIT BLANK** (secondary) buttons.

### 7. CV Structure Editor — Inference (parallel threads)
- Centered 460px bracketed panel, "INFERENCE IN PROGRESS" + source filename + elapsed seconds.
- Five independently-timed progress rows (`THREAD_01 · PARSE_DOCUMENT` … `THREAD_05 · VALIDATE_SCHEMA`), each with a status dot (spinner while running, check when done), percentage, and a thin glowing progress bar — timed with different delay/duration so they visibly overlap, reinforcing "parallel," not a single linear bar.

### 8. CV Structure Editor — Blocks View
- Max-width 760px centered column. Contact/"IDENTITY" card at top (bracketed), then one card per CV section ("MOD_01" etc.), each with a drag handle, kind badge (icon + label + 3-letter code), editable title, and hover-revealed move-up/down/delete tools. Body editor varies by section kind (summary = textarea, bullets = list with bullet dots, skills = tag editor per category, experience/projects/education = repeating entry cards). "INJECT MODULE" button + popover (corner-bracketed, lists the 6 section kinds with icon/code/description) at the end and between every section on hover.

### 9. CV Structure Editor — Document View
- **Stays light/paper-realistic** — this is the literal export preview, intentionally not reskinned dark. It's presented inside a dark "EXPORT_PREVIEW · READ-ONLY LAYOUT" viewport frame (dashed border, corner brackets, pulsing cyan dot) so it reads as an artifact inside the daemon console rather than a jarring light page.

### 10. CV Structure Editor — Split View
- 280px "INDEX" rail (section list with icon/name/code/count, click to select, hover move arrows) + the same paper preview on the right, with the selected section getting an accent selection ring.

### 11. CV Structure Editor — JSON Drawer
- Right-side 400px panel, "SOURCE.JSON" header with a pulsing "LIVE · READ-ONLY · SCHEMA-VALID" tag, copy/close icon buttons, syntax-highlighted (string green, key cyan, number/bool pink/amber) read-only `<pre>` of the exported schema shape.

## Interactions & Behavior
- **Logo-as-switch:** see "New behavior" above — this replaces the old separate "Structure Editor" header button and the old floating "Close" button.
- **Backend cluster click:** toggles a dropdown (closes on re-click or selecting elsewhere); no outside-click handling implemented in the mock — add it in production for polish.
- **Download button click:** toggles PDF/DOCX dropdown; selecting an option closes it (wire to the real file URLs already computed in `ReviewPane.tsx`).
- **Job row / section selection:** instant, no animation beyond the existing hover/selected style transitions (all ~120–140ms).
- **Drag-to-reorder (Blocks view sections):** native HTML5 drag and drop; dragged item drops to 40% opacity, drop target gets an accent ring + glow.
- **Undo/redo:** ⌘Z / ⌘⇧Z (or Ctrl+Z / Ctrl+Y) plus toolbar buttons; text edits debounce into history at 600ms, structural edits (add/delete/reorder/move) push immediately.
- **Inference progress:** five independently timed intervals (see component for exact delay/duration per thread) driven by `performance.now()`, polled every 60ms — not real backend progress in the mock, but the timing pattern (staggered starts, overlapping durations) should be preserved when wiring real progress events so threads visibly overlap rather than queue.
- **Ambient telemetry (toggleable):** a faint cyan grid (40px cells, fades out via radial mask toward the bottom), a single soft scanline band continuously scrolling top-to-bottom (~16s per loop, low opacity, never resets/jumps — uses a single seamlessly-tiling gradient, not a hard-reset translate), and small monospace session-id/sync-ms readouts in header/corners. This is a visual treatments only — no real telemetry is wired up. Treat as decorative chrome that can be toggled per the `telemetry` prop.
- **Pulsing "live" dots:** 2–2.4s ease-in-out opacity pulse (`jsblink`/`cyblink` keyframes) used for: uplink status, active backend, active pipeline step, JSON drawer "LIVE" tag.
- **Spinner:** 0.7s linear rotation for in-progress icons (running status badge, active inference thread, active pipeline node uses a soft pulse rather than a spinner).

## State Management
No new state shapes are required beyond what `store.ts`/`editorStore.ts` already track — this is a visual/structural pass. Two things worth confirming against the real store:
- The header's **Workers** count should derive from however you already count "actively processing" jobs (the mock counts `running`/`pending`/`cv_done`/`cl_done`), capped at 5 to match the documented concurrency semaphore.
- The **Backend cluster** needs a small new piece of state: an ordered list of `{ id, label, status: 'active' | 'standby' }`. Source this from `/api/config` (extend the existing `backend` field) and/or the `backend_switched` WS event already defined in `types.ts`.

## Design Tokens

**Colors**
- Canvas (page bg): `#070A0F`
- Surface (cards/panels): `#10151D`
- Subtle (rail bg / nested panels): `#0E141B`
- Sunk (recessed fields, pills, code blocks): `#0B0F15`
- Border: `rgba(140,190,210,0.16)` (default), `rgba(140,190,210,0.30)` (stronger)
- Text: `#E8EEF2` (primary), `#8B97A6` (secondary), `#4D5868` (tertiary/disabled)
- **Accent** (tweakable; default amber): `#F4CE4A` — alt options `#33E6E6` (cyan), `#FF4655` (red), `#FF2BD6` (magenta). Used for primary buttons, active states, selection, glows.
- Accent-2 (fixed cyan, "system/live" signal — never swapped by the accent tweak): `#33E6E6`
- Danger: `#FF4655`
- Violet (Review-state badge only): `#9D7BFF`
- Success / Approved: `#8FE3A0`
- Soft accent fill: `color-mix(in srgb, <accent> 14%, #10151D)`; soft border: `color-mix(in srgb, <accent> 55%, #10151D)`
- **Paper view only** (Document/Split preview — stays light): accent `#2563EB`, surface `#FCFBF8`, border `#E3DFD6`, ink `#211C16` / `#6B6358` / `#A39B8D`

**Typography**
- Display / labels / buttons / headings: **Chakra Petch** 500–700, usually uppercase, letter-spacing 0.02–0.06em
- Body / form copy: **IBM Plex Sans** 400–700
- Mono / telemetry / code / status codes: **Share Tech Mono**
- Paper view body (toggleable): **Newsreader** (italic supported) falling back to Georgia, or IBM Plex Sans if the `paperSerif` toggle is off
- All loaded from Google Fonts; self-host in production per your normal font pipeline.

**Shape system** (controlled by a `skin` toggle: `terminal` vs `hud`)
- `terminal` (default): sharp rectangular panels (border-radius 0–3px on panels, 2px on buttons), with 4 small L-shaped corner brackets (9×9px, 2px stroke) drawn at each panel corner in the border color (or accent color when highlighted).
- `hud`: panels have one corner-chamfer per side via `clip-path: polygon(...)` (8–18px chamfer depending on element size), buttons get 7px radius, and corner brackets are omitted (the chamfer itself reads as the "tech" cue).
- Pick one system and use it consistently across the whole app; don't mix.

**Glow / shadow**
- Accent glow: `box-shadow: 0 0 16px <accent>66` (logo tile), `0 1px 14px <accent>55` (primary buttons), `0 0 8px <color>` (small status dots)
- Panel shadow (small): `0 1px 2px rgba(0,0,0,.4)`; (medium/popovers): `0 10px 32px rgba(0,0,0,.55)`

**Spacing / sizing**
- Header padding `10px 18px`; job rail width `290px` fixed; JSON drawer width `400px` fixed; Split-view index rail `280px`
- Card padding ~13–17px depending on a `density: comfortable | compact` toggle (comfortable = 17px/12px gap/18px section gap; compact = 13/8/12)
- Standard icon size 13–16px, stroke width 1.55

**Motion**
- Fade-in on first paint: 6px rise + opacity, ~140–200ms ease (don't apply this to elements that re-render on a frequently-ticking timer/clock — it will appear "stuck" since the animation restarts every re-render; we hit this bug during the build and removed the entrance fade from clock-adjacent containers)
- Hover/focus transitions: 120–140ms
- Pulsing "live" dot: 2–2.4s ease-in-out opacity
- Spinner: 0.7s linear rotation
- Scanline ambient sweep: ~16s linear loop, single seamlessly-tiling gradient (no hard reset)

## Assets
No external images/icons — all icons are small hand-drawn inline SVGs (16×16 viewBox, 1.55 stroke). Recreate as an icon set in your codebase (SVG components or an icon font) rather than re-drawing inline per-instance.

## Files
- `JSA App Shell.dc.html` — header, job rail, job detail, all job-state surfaces, unfit overlay, embeds the editor as a full-screen overlay. Self-contained mock data (8 sample jobs spanning every state) — open directly in a browser.
- `CV Structure Editor.dc.html` — the Structure Editor: topbar, empty/infer, Blocks/Document/Split, JSON drawer. Also opens standalone.
- `support.js` — our internal component runtime; reference only, do not port.

Both files are plain HTML and open directly in any browser with no build step.
