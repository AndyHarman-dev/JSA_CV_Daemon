import { create } from "zustand";
import type { JobDTO, TranscriptTurn, WSEvent } from "./types";
import { api } from "./api";
import { useEditorStore } from "./editorStore";

interface BackendSwitchEvent {
  job_id: string;
  from_backend: string;
  to_backend: string;
}

// A user-facing notification for a BF-19 backend/model switch (jsa/events/schema.py's
// BackendSwitchedEvent / ModelSwitchedEvent). These fire mid-conversation — e.g. a
// follow-up question gets silently re-asked on the new backend/model after a switch —
// so the toast exists to explain that apparent hang/repeat, not just log it to console.
export interface ToastItem {
  id: string;
  jobId: string;
  jobLabel: string; // "{company} — {role}", or jobId if the job isn't in the local map
  kind: "backend" | "model";
  from: string;
  to: string;
}

interface Store {
  jobs: Record<string, JobDTO>;
  selectedId: string | undefined;
  wsStatus: "connecting" | "open" | "closed";
  editorOpen: boolean;
  // Most recent `backend_switched` WS event — additive signal so surfaces (e.g. Header's
  // backend cluster) can react without changing applyEvent's per-type behavior for others.
  lastBackendSwitch: BackendSwitchEvent | null;
  // Queue of live backend/model-switch notifications for Toast.tsx (bottom-right stack).
  // Independent of `lastBackendSwitch` above (Header's backend-cluster reordering signal) —
  // this is the user-visible "why did my answered question just get asked again" surface.
  toasts: ToastItem[];
  // Global output/UI language preference (ISO 639-1 code). Hydrated from GET /api/preferences
  // on app boot; every component rendering translatable chrome reads this via useT().
  language: string;
  languages: [string, string, string][];
  // --select-language boot gate (jsa/config.py Settings.select_language, surfaced via
  // /api/config). When true, App.tsx mounts BootGate before the dashboard on first load.
  selectLanguageMode: boolean;
  bootStage: "lang" | "boot" | "app";
  bootLang: string; // the picker's in-progress selection, defaults to the current `language`
  // False until hydrateLanguage's /api/config round-trip resolves (or fails). App.tsx keeps
  // the screen blank until this flips — otherwise the dashboard would paint for one frame
  // before selectLanguageMode is known, flashing behind the boot gate on a --select-language
  // cold load (the exact case the gate exists to prevent).
  configReady: boolean;
  // Whether cv_structure.json exists yet (GET /api/config's cv_structure_exists). null =
  // not yet known (pre-hydration). The orchestrator keeps jobs pending until this is true —
  // JobList surfaces a gate banner pointing at the Structure Editor while it's false.
  cvStructureExists: boolean | null;
  // Which artifact the user is looking at, independent of what the pipeline is currently
  // running — driven by clicking the CV_ADJUST / COVER_LETTER dots in StageTimeline. `null`
  // means "follow the pipeline" (today's default behaviour). Reset to null on every job
  // switch (selectJob) so a stale view never carries over to a different job.
  viewedStage: "cv" | "cl" | null;
  // Per-job display-ready transcript turns (GET /api/jobs/{id}/transcript). Undefined
  // until first fetched. Never capped — see CLAUDE.md-adjacent plan note: the toast
  // `.slice(-5)` pattern bounds ephemeral notifications, not conversation history.
  transcripts: Record<string, TranscriptTurn[]>;
  // Phase 8 — per-job in-progress streamed content, keyed by job_id. Cleared on
  // agent_turn_end (discarded outright when superseded=true, since that means a
  // retry/nudge/self-heal path replayed the whole turn and this buffer is stale).
  // Not persisted anywhere — a page reload loses an in-flight stream, same as today's
  // "loading" state until the next transcript fetch lands.
  streamBuffers: Record<
    string,
    { stage: string; content: string; reasoning: string; tools: { name: string; detail: string; ok: boolean; at: number }[] }
  >;
  upsertJob(j: JobDTO): void;
  selectJob(id: string | undefined): void;
  setViewedStage(stage: Store["viewedStage"]): void;
  setWsStatus(s: Store["wsStatus"]): void;
  setEditorOpen(open: boolean): void;
  setCvStructureExists(exists: boolean): void;
  dismissToast(id: string): void;
  jobLabelFor(id: string): string;
  applyEvent(e: WSEvent): void;
  refetchAll(): Promise<void>;
  fetchTranscript(jobId: string): Promise<void>;
  removeJob(id: string): void;
  hydrateLanguage(): Promise<void>;
  setLanguage(code: string): Promise<boolean>;
  setBootStage(stage: Store["bootStage"]): void;
  setBootLang(code: string): void;
  // --- Manual job launch (jobs are parked as `queued` until explicitly launched) ---
  launchJob(id: string): Promise<void>;
  launchAll(): Promise<void>;
}

