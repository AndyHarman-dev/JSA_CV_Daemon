# Handoff: CV Structure Editor

## Overview
A full-screen editor for the **structured CV JSON** (`CVDocument`) that the JSA pipeline produces at the `cv_adjust` stage. The user inspects and reshapes that JSON with visual feedback instead of editing Markdown or re-running the model. Every edit writes straight to the JSON (the source of truth) — there is **no separate save button**. The user reaches it as a dedicated route/tab that replaces the main job view and fills the screen.

It does three jobs:
1. **Infer** a structure from an uploaded CV (button → pipeline → populated editor).
2. **Edit** every field, section, and entry of the CV with inline controls.
3. **Reorder** sections (and entries within them) — the headline interaction: sections are interchangeable blocks.

## About the Design Files
The file in this bundle — `CV Structure Editor.dc.html` — is a **design reference created in HTML**. It is a working prototype that shows the intended look, layout, typography, and interaction model. **It is not production code to copy directly.**

The task is to **recreate this design inside the existing JSA frontend** (`JSA/frontend/`), which is **React 18 + TypeScript + Vite + Tailwind CSS**, with a **Zustand** store (`src/store.ts`) and a typed API layer (`src/api.ts`). Use the codebase's established patterns and component conventions (see `src/components/ReviewPane.tsx`, `Header.tsx`, `JobDetail.tsx` for the house style: Tailwind classes, `bg-gray-50`, blue accents, small system-font UI). Do **not** ship the HTML directly, and do **not** use `React.createElement`-style construction as the prototype does (it was built that way only for the prototyping environment).

> ⚠️ The prototype renders its dynamic surface via `React.createElement` for streaming-environment reasons. In the real codebase, build normal JSX components. The visual spec below is what matters.

## Fidelity
**High-fidelity (hifi).** Final colors, typography, spacing, and interactions are specified below and should be matched closely. The one deliberate deviation: the prototype uses an **elevated/editorial** palette (warm neutrals + a refined indigo-blue) that is a polished cousin of the current app's plain Tailwind look. Decide with the team whether to (a) adopt this elevated palette for the editor, or (b) map it back onto the existing Tailwind tokens (`gray-*`, `blue-500/600`). Both are documented in **Design Tokens**.

---

## The Data Model (source of truth)
The editor reads and writes exactly the `CVDocument` Pydantic schema defined in **`JSA/jsa/schema/cv.py`**. Do not invent fields — the serializer (`JSA/jsa/render/serialize.py`) only renders known fields.

```ts
interface CVDocument {
  contact: {
    name: string;          // required, non-empty
    email?: string;
    phone?: string;
    location?: string;
    links: string[];       // linkedin / github / portfolio URLs
  };
  sections: Section[];     // min length 1, ordered, INTERCHANGEABLE
}

interface Section {
  name: string;            // "Summary", "Experience", "Skills", …
  text?: string;           // free prose (Summary)
  items: string[];         // flat keyword/bullet list
  entries: Entry[];        // structured sub-entries
}

interface Entry {
  heading?: string;        // role / project / degree / award title
  subheading?: string;     // company / institution / issuer
  dates?: string;
  location?: string;
  text?: string;           // prose description for the entry
  bullets: string[];
  links: string[];         // repo / demo / portfolio URLs
}
```

### Section "kinds" are a UI convenience, not a schema field
The schema has **no `type`/`kind`** on a section — the backend serializer infers layout from the section **name** (regex for Summary/Profile and for Skills/Tech) plus which of `text` / `items` / `entries` are populated. The editor keeps a local `kind` per section **only to drive which editing UI to show**, and maps each kind onto the real schema fields:

| Editor kind  | Default name | JSON shape produced                                                                 |
|--------------|--------------|-------------------------------------------------------------------------------------|
| `summary`    | Summary      | `{ name, text }`                                                                     |
| `bullets`    | Highlights   | `{ name, items: string[] }`                                                          |
| `skills`     | Skills       | `{ name, entries: [{ heading: "Languages", bullets: ["C++", …] }, …] }`              |
| `experience` | Experience   | `{ name, entries: [{ heading, subheading, dates, location, bullets }] }`             |
| `projects`   | Projects     | `{ name, entries: [{ heading, text, links }] }`                                      |
| `education`  | Education    | `{ name, entries: [{ heading, subheading, dates, location }] }`                      |

**Skills note:** a skill group is an `Entry` whose `heading` is the category and whose `bullets` are the keywords. The serializer's `_skills_block` renders it as `**Languages:** C++, Blueprint, Python` (one line per group). Store skills exactly this way.

