// Zustand store for the CV Structure Editor — a standalone editor over the single canonical
// base-CV CVDocument JSON of *one deck* (job-less). It is the source of truth while editing;
// the server is written only on Done / before navigating away, not per keystroke.
//
// Decks: the editor edits one deck at a time (`activeDeckId`). The rail's switch / new /
// duplicate actions all pass through flushAndPersist() first, so leaving a deck never
// silently drops its buffer.
//
// Identity & kind: each section/entry carries a transient `id` (React keys / drag / selection)
// and each section an inferred `kind` (drives the editing UI). Both are stripped on export
// (exportJson) — they are not part of jsa/schema/cv.py.
//
// History: full-snapshot undo/redo. Typing is *coalesced* (one snapshot per burst, committed
// ~600ms after the last keystroke or on the next structural op); structural changes (add /
// delete / reorder) commit immediately. See commit()/undo()/redo().

import { create } from "zustand";
import { api } from "./api";
import { useStore } from "./store";
import { getCatalog, englishCatalog } from "./i18n";
import type {
  CVDocument,
  CVSection,
  CvDeckDTO,
  EditorCV,
  EditorEntry,
  EditorSection,
  SectionKind,
  WSEvent,
} from "./types";

// --- id + classification ----------------------------------------------------------------

let _idSeq = 0;
function uid(): string {
  _idSeq += 1;
  return `e${_idSeq}_${Math.random().toString(36).slice(2, 8)}`;
}

const SUMMARY_RE = /\b(summary|profile|objective|about|overview)\b/i;
const SKILLS_RE =
  /\b(skills?|technolog|competenc|tool|expertise|proficienc|tech\s*stack|stack)\b/i;
const PROJECTS_RE = /\b(projects?|portfolio)\b/i;
const EDUCATION_RE = /\b(education|degree|academic|university|school)\b/i;
const EXPERIENCE_RE = /\b(experience|employment|work|career|history|positions?)\b/i;

// Mirror jsa/render/serialize.py's name+shape classification so `kind` round-trips on reload.
export function inferKind(s: CVSection): SectionKind {
  const name = s.name || "";
  if (SUMMARY_RE.test(name)) return "summary";
  if (SKILLS_RE.test(name)) return "skills";
  if (PROJECTS_RE.test(name)) return "projects";
  if (EDUCATION_RE.test(name)) return "education";
  if (EXPERIENCE_RE.test(name)) return "experience";
  // Shape-based fallback when the name is non-standard.
  if (s.entries && s.entries.length > 0) {
    const e = s.entries[0];
    if (e.bullets && e.bullets.length > 0) return "experience";
    if (e.links && e.links.length > 0) return "projects";
    if (e.subheading || e.dates) return "education";
    return "experience";
  }
  if (s.items && s.items.length > 0) return "bullets";
  if (s.text) return "summary";
  return "bullets";
}

// --- import (schema → editor) -----------------------------------------------------------

function toEditorEntry(e: Partial<EditorEntry>): EditorEntry {
  return {
    id: uid(),
    heading: e.heading,
    subheading: e.subheading,
    dates: e.dates,
    location: e.location,
    text: e.text,
    bullets: [...(e.bullets ?? [])],
    links: [...(e.links ?? [])],
  };
}

export function toEditor(cv: CVDocument): EditorCV {
  return {
    contact: {
      name: cv.contact.name ?? "",
      email: cv.contact.email,
      phone: cv.contact.phone,
      location: cv.contact.location,
      links: [...(cv.contact.links ?? [])],
    },
    sections: (cv.sections ?? []).map((s) => {
      const section: EditorSection = {
        id: uid(),
        kind: inferKind(s),
        name: s.name ?? "",
        text: s.text,
        items: [...(s.items ?? [])],
        entries: (s.entries ?? []).map(toEditorEntry),
      };
      return section;
    }),
  };
}

