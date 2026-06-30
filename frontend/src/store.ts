import { create } from "zustand";
import type { JobDTO, WSEvent } from "./types";
import { api } from "./api";
import { useEditorStore } from "./editorStore";

interface Store {
  jobs: Record<string, JobDTO>;
  selectedId: string | undefined;
  wsStatus: "connecting" | "open" | "closed";
  editorOpen: boolean;
  upsertJob(j: JobDTO): void;
  selectJob(id: string | undefined): void;
  setWsStatus(s: Store["wsStatus"]): void;
  setEditorOpen(open: boolean): void;
  applyEvent(e: WSEvent): void;
  refetchAll(): Promise<void>;
  removeJob(id: string): void;
}

export const useStore = create<Store>((set, get) => ({
  jobs: {},
  selectedId: undefined,
  wsStatus: "connecting",
  editorOpen: false,

  upsertJob(j: JobDTO) {
    set((state) => ({
      jobs: { ...state.jobs, [j.id]: j },
    }));
  },

  selectJob(id: string | undefined) {
    set({ selectedId: id });
  },

  setWsStatus(s: Store["wsStatus"]) {
    set({ wsStatus: s });
  },

  setEditorOpen(open: boolean) {
    set({ editorOpen: open });
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
}));
