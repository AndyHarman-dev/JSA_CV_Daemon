---
status: Done
---

# Scratch Buffer (Floating Notes Window)

## Context

The design handoff `.claude/designs/Scratch Buffer Design.zip` (`design_handoff_scratch_buffer/`)
specs a persistent, **cross-job** scratch pad for the JSA daemon UI: users capture recurring
answers (salary floor, visa status, notice period, reusable blurbs) once and reuse them while
reviewing *any* job. It is framed as a daemon surface (`SCRATCH_BUFFER`) matching the app's
terminal/HUD visual language — not a generic notepad.

The rest of the app shell already exists. This plan adds **only** the Scratch Buffer: a parked
orb (always visible) + a floating, draggable capture/history window, mounted once globally so it
shows regardless of which job is selected.

Product decisions (confirmed with user):
- **First run:** start empty (show empty-state copy), no seed entries.
- **Delete:** immediate, no confirmation (matches reference).
- **Persistence:** `localStorage`, key `jsa_scratch_entries` (matches design; fits this single-user, local-only tool).

The reference implementation lives in the bundled `JSA App Shell.dc.html` (search `SCRATCH_BUFFER`);
it is a React-class prototype. We re-create it in the real stack (React function components +
Zustand + the existing `theme/` design tokens), reusing tokens/helpers rather than hardcoding hex.

## Approach

Two new files + two small edits. Persistent note data lives in a dedicated Zustand store
(mirroring the `editorStore.ts` precedent, testable in isolation); ephemeral view state
(open / draft text / window position / drag) is component-local `useState`.

### 1. New: `frontend/src/scratchStore.ts` (Zustand slice + pure helpers)

Model on `frontend/src/editorStore.ts` (`create<...>((set, get) => ...)` with module-level helpers).

Data model (add `ScratchEntry` here or to `frontend/src/types.ts`):
```ts
interface ScratchEntry { id: string; text: string; tag: string; pinned: boolean; ts: number; }
```

Exported **pure helpers** (unit-testable, ported from the reference lines 139–182):
- `guessTag(text): string` — leading `#word` → that tag; else keyword match
  `salary|comp → #salary`, `visa|sponsor → #visa`, `notice → #notice`; fallback `#note`.
- `timeLabel(ts, now = Date.now()): string` — `<1h → "Nm"` (min 1), `<24h → "Nh"`, else `"Nd"`.
- `sortEntries(entries)` — pinned first, then `ts` desc (`(b.pinned - a.pinned) || (b.ts - a.ts)`).

Store shape:
```ts
interface ScratchState {
  entries: ScratchEntry[];
  load(): void;                 // read localStorage["jsa_scratch_entries"], JSON.parse guarded; [] on error — NO seed
  addEntry(text: string): void; // trim; ignore empty; prepend {id:`e${Date.now()}`, tag:guessTag, pinned:false, ts:now}; persist
  togglePin(id: string): void;  // flip pinned; persist
  deleteEntry(id: string): void;// filter out; persist
}
```
Persistence: a private `save(entries)` does `localStorage.setItem("jsa_scratch_entries", JSON.stringify(entries))`
in a try/catch, called after every mutation (no separate save action) — matches the design's
`saveScratchEntries`. Only `entries` is persisted; open/draft/position are never persisted.

### 2. New: `frontend/src/components/ScratchBuffer.tsx` (orb + floating window)

Function component, `const T = SHELL_THEME` (from `../theme/tokens`), inline styles off `T`,
using `panelBase`/`chamferPath` from `../theme/chrome` and `<Icon>`/`<Grip>` from `../theme/Icon`.
Subscribe to the store via selectors: `useStore`-style `useScratchStore((s) => s.entries)`, etc.

Local `useState`: `open`, `draft`, `pos: {x:number|null, y:number}` (init `{x:null, y:78}`), and a
drag ref. On mount, call `load()` once (`useEffect(..., [])`).

