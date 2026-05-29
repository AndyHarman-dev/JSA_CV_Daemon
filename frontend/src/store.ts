import { create } from "zustand";
import type { JobDTO, WSEvent, LogEntry } from "./types";
import { api } from "./api";

const MAX_LOGS = 200;

interface Store {
  jobs: Record<string, JobDTO>;
  selectedId: string | undefined;
  wsStatus: "connecting" | "open" | "closed";
  logs: LogEntry[];
  upsertJob(j: JobDTO): void;
  selectJob(id: string): void;
  setWsStatus(s: Store["wsStatus"]): void;
  applyEvent(e: WSEvent): void;
  refetchAll(): Promise<void>;
  appendLog(entry: LogEntry): void;
  removeJob(id: string): void;
}

export const useStore = create<Store>((set, get) => ({
  jobs: {},
  selectedId: undefined,
  wsStatus: "connecting",
  logs: [],

  upsertJob(j: JobDTO) {
    set((state) => ({
      jobs: { ...state.jobs, [j.id]: j },
    }));
  },

  selectJob(id: string) {
    set({ selectedId: id });
  },

  setWsStatus(s: Store["wsStatus"]) {
    set({ wsStatus: s });
  },

  appendLog(entry: LogEntry) {
    set((state) => {
      const logs = [...state.logs, entry];
      return { logs: logs.length > MAX_LOGS ? logs.slice(logs.length - MAX_LOGS) : logs };
    });
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
      case "log": {
        const level: LogEntry["level"] =
          e.level === "warn" || e.level === "error" ? e.level : "info";
        store.appendLog({ job_id: e.job_id, level, text: e.text, ts: Date.now() });
        break;
      }
      case "error": {
        store.appendLog({ job_id: e.job_id, level: "error", text: e.message, ts: Date.now() });
        break;
      }
      case "job_removed":
        store.removeJob(e.job_id);
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