**Export rule (what goes to JSON):** strip the local `id` and `kind`; omit empty strings and empty arrays. See the prototype's `exportJson()` for the precise filtering (e.g. `contact.links` only included if it has non-empty members; an entry is dropped if it has no populated fields).

---

## Screens / Views
There is one route with **three switchable editing paradigms** over the same state, plus an empty/inferring state. A top bar is always present.

### Top bar (persistent)
- **Layout:** full-width flex row, space-between, `padding: 11px 18px`, `background: rgba(250,248,244,.85)` with `backdrop-filter: blur(10px)`, `border-bottom: 1px solid #E7E2DA`, sits above content (`z-index: 20`).
- **Left cluster:** a 30×30 rounded-8 accent tile with a "blocks" glyph (`box-shadow: 0 1px 3px <accent>66`); product label **"Structure Editor"** (Geist 600 / 14px, `letter-spacing: -.01em`) over a mono sub-label **"CVDocument · live JSON"** (Geist Mono 500 / 10.5px, `#A39B8D`). When a CV is loaded, a vertical divider then the **candidate name** (truncating) + a pill **"N sections"** (mono 11px on `#F0EDE6`, radius 6).
- **Right cluster (only when CV loaded):**
  - **View switcher** — segmented control, 3 buttons (Blocks / Document / Split), each with a 15px icon. Container: `padding: 3px`, `background: #F0EDE6`, `border: 1px solid #E7E2DA`, `border-radius: 11px`. Active button: white bg, `box-shadow: 0 1px 2px rgba(0,0,0,.07)`, weight 600, ink color; inactive: transparent, `#6B6358`.
  - **Undo / Redo** — icon buttons in a matching segmented container. Disabled at history ends (opacity .3).
  - **View JSON / Hide JSON** — ghost button (toggles the drawer).
  - **Infer from CV / Re-infer** — primary (accent) button when no CV yet; ghost-ish "Re-infer" once loaded.
  - **Done** — primary accent button with a check icon (stub; wire to "close editor / return to job").

### View A — Blocks (PRIMARY, the chosen paradigm)
A vertically scrolling column, `max-width: 760px`, centered, `padding: 28px 24px 120px`.

- **Contact card** (first): white card, `border: 1px solid #E7E2DA`, `border-radius: 16px`, `padding: 20px`, subtle shadow. Header micro-label "CONTACT" (mono 10.5px, tracked, `#A39B8D`) + a hairline. Then a large **name** input (Geist 600 / 23px). Below, a 2-column grid of labeled rows for `email`, `phone`, `location` — each row is a `#FAF8F4` pill (`border-radius: 9px`) with a mono field-label (width 58px) and a borderless input. Then a "Links" group with the link-list editor.
- **Section cards** (one per section): white card, `border-radius: 16px`, `padding: 18px`, `box-shadow: 0 1px 2px rgba(40,34,24,.04)`.
  - **Header row:** a **drag grip** (6-dot SVG, `cursor: grab`) · a **kind chip** (icon + label, accent text on `aSoft` bg, radius 7) · the **section-name** input (Geist 600 / 16px) · a **tools** cluster (revealed on hover/focus-within): **move up**, **move down**, **delete** icon buttons.
  - **Body** dispatches on kind:
    - *summary*: one auto-growing textarea (Geist 14px).
    - *bullets*: a list of rows — each a small dot + auto-grow textarea + an `×` (revealed on row hover); plus a ghost "Add item".
    - *skills*: a list of group rows — each a `#FAF8F4` pill containing a 132px category input + a **tag editor** (chips with `×`, plus an inline "Add skill…" input that commits on Enter/comma and removes the last chip on Backspace-when-empty); plus "Add category".
    - *experience / projects / education*: a list of **entry editors** (see below) + a dashed "Add role / project / education" button.
  - **Between every card** there is a hover-reveal "Add" affordance (the `+` opens the Add-section menu inserting after this section).
- **Add section** (full-width dashed button) at the very bottom, and the inline `+` between cards — both open the **Add-section menu**.

**Entry editor (sub-card inside a section card):** `background: #FAF8F4`, `border: 1px solid #E7E2DA`, `border-radius: 11px`, `border-left: 2px solid #DCD6CB`, `padding: 11px 12px`. Fields shown per kind:
- experience → heading (bold) + dates (right, 138px); subheading + location (right, 138px); bullet list.
- projects → heading; description textarea; bullet list; **link list**.
- education → heading (= degree) + dates (right); subheading (= institution) + location (right).
A hover-revealed floating toolbar (top-right): entry **up / down / delete**.

