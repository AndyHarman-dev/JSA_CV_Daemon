import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useStore } from "../store";
import type { JobDTO, WSEvent } from "../types";

// Mock the api module so refetchAll doesn't make real HTTP calls
vi.mock("../api", () => ({
  api: {
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

import { api } from "../api";

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

describe("applyEvent - status_changed event", () => {
  it("calls refetchAll (api.getJobs) when a status_changed event is received", async () => {
    const event: WSEvent = {
      type: "status_changed",
      job_id: "job1",
      from_state: "pending",
      to_state: "running",
    };
    useStore.getState().applyEvent(event);

    // refetchAll is fire-and-forgot; flush microtask queue
    await Promise.resolve();
    await Promise.resolve();

    expect(api.getJobs).toHaveBeenCalledTimes(1);
  });

});

describe("applyEvent - stage_complete event", () => {
  it("calls refetchAll (api.getJobs) when a stage_complete event is received", async () => {
    const event: WSEvent = {
      type: "stage_complete",
      job_id: "job1",
      stage: "cv_adjust",
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