// --- export (editor → schema JSON) ------------------------------------------------------
// Strip id/kind; omit empty strings and empty arrays; drop fully-empty entries.

function nonEmpty(s: string | undefined): string | undefined {
  const t = (s ?? "").trim();
  return t ? t : undefined;
}

function cleanList(xs: string[] | undefined): string[] {
  return (xs ?? []).map((x) => x.trim()).filter((x) => x.length > 0);
}

function exportEntry(e: EditorEntry): Record<string, unknown> | null {
  const out: Record<string, unknown> = {};
  const heading = nonEmpty(e.heading);
  const subheading = nonEmpty(e.subheading);
  const dates = nonEmpty(e.dates);
  const location = nonEmpty(e.location);
  const text = nonEmpty(e.text);
  const bullets = cleanList(e.bullets);
  const links = cleanList(e.links);
  if (heading) out.heading = heading;
  if (subheading) out.subheading = subheading;
  if (dates) out.dates = dates;
  if (location) out.location = location;
  if (text) out.text = text;
  if (bullets.length) out.bullets = bullets;
  if (links.length) out.links = links;
  return Object.keys(out).length ? out : null;
}

export function exportJson(cv: EditorCV): CVDocument {
  const contact: Record<string, unknown> = { name: (cv.contact.name ?? "").trim() };
  const email = nonEmpty(cv.contact.email);
  const phone = nonEmpty(cv.contact.phone);
  const location = nonEmpty(cv.contact.location);
  const links = cleanList(cv.contact.links);
  if (email) contact.email = email;
  if (phone) contact.phone = phone;
  if (location) contact.location = location;
  if (links.length) contact.links = links;

  const sections = cv.sections.map((s) => {
    const out: Record<string, unknown> = { name: (s.name ?? "").trim() };
    const text = nonEmpty(s.text);
    const items = cleanList(s.items);
    const entries = s.entries.map(exportEntry).filter((e): e is Record<string, unknown> => e !== null);
    if (text) out.text = text;
    if (items.length) out.items = items;
    if (entries.length) out.entries = entries;
    return out;
  });

  return { contact, sections } as unknown as CVDocument;
}

// --- defaults for new sections ----------------------------------------------------------

const KIND_DEFAULTS: Record<SectionKind, { name: string; build: () => Partial<EditorSection> }> = {
  summary: { name: "Summary", build: () => ({ text: "" }) },
  bullets: { name: "Highlights", build: () => ({ items: [""] }) },
  skills: { name: "Skills", build: () => ({ entries: [toEditorEntry({ heading: "", bullets: [] })] }) },
  experience: {
    name: "Experience",
    build: () => ({
      entries: [toEditorEntry({ heading: "", subheading: "", dates: "", location: "", bullets: [""] })],
    }),
  },
  projects: {
    name: "Projects",
    build: () => ({ entries: [toEditorEntry({ heading: "", text: "", links: [] })] }),
  },
  education: {
    name: "Education",
    build: () => ({ entries: [toEditorEntry({ heading: "", subheading: "", dates: "", location: "" })] }),
  },
};

function newSection(kind: SectionKind): EditorSection {
  const def = KIND_DEFAULTS[kind];
  return {
    id: uid(),
    kind,
    name: def.name,
    text: undefined,
    items: [],
    entries: [],
    ...def.build(),
  } as EditorSection;
}

function blankCV(): EditorCV {
  return {
    contact: { name: "", links: [] },
    sections: [newSection("summary")],
  };
}

// --- store ------------------------------------------------------------------------------

const COALESCE_MS = 600;
const HISTORY_CAP = 120;

export type EditorView = "blocks" | "document" | "split";

interface EditorState {
  cv: EditorCV | null;
  view: EditorView;
  jsonOpen: boolean;
  paperSerif: boolean;

  // infer flow
  inferring: boolean;
  inferStep: number;
  inferTotal: number;
  inferLabel: string;
  inferError: string | null;
  inferFilename: string;