**Triggers / effects** (based on the keydown pattern from `CvEditor.tsx:317–332`):
- Global `keydown` listener in a `useEffect(..., [])` (mounted once): `e.code === "Space" && (e.metaKey || e.ctrlKey)`
  → `preventDefault()` + toggle; `e.key === "Escape"` → close. Clean up on unmount.
- **⚠️ Stale-closure trap:** that `CvEditor` pattern is safe only because it calls stable store
  actions and never *reads* mutable state. Our handler reads `open` (Esc-closes). Do **not** copy it
  literally with `else if (e.key === "Escape" && open)` — `open` would be captured as `false` on first
  render forever and Esc would never close. Fix: toggle with the functional updater `setOpen(o => !o)`
  and make Esc an **unconditional** `setOpen(false)` (harmless when already closed; the reference doesn't
  `preventDefault` Esc). Then autofocus via a **separate** `useEffect(() => { if (open) inputRef.current?.focus() }, [open])`,
  which also unifies autofocus across orb-click and keyboard.
- Orb `onClick` → `setOpen(true)` (autofocus handled by the `[open]` effect).
- Minimize button → `setOpen(false)` (never discards; storage untouched).
- First open with `pos.x === null` → set `x = Math.max(20, window.innerWidth - 320)` (top-right, ~top:78).

**Orb** (`renderScratchOrb`, ref lines 616–622): fixed `right:22 bottom:22`, 46×46, z-index ~70,
`border 1px ${T.aBorder}`, `background T.surface`, pin icon (`<Icon name="pin" size={18} color={T.a}/>`),
`borderRadius: T.chamfer ? 10 : 46`, shadow `0 6px 24px rgba(0,0,0,.5), 0 0 18px ${T.a}44`, title
"Scratch buffer (⌘ + Space)". Count **badge** (top-right, `T.a` bg, dark text, mono) hidden when 0.

