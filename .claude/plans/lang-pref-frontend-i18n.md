---
status: Done
---

# Sub-plan: whole-frontend translation pass

Part of the language-preference feature (see `.claude/plans/streamed-meandering-deer.md`
Workstream F). The i18n plumbing (Workstream E) and the CV Structure Editor top bar/empty
state (Workstream D) are already done and merged — this workstream extends string
externalization to the **rest of the frontend**, per the user's explicit choice of
whole-app coverage over CV-editor-only parity.

## What already exists (read/reuse, do not reinvent)

- `frontend/src/i18n/strings.en.json` — the hand-maintained English source catalog, flat
  dotted keys grouped by surface (e.g. `"cvEditor.commit"`, `"lang.panelHeader"`). **Add
  your new keys here**, following the same `"<component>.<label>"` dotted-key convention
  (e.g. `"header.backendLabel"`, `"jobList.emptyState"`, `"reviewPane.approveButton"`).
- `frontend/src/i18n/index.ts` — loads every `strings.*.json` via Vite glob; nothing to
  change here (new locale files, once generated, are picked up automatically).
- `frontend/src/i18n/useT.ts` — the hook every component uses:
  ```ts
  import { useT } from "../i18n/useT"; // adjust relative path per file location
  const t = useT();
  t("some.key")                              // plain lookup, falls back to English, then the key itself
  t("some.key", { n: 3, reason: "oops" })     // {n}/{reason}-style placeholder interpolation
  ```
  `useT()` reads `language` from the global Zustand store (`frontend/src/store.ts`) — no
  props needed, it's reactive to a language change everywhere it's called.
- `scripts/translate-ui.sh` — the LLM-generation runner (`python -m jsa.i18n.translate`
  under the hood). **Do not attempt to run this yourself** — it requires an
  `ANTHROPIC_API_KEY` env var this environment does not have configured. Your job is the
  *extraction* (English strings → `strings.en.json` + `t()` calls in JSX); actually
  generating the `es`/`fr`/`ja`/etc. catalogs is a follow-up step the user runs locally
  once this PR lands. Do not block your work on it or attempt workarounds.
