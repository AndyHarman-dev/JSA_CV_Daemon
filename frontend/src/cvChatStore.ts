// Zustand store for the CV Editor AI chat — a sibling of editorStore, deliberately NOT
// part of its undo/history state (the diff card's own APPLY/DISCARD is the user action
// that feeds editorStore's undo stack, via applyAiDocument; the thread itself is not an
// undoable edit). See the CV Editor AI Chat plan's Phase 4.
//
// Circular-import note: this module imports editorStore.ts (for exportJson/toEditor/
// applyAiDocument/activeDeckId) and store.ts imports THIS module (to forward chat_chunk /
// chat_turn_end WS events) while editorStore.ts already imports store.ts. All three edges
// are real ES module cycles, but every actual `.getState()`/hook read here happens inside
// a function body, never at module-eval time -- the same precondition editorStore.ts and
// store.ts already rely on for their existing mutual cycle.

import { create } from "zustand";
import { api } from "./api";
import { exportJson, tr, useEditorStore } from "./editorStore";
import type { ChatTurnDTO, CVDocument, EditorCV, WSEvent } from "./types";

export type ScopeType = "cv" | "contact" | "section" | "entry";

// Client-id-based addressing for UI purposes only (highlight, dim, anchor measurement).
// Converted to the server's index-based scope at send time -- see scopeIndices() below;
// indices are the only addressing the server understands (the plan's D1/D2).
export interface ChatScope {
  type: ScopeType;
  sectionId?: string;
  entryId?: string;
}

export interface PendingFile {
  id: string;
  file: File;
}

// The wire ChatTurnDTO plus which deck it was sent for (so a turn resolving after a deck
// switch can be refused outright -- see applyTurn) and a client-only "stale" status that
// ChatTurnDTO.status (mirroring the server's Literal exactly) has no slot for.
export type ClientTurnStatus = ChatTurnDTO["status"] | "stale";
export interface ClientChatTurn extends Omit<ChatTurnDTO, "status"> {
  status: ClientTurnStatus;
  deckId: string | null;
}

const _MAX_FILES = 6;

// A cheap, deterministic (non-cryptographic) hash of the exported CV JSON -- just needs to
// change when the document changes and match when it doesn't, for D4's staleness check.
// FNV-1a over the JSON string; always built from the same exportJson() shape, so key order
// is stable across calls.
export function hashCv(cv: CVDocument): string {
  const s = JSON.stringify(cv);
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return (h >>> 0).toString(16);
}

function scopeIndices(
  scope: ChatScope,
  cv: EditorCV | null
): { section_index: number | null; entry_index: number | null } {
  if (!cv || (scope.type !== "section" && scope.type !== "entry")) {
    return { section_index: null, entry_index: null };
  }
  const sIdx = cv.sections.findIndex((s) => s.id === scope.sectionId);
  if (sIdx < 0) return { section_index: null, entry_index: null };
  if (scope.type === "section") return { section_index: sIdx, entry_index: null };
  const eIdx = cv.sections[sIdx].entries.findIndex((e) => e.id === scope.entryId);
  return { section_index: sIdx, entry_index: eIdx < 0 ? null : eIdx };
}

function blankTurn(partial: Partial<ClientChatTurn> & { id: string; deckId: string | null }): ClientChatTurn {
  return {
    role: "scope",
    scope: {},
    text: "",
    question: null,
    reasoning: "",
    items: [],
    document: null,
    status: "none",
    files: [],
    base_hash: "",
    created_at: new Date().toISOString(),
    ...partial,
  };
}

interface CvChatState {
  open: boolean;
  collapsed: boolean;
  scope: ChatScope | null;
  deckId: string | null;
  turns: ClientChatTurn[];
  input: string;
  attach: PendingFile[];
  busy: boolean;
  elapsed: number;
  taskId: string | null;
  reasoning: string;
  content: string;
  auto: boolean;
  flashKey: string | null;
  showReason: Record<string, boolean>;
  error: string | null;