  selectedId: string | null;

  // drag transients (HTML5 DnD, armed only from the grip)
  dragId: string | null;
  overId: string | null;
  armed: boolean;

  addOpen: string | null; // "end" | section id | null

  // history
  history: EditorCV[];
  hpos: number;
  // `dirty` is the *coalescing* flag only: "the buffer differs from the newest history
  // snapshot". commit() always clears it, and structural edits commit immediately, so it is
  // false almost all the time and says nothing about the server.
  dirty: boolean;
  // `unsaved` is the real "buffer differs from what the server holds for activeDeckId" flag,
  // and the one flushAndPersist() gates on. It has to be separate from `dirty`: an add /
  // delete / reorder commits synchronously, leaving `dirty` false while the change has never
  // been PUT — gating on `dirty` would drop exactly those edits on a deck switch.
  unsaved: boolean;
  canUndo: boolean;
  canRedo: boolean;

  // save
  saving: boolean;
  saveError: string | null;

  // --- decks (the editor rail) ---
  decks: CvDeckDTO[];
  defaultDeckId: string | null;
  activeDeckId: string | null;
  railOpen: boolean;
  renameId: string | null;
  renameDraft: string;
  deckBusy: boolean;

  // --- lifecycle ---
  load(cv: CVDocument): void;
  startBlank(): void;
  reset(): void;

  // --- history ---
  commit(): void;
  undo(): void;
  redo(): void;

  // --- view / panels ---
  setView(v: EditorView): void;
  toggleJson(): void;
  togglePaperSerif(): void;
  setSelected(id: string | null): void;

  // --- drag ---
  arm(id: string): void;
  setDrag(id: string | null): void;
  setOver(id: string | null): void;
  endDrag(): void;

  setAddOpen(anchor: string | null): void;

  // --- contact (coalesced) ---
  updateContact(patch: Partial<EditorCV["contact"]>): void;

  // --- section field edits (coalesced) ---
  updateSection(id: string, patch: Partial<Pick<EditorSection, "name" | "text">>): void;
  updateItem(sectionId: string, idx: number, value: string): void;
  updateEntry(sectionId: string, entryId: string, patch: Partial<EditorEntry>): void;
  updateBullet(sectionId: string, entryId: string, idx: number, value: string): void;

  // --- structural (immediate commit) ---
  addSection(kind: SectionKind, afterId: string | null): void;
  deleteSection(id: string): void;
  moveSection(id: string, dir: -1 | 1): void;
  reorderSection(fromId: string, toIndex: number): void;
  addItem(sectionId: string): void;
  removeItem(sectionId: string, idx: number): void;
  addEntry(sectionId: string): void;
  deleteEntry(sectionId: string, entryId: string): void;
  moveEntry(sectionId: string, entryId: string, dir: -1 | 1): void;
  addBullet(sectionId: string, entryId: string): void;
  removeBullet(sectionId: string, entryId: string, idx: number): void;

  // --- infer / save ---
  beginInfer(filename: string): void;
  onInferProgress(e: Extract<WSEvent, { type: "infer_progress" }>): void;
  inferFromFile(file: File): Promise<void>;
  save(): Promise<boolean>;

  // --- decks ---
  hydrateDecks(): Promise<void>;
  refreshDeckIndex(): Promise<void>;
  setRailOpen(open: boolean): void;
  beginRename(id: string, draft: string): void;
  setRenameDraft(v: string): void;
  cancelRename(): void;
  flushAndPersist(): Promise<boolean>;
  switchDeck(id: string): Promise<void>;
  newDeck(): Promise<void>;
  duplicateDeck(id: string): Promise<void>;
  renameDeck(id: string, name: string): Promise<void>;
  setDefaultDeck(id: string): Promise<void>;
  deleteDeck(id: string): Promise<void>;
  deckLabel(d: CvDeckDTO): string;
}

let _commitTimer: ReturnType<typeof setTimeout> | null = null;