- `frontend/src/theme/Icon.tsx` already has `globe`/`search` glyphs added; you should not
  need new icons for this pass (it's a string-extraction pass, not a UI-redesign pass).

## Scope: every component with user-visible English chrome

`find frontend/src/components -name "*.tsx"` (17 files) — sweep all of them. Already done
(skip): `cv-editor/CvEditor.tsx`'s top-bar + empty-state strings and `cv-editor/LanguagePill.tsx`
(both already wired through `useT()`/`strings.en.json`). Everything else in this list still has
hardcoded English and needs the same treatment:

```
ChatBox.tsx
cv-editor/BlocksView.tsx
cv-editor/JsonDrawer.tsx
cv-editor/PaperSheet.tsx
cv-editor/SplitView.tsx
cv-editor/ui.tsx
FollowUpPane.tsx
Header.tsx
JobDetail.tsx
JobList.tsx
MarkdownPreview.tsx
ReviewPane.tsx
ScratchBuffer.tsx
StageTimeline.tsx
StatusBadge.tsx
UnfitModal.tsx
```
Also check `frontend/src/App.tsx` and `frontend/src/theme/chrome.tsx` (the latter has the
`stateMeta()` job-state label map — e.g. `"PENDING"`, `"RUNNING"`, `"NEEDS INPUT"` — these are
prime, high-visibility candidates for externalization).

## What counts as "translatable chrome" (externalize) vs. not (leave alone)

**Externalize**: button labels, headings, body copy, empty-state text, placeholder text,
tooltips/`title` attributes, status/badge labels, toast/error messages shown to the user,
column headers, modal copy.

**Do NOT externalize** (leave as literal code, not i18n keys):
- Developer-facing values: CSS class names, `data-testid`, log/console strings, code
  comments.
- Values that are inherently non-linguistic: dates/times (already locale-formatted via
  `toLocaleTimeString`/similar — leave those calls alone), raw numbers, IDs, hex colors,
  file extensions, HTTP methods, URLs.
- Mono/hex "system" glyphs that are code-like by design in this HUD skin (e.g. `0x{sessionRef}`,
  job state short-codes like `PROC`/`WAIT`/`ERR` in `stateMeta()`'s `code` field — check
  `jsa/theme/chrome.tsx`'s `StateMeta.code` vs `StateMeta.label`: `code` is a terminal-style
  abbreviation shown alongside the label, arguably intentional HUD flavor rather than
  user-facing prose — externalize `label`, leave `code` as-is unless it reads as ambiguous).
- Anything already parameterized from data (job company/role names, file paths, user-typed
  text in ChatBox/ScratchBuffer inputs).

Use judgment; when in doubt, prefer externalizing — a missing translation falls back to
English automatically (`useT()`'s fallback chain), so over-extracting is safe and cheap,
while under-extracting silently ships un-translatable copy.

## Mechanical procedure (per file)

1. Read the file. Identify every JSX text node / string literal prop (`label=`, `title=`,
   `placeholder=`, `aria-label=`) that is user-facing English prose per the scope above.
2. Pick a dotted key: `"<componentCamelCase>.<shortLabel>"`, e.g. `Header.tsx` →
   `"header.switchBackend"`, `JobList.tsx` → `"jobList.noJobs"`. Keep keys stable and
   descriptive — they are the permanent identifier translators/LLM-generation key off of.
3. Add the key + its current English text to `frontend/src/i18n/strings.en.json` (keep it
   alphabetically or section-grouped — match the existing file's style, don't reformat
   unrelated entries).
4. Replace the literal in the component with `t("that.key")` (or `t("that.key", {vars})` for
   any string with interpolated data — e.g. counts, names — mirroring the
   `"{n} MODULE(S)"` / `{ n }` pattern already used in `CvEditor.tsx`).
5. Import `useT` and call `const t = useT();` once near the top of the component function
   (function-scoped, like every other hook call in these files — see `CvEditor.tsx`,
   `LanguagePill.tsx` for the established pattern). For files with multiple components,
   each component that renders translatable text needs its own `const t = useT();` (hooks
   can't be shared across component functions).
6. If a file exports a plain function (not a component) that returns display strings (e.g.
   `stateMeta(T)` in `chrome.tsx`, which is NOT a hook and can't call `useT()` since it's not
   a component) — thread a `t` parameter into it instead: `stateMeta(T, t)`, called from
   the component that already has `const t = useT()`. Do not call `useT()` outside a
   component/hook context; that will violate the rules of hooks.

## After extraction: verification, not translation-running

1. `cd frontend && npx tsc --noEmit` — must pass clean.
2. `npm run build` — must succeed (also regenerates `../jsa/static` per project convention —
   the jsa CLI serves that built bundle, not live source).
3. `npx vitest run` — existing component tests must still pass. Some may need small updates
   if they asserted on literal English strings that are now sourced from
   `strings.en.json` via `t()` — that's expected and fine, update the test's expected string
   to match (same English text, now indirected through the catalog) rather than skipping it.
   Pre-existing failures (currently 9 in `ReviewPane.test.tsx`, unrelated `api.getJob` fixture
   debt — see project memory) are not yours to fix; don't let them block you, but don't let
   your changes silently add new failures either — check any newly-red test traces back to
   your edit, not that pre-existing debt.
4. Grep sanity check: after your pass, a search for a handful of the (previously) most
   common literal strings you extracted should show 0 hits in `.tsx` JSX (only inside
   `strings.en.json` and inside comments/tests referencing the English text, which is fine).
5. Do NOT run `scripts/translate-ui.sh` — leave that for the user (needs their API key).

## Deliverable summary to report back

- Full list of files touched.
- Total count of new keys added to `strings.en.json`.
- Any component you deliberately left un-touched and why (e.g. a component with no
  user-visible English text, or text you judged non-linguistic per the scope section).
- Confirmation of tsc/build/vitest results.