  openChat(scope: ChatScope): void;
  closeChat(): void;
  setCollapsed(collapsed: boolean): void;
  widenToCv(): void;
  setInput(v: string): void;
  addFiles(files: FileList | File[]): void;
  removeFile(id: string): void;
  toggleShowReason(turnId: string): void;
  send(quickAction?: string): Promise<void>;
  newThread(): Promise<void>;
  hydrate(deckId: string): Promise<void>;
  applyTurn(turnId: string): void;
  discardTurn(turnId: string): void;
  toggleAuto(): void;
  onChunk(e: Extract<WSEvent, { type: "chat_chunk" }>): void;
  onTurnEnd(e: Extract<WSEvent, { type: "chat_turn_end" }>): void;
}

let _elapsedTimer: ReturnType<typeof setInterval> | null = null;

export const useCvChatStore = create<CvChatState>((set, get) => ({
  open: false,
  collapsed: false,
  scope: null,
  deckId: null,
  turns: [],
  input: "",
  attach: [],
  busy: false,
  elapsed: 0,
  taskId: null,
  reasoning: "",
  content: "",
  auto: false,
  flashKey: null,
  showReason: {},
  error: null,

  openChat(scope) {
    const prev = get().scope;
    const changed =
      !!prev &&
      (prev.type !== scope.type || prev.sectionId !== scope.sectionId || prev.entryId !== scope.entryId);
    set({ open: true, collapsed: false, scope, error: null });
    if (changed) {
      const deckId = get().deckId;
      set((s) => ({
        turns: [...s.turns, blankTurn({ id: `scope-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, deckId })],
      }));
    }
  },

  closeChat() {
    set({ open: false });
  },

  setCollapsed(collapsed) {
    set({ collapsed });
  },

  widenToCv() {
    get().openChat({ type: "cv" });
  },

  setInput(v) {
    set({ input: v });
  },

  addFiles(files) {
    const incoming = Array.from(files);
    set((s) => ({
      attach: [
        ...s.attach,
        ...incoming.map((file) => ({ id: `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`, file })),
      ].slice(0, _MAX_FILES),
    }));
  },

  removeFile(id) {
    set((s) => ({ attach: s.attach.filter((f) => f.id !== id) }));
  },

  toggleShowReason(turnId) {
    set((s) => ({ showReason: { ...s.showReason, [turnId]: !s.showReason[turnId] } }));
  },

  toggleAuto() {
    set((s) => ({ auto: !s.auto }));
  },

  async hydrate(deckId) {
    set({ deckId, turns: [], open: false, scope: null, error: null });
    try {
      const { turns } = await api.getDeckChat(deckId);
      set({ turns: turns.map((t) => ({ ...t, deckId })) });
    } catch {
      set({ turns: [] });
    }
  },

  async newThread() {
    const deckId = get().deckId;
    if (!deckId) return;
    try {
      await api.clearDeckChat(deckId);
    } finally {
      set({ turns: [] });
    }
  },

  async send(quickAction) {
    const state = get();
    if (state.busy || !state.scope || !state.deckId) return;
    const editorCv = useEditorStore.getState().cv;
    if (!editorCv) return;

    const exported = exportJson(editorCv);
    const baseHash = hashCv(exported);
    const { section_index, entry_index } = scopeIndices(state.scope, editorCv);
    const scopePayload: Record<string, unknown> = { type: state.scope.type };
    if (section_index !== null) scopePayload.section_index = section_index;
    if (entry_index !== null) scopePayload.entry_index = entry_index;

    const instruction = quickAction ? null : state.input.trim() || null;
    const files = state.attach.map((a) => a.file);
    const deckId = state.deckId;

    set({ busy: true, elapsed: 0, reasoning: "", content: "", error: null, input: "", attach: [] });
    if (_elapsedTimer) clearInterval(_elapsedTimer);
    _elapsedTimer = setInterval(() => set((s) => ({ elapsed: s.elapsed + 1 })), 1000);

    try {
      const res = await api.sendDeckChat(
        deckId,
        { scope: scopePayload, instruction, quick_action: quickAction ?? null, cv: exported, base_hash: baseHash },
        files
      );
      const agentTurn = res.turns[res.turns.length - 1];
      set((s) => ({
        turns: [...s.turns, ...res.turns.map((t) => ({ ...t, deckId } as ClientChatTurn))],
        taskId: res.task_id,
        // Per-op failures (an unknown id, a bad_argument) don't fail the turn as a
        // whole (jsa/pipeline/cv_chat.py::_apply_ops) -- the rest of the ops still
        // land, and the model's own `answer` text may confidently describe the
        // change as done. Surface them, or the user has no way to know part of what
        // they asked for silently didn't happen.
        error: res.rejected.length > 0 ? `${tr("cvChat.partialApplyWarning")} ${res.rejected.join("; ")}` : null,
      }));
      if (agentTurn && agentTurn.status === "pending" && agentTurn.document && get().auto) {
        const currentCv = useEditorStore.getState().cv;
        const stillFresh = !!currentCv && hashCv(exportJson(currentCv)) === baseHash;
        if (stillFresh) {
          get().applyTurn(agentTurn.id);
          set((s) => ({
            turns: s.turns.map((t) => (t.id === agentTurn.id ? { ...t, status: "auto" } : t)),
          }));
        } else {
          // D4: auto-mode must refuse to auto-apply a stale diff -- leave it PROPOSED.
          set((s) => ({
            turns: s.turns.map((t) => (t.id === agentTurn.id ? { ...t, status: "stale" } : t)),
          }));
        }
      }
    } catch (err) {
      set({ error: err instanceof Error ? err.message : tr("cvChat.requestFailed") });
    } finally {
      if (_elapsedTimer) {
        clearInterval(_elapsedTimer);
        _elapsedTimer = null;
      }
      set({ busy: false });
    }
  },

  applyTurn(turnId) {
    const turn = get().turns.find((t) => t.id === turnId);
    if (!turn || !turn.document || turn.status === "applied" || turn.status === "auto") return;
    const editor = useEditorStore.getState();

    // Deck identity, checked first and explicitly: a turn resolving after a mid-flight
    // switchDeck() must never write one deck's document into another's buffer. The
    // base_hash check below happens to also catch most such cases (a different deck's
    // document rarely hashes the same), but only incidentally -- this guard does not
    // depend on that coincidence.
    if (turn.deckId !== null && turn.deckId !== editor.activeDeckId) {
      set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "error" } : t)) }));
      return;
    }

    // A rehydrated/persisted turn can be applied with no live buffer at all (editor
    // unmounted, deck cleared) -- applyAiDocument would silently no-op and leave the turn
    // stuck "pending" with no feedback. Fail the turn explicitly instead.
    if (!editor.cv) {
      set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "error" } : t)) }));
      return;
    }

    if (turn.base_hash && hashCv(exportJson(editor.cv)) !== turn.base_hash) {
      set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "stale" } : t)) }));
      return;
    }

    editor.applyAiDocument(turn.document);
    set((s) => ({
      turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "applied" } : t)),
      flashKey: turnId,
    }));
  },

  discardTurn(turnId) {
    set((s) => ({ turns: s.turns.map((t) => (t.id === turnId ? { ...t, status: "discarded" } : t)) }));
  },

  onChunk(e) {
    // Single-flight gating, not task_id correlation -- same convention as
    // editorStore.onInferProgress (this is a single-user, local-only tool; the POST's own
    // resolution is what ends `busy`, and the server's task_id is minted only inside the
    // request, unknowable to the client before it resolves).
    if (!get().busy) return;
    set((s) => ({
      content: e.kind === "content" ? s.content + e.text : s.content,
      reasoning: e.kind === "reasoning" ? s.reasoning + e.text : s.reasoning,
    }));
  },

  onTurnEnd(_e) {
    // No-op: `send()`'s own `finally` clears `busy` and finalizes turns once the POST
    // resolves -- this event exists for parity with the job-scoped agent_turn_end handling
    // in store.ts, not because this store needs to act on it separately.
  },
}));