### View B — Document (WYSIWYG paper)
The CV rendered as a **paper sheet** you edit directly in place. Sheet: `width: 760px`, white, `border-radius: 4px`, `box-shadow: 0 1px 2px rgba(40,34,24,.05), 0 16px 40px rgba(40,34,24,.07)`, `padding: 54px 60px 70px`, `min-height: 900px`, font = **Newsreader serif** (toggle to sans). Centered on the warm canvas, `padding: 36px 24px 120px`.
- **Name**: serif 30px/500, centered, editable.
- **Contact line**: centered, wraps, items separated by `·` dots — email · phone · links (accent color) · location. Each item is a borderless inline input sized to its content (`width: calc(<len>ch + 4px)`).
- **Sections**: separated by a top hairline (`#DCD6CB`). Section name is an uppercase, letter-spaced (`.13em`) sans label (11.5px/600) — **size the input to `calc(<len>ch + <len*0.14>em + 10px)`** so the tracking doesn't clip the last letter. Bodies mirror the PDF: summary paragraph; bullets as `<ul>`; skills as `Category: a, b, c` lines; entries as bold heading + right-aligned mono dates, italic subheading + right-aligned location, bulleted achievements, accent links.
- **Hover** over a section reveals a vertical tools rail (up/down/delete) pinned to the **right gutter inside the sheet** (`position: absolute; top: 8px; right: 0`) — keep it inside so it never causes horizontal scroll.
- Fields show a subtle accent underline on focus and a faint accent wash on hover.

### View C — Split (outline + live paper)
A 280px **outline** column (`flex: none`, `border-right: 1px solid #E7E2DA`, `background: #FAF8F4`, `padding: 18px 14px`) beside the scrolling **Document paper** on the right.
- Outline header "OUTLINE" (mono, tracked). Then one row per section: kind icon + section name (truncating) + `kind · count` sub-label. **Selected** row: white bg, accent border, small shadow; icon turns accent. Row hover reveals up/down arrows. Clicking selects.
- "Add section" button under the list.
- Right side renders the **same paper as View B** but in *selectable* mode: the selected section gets an accent **focus ring** (`box-shadow: 0 0 0 2px <accent>, 0 0 0 6px <accentSoft2>`, radius 6, extra padding). Clicking a section in the paper selects it in the outline. Editing happens on the paper.

### Empty state (no CV yet)
Centered card-free column, `max-width: 460px`. A 60×60 rounded-16 surface tile with a document glyph; **"No structure yet"** (Geist 600 / 21px); a paragraph explaining infer-or-blank and that *"The JSON is the source of truth — every edit here writes straight to it."*; two buttons: **Infer from CV** (primary) and **Start blank** (ghost, loads a single empty Summary section).

### Inferring state
Centered 420px card. Header: accent-tinted spark tile + "Inferring structure" + the source filename. A thin progress bar (`#F0EDE6` track, accent fill, width = `step/total`). Then a checklist of 5 steps, each with an 18px status disc:
`Reading document` → `Detecting section breaks` → `Extracting entries & dates` → `Structuring JSON` → `Validating against schema`.
Done steps: filled accent disc + white check. Active step: spinning ring + bold label. Pending: dim. **In production**, drive these from the **real infer pipeline** (file upload → backend job → progress events; the JSA backend already streams stage events over WebSocket — see `src/ws.ts` and `WSEvent` in `src/types.ts`). The prototype fakes it with `setTimeout` (~480ms/step).

### JSON drawer (overlay panel, toggled by "View JSON")
Right-side panel, `width: 400px`, `flex: none`, `border-left: 1px solid #E7E2DA`, `background: #FBFAF7`, full height, sits beside `main`.
- Header: braces icon + "Source JSON" + a live sub-label `● live · read-only · schema-valid` (green dot `#3E9A6B`), with **copy** and **close** icon buttons.
- Body: a `<pre>` (Geist Mono 12px/1.6, `tab-size: 2`) of `JSON.stringify(exportJson(), null, 2)` with **syntax highlighting**: keys = accent, strings = `#3E7A5E`, numbers = `#A8581E`, booleans/null = `#9A4DB8`. It is **read-only** and updates live on every edit.

---

