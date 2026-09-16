---
status: Done
---

# Merge reference CV editor's document rendering + export into the CV Structure Editor

## Context

`/Users/wiam/Desktop/cv-editor.html` is a standalone (single-file, CDN React) CV editor that
was originally forked from this repo's CV Structure Editor and later improved by hand — it
has a print-realistic "paper" document view with real bullet points, and a working
HTML/PDF export flow that the in-repo editor never got. The user wants those two things
**merged back** into the live editor (`frontend/src/components/cv-editor/`), not a
wholesale replacement — Blocks view, Split view, deck save/infer, undo/redo, the Zustand
store architecture, and the schema all stay exactly as they are.

Concretely, two gaps were confirmed by reading both the reference file and the current
code:

1. **Bug**: in the live editor's Document/Split "paper" preview (`PaperSheet.tsx`), bullet
   lists render as plain indented text with **no bullet glyph at all**. Root cause:
   `frontend/src/index.css` pulls in Tailwind's Preflight (`@tailwind base`), which resets
   `ul,ol{list-style:none;margin:0;padding:0}` globally, and `PaperSheet.tsx` never
   restores a marker or draws one itself — unlike `BlocksView.tsx`, which already draws a
   manual dot `<span>` for its own bullet rows and looks correct. The reference file never
   relies on the native `<li>` marker either: it explicitly sets `listStyle:'none'` on every
   bullet `<ul>` and draws a small circular `<span>` dot before each bullet row (see
   `cv-editor.html:961-964` for section-level bullets, `:988-990` for entry-level bullets).
2. **Missing feature**: there is no export/download of any kind from the CV Structure
   Editor today (confirmed via grep — no `window.print`, no iframe-print, no export API
   call anywhere under `frontend/src/components/cv-editor/`). The reference file has a full
   client-side export: a hand-built static HTML rendering of the CV
   (`buildExportBody`/`buildExportBody`+`exportEntryHtml`, `cv-editor.html:530-568`), a
   "Download HTML" button (Blob + `<a download>`), and a "Download PDF" button that opens
   an off-screen same-origin `<iframe>`, writes the export HTML into it, and calls
   `iframe.contentWindow.print()` (`cv-editor.html:596-609`) — no backend involvement at
   all. This is deliberately different from the repo's existing WeasyPrint/DOCX renderer
   pipeline (`jsa/render/weasy.py`, `jsa/render/docx_render.py`), which only renders
   already-generated per-job `Document.markdown` rows via `POST /api/jobs/{id}/export`
   (`jsa/api/routes_jobs.py`) — there is no deck/raw-`CVDocument` export route, and adding
   one is out of scope. The reference's export is what "bring over its export option"
   refers to, and it fits the ask exactly: a fast, backend-free, WYSIWYG-with-the-editor
   export.

Non-goals (explicitly confirmed with the user): do not touch `BlocksView.tsx`'s already-
correct bullet rendering, do not touch the backend renderers/schema, do not add a new
backend export API route, do not change the Zustand store architecture or replace the
editor's class-based-vs-hooks approach (the live editor is already hooks/Zustand — keep
it that way, do not port the reference's React class component).

## Phase 1 — Fix bullet rendering in the Document/Split paper preview

### Current State
`frontend/src/components/cv-editor/PaperSheet.tsx` (used by both `DocumentView` and
`SplitView`) renders bullets two places, both broken the same way:
- Section-kind `"bullets"` body, lines 135-158: `<ul style={{margin:"4px 0 0",
  paddingLeft:20}}><li className="cvitem" style={{...,display:"flex",...}}>` — no
  `listStyle`, no dot, relies on the native `<li>` marker, which Tailwind Preflight has
  already zeroed globally.
- Entry-level bullets, lines 245-267: same shape, `<ul style={{margin:"3px 0 0",
  paddingLeft:20}}><li className="cvitem" style={{...,display:"flex",...}}>`.

