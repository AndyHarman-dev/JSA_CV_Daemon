// Zustand store for the Scratch Buffer — a persistent, cross-job notes pad. Notes belong to
// the user, not to any single job: same list everywhere, unaffected by job selection.
// Ported from the design handoff (.claude/designs/Scratch Buffer Design.zip,
// design_handoff_scratch_buffer/JSA App Shell.dc.html, search SCRATCH_BUFFER) into a real
// Zustand store, modeled on editorStore.ts's standalone-slice pattern.
//
// Persistence: localStorage key STORAGE_KEY, full array rewritten on every mutation — no
// separate "save" action. First run (no stored entries) starts empty; no seed data.

import { create } from "zustand";

export interface ScratchEntry {
  id: string;
  text: string;
  tag: string;
  pinned: boolean;
  ts: number;
}

const STORAGE_KEY = "jsa_scratch_entries";

// --- pure helpers (unit-testable independent of the store) -----------------------------

// Best-effort, cosmetic-only tag inference — not a hard categorization system.
export function guessTag(text: string): string {
  const m = text.match(/^#(\S+)/);
  if (m) return "#" + m[1];
  const low = text.toLowerCase();
  if (low.includes("salary") || low.includes("comp")) return "#salary";
  if (low.includes("visa") || low.includes("sponsor")) return "#visa";
  if (low.includes("notice")) return "#notice";
  return "#note";
}

// Relative time label: <1h -> "Nm" (min 1), <24h -> "Nh", else "Nd".
export function timeLabel(ts: number, now: number = Date.now()): string {
  const diff = now - ts;
  if (diff < 3_600_000) return Math.max(1, Math.round(diff / 60_000)) + "m";
  if (diff < 86_400_000) return Math.round(diff / 3_600_000) + "h";
  return Math.round(diff / 86_400_000) + "d";
}

// Pinned entries first, then most-recent-first within each group.
export function sortEntries(entries: ScratchEntry[]): ScratchEntry[] {
  return [...entries].sort((a, b) => Number(b.pinned) - Number(a.pinned) || b.ts - a.ts);
}

function load(): ScratchEntry[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ScratchEntry[]) : [];
  } catch {
    return [];
  }
}

function save(entries: ScratchEntry[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    /* localStorage unavailable (private mode, quota, etc.) — notes stay in memory only */
  }
}

// The design reference used a bare `e${Date.now()}` id, which collides whenever two entries
// are added within the same millisecond (trivially reachable via paste-then-Enter or rapid
// typing) — a collision then makes pin/delete silently act on every entry sharing that id.
// A monotonic counter suffix (mirrors editorStore.ts's `uid()`) guarantees uniqueness.
let _seq = 0;
function nextId(): string {
  _seq += 1;
  return `e${Date.now()}_${_seq}`;
}

// --- store ------------------------------------------------------------------------------

interface ScratchState {
  entries: ScratchEntry[];

  load(): void;
  addEntry(text: string): void;
  togglePin(id: string): void;
  deleteEntry(id: string): void;
}

export const useScratchStore = create<ScratchState>((set, get) => ({
  entries: [],

  load() {
    set({ entries: load() });
  },

  addEntry(text) {
    const trimmed = text.trim();
    if (!trimmed) return;
    const entry: ScratchEntry = {
      id: nextId(),
      text: trimmed,
      tag: guessTag(trimmed),
      pinned: false,
      ts: Date.now(),
    };
    const entries = [entry, ...get().entries];
    set({ entries });
    save(entries);
  },

  togglePin(id) {
    const entries = get().entries.map((e) => (e.id === id ? { ...e, pinned: !e.pinned } : e));
    set({ entries });
    save(entries);
  },

  deleteEntry(id) {
    const entries = get().entries.filter((e) => e.id !== id);
    set({ entries });
    save(entries);
  },
}));
