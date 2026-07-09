---
status: Done
---

# Docked Chat Input (sticky-footer composer)

## Context

The user delivered a design handoff (`.claude/designs/floating_input_box.zip` →
`design_handoff_docked_chat_input/`) for a **single, narrow UI mechanic**: the chat
composer (textarea + submit button) should **dock to the bottom of its scrollable
panel** when the panel's content overflows, and sit inline when content is short.

Right now the composer (`frontend/src/components/ChatBox.tsx`) is a plain in-flow block
at the end of the job-detail panel. When the review pane's 58vh PDF preview pushes the
composer far below the fold, the user must scroll all the way down to answer a
follow-up or request a revision. The design fixes this with a pinned, visually
"elevated" composer bar — achieved with **pure CSS `position: sticky`**, no JS.

The handoff is high-fidelity: recreate the exact CSS values, adapt only color tokens
(which already match our `SHELL_THEME` — `surface: #10151D`, `canvas: #070A0F`).

## The mechanic (from the handoff)

- Composer is `position: sticky; bottom: 0` inside the scrolling container.
- Short content → stays in natural flow (looks inline).
- Long content → docks to the **bottom of the panel** (not the viewport) and content
  scrolls underneath it.
- When docked it reads as elevated: soft upward drop shadow, top border, a background a
  shade lighter than the page, and a gradient fade at the top edge.
- Transition is automatic/continuous — no breakpoint, no JS scroll listener.

## Why our layout already satisfies the structural requirements

Verified during exploration — the sticky mechanic will "just work" with no restructuring:

- **Scroll container** = `<main className="flex-1 overflow-y-auto">` in
  `frontend/src/App.tsx:60` — bounded height (h-screen flex chain) + `overflow-y-auto`. ✓
- **Padded content column** = `JobDetail`'s root `<div>` with `padding: "20px 26px 80px"`
  (`frontend/src/components/JobDetail.tsx:206`) — identical to the design reference, so
  the bleed math (`-26` horizontal, `-80` bottom) is exactly right. ✓
- **No sticky-breaking ancestor** between ChatBox and `<main>`: `JobDetail`'s root uses
  `position:relative; z-index:1` (these do **not** break sticky) and the pane wrappers
  are zero-padding flex columns — no `transform`/`filter`/`contain`/`overflow:hidden`. ✓
- **ChatBox is the last in-flow element** in both callers:
  - `FollowUpPane` (`FollowUpPane.tsx:102`) — direct last child.
  - `ReviewPane` non-approved branch (`ReviewPane.tsx:431`) — last child through nested
    zero-padding flex columns. ✓

Because both callers share the same `JobDetail` root padding, a single hardcoded set of
bleed margins is correct for both.

## Change (one component)

**File: `frontend/src/components/ChatBox.tsx`** — the root `<div>` (currently
`style={{ display: "flex", flexDirection: "column", gap: 8 }}` at line ~126).

Replace its style with the sticky-footer block, ported from the handoff's `chatBox()`
and adapted to our tokens (`T = SHELL_THEME`):

```tsx
// Sticky-footer composer: docks to the bottom of the scrolling <main> when the
// job-detail panel overflows; sits inline when content is short (pure CSS sticky,
// no JS). The negative margins bleed the bar to the scroll-container edges and cancel
// JobDetail's root padding — they are COUPLED to JobDetail's `padding: "20px 26px 80px"`
// (the only place ChatBox is rendered). See .claude/designs/design_handoff_docked_chat_input.
style={{
  position: "sticky",
  bottom: 0,
  zIndex: 8,
  marginLeft: -26,
  marginRight: -26,
  marginBottom: -80,
  paddingLeft: 26,
  paddingRight: 26,
  paddingTop: 14,
  paddingBottom: 20,
  display: "flex",
  flexDirection: "column",
  gap: 8,
  background: `linear-gradient(${T.canvas}00, ${T.surface} 22%)`,
  borderTop: `1px solid ${T.bd}`,
  boxShadow: "0 -16px 28px -12px rgba(0,0,0,.5)",
}}
```

Everything inside the div (textarea, mention dropdown, revise `<select>`, error line,
submit button) is unchanged. No other files change.

## Known deviation (intentional, matches the handoff)

Handoff point 1 describes the *ideal* of "no shadow/border when content is short," but
points 3 forbids any JS-measured toggle and the reference `chatBox()` applies the
elevation styles **unconditionally**. Pure CSS `sticky` cannot detect "am I currently
stuck." So: **a short follow-up will show the border/shadow/gradient even when inline.**
This is the faithful reading of the handoff — we do **not** add IntersectionObserver or
`animation-timeline` stuck-detection.

## Verification (manual browser check — required)

Layout/scroll behavior is invisible to vitest/jsdom, so tests can only confirm ChatBox
still renders and submits. The real verification is manual:

1. **Rebuild the bundle first** — the `jsa` CLI serves the gitignored `jsa/static`, not
   live source:
   ```bash
   cd frontend && npm run build
   ```
2. Run the app (`jsa --csv <jobs.csv> --cv <resume.pdf> --no-browser`, open
   http://localhost:8765) and open a job.
3. **Review state (overflows):** select a job in `review` — the 58vh PDF preview forces
   a scroll. Confirm the composer **docks at the bottom of the right panel** and the
   preview/approve content scrolls underneath it; scroll up and confirm the bar stays
   pinned and never leaves the panel.
4. **Follow-up state (short):** a job in `awaiting_input` with a short question — confirm
   the composer sits inline directly under the question (and eyeball the always-on
   elevation so the known deviation is confirmed, not a surprise).
5. **Regression:** confirm the sticky change didn't disturb submit, the `@`-mention
   dropdown, or the revise-target `<select>`.
6. Run the frontend suite to confirm no behavioral regression (note: pre-existing
   failing tests exist on HEAD — do not misattribute):
   ```bash
   cd frontend && npm test
   ```

## Critical files

- `frontend/src/components/ChatBox.tsx` — **the only file edited** (root `<div>` style).
- `frontend/src/components/JobDetail.tsx` — context only (root padding `20px 26px 80px`
  the bleed margins are coupled to). Not edited.
- `frontend/src/App.tsx` — context only (`<main overflow-y-auto>` scroll container). Not edited.