### Desired State
Both bullet lists draw an explicit, always-visible bullet dot (matching the reference's
`cv-editor.html:961-964` / `:988-990` pattern and `BlocksView.tsx`'s existing dot), so the
Document/Split preview visually matches what actually gets exported/printed, independent
of any future Preflight/CSS change.

### Problems/Bugs
No bullet glyph renders today in Document/Split view for bullet-list sections or for
experience/education entry bullets — confirmed root cause above (global `list-style:none`
reset + nothing restoring or drawing a marker).

### Solutions
In `PaperSheet.tsx`, for both bullet blocks:
- Add `listStyle: "none"` explicitly to the `<ul style={{...}}>` (make the reset
  intentional/defensive rather than accidental).
- Prepend each `<li>` row with a small circular `<span>` dot before the `AutoTextarea`,
  sized/positioned to match the reference: `width:4, height:4, borderRadius:4,
  background: PA.ink3, marginTop:8, flex:"none"` for the section-bullets block (13.5px
  text), and `marginTop:7` for the entry-bullets block (13px text) — same values the
  reference uses for its 13.5px vs 13px bullet rows.

No other files change in this phase. `BlocksView.tsx` is untouched (already correct).

## Phase 2 — Bring over the export feature (Download HTML + Download PDF)

### Current State
No export UI or logic exists in the CV Structure Editor. `editorStore.ts` already exposes
`exportJson(cv: EditorCV): CVDocument` (trims/cleans fields into the wire schema, same
shape the deck save PUT sends) and `inferKind(s: CVSection): SectionKind` (guesses
summary/bullets/skills/experience/projects/education from field-presence, same heuristic
the backend serializer effectively encodes) — both already the right building blocks,
just unused for this purpose today.

The header (`CvEditor.tsx`, ~lines 479-562) already has a working pattern for this kind of
add-on: the `jsonOpen` boolean in `editorStore.ts` + a `toggleJson()` action + a header
button (`braces` icon) + `<JsonDrawer />` conditionally rendered in the body. A separate,
unrelated modal in this codebase (`UnfitModal.tsx`) shows the established fixed-overlay +
`panelBase`/`cornerMarks` dark-chamfered-panel pattern to reuse for a new modal.

### Desired State
A header "EXPORT" button (next to the existing SRC.JSON toggle and Commit button) opens an
export modal showing a live light/paper preview of the CV as it will be exported, a page
bottom-margin control, and two actions: "Download HTML" and "Download PDF" — both
client-side only, no backend calls, matching the reference's mechanism exactly.

### Problems/Bugs
N/A — this is new functionality, not a fix.

### Solutions

**New file `frontend/src/components/cv-editor/cvExport.ts`** (pure functions, no React,
mirrors `cv-editor.html:461-609` adapted to this repo's types/helpers):
- `escapeHtml(s: string | undefined): string`
- `buildExportBody(cv: CVDocument): string` — for each `cv.sections[i]`, compute
  `const kind = inferKind(section)` (reused from `editorStore.ts`, not re-implemented) and
  branch exactly like the reference's `buildExportBody`: `summary` → paragraph, `bullets`
  → `&bull;&nbsp;` list with hanging indent (`text-indent:-14px;padding-left:14px`, the
  reference's page-break-friendly technique — not a flex row, this HTML is static/
  non-editable), `skills` → `Heading: a, b, c` lines, everything else → one block per
  entry via an `entryHtml(e, esc)` helper mirroring `exportEntryHtml`
  (`cv-editor.html:557-568`): heading+dates row, subheading+location row, description,
  bullets, links — all field-presence-driven, not kind-specific (matches the reference,
  which never branches entry-level rendering on kind). Reuse the reference's CSS classes
  (`cv-section`, `cv-section-title`, `cv-entries`, `cv-entry`, `cv-entry-head`,
  `cv-bullets`, `cv-bullet`) so the print/page-break rules below apply.
- `buildExportHtml(cv: CVDocument, opts: { bottomMargin: number }): string` — wraps
  `buildExportBody` in a standalone document: Newsreader/IBM-Plex-Sans/Share-Tech-Mono
  Google Fonts `<link>` (same font family the app already loads in
  `frontend/src/index.css:3` — the export doc is a separate document/iframe context and
  needs its own `<link>`, it does not inherit the host page's), `@page{margin-bottom:
  ${bottomMargin}px}`, and the reference's page-break-avoidance rules
  (`cv-editor.html:578-586`: section titles/entry heads/bullets avoid breaking across
  pages, section bodies/bullet lists may break).
- `downloadHtml(cv: CVDocument): void` — `Blob` of `buildExportHtml(...)` + a temporary
  `<a download>` click, filename `{slugified name || "cv"}.html`, mirrors
  `cv-editor.html:589-595`.
- `downloadPdf(cv: CVDocument, bottomMargin: number): void` — off-screen same-origin
  `<iframe>` (`position:fixed;right:0;bottom:0;width:0;height:0;visibility:hidden`),
  `doc.write(buildExportHtml(...))`, then `iframe.contentWindow.print()` on load (with the
  same `onafterprint`-then-timeout cleanup fallback as `cv-editor.html:596-609`) — avoids
  popup blockers, no backend call.

**`editorStore.ts`**: add `exportOpen: boolean` (default `false`) and `toggleExport()`,
same shape as the existing `jsonOpen`/`toggleJson()` pair, so it resets alongside
`jsonOpen` on deck switch/load (same lines that already reset `jsonOpen: false`, e.g.
`editorStore.ts:510,914,927,1004`).

**New file `frontend/src/components/cv-editor/ExportModal.tsx`**: fixed-overlay modal
following the `UnfitModal.tsx` pattern (`panelBase(EDITOR_THEME, {chamfer:16})`,
`cornerMarks`, backdrop blur) sized like the reference's export dialog (`min(880px,100%)`
wide, scrollable body, `max-height:92vh`). Body renders a light "paper" preview card
(`dangerouslySetInnerHTML: buildExportBody(exportJson(cv))`, static/read-only, same
`#FCFBF8` surface + border the reference uses) so the user sees exactly what will export.
Footer: a bottom-margin `<input type="range" min={0} max={96} step={2}>` (component-local
`useState`, default `36`, not persisted to the store — export-time-only UI state, no
undo/redo interaction needed) + "DOWNLOAD HTML" ghost button (`Icon name="braces"`) +
"DOWNLOAD PDF" primary button (`Icon name="doc"`), calling `downloadHtml`/`downloadPdf`
from `cvExport.ts` with `exportJson(cv)` as input.

**`CvEditor.tsx`**: add an "EXPORT" header button next to the existing SRC.JSON toggle
(~line 545-550), `onClick={st.toggleExport}`, `Icon name="doc"` (same icon the Document
tab already uses — the reference reuses it for its export button too), and render
`{cv && exportOpen && <ExportModal />}` alongside the existing `{cv && jsonOpen &&
<JsonDrawer />}` (~line 591).

### Verification (both phases)

1. `cd frontend && npm run dev`, open the app, click the CV Structure Editor entry point,
   load or infer a CV with at least one experience/education entry that has bullets and
   one standalone "bullets"/highlights section.
2. Switch to **Document** view: confirm every bullet row now shows a small dot marker.
   Switch to **Split** view: same check in the paper preview pane.
3. Click **EXPORT**: confirm the modal opens with a correctly formatted light preview
   (centered name, contact line, section separators, bulleted lists with dots, dated
   entries right-aligned).
4. Click **DOWNLOAD HTML**: confirm a `.html` file downloads and opens correctly in a
   browser with bullets visible and fonts loaded.
5. Click **DOWNLOAD PDF**: confirm the browser's native print dialog opens with the CV
   correctly laid out (no mid-bullet or mid-entry-head page breaks); complete a "Save as
   PDF" and confirm the resulting PDF looks correct.
6. Confirm **Blocks** view and the **Commit/Save** flow are unaffected (no regressions —
   this is a pure merge/addition).
7. `cd frontend && npm test` — no existing test should break; this repo's CLAUDE.md
   frontend-i18n convention doesn't strictly apply here (the export HTML is a
   print artifact, not app chrome), but any new user-visible **editor UI** strings (the
   EXPORT button label, modal header/labels) must go through `useT()`/
   `strings.en.json` per the existing convention — the exported document's own content
   (names, section titles, bullets) is just the user's CV data and needs no translation.

## Phase 3 — Port date pickers, location autocomplete, and the section-tools pill

### Current State
Three deltas against the reference were explicitly flagged as "consciously not ported" in
Phase 1/2's Change Log (see below): `PaperSheet.tsx` entry dates were still a free-text
input, entry/contact location fields had no suggestion list, and `SectionTools` was a
top-right vertical stack instead of the reference's centered pill straddling the section's
top border.

### Desired State
`PaperSheet.tsx`'s Document/Split preview matches the reference on these three points:
entry dates edit through a month/year `<select>` pair plus a PRESENT toggle
(`cv-editor.html:193-223`); contact/entry location fields suggest previously-typed and
currently-used locations via a shared `<datalist>` (`cv-editor.html:278-298`); and the
section move-up/down/delete tools render as a horizontal pill centered on the section's
top border, hover-revealed (`cv-editor.html:995-1001`), not the existing top-right stack.

### Problems/Bugs
N/A — this is new functionality/restyling, not a fix. Scope is deliberately limited to
`PaperSheet.tsx` (Document/Split views) — `BlocksView.tsx` has its own separate free-text
date/location inputs and is untouched, matching how the reference itself frames these as
paper-view-specific (`dateRangeField(..., {variant:'paper'})` at `cv-editor.html:981`,
`allLocations()`/datalist reused across both views in the reference, but only the paper
call sites are in scope here per the original Phase 2 Change Log's framing).

### Solutions
- **New file `dateRange.ts`**: pure parse/format functions ported verbatim from
  `cv-editor.html:154-192` (`toMonthInput`, `parseDateStr`, `monthName`,
  `formatDateRange`, `yearOptions`) — kept separate from the picker component so the
  date-string parsing logic is independently testable.
- **New file `DateRangeField.tsx`**: the month/year `<select>` pair + PRESENT toggle UI,
  paper-variant styling only (reference's dark-chrome `blocks` variant has no equivalent
  target in this repo, see Problems/Bugs above). Wired into `PaperSheet.tsx`'s entry-level
  `dates` field, replacing the free-text `<input>`.
- **New file `locationHistory.ts`**: `useRecentLocations()` hook (localStorage-backed,
  key `jsa.cvEditor.recentLocations`) and `allLocations(cv, recent)` (merges recent +
  every location string currently in the loaded CV, deduped/sorted) — ported from
  `loadLocations`/`recordLocation`/`allLocations` (`cv-editor.html:278-298`).
- **`PaperSheet.tsx`**: renders one `<datalist id="cv-locations-list">` fed by
  `allLocations()`; contact location input and entry location inputs get
  `list="cv-locations-list"` + `onBlur` recording via the hook's `record()`.
- **`PaperSheet.tsx`**: `SectionTools`' style block changed from
  `{top:8, right:0, flexDirection:"column"}` to
  `{top:-13, left:"50%", transform:"translateX(-50%)", flexDirection:"row"}` — same
  hover-reveal CSS classes (`.cvsec .cvtools`) already in `src/index.css`, no CSS change
  needed.

### Verification
1. `npx tsc --noEmit` and `npm run build` clean; rebuild the served `jsa/static` bundle.
2. `npm test` — no regressions.
3. Live Playwright check against a real deck (Chrome extension MCP not connected this
   session either): Document view shows the month/year date picker with a working PRESENT
   toggle; hovering a section reveals the centered pill (up/down/trash) straddling its top
   border; the location `<datalist>` populates with the CV's own location strings, and
   typing+blurring a new location writes it to `localStorage`.

## Decisions Log

*(For the user's own notes — not written to by the assistant. Add an entry here only when
you reject an approach, override a proposal, or deliberately defer something.)*

## Change Log

**2026-09-16**: Implemented both phases on branch `feat/cv-editor-document-export` (from
`main`).

- **Actions**: Phase 1 — added `listStyle:"none"` + a manual 4×4px dot `<span>` before each
  bullet row in `PaperSheet.tsx` (both the section-kind `bullets` body and entry-level
  bullets), matching the reference's marker technique. Phase 2 — added
  `frontend/src/components/cv-editor/cvExport.ts` (buildExportBody/buildExportHtml/
  downloadHtml/downloadPdf) and `ExportModal.tsx`; wired `exportOpen`/`toggleExport()` into
  `editorStore.ts` (mirroring `jsonOpen`/`toggleJson`, including the three deck-switch reset
  sites); added an EXPORT header button in `CvEditor.tsx` next to SRC.JSON; added
  `cvEditor.export` + `exportModal.*` keys to `strings.en.json`.
- **Decisions/friction**: The plan originally had `cvExport.ts` take the already-exported
  `CVDocument` (`exportJson(cv)` output) as input. This crashed the app on first real-world
  test (`TypeError: Cannot read properties of undefined (reading 'filter')`) — `exportJson()`
  *omits* empty array fields entirely (`contact.links`/`section.items`/`entry.bullets` etc.
  are absent, not `[]`, when empty, mirroring the backend's optional-field schema), which
  the export code's `.filter()` calls assumed were always arrays. Fixed by rendering from the
  raw `EditorCV` (store state) instead, where `toEditor`/`toEditorEntry` guarantee real
  arrays and `kind` is already known — this is also what the reference file itself actually
  does (its `buildExportBody` reads `this.state.cv`, not its own `exportJson()`). Caught by
  browser verification, not by `tsc`/tests, since the type-level `CVDocument` interface
  doesn't reflect exportJson's actual runtime shape.
- **Verification**: `tsc --noEmit` clean; `npm run build` clean (also rebuilt the served
  `jsa/static` bundle); `npm test` — 469/469 passing, no regressions, both before and after
  the fix above. Live end-to-end check via a headless Playwright run against `jsa --csv
  ~/test.csv --no-browser --port 8765` (Chrome extension wasn't connected, so used Playwright
  instead — flagging this substitution since it wasn't the plan's literal instruction): opened
  the CV Structure Editor against a real multi-section deck, confirmed visible bullet dots in
  Document view (screenshot), opened the EXPORT modal (confirmed correct paper-style preview,
  no crash), clicked DOWNLOAD HTML, and rendered the downloaded file in a fresh headless page
  — confirmed 115 bullets rendered with `&bull;` markers, hanging indent, and correct
  entry/skills/summary layout. DOWNLOAD PDF's `iframe.print()` call path itself was not run
  headlessly (native print dialogs don't execute in headless Chromium), but it shares
  `buildExportHtml` with the verified HTML path — not independently verified end-to-end;
  recommend a manual click-through if that matters before relying on it further.
- **Follow-up fix**: an advisor pass caught a second defect before sign-off:
  `downloadHtml(cv)` hardcoded `bottomMargin: 36` instead of threading the modal's slider
  value (only `downloadPdf` did), so the exported `.html` always shipped `@page{margin-
  bottom:36px}` regardless of the slider — the reference doesn't have this split (its
  `downloadHtml()` reads the same `this.state.pdfBottomMargin` as PDF). Fixed by giving
  `downloadHtml` a `bottomMargin` parameter and passing it from `ExportModal.tsx`.
  Re-verified live: dragged the slider to 80px, downloaded HTML, confirmed the file's
  `@page` rule read `80px` (not `36`). Also confirmed Split view (which reuses `PaperSheet`)
  renders bullet dots and the paper preview correctly, not just Document view.
- **Consciously not ported** (in scope was "bullets + export", not a pixel-for-pixel
  merge — flagging these so they're a one-message redirect, not a surprise):
  - Reference's dated fields use a month/year `<select>` pair + a PRESENT toggle
    (`cv-editor.html:193-223`); the live editor's `PaperSheet.tsx` dates are still a free-
    text input.
  - Reference's per-section move/delete tools are a centered pill straddling the section's
    top border (`paperSectionTools`); the live editor keeps its existing top-right vertical
    stack (`SectionTools`).
  - Reference wires a `<datalist>`-backed location autocomplete (recent locations via
    localStorage) on the paper location fields; the live editor's paper location inputs
    have no such suggestion list.
  - Kept the live editor's existing presence-gated entry-bullet rendering (`e.bullets.length
    > 0`, any kind) over the reference's `kind === "experience"`-only gate — a deliberate
    keep of existing repo behavior, not an omission.
- **`/code-review low`**: run twice. First pass only covered already-tracked files (new
  files `ExportModal.tsx`/`cvExport.ts` are invisible to `git diff` until staged) and found
  nothing; staged the two new files and re-ran, which surfaced three real findings, all
  fixed:
  - `downloadPdf`'s `go()` (the `win.print()` call) could fire twice — once from
    `iframe.onload`, once from the unconditional `setTimeout(go, 350)` fallback — popping
    two native print dialogs back to back on browsers where a `doc.write()`'d iframe's load
    timing races the fallback. (Inherited from the reference file, which has the identical
    race — fixed here rather than left as "matches the reference.") Fixed with a `printed`
    idempotency flag.
  - Contact/entry links rendered as plain colored text (`<span>`), not real `<a href>`
    elements — also present in the reference, but a genuine usability gap for a document
    meant to be opened/printed. Added a `linkHtml()` helper (prefixes `https://` when the
    raw text has no scheme, keeps the displayed text as-typed) used for both contact links
    and entry links. Deliberately did NOT also make email/phone `mailto:`/`tel:` links —
    that wasn't part of the finding or the reference, would be scope creep.
  - The margin slider's `useState(36)` was local to `ExportModal`, so it silently reset to
    36 every time the dialog was closed and reopened (the modal fully unmounts) — the
    reference keeps this value on its top-level component, so it survives close/reopen.
    Fixed by lifting the state to `CvEditor.tsx` (still local component state, not the
    Zustand store — no undo/redo/save interaction needed) and passing it down as props.
  - Rejected one low-severity finding (no `try/finally` around `a.click()` in
    `downloadHtml`) — the reviewer itself flagged it as included only "to round out file
    coverage"; not worth defensive code for an exception that can't realistically occur on
    a synthetic, just-appended anchor element.
  - Re-verified live after fixes: margin threads through and persists across modal
    close/reopen, exported links are real `<a href="https://…">` elements, and
    `window.print()` fires exactly once per PDF-download click (instrumented count).
- **Not yet done**: `scripts/translate-ui.sh` hasn't been run for the new i18n keys
  (`cvEditor.export`, `exportModal.*`) — `useT()` falls back to English so nothing breaks,
  but the non-English locale catalogs are now behind per the repo's i18n convention; needs
  an API key this session doesn't have. Nothing is committed — branch
  `feat/cv-editor-document-export` off `main`, awaiting the user's review/merge decision.
- **Result**: Verified — both phases implemented, built, type-checked, unit-tested, and
  smoke-tested live against real data (Document + Split views, and a live export
  download/margin round-trip).

**2026-09-16**: Implemented Phase 3 (date pickers, location autocomplete, section-tools
pill) on branch `feat/cv-editor-document-export`, following the user's follow-up request
to port these three items that Phase 1/2's Change Log had explicitly deferred.

- **Actions**: added `dateRange.ts` (pure parse/format helpers), `DateRangeField.tsx` (the
  month/year picker + PRESENT toggle), and `locationHistory.ts` (`useRecentLocations` hook
  + `allLocations`); wired all three into `PaperSheet.tsx` — entry `dates` field now
  renders `DateRangeField` instead of free text, contact/entry `location` inputs get
  `list="cv-locations-list"` + `onBlur` recording, and a `<datalist>` is rendered once per
  sheet; restyled `SectionTools` from a top-right vertical stack to a centered pill
  straddling the section's top border (`top:-13, left:"50%", flexDirection:"row"`), reusing
  the existing `.cvsec .cvtools` hover-reveal CSS. Added `paperSheet.datePresentLabel` /
  `paperSheet.dateEditPresentTitle` to `strings.en.json`.
- **Decisions/friction**: kept scope to `PaperSheet.tsx` only (Document/Split), matching
  how the original Phase 2 Change Log framed all three items as paper-view deltas —
  `BlocksView.tsx` keeps its own separate free-text date/location inputs, untouched, same
  non-goal as Phase 1/2. `paperSheet.datesPlaceholder` is now unused in code (dead in all
  20 locale catalogs) since the free-text dates input it labeled no longer exists — left
  in place rather than editing every locale file; flagging as a minor loose end.
- **Verification**: `tsc --noEmit` clean; `npm run build` clean (rebuilt served
  `jsa/static`); `npm test` — 469/469 passing, no regressions. Live Playwright run against
  `jsa --csv ~/test.csv --no-browser --port 8766`: confirmed the month/year date picker
  renders and its PRESENT toggle highlights correctly on a real entry; confirmed the
  section-tools pill renders centered and straddling the top border on hover (screenshot);
  confirmed the location `<datalist>` populates with the 5 unique locations already in the
  loaded CV, and that typing a new location into the contact field and blurring persists it
  to `localStorage` (`jsa.cvEditor.recentLocations`) — confirmed this test edit was never
  committed to the deck (COMMIT was not clicked; `GET /api/cv-structure` still shows no
  `contact.location`).
- **`/code-review low`**: launched (staged the three new files first, same as Phase 1/2's
  scope-verification step). One real finding, applied: `paperSheet.datesPlaceholder` was
  now dead — the free-text `dates` input it labeled was replaced by `DateRangeField`, which
  has no placeholder using that key, and no other call site referenced it. Rather than
  removing it from `strings.en.json` alone (which would have left ~19 translated locale
  catalogs carrying a key with no English source), removed it from every
  `strings.<lang>.json` file and its checksum entries in `strings.meta.json` via a small
  script (load → delete key → re-dump with `indent=2, ensure_ascii=False`, confirmed
  byte-identical formatting elsewhere via `git diff --stat` showing exactly 1 line removed
  per locale file). No other findings — reviewer explicitly checked and cleared inverted
  conditions, off-by-ones, null derefs, missing awaits, swallowed errors,
  `exportOpen`-reset consistency across all 4 `editorStore.ts` set-sites, and
  `sectionBodyHtml`'s default-kind fallback.
- **Verification (post-fix)**: `tsc --noEmit` and `npm run build` clean after the i18n
  removal; all 21 locale JSON files re-validated as parseable.
- **Result**: Verified — Phase 3 implemented, built, type-checked, unit-tested,
  live-smoke-tested, and code-reviewed with its one finding applied. Nothing committed;
  branch `feat/cv-editor-document-export` off `main`, awaiting the user's review/merge
  decision.