**Window** (`renderScratchWindow`, ref lines 633–651): fixed at `pos`, z-index ~70, width 288,
`maxHeight: min(70vh, 460px)`, flex column, `background: color-mix(in srgb, ${T.surface} 94%, transparent)`,
`backdropFilter: blur(8px)`, `1px ${T.aBorder}`, chamfer via `panelBase`/`chamferPath` in hud skin (radius 10 otherwise),
shadow `0 24px 60px rgba(0,0,0,.6), 0 0 0 1px ${T.a}22`. Sections:
- **Header (drag handle):** `<Grip>` icon, a pulsing accent dot (**reuse the existing `jsblink` keyframe
  at `index.css:54`** — `animation: jsblink 2.4s ease-in-out infinite`), label `SCRATCH_BUFFER` (mono, letterspaced),
  live count, and a minimize button using a **new `minus` icon** (see edit #3). `onMouseDown` starts drag
  (excluding the minimize button).
- **Quick-capture row:** `<Icon name="plus">`, single-line input (`ref` for autofocus, mono, transparent,
  placeholder `"quick note… (Enter to log)"`), Enter (non-empty) → `addEntry(draft)` + clear + keep focus;
  a `↵` keycap hint. Empty submissions ignored.
- **Entries feed:** scrollable (`overflow:auto`, only this section scrolls); `sortEntries(entries)`.
  Each row (reuse the existing `.jrow` class for the hover transition, `.jbtn` for the icon buttons —
  both in `index.css:65–68`): `timeLabel` (fixed-width mono, `T.ink3`) · tag (`T.accent2`, mono) ·
  text (`T.ui`, wraps) · pin toggle (`<Icon name="pin">`, `T.a` when pinned else `T.ink3`) ·
  delete `<Icon name="x">` (immediate). Empty state: centered italic muted "No notes yet — type above."
- **Footer:** thin mono microcopy "Persists across all jobs · ⌘+Space to toggle".

**Drag** (ref lines 183–197): on header `mousedown`, record start pointer + `pos`; on `mousemove`
update `pos` by delta; `mouseup` removes listeners. Clamp to keep the window on-screen on **all four
edges** (~8px min visible; window is 288 wide) — the README asks to extend the reference's 2-edge clamp.
Swap cursor `grab`→`grabbing` during drag.

### 3. Edit: `frontend/src/theme/Icon.tsx` — add `minus`

`Icon.tsx` has `pin`, `plus`, `x` but no `minus` (needed for the minimize button). Add `"minus"` to the
`IconName` union and `PATHS`: `minus: ["M4 8h8"]` (single horizontal stroke, consistent 16-viewBox style).
Use `<Grip>` (already exists) for the drag-handle "move" glyph.

### 4. Edit: `frontend/src/App.tsx` — mount globally

Add `<ScratchBuffer />` as a sibling of `{editorOpen && <CvEditor />}` (around `App.tsx:31`), rendered
**unconditionally** so it owns its own keyboard listener and visibility. Root is `overflow:hidden`, so
the orb/window use `position:fixed` (already specified). No portal needed — matches how `UnfitModal`/`CvEditor`
overlays are done. Use z-index ~70 (above the modal layer at 50 / editor at 40). **Intentional consequence:**
at z-70 the amber orb floats *over* the full-screen CV editor (z-40, which uses the red `EDITOR_THEME`) —
this matches the README's "always visible, in every screen/state." Accepted, not a bug.

## Files

- **New** `frontend/src/scratchStore.ts` — Zustand store + `guessTag`/`timeLabel`/`sortEntries` + localStorage persistence.
- **New** `frontend/src/components/ScratchBuffer.tsx` — orb + floating draggable window.
- **Edit** `frontend/src/theme/Icon.tsx` — add `minus` icon.
- **Edit** `frontend/src/App.tsx` — mount `<ScratchBuffer />` globally.
- **New** `frontend/src/__tests__/scratchStore.test.ts` — helpers + store mutations + persistence.
- **New** `frontend/src/__tests__/ScratchBuffer.test.tsx` — render, keyboard toggle, add/pin/delete, empty state.

## Testing / Verification

**Unit (Vitest + testing-library, `frontend/`):**
- `scratchStore.test.ts`: `guessTag` cases (`#custom`, salary/comp, visa/sponsor, notice, fallback);
  `timeLabel` boundaries (min 1m, <24h→h, ≥24h→d); `sortEntries` (pinned first, ts desc); `addEntry`
  trims/ignores-empty/prepends; `togglePin`/`deleteEntry`; localStorage written after each mutation and
  `load()` re-reads it. jsdom provides `localStorage` natively (no mock needed — `setupTests.ts` only
  loads jest-dom); just `localStorage.clear()` and reset the store in `beforeEach`.
- `ScratchBuffer.test.tsx`: orb renders with hidden badge when empty; ⌘/Ctrl+Space toggles + autofocuses;
  Esc closes when open; typing + Enter adds a row and clears input; empty Enter is a no-op; pin re-sorts;
  × removes immediately; empty-state copy shows with no entries.
- Run: `cd frontend && npm test`. All new tests pass; no regressions in existing suites
  (note: per memory, ~9 frontend tests are pre-existing debt failing on HEAD — confirm the count is unchanged).

**Manual smoke (`cd frontend && npm run dev`, http://localhost:5173):**
1. Orb visible bottom-right on every screen; click → window opens top-right (~top:78).
2. ⌘/Ctrl+Space toggles from anywhere; Esc closes; minimize button closes.
3. Type a note + Enter → appears at top, tag inferred, input cleared, badge increments.
4. Pin floats it to top; × removes it; reload page → notes persist; switch jobs → same list.
5. Drag the header to reposition; window stays clamped on-screen; position resets to default only on full reload.

## Git

Branch from `main`: `git checkout -b feat/scratch-buffer`. Do not merge without explicit approval.