function clone<T>(v: T): T {
  return structuredClone(v);
}

// apiFetch throws `HTTP <code>: <body>`, where body is FastAPI's {"detail": "..."}. Unwrap
// both layers so the UI shows the server's reason and not the transport envelope. Extracted
// here because inferFromFile and save() each carried a copy and the deck actions would have
// made it four.
function detailOf(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : fallback;
  const m = /^HTTP \d+:\s*(.*)$/s.exec(raw);
  let detail = m ? m[1] : raw;
  try {
    const parsed = JSON.parse(detail) as { detail?: string };
    if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
  } catch {
    /* body wasn't JSON — keep the raw text */
  }
  return detail;
}

// The store's non-hook twin of useT(): same catalogs, same en-then-key fallback, but read
// imperatively because these strings are needed inside async actions (window.confirm text,
// the duplicate suffix), not during render.
function tr(key: string): string {
  const language = useStore.getState().language;
  return getCatalog(language)?.[key] ?? englishCatalog[key] ?? key;
}

export const useEditorStore = create<EditorState>((set, get) => {
  // Find + immutably map a section by id.
  function mapSection(id: string, fn: (s: EditorSection) => EditorSection): EditorCV | null {
    const cv = get().cv;
    if (!cv) return null;
    return { ...cv, sections: cv.sections.map((s) => (s.id === id ? fn(s) : s)) };
  }

  // Pull one deck's saved CV into the buffer, or clear to the empty state. Shared by
  // hydrateDecks / switchDeck / deleteDeck, which all need exactly this and nothing else.
  // Swallows like the pre-decks mount effect did: a failed fetch shows the empty state.
  async function loadDeckInto(id: string | null): Promise<void> {
    if (id === null) {
      get().reset();
      return;
    }
    try {
      const cv = await api.getCvDeck(id);
      if (cv) get().load(cv);
      else get().reset(); // a registered slot that has no CV saved yet
    } catch {
      get().reset();
    }
  }

  // Apply a mutation. coalesce=true → typing burst (debounced single snapshot);
  // false → structural (flush pending typing, then push this change immediately).
  function applyEdit(next: EditorCV, coalesce: boolean): void {
    if (coalesce) {
      set({ cv: next, dirty: true, unsaved: true, canUndo: true });
      if (_commitTimer) clearTimeout(_commitTimer);
      _commitTimer = setTimeout(() => get().commit(), COALESCE_MS);
    } else {
      get().commit(); // push any pending typed state as its own snapshot first
      set({ cv: next, dirty: true, unsaved: true });
      get().commit(); // then push the structural change
    }
  }

  return {
    cv: null,
    view: "blocks",
    jsonOpen: false,
    paperSerif: true,
    inferring: false,
    inferStep: 0,
    inferTotal: 5,
    inferLabel: "",
    inferError: null,
    inferFilename: "",
    selectedId: null,
    dragId: null,
    overId: null,
    armed: false,
    addOpen: null,
    history: [],
    hpos: -1,
    dirty: false,
    unsaved: false,
    canUndo: false,
    canRedo: false,
    saving: false,
    saveError: null,
    decks: [],
    defaultDeckId: null,
    activeDeckId: null,
    railOpen: false,
    renameId: null,
    renameDraft: "",
    deckBusy: false,

    load(cv) {
      const ed = toEditor(cv);
      if (_commitTimer) clearTimeout(_commitTimer);
      set({
        cv: ed,
        history: [clone(ed)],
        hpos: 0,
        dirty: false,
        canUndo: false,
        canRedo: false,
        inferring: false,
        inferError: null,
        selectedId: ed.sections[0]?.id ?? null,
        saveError: null,
        unsaved: false,
      });
    },

    startBlank() {
      const ed = blankCV();
      if (_commitTimer) clearTimeout(_commitTimer);
      set({
        cv: ed,
        history: [clone(ed)],
        hpos: 0,
        dirty: false,
        canUndo: false,
        canRedo: false,
        inferring: false,
        inferError: null,
        selectedId: ed.sections[0]?.id ?? null,
        saveError: null,
        // A pristine blank slate is not an unsaved *change* — the first edit sets the flag.
        // Otherwise INIT BLANK followed by an immediate deck switch would 422 and prompt.
        unsaved: false,
      });
    },

    reset() {
      if (_commitTimer) clearTimeout(_commitTimer);
      set({
        cv: null,
        history: [],
        hpos: -1,
        dirty: false,
        canUndo: false,
        canRedo: false,
        inferring: false,
        inferStep: 0,
        inferError: null,
        selectedId: null,
        jsonOpen: false,
        saveError: null,
        unsaved: false,
      });
    },

    commit() {
      if (_commitTimer) {
        clearTimeout(_commitTimer);
        _commitTimer = null;
      }
      const { dirty, cv, history, hpos } = get();
      if (!dirty || !cv) return;
      // Drop any redo tail, push a deep clone, cap the stack.
      let next = history.slice(0, hpos + 1);
      next.push(clone(cv));
      let newPos = next.length - 1;
      if (next.length > HISTORY_CAP) {
        next = next.slice(next.length - HISTORY_CAP);
        newPos = next.length - 1;
      }
      set({
        history: next,
        hpos: newPos,
        dirty: false,
        canUndo: newPos > 0,
        canRedo: false,
      });
    },

    undo() {
      get().commit(); // flush a pending typing burst so it becomes its own undo step
      const { history, hpos } = get();
      if (hpos <= 0) {
        set({ canUndo: false });
        return;
      }
      const newPos = hpos - 1;
      set({
        cv: clone(history[newPos]),
        hpos: newPos,
        dirty: false,
        canUndo: newPos > 0,
        canRedo: true,
        unsaved: true,
      });
    },

    redo() {
      if (get().dirty) get().commit();
      const { history: h2, hpos: p2 } = get();
      if (p2 >= h2.length - 1) {
        set({ canRedo: false });
        return;
      }
      const newPos = p2 + 1;
      set({
        cv: clone(h2[newPos]),
        hpos: newPos,
        dirty: false,
        canUndo: newPos > 0,
        canRedo: newPos < h2.length - 1,
        unsaved: true,
      });
    },

    setView(v) {
      set({ view: v });
    },
    toggleJson() {
      set((s) => ({ jsonOpen: !s.jsonOpen }));
    },
    togglePaperSerif() {
      set((s) => ({ paperSerif: !s.paperSerif }));
    },
    setSelected(id) {
      set({ selectedId: id });
    },

    arm(id) {
      set({ armed: true, dragId: id });
    },
    setDrag(id) {
      set({ dragId: id });
    },
    setOver(id) {
      set({ overId: id });
    },
    endDrag() {
      set({ armed: false, dragId: null, overId: null });
    },

    setAddOpen(anchor) {
      set({ addOpen: anchor });
    },

    updateContact(patch) {
      const cv = get().cv;
      if (!cv) return;
      applyEdit({ ...cv, contact: { ...cv.contact, ...patch } }, true);
    },

    updateSection(id, patch) {
      const next = mapSection(id, (s) => ({ ...s, ...patch }));
      if (next) applyEdit(next, true);
    },

    updateItem(sectionId, idx, value) {
      const next = mapSection(sectionId, (s) => {
        const items = [...s.items];
        items[idx] = value;
        return { ...s, items };
      });
      if (next) applyEdit(next, true);
    },

    updateEntry(sectionId, entryId, patch) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        entries: s.entries.map((e) => (e.id === entryId ? { ...e, ...patch } : e)),
      }));
      if (next) applyEdit(next, true);
    },

    updateBullet(sectionId, entryId, idx, value) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        entries: s.entries.map((e) => {
          if (e.id !== entryId) return e;
          const bullets = [...e.bullets];
          bullets[idx] = value;
          return { ...e, bullets };
        }),
      }));
      if (next) applyEdit(next, true);
    },

    addSection(kind, afterId) {
      const cv = get().cv;
      if (!cv) return;
      const sec = newSection(kind);
      const sections = [...cv.sections];
      const at = afterId === null ? sections.length : sections.findIndex((s) => s.id === afterId) + 1;
      sections.splice(at, 0, sec);
      applyEdit({ ...cv, sections }, false);
      set({ selectedId: sec.id, addOpen: null });
    },

    deleteSection(id) {
      const cv = get().cv;
      if (!cv) return;
      applyEdit({ ...cv, sections: cv.sections.filter((s) => s.id !== id) }, false);
      if (get().selectedId === id) set({ selectedId: get().cv?.sections[0]?.id ?? null });
    },

    moveSection(id, dir) {
      const cv = get().cv;
      if (!cv) return;
      const i = cv.sections.findIndex((s) => s.id === id);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= cv.sections.length) return;
      const sections = [...cv.sections];
      [sections[i], sections[j]] = [sections[j], sections[i]];
      applyEdit({ ...cv, sections }, false);
    },

    reorderSection(fromId, toIndex) {
      const cv = get().cv;
      if (!cv) return;
      const from = cv.sections.findIndex((s) => s.id === fromId);
      if (from < 0) return;
      const sections = [...cv.sections];
      const [moved] = sections.splice(from, 1);
      const clamped = Math.max(0, Math.min(toIndex, sections.length));
      sections.splice(clamped, 0, moved);
      applyEdit({ ...cv, sections }, false);
    },

    addItem(sectionId) {
      const next = mapSection(sectionId, (s) => ({ ...s, items: [...s.items, ""] }));
      if (next) applyEdit(next, false);
    },

    removeItem(sectionId, idx) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        items: s.items.filter((_, i) => i !== idx),
      }));
      if (next) applyEdit(next, false);
    },

    addEntry(sectionId) {
      const next = mapSection(sectionId, (s) => {
        // Seed the new entry with the same populated-field shape as the section's kind.
        const seed = newSection(s.kind).entries[0] ?? toEditorEntry({});
        return { ...s, entries: [...s.entries, { ...seed, id: uid() }] };
      });
      if (next) applyEdit(next, false);
    },

    deleteEntry(sectionId, entryId) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        entries: s.entries.filter((e) => e.id !== entryId),
      }));
      if (next) applyEdit(next, false);
    },

    moveEntry(sectionId, entryId, dir) {
      const next = mapSection(sectionId, (s) => {
        const i = s.entries.findIndex((e) => e.id === entryId);
        const j = i + dir;
        if (i < 0 || j < 0 || j >= s.entries.length) return s;
        const entries = [...s.entries];
        [entries[i], entries[j]] = [entries[j], entries[i]];
        return { ...s, entries };
      });
      if (next) applyEdit(next, false);
    },

    addBullet(sectionId, entryId) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        entries: s.entries.map((e) =>
          e.id === entryId ? { ...e, bullets: [...e.bullets, ""] } : e
        ),
      }));
      if (next) applyEdit(next, false);
    },

    removeBullet(sectionId, entryId, idx) {
      const next = mapSection(sectionId, (s) => ({
        ...s,
        entries: s.entries.map((e) =>
          e.id === entryId ? { ...e, bullets: e.bullets.filter((_, i) => i !== idx) } : e
        ),
      }));
      if (next) applyEdit(next, false);
    },

    beginInfer(filename) {
      set({
        inferring: true,
        inferStep: 0,
        inferLabel: "",
        inferError: null,
        inferFilename: filename,
      });
    },

    onInferProgress(e) {
      // Single-user tool: accept progress while inferring (no task_id gating — the POST
      // returns its task_id only on resolution, but WS events carry it from the start).
      if (!get().inferring) return;
      set({
        inferStep: e.step,
        inferTotal: e.total,
        inferLabel: e.label,
        inferError: e.status === "error" ? e.message || "Inference failed" : null,
      });
    },

    async inferFromFile(file) {
      get().beginInfer(file.name);
      try {
        const { structured } = await api.inferCvStructure(file);
        get().load(structured); // populate + reset history; clears inferring
      } catch (err) {
        set({ inferring: true, inferError: detailOf(err, "Inference failed") });
      }
    },

    async save() {
      const cv = get().cv;
      if (!cv) return false;
      get().commit();
      set({ saving: true, saveError: null });
      try {
        // No active deck means a fresh install (or the slot right after NEW BASE CV that was
        // never adopted): mint one and adopt it, so the very first save still lands somewhere.
        let id = get().activeDeckId;
        if (id === null) {
          const deck = await api.createCvDeck(null);
          id = deck.id;
          set({ activeDeckId: deck.id });
        }
        const saved = await api.saveCvDeck(id, exportJson(cv));
        // Reload from the server's canonical form so ids/kinds re-derive cleanly.
        get().load(saved);
        set({ saving: false });
        // Unblock the orchestrator's gate banner without a page reload.
        useStore.getState().setCvStructureExists(true);
        // auto_title / has_cv just changed for this deck — refresh the rail's rows.
        await get().refreshDeckIndex();
        return true;
      } catch (err) {
        set({ saving: false, saveError: detailOf(err, "Save failed") });
        return false;
      }
    },

    // --- decks ------------------------------------------------------------------------

    async hydrateDecks() {
      try {
        const { decks, default_id } = await api.listCvDecks();
        const active = default_id ?? decks[0]?.id ?? null;
        set({ decks, defaultDeckId: default_id, activeDeckId: active });
        await loadDeckInto(active);
      } catch {
        // Same failure shape as the pre-decks mount effect: show the empty state rather
        // than a broken editor. The rail simply renders no rows.
        set({ decks: [], defaultDeckId: null, activeDeckId: null });
        get().reset();
      }
    },

    // Metadata-only re-read. Deliberately swallows: a stale rail row is cosmetic, and this
    // runs on the tail of actions (save, rename, set-default) whose real work already
    // succeeded — surfacing a refresh failure as a save failure would be a lie.
    async refreshDeckIndex() {
      try {
        const { decks, default_id } = await api.listCvDecks();
        set({ decks, defaultDeckId: default_id });
      } catch {
        /* keep the last-known rows */
      }
    },

    setRailOpen(open) {
      set({ railOpen: open, ...(open ? {} : { renameId: null, renameDraft: "" }) });
    },

    beginRename(id, draft) {
      set({ renameId: id, renameDraft: draft });
    },

    setRenameDraft(v) {
      set({ renameDraft: v });
    },

    cancelRename() {
      set({ renameId: null, renameDraft: "" });
    },

    async flushAndPersist() {
      get().commit(); // fold a pending typing burst into history first
      if (!get().unsaved || !get().cv) return true;
      if (await get().save()) return true;
      // The buffer does not validate (422) or the server is unreachable. Navigating away
      // would drop it silently, so make the user own that: OK discards, Cancel stays put
      // with the inline saveError already on screen.
      return window.confirm(tr("cvDecks.discardDraft"));
    },

    async switchDeck(id) {
      if (id === get().activeDeckId || get().deckBusy) return;
      if (!(await get().flushAndPersist())) return;
      set({ deckBusy: true });
      try {
        // view/jsonOpen first: load()/reset() own selectedId and saveError, so setting them
        // afterwards would clobber the fresh deck's own selection.
        set({ activeDeckId: id, view: "blocks", jsonOpen: false });
        await loadDeckInto(id);
      } finally {
        set({ deckBusy: false });
      }
    },

    async newDeck() {
      if (get().deckBusy) return;
      if (!(await get().flushAndPersist())) return;
      set({ deckBusy: true });
      try {
        const deck = await api.createCvDeck(null);
        set({ activeDeckId: deck.id, view: "blocks", jsonOpen: false });
        // reset() (not startBlank) so the existing EmptyState re-offers RUN INFERENCE /
        // INIT BLANK for the new slot, and leaves `unsaved` false — switching straight back
        // out of an untouched new deck must not try to PUT an empty CV.
        get().reset();
        await get().refreshDeckIndex();
      } catch (err) {
        set({ saveError: detailOf(err, "Could not create a base CV") });
      } finally {
        set({ deckBusy: false });
      }
    },

    async duplicateDeck(id) {
      if (get().deckBusy) return;
      // Deliberately NOT flushAndPersist(): the server duplicates the deck *file*, so
      // taking its discard branch would copy stale on-disk content while the user believes
      // they duplicated what is on screen. If the live buffer is the source and it will not
      // save, refuse outright and leave the inline saveError up — no prompt, nothing copied.
      if (id === get().activeDeckId && get().cv) {
        get().commit();
        if (get().unsaved && !(await get().save())) return;
      }
      let created: string | null = null;
      set({ deckBusy: true });
      try {
        const src = get().decks.find((d) => d.id === id);
        // deckLabel() reads the live buffer for the active deck, so the copy is named after
        // what is on screen — which, thanks to the flush above, is also what was copied.
        const name = src ? `${get().deckLabel(src)} ${tr("cvDecks.copySuffix")}` : null;
        created = (await api.duplicateCvDeck(id, name)).id;
        await get().refreshDeckIndex();
      } catch (err) {
        set({ saveError: detailOf(err, "Could not duplicate this base CV") });
      } finally {
        set({ deckBusy: false });
      }
      // Outside the try on purpose: a failure inside switchDeck is a *switch* failure and
      // must not surface as "could not duplicate". deckBusy is released by now, so the
      // switch is not turned away by its own busy guard.
      if (created) await get().switchDeck(created);
    },

    async renameDeck(id, name) {
      const clean = name.trim();
      set({ renameId: null, renameDraft: "" });
      try {
        // "" means "drop the custom name", i.e. fall back to the server's auto title.
        await api.patchCvDeck(id, { name: clean === "" ? null : clean });
      } catch (err) {
        set({ saveError: detailOf(err, "Could not rename this base CV") });
      }
      await get().refreshDeckIndex();
    },

    async setDefaultDeck(id) {
      const previous = get().defaultDeckId;
      set({ defaultDeckId: id }); // optimistic: the star moves on click
      try {
        await api.patchCvDeck(id, { is_default: true });
      } catch (err) {
        set({ defaultDeckId: previous, saveError: detailOf(err, "Could not set the default") });
      }
      await get().refreshDeckIndex();
    },

    async deleteDeck(id) {
      if (get().deckBusy) return;
      set({ deckBusy: true });
      try {
        await api.deleteCvDeck(id);
        const { decks, default_id } = await api.listCvDecks();
        set({ decks, defaultDeckId: default_id });
        if (get().activeDeckId === id) {
          // The buffer belonged to the deck that is now gone — land on the new default,
          // or on the empty state when that was the last deck.
          const next = default_id ?? decks[0]?.id ?? null;
          set({ activeDeckId: next, view: "blocks", jsonOpen: false });
          await loadDeckInto(next);
        }
      } catch (err) {
        set({ saveError: detailOf(err, "Could not delete this base CV") });
      } finally {
        set({ deckBusy: false });
      }
    },

    deckLabel(d) {
      // The active row tracks the *live* buffer, so renaming yourself in the contact block
      // renames the row as you type; every other row reads the server's cached auto_title.
      const live = d.id === get().activeDeckId ? get().cv?.contact.name : undefined;
      return (d.name || live || d.auto_title || "").trim() || tr("cvDecks.untitled");
    },
  };
});
