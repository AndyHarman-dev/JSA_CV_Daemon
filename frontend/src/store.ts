import { create } from "zustand";
import type { JobDTO, WSEvent } from "./types";
import { api } from "./api";
import { useEditorStore } from "./editorStore";

interface BackendSwitchEvent {
  job_id: string;
  from_backend: string;
  to_backend: string;
}

interface Store {
  jobs: Record<string, JobDTO>;
  selectedId: string | undefined;
  wsStatus: "connecting" | "open" | "closed";
  editorOpen: boolean;
  // Most recent `backend_switched` WS event — additive signal so surfaces (e.g. Header's
  // backend cluster) can react without changing applyEvent's per-type behavior for others.
  lastBackendSwitch: BackendSwitchEvent | null;
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
  upsertJob(j: JobDTO): void;
  selectJob(id: string | undefined): void;
  setViewedStage(stage: Store["viewedStage"]): void;
  setWsStatus(s: Store["wsStatus"]): void;
  setEditorOpen(open: boolean): void;
  setCvStructureExists(exists: boolean): void;
  applyEvent(e: WSEvent): void;
  refetchAll(): Promise<void>;
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
  language: "en",
  languages: [],
  selectLanguageMode: false,
  bootStage: "app",
  bootLang: "en",
  configReady: false,
  cvStructureExists: null,
  viewedStage: null,

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
      case "stage_complete":
      case "approved":
      case "follow_up_needed":
        store.refetchAll().catch((err: unknown) => {
          console.error("refetchAll failed:", err);
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
