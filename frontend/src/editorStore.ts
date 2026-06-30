// Zustand store for the CV Structure Editor — a standalone editor over the single canonical
// base-CV CVDocument JSON (job-less). It is the source of truth while editing; the server is
// written only on Done (PUT /api/cv-structure), not per keystroke.
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
import type {
  CVDocument,
  CVSection,
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
  dirty: boolean;
  canUndo: boolean;
  canRedo: boolean;

  // save
  saving: boolean;
  saveError: string | null;

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
}

let _commitTimer: ReturnType<typeof setTimeout> | null = null;

function clone<T>(v: T): T {
  return structuredClone(v);
}

export const useEditorStore = create<EditorState>((set, get) => {
  // Find + immutably map a section by id.
  function mapSection(id: string, fn: (s: EditorSection) => EditorSection): EditorCV | null {
    const cv = get().cv;
    if (!cv) return null;
    return { ...cv, sections: cv.sections.map((s) => (s.id === id ? fn(s) : s)) };
  }

  // Apply a mutation. coalesce=true → typing burst (debounced single snapshot);
  // false → structural (flush pending typing, then push this change immediately).
  function applyEdit(next: EditorCV, coalesce: boolean): void {
    if (coalesce) {
      set({ cv: next, dirty: true, canUndo: true });
      if (_commitTimer) clearTimeout(_commitTimer);
      _commitTimer = setTimeout(() => get().commit(), COALESCE_MS);
    } else {
      get().commit(); // push any pending typed state as its own snapshot first
      set({ cv: next, dirty: true });
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
    canUndo: false,
    canRedo: false,
    saving: false,
    saveError: null,

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
        const raw = err instanceof Error ? err.message : "Inference failed";
        // apiFetch throws `HTTP <code>: <body>` where body is FastAPI's {"detail": "..."}.
        const m = /^HTTP \d+:\s*(.*)$/s.exec(raw);
        let detail = m ? m[1] : raw;
        try {
          const parsed = JSON.parse(detail) as { detail?: string };
          if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
        } catch {
          /* body wasn't JSON — keep raw text */
        }
        set({ inferring: true, inferError: detail });
      }
    },

    async save() {
      const cv = get().cv;
      if (!cv) return false;
      get().commit();
      set({ saving: true, saveError: null });
      try {
        const saved = await api.saveCvStructure(exportJson(cv));
        // Reload from the server's canonical form so ids/kinds re-derive cleanly.
        get().load(saved);
        set({ saving: false });
        return true;
      } catch (err) {
        const raw = err instanceof Error ? err.message : "Save failed";
        // apiFetch throws `HTTP <code>: <body>` where body is FastAPI's {"detail": "..."}.
        const m = /^HTTP \d+:\s*(.*)$/s.exec(raw);
        let detail = m ? m[1] : raw;
        try {
          const parsed = JSON.parse(detail) as { detail?: string };
          if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
        } catch {
          /* body wasn't JSON — keep the raw text */
        }
        set({ saving: false, saveError: detail });
        return false;
      }
    },
  };
});