## Interactions & Behavior
- **Live JSON, no save:** every field `onChange` updates the in-memory `CVDocument` immediately; the JSON drawer and (in production) the backend reflect it. Debounce a network write if persisting server-side.
- **Reorder sections:** both **drag-and-drop** and **up/down arrows**.
  - *Drag* uses HTML5 DnD but is **armed only from the grip handle** (`onMouseDown` on the grip sets `draggable=true` for that card; cleared on `dragEnd`) so text selection in inputs is unaffected. While dragging: source card `opacity: .4`; the card under the cursor shows an accent border + `box-shadow: 0 0 0 3px <accentSoft2>`. On drop, move the dragged section to the target's index.
  - *Arrows* swap with the neighbor; disabled at the ends.
- **Reorder entries:** up/down arrows (hover-revealed floating toolbar). Bullets/skills: add + remove (consider adding drag later).
- **Add section:** opens a popover menu listing the 6 kinds (icon tile + label + one-line description). Choosing inserts a new section (with that kind's default name + one empty entry/item) either after the clicked card or at the end, and selects it. Menu: white, `border-radius: 14px`, `box-shadow: 0 12px 32px rgba(40,34,24,.16)`, fades in (`cvfade` 140ms).
- **Tag editor (skills):** Enter or comma commits the trimmed value as a chip; Backspace on empty input removes the last chip; chips reveal an `×` on hover.
- **Auto-growing textareas:** prototype uses CSS `field-sizing: content` (Chromium). For broad browser support in production, use an auto-resize approach (e.g. measure `scrollHeight` on input, or a battle-tested hook) instead of relying on `field-sizing`.
- **Undo / redo:** ⌘Z / Ctrl+Z and ⌘⇧Z / Ctrl+Y, plus toolbar buttons. History is a stack of full `CVDocument` snapshots with a pointer. **Structural** changes (add/delete/reorder) push a snapshot immediately; **typing** is *coalesced* — the value updates live but a snapshot is pushed only ~600ms after the last keystroke (so one undo reverts a typing burst, not each character). Flush any pending coalesced snapshot before an undo. Cap history (~120 entries).
- **Transitions:** field hover/focus 120ms; tool reveals (opacity) 140ms; selection ring 150ms; menu/empty/inferring fade-in `cvfade` (opacity + 6px translateY, ~140–200ms).
- **Hover/focus states (define as CSS, not inline, since they're pseudo-states):**
  - card field hover (not focused): `background: #F2F0EA`; focus: `background: #fff`, accent border, `box-shadow: 0 0 0 3px <accent>@13%`.
  - paper field hover: faint accent wash; focus: accent bottom-border + wash.
  - icon button hover: `background: #ECE9E2`, ink color; disabled: opacity .3, no hover.

## State Management
Use Zustand (consistent with `src/store.ts`) or local component state. Needed state:
- `cv: CVDocument | null` — `null` = empty state.
- `view: 'blocks' | 'document' | 'split'`.
- `jsonOpen: boolean`.
- `inferring: boolean`, `inferStep: number`.
- `selectedId: string | null` — selected section (Split highlight; also useful for scroll-sync).
- Drag transients: `dragId`, `overId`, `armed`.
- `addOpen: string | null` — which Add-section anchor's menu is open (`'end'` or a section id).
- History: `history: CVDocument[]`, `hpos: number`.
- **Local-only identity:** attach a transient `id` to each section and entry for React keys, drag targets, and selection. **Strip `id` and `kind` on export** — they are not part of the schema.

Transitions are triggered by: field edits (→ update cv, maybe coalesced history push), structural actions (→ immediate history push), view-switch buttons, infer flow (→ inferring → load cv → seed history), undo/redo (→ move `hpos`, replace cv from snapshot).

### Backend / data wiring (production)
- Source the `CVDocument` from the `cv_adjust` document. The DB stores Markdown (`DocumentDTO.markdown`); to power this editor you'll want the **structured JSON** persisted alongside it. Coordinate with backend to expose/accept the `CVDocument` JSON (the pipeline already validates it via `jsa/schema/cv.py`; serialization to Markdown/PDF/DOCX is `jsa/render/serialize.py` → `cv_to_markdown`). On edit, persist JSON and re-run serialization to refresh the rendered preview/downloads.
- "Infer from CV" should call the real ingest/infer pipeline and stream progress (WebSocket events already exist — `src/ws.ts`).
- "Done" returns to the job view (`ReviewPane`), where the PDF/DOCX get re-rendered from the edited JSON.

## Design Tokens

### Elevated palette (as designed)
| Token        | Value                                  | Use                          |
|--------------|----------------------------------------|------------------------------|
| accent       | `#3B5BD9` (tweakable; alts `#2E7D5B`, `#B4530A`, `#6B4E9E`) | primary actions, active, chips |
| accent soft  | `color-mix(in srgb, accent 9%, #fff)`  | chip bg, tints               |
| accent soft2 | `color-mix(in srgb, accent 15%, #fff)` | drag/selection halo          |
| accent border| `color-mix(in srgb, accent 38%, #fff)` | focus/selected borders       |
| canvas       | `#F4F2ED`                              | app background               |
| surface      | `#FFFFFF`                              | cards, sheet                 |
| subtle       | `#FAF8F4`                              | entry sub-cards, pills, outline |
| sunk         | `#F0EDE6`                              | segmented controls, count pill |
| border       | `#E7E2DA`                              | card/divider borders         |
| border2      | `#DCD6CB`                              | stronger borders, left accent rail |
| ink          | `#211C16`                              | primary text                 |
| ink2         | `#6B6358`                              | secondary text               |
| ink3         | `#A39B8D`                              | tertiary / labels / placeholders |
| danger       | `#B4543E`                              | delete icon                  |
| json string  | `#3E7A5E`                              | JSON highlight               |
| json number  | `#A8581E`                              | JSON highlight               |
| json bool    | `#9A4DB8`                              | JSON highlight               |
| json key     | accent                                 | JSON highlight               |

### Mapping to existing Tailwind app (if you keep current tokens)
accent → `blue-600` (active) / `blue-500`; canvas → `gray-50`; surface → `white`; border → `gray-200`; ink → `gray-800/900`; ink2 → `gray-500`; ink3 → `gray-400`; danger → `red-500`. Adopt the existing `font-medium`/`text-sm` scale and `rounded`/`shadow` utilities.

### Typography
- UI: **Geist** (Google Fonts), weights 300–700. Base 13–14px. Mono labels/dates/JSON: **Geist Mono**. (House alternative: keep the app's `-apple-system` system stack.)
- Paper (Document/Split): **Newsreader** serif (Google Fonts) — name 30px/500, entry headings 14px/600, body 13–13.5px; toggleable to sans. Section labels are always uppercase sans, 11.5px/600, `letter-spacing: .13em`.
- Type scale used: name 30/23, section title 16, body 14, secondary 12.5–13, labels 10.5–11px.

### Spacing / radius / shadow
- Section gap: 18px (12px compact). Card padding: 17–18px (13px compact). Field padding: ~6px 9px.
- Radius: cards 16, sheet 4, sub-cards/menus 9–14, fields 8, chips/pills 6–9.
- Shadows: card `0 1px 2px rgba(40,34,24,.04)`; sheet `0 1px 2px …, 0 16px 40px rgba(40,34,24,.07)`; menu `0 12px 32px rgba(40,34,24,.16), 0 2px 6px rgba(40,34,24,.08)`; primary button `0 1px 2px <accent>55`.
- Keyframes: `cvspin` (360° spinner), `cvpulse`, `cvfade` (opacity + 6px rise), `cvbar` (scaleX progress).

### Tweakable options (exposed in the prototype)
- `accent` color (4 curated swatches), `density` (`comfortable` | `compact`), `paperSerif` (boolean: Newsreader vs sans on the paper views).

## Sample content
The prototype is populated with a fictional Unreal/game-dev engineer ("Daniel Okafor") covering all six section kinds. It is illustrative only — replace with real inferred data.

## Assets
- **Fonts:** Geist, Geist Mono, Newsreader — all Google Fonts (no licensing concern). Swap to the codebase's font system if it has one.
- **Icons:** all icons are inline SVGs hand-built in the prototype (grip dots, chevrons, plus, x, check, trash, undo/redo, braces, spark, link, copy, eye, and kind glyphs: text/list/tag/work/cap/blocks/doc/cols). Replace with the codebase's existing icon set (e.g. Lucide/Heroicons) — match the line weight (~1.55, rounded caps/joins, 15–16px).
- **No raster images.**

## Files
- `CV Structure Editor.dc.html` — the high-fidelity design reference (this bundle). Open it in a browser to see every state and interaction. Switch views in the top bar; click "Infer from CV" to populate; toggle "View JSON".
- Reference in the JSA repo:
  - `JSA/jsa/schema/cv.py` — the authoritative `CVDocument` schema (mirror its field tolerance & validation).
  - `JSA/jsa/render/serialize.py` — how the JSON becomes Markdown/PDF (Summary floats to top; Skills render compactly; `---` rules per section). The editor's layout intentionally mirrors this output.
  - `JSA/frontend/src/` — target codebase: `store.ts`, `api.ts`, `types.ts`, `ws.ts`, and `components/` for house style.
