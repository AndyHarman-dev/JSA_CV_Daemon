import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useStore } from "../store";
import type { JobDTO, LogEntry, WSEvent } from "../types";

// Mock the api module so refetchAll doesn't make real HTTP calls
vi.mock("../api", () => ({
  api: {
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

import { api } from "../api";

// Snapshot the initial state to reset between tests
const initialState = {
  jobs: {},
  selectedId: undefined as string | undefined,
  wsStatus: "connecting" as const,
  logs: [] as LogEntry[],
};

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "abc123",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    state: "pending",
    current_stage: null,
    error: null,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  // Reset only the data fields; preserve action functions
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
    logs: [],
  });
  vi.clearAllMocks();
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("upsertJob", () => {
  it("adds a new job to the map", () => {
    const job = makeJob({ id: "job1" });
    useStore.getState().upsertJob(job);

    const jobs = useStore.getState().jobs;
    expect(jobs["job1"]).toEqual(job);
  });

  it("updates an existing job in the map", () => {
    const job = makeJob({ id: "job1", state: "pending" });
    useStore.getState().upsertJob(job);

    const updatedJob = makeJob({ id: "job1", state: "running" });
    useStore.getState().upsertJob(updatedJob);

    const jobs = useStore.getState().jobs;
    expect(jobs["job1"].state).toBe("running");
  });

  it("does not remove other jobs when upserting", () => {
    const job1 = makeJob({ id: "job1" });
    const job2 = makeJob({ id: "job2", company: "Beta" });
    useStore.getState().upsertJob(job1);
    useStore.getState().upsertJob(job2);

    const updatedJob1 = makeJob({ id: "job1", state: "cv_done" });
    useStore.getState().upsertJob(updatedJob1);

    const jobs = useStore.getState().jobs;
    expect(jobs["job1"].state).toBe("cv_done");
    expect(jobs["job2"]).toEqual(job2);
  });
});

describe("selectJob", () => {
  it("sets selectedId to the given id", () => {
    useStore.getState().selectJob("job-xyz");
    expect(useStore.getState().selectedId).toBe("job-xyz");
  });

  it("overwrites a previously selected id", () => {
    useStore.getState().selectJob("first");
    useStore.getState().selectJob("second");
    expect(useStore.getState().selectedId).toBe("second");
  });
});

describe("setWsStatus", () => {
  it("updates wsStatus to open", () => {
    useStore.getState().setWsStatus("open");
    expect(useStore.getState().wsStatus).toBe("open");
  });

  it("updates wsStatus to closed", () => {
    useStore.getState().setWsStatus("closed");
    expect(useStore.getState().wsStatus).toBe("closed");
  });

  it("updates wsStatus to connecting", () => {
    useStore.getState().setWsStatus("open");
    useStore.getState().setWsStatus("connecting");
    expect(useStore.getState().wsStatus).toBe("connecting");
  });
});

describe("appendLog", () => {
  it("adds a log entry to the logs array", () => {
    const entry: LogEntry = { job_id: "job1", level: "info", text: "hello", ts: 1000 };
    useStore.getState().appendLog(entry);

    const logs = useStore.getState().logs;
    expect(logs).toHaveLength(1);
    expect(logs[0]).toEqual(entry);
  });

  it("caps logs at 200 when more than 200 entries are appended", () => {
    // Append 201 entries
    for (let i = 0; i < 201; i++) {
      useStore.getState().appendLog({
        job_id: "job1",
        level: "info",
        text: `log line ${i}`,
        ts: i,
      });
    }

    const logs = useStore.getState().logs;
    expect(logs).toHaveLength(200);
    // Should have kept the most recent 200 (indices 1..200, i.e. "log line 1" through "log line 200")
    expect(logs[0].text).toBe("log line 1");
    expect(logs[199].text).toBe("log line 200");
  });

  it("retains all entries when exactly 200 are appended", () => {
    for (let i = 0; i < 200; i++) {
      useStore.getState().appendLog({
        job_id: "job1",
        level: "info",
        text: `line ${i}`,
        ts: i,
      });
    }
    expect(useStore.getState().logs).toHaveLength(200);
  });
});

describe("applyEvent - log event", () => {
  it("appends a LogEntry with level info and correct text for a log event", () => {
    const event: WSEvent = {
      type: "log",
      job_id: "job1",
      payload: { level: "info", text: "build started" },
    };
    useStore.getState().applyEvent(event);

    const logs = useStore.getState().logs;
    expect(logs).toHaveLength(1);
    expect(logs[0].job_id).toBe("job1");
    expect(logs[0].level).toBe("info");
    expect(logs[0].text).toBe("build started");
  });

  it("appends a LogEntry with level warn for a warn log event", () => {
    const event: WSEvent = {
      type: "log",
      job_id: "job2",
      payload: { level: "warn", text: "quota near limit" },
    };
    useStore.getState().applyEvent(event);

    const logs = useStore.getState().logs;
    expect(logs[0].level).toBe("warn");
    expect(logs[0].text).toBe("quota near limit");
  });

  it("defaults to info level for unknown log levels", () => {
    const event: WSEvent = {
      type: "log",
      job_id: "job1",
      payload: { level: "debug", text: "verbose message" },
    };
    useStore.getState().applyEvent(event);

    const logs = useStore.getState().logs;
    expect(logs[0].level).toBe("info");
  });
});

describe("applyEvent - error event", () => {
  it("appends an error-level LogEntry for an error event", () => {
    const event: WSEvent = {
      type: "error",
      job_id: "job1",
      payload: { message: "agent timed out" },
    };
    useStore.getState().applyEvent(event);

    const logs = useStore.getState().logs;
    expect(logs).toHaveLength(1);
    expect(logs[0].level).toBe("error");
    expect(logs[0].text).toBe("agent timed out");
    expect(logs[0].job_id).toBe("job1");
  });

  it("converts non-string message to string for error events", () => {
    const event: WSEvent = {
      type: "error",
      job_id: "job1",
      payload: { message: 42 as unknown as string },
    };
    useStore.getState().applyEvent(event);

    const logs = useStore.getState().logs;
    expect(logs[0].text).toBe("42");
  });
});

describe("applyEvent - status_changed event", () => {
  it("calls refetchAll (api.getJobs) when a status_changed event is received", async () => {
    const event: WSEvent = {
      type: "status_changed",
      job_id: "job1",
      payload: { from: "pending", to: "running" },
    };
    useStore.getState().applyEvent(event);

    // refetchAll is fire-and-forgot; flush microtask queue
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getJobs).toHaveBeenCalledTimes(1);
  });

  it("does not append a log entry for status_changed event", async () => {
    const event: WSEvent = {
      type: "status_changed",
      job_id: "job1",
      payload: { from: "pending", to: "running" },
    };
    useStore.getState().applyEvent(event);
    await Promise.resolve();

    expect(useStore.getState().logs).toHaveLength(0);
  });
});

describe("applyEvent - stage_complete event", () => {
  it("calls refetchAll (api.getJobs) when a stage_complete event is received", async () => {
    const event: WSEvent = {
      type: "stage_complete",
      job_id: "job1",
      payload: { stage: "cv_adjust" },
    };
    useStore.getState().applyEvent(event);

    await Promise.resolve();
    await Promise.resolve();

    expect(api.getJobs).toHaveBeenCalledTimes(1);
  });
});

describe("refetchAll", () => {
  it("updates the jobs map with returned jobs", async () => {
    const mockJob = makeJob({ id: "fetched1", state: "running" });
    vi.mocked(api.getJobs).mockResolvedValueOnce([mockJob]);

    await useStore.getState().refetchAll();

    expect(useStore.getState().jobs["fetched1"]).toEqual(mockJob);
  });

  it("merges new jobs with existing ones", async () => {
    const existingJob = makeJob({ id: "existing", state: "pending" });
    useStore.setState({ jobs: { existing: existingJob } });

    const newJob = makeJob({ id: "fresh", state: "running" });
    vi.mocked(api.getJobs).mockResolvedValueOnce([newJob]);

    await useStore.getState().refetchAll();

    const jobs = useStore.getState().jobs;
    expect(jobs["existing"]).toEqual(existingJob);
    expect(jobs["fresh"]).toEqual(newJob);
  });

  it("leaves state unchanged when api.getJobs rejects", async () => {
    const existingJob = makeJob({ id: "keep", state: "pending" });
    useStore.setState({ jobs: { keep: existingJob } });

    vi.mocked(api.getJobs).mockRejectedValueOnce(new Error("network error"));

    // Should not throw
    await expect(useStore.getState().refetchAll()).resolves.toBeUndefined();

    // State is unchanged
    expect(useStore.getState().jobs["keep"]).toEqual(existingJob);
  });
});