export const useStore = create<Store>((set, get) => ({
  jobs: {},
  selectedId: undefined,
  wsStatus: "connecting",
  editorOpen: false,
  lastBackendSwitch: null,
  toasts: [],
  language: "en",
  languages: [],
  selectLanguageMode: false,
  bootStage: "app",
  bootLang: "en",
  configReady: false,
  cvStructureExists: null,
  viewedStage: null,
  transcripts: {},
  streamBuffers: {},

  upsertJob(j: JobDTO) {
    set((state) => ({
      jobs: { ...state.jobs, [j.id]: j },
    }));
  },

  selectJob(id: string | undefined) {
    set({ selectedId: id, viewedStage: null });
  },

  setViewedStage(stage: Store["viewedStage"]) {
    set({ viewedStage: stage });
  },

  setWsStatus(s: Store["wsStatus"]) {
    set({ wsStatus: s });
  },

  setEditorOpen(open: boolean) {
    set({ editorOpen: open });
  },

  setCvStructureExists(exists: boolean) {
    set({ cvStructureExists: exists });
  },

  dismissToast(id: string) {
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) }));
  },

  jobLabelFor(id: string) {
    const job = get().jobs[id];
    return job ? `${job.company} — ${job.role}` : id;
  },

  removeJob(id: string) {
    set((state) => {
      const { [id]: _, ...remaining } = state.jobs;
      return {
        jobs: remaining,
        selectedId: state.selectedId === id ? undefined : state.selectedId,
      };
    });
  },

  applyEvent(e: WSEvent) {
    const store = get();
    switch (e.type) {
      case "status_changed":
        // Also invalidate this job's transcript specifically — soft/nuclear resets and
        // the session-expired auto-reset delete Message rows and only emit
        // status_changed, not transcript_changed, at some call sites (see CLAUDE.md /
        // the transcript-projection plan's Phase 2 note). refetchAll alone would leave
        // the thread showing rows that no longer exist.
        store.fetchTranscript(e.job_id).catch((err: unknown) => {
          console.error("fetchTranscript failed:", err);
        });
        store.refetchAll().catch((err: unknown) => {
          console.error("refetchAll failed:", err);
        });
        break;
      case "stage_complete":
      case "approved":
      case "follow_up_needed":
        store.refetchAll().catch((err: unknown) => {
          console.error("refetchAll failed:", err);
        });
        break;
      case "transcript_changed":
        // Id-only invalidation hint — refetch just this job's transcript, never
        // refetchAll (which hits GET /api/jobs, a shape with no transcript at all).
        store.fetchTranscript(e.job_id).catch((err: unknown) => {
          console.error("fetchTranscript failed:", err);
        });
        break;
      case "backend_switched":
        // Refetch so the UI reflects the new backend assignment and log the switch.
        console.info(
          `[JSA] Backend switched for job ${e.job_id}: ${e.from_backend} → ${e.to_backend}`
        );
        set({
          lastBackendSwitch: {
            job_id: e.job_id,
            from_backend: e.from_backend,
            to_backend: e.to_backend,
          },
          // Capped at 5 — a stuck/unmounted Toast surface (e.g. a background tab) must not
          // grow this array without bound.
          toasts: [
            ...store.toasts,
            {
              id: `backend-${e.job_id}-${Date.now()}`,
              jobId: e.job_id,
              jobLabel: store.jobLabelFor(e.job_id),
              kind: "backend" as const,
              from: e.from_backend,
              to: e.to_backend,
            },
          ].slice(-5),
        });
        // A backend switch resets the job's stage (backend_switch_reset), which deletes
        // its Messages — refetch this job's transcript specifically, not just the job row.
        store.fetchTranscript(e.job_id).catch((err: unknown) => {
          console.error("fetchTranscript failed:", err);
        });
        store.refetchAll().catch((err: unknown) => {
          console.error("refetchAll failed:", err);
        });
        break;
      case "model_switched":
        // A ladder hop also rewinds the job's state (e.g. running -> pending), not just
        // its model — refetch so both the job row's state badge and its effective-model
        // display pick up the new row. No dedicated store field: unlike backend_switched,
        // nothing outside the job row (e.g. Header's dropdown) needs to reconcile against
        // this — see CLAUDE.md's "Model ladder" section.
        console.info(
          `[JSA] Model switched for job ${e.job_id} on ${e.backend}: ${e.from_model} → ${e.to_model}`
        );
        set({
          toasts: [
            ...store.toasts,
            {
              id: `model-${e.job_id}-${Date.now()}`,
              jobId: e.job_id,
              jobLabel: store.jobLabelFor(e.job_id),
              kind: "model" as const,
              from: e.from_model,
              to: e.to_model,
            },
          ].slice(-5),
        });
        // A model hop is also a backend_switch_reset under the hood — same Message
        // deletion, same need to refetch this job's transcript specifically.
        store.fetchTranscript(e.job_id).catch((err: unknown) => {
          console.error("fetchTranscript failed:", err);
        });
        store.refetchAll().catch((err: unknown) => {
          console.error("refetchAll failed:", err);
        });
        break;
      case "log":
        break;
      case "error":
        break;
      case "job_removed":
        store.removeJob(e.job_id);
        break;
      case "infer_progress":
        // Job-less editor event — drive the inferring checklist in the editor store.
        useEditorStore.getState().onInferProgress(e);
        break;
      case "agent_chunk":
        set((state) => {
          const existing = state.streamBuffers[e.job_id];
          const base = existing && existing.stage === e.stage ? existing : { stage: e.stage, content: "", reasoning: "", tools: [] };
          return {
            streamBuffers: {
              ...state.streamBuffers,
              [e.job_id]: {
                stage: e.stage,
                content: e.kind === "content" ? base.content + e.text : base.content,
                reasoning: e.kind === "reasoning" ? base.reasoning + e.text : base.reasoning,
                tools: base.tools,
              },
            },
          };
        });
        break;
      case "agent_tool":
        // Anchor the mark at the reasoning buffer's length AT ARRIVAL — that offset is
        // what ReasoningCard/mergeToolSteps use to interleave it against the segmented
        // reasoning text in the right position.
        set((state) => {
          const existing = state.streamBuffers[e.job_id];
          const base = existing && existing.stage === e.stage ? existing : { stage: e.stage, content: "", reasoning: "", tools: [] };
          return {
            streamBuffers: {
              ...state.streamBuffers,
              [e.job_id]: {
                ...base,
                tools: [...base.tools, { name: e.name, detail: e.summary || e.detail, ok: e.status === "ok", at: base.reasoning.length }],
              },
            },
          };
        });
        break;
      case "agent_turn_end":
        // Either the turn completed (checkpoint already landed — transcript_changed will
        // follow and render the real turn) or it was superseded (discard outright). Either
        // way the live buffer's job is done.
        set((state) => {
          const { [e.job_id]: _, ...rest } = state.streamBuffers;
          return { streamBuffers: rest };
        });
        break;
    }
  },

  async refetchAll() {
    try {
      const jobs = await api.getJobs();
      set((state) => {
        const updated = { ...state.jobs };
        for (const j of jobs) {
          updated[j.id] = j;
        }
        return { jobs: updated };
      });
    } catch (err) {
      console.error("refetchAll error:", err);
    }
  },

  async fetchTranscript(jobId: string) {
    const turns = await api.getTranscript(jobId);
    set((state) => ({ transcripts: { ...state.transcripts, [jobId]: turns } }));
  },

  async hydrateLanguage() {
    try {
      // A stalled (not just rejected) /api/config or /api/preferences request must not
      // leave configReady false forever — race it against a timeout so the catch below's
      // "fail open" actually fires instead of hanging on a blank canvas indefinitely.
      const timeout = new Promise<never>((_, reject) => {
        window.setTimeout(() => reject(new Error("config fetch timed out")), 8000);
      });
      const [prefs, config] = await Promise.race([
        Promise.all([api.getPreferences(), api.config()]),
        timeout,
      ]);
      const selectLanguageMode = Boolean(config.select_language);
      set({
        language: prefs.language,
        languages: (config.languages as [string, string, string][] | undefined) ?? [],
        selectLanguageMode,
        bootLang: prefs.language,
        // Only the --select-language boot gate starts at the picker; otherwise the
        // dashboard is shown straight away (bootStage stays at its 'app' default).
        bootStage: selectLanguageMode ? "lang" : "app",
        configReady: true,
        cvStructureExists: Boolean(config.cv_structure_exists),
      });
    } catch (err) {
      console.error("hydrateLanguage error:", err);
      // Fail open — don't leave the screen blank forever if /api/config is unreachable;
      // the dashboard renders in English rather than getting stuck pre-hydration.
      set({ configReady: true });
    }
  },

  async setLanguage(code: string) {
    const previous = get().language;
    set({ language: code });
    try {
      await api.putPreferences(code);
      return true;
    } catch (err) {
      console.error("setLanguage failed, reverting:", err);
      set({ language: previous });
      return false;
    }
  },

  setBootStage(stage) {
    set({ bootStage: stage });
  },

  setBootLang(code: string) {
    set({ bootLang: code });
  },

  async launchJob(id: string) {
    try {
      const job = await api.launch(id);
      get().upsertJob(job);
    } catch (err) {
      console.error("launchJob failed:", err);
      // Refetch so the row reverts to its real (still-queued) state rather than
      // staying stuck on a client-side animation that never completed.
      await get().refetchAll();
      throw err; // let the caller (LaunchButton) know the launch didn't happen
    }
  },

  async launchAll() {
    try {
      await api.launchAll();
      await get().refetchAll();
    } catch (err) {
      console.error("launchAll failed:", err);
    }
  },
}));
