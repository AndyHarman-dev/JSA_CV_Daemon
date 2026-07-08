import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { useStore } from "../store";
import type { JobDTO, WSEvent } from "../types";

// Mock the api module so refetchAll doesn't make real HTTP calls
vi.mock("../api", () => ({
  api: {
    getJobs: vi.fn().mockResolvedValue([]),
    getPreferences: vi.fn().mockResolvedValue({ language: "en" }),
    putPreferences: vi.fn().mockResolvedValue({ language: "en" }),
    config: vi.fn().mockResolvedValue({ languages: [] }),
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
    language: "en",
    languages: [],
    cvStructureExists: null,
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

describe("hydrateLanguage", () => {
  it("populates language and languages from the API", async () => {
    vi.mocked(api.getPreferences).mockResolvedValueOnce({ language: "es" });
    vi.mocked(api.config).mockResolvedValueOnce({
      languages: [["en", "English", "English"], ["es", "Spanish", "Español"]],
    });

    await useStore.getState().hydrateLanguage();

    expect(useStore.getState().language).toBe("es");
    expect(useStore.getState().languages).toEqual([
      ["en", "English", "English"],
      ["es", "Spanish", "Español"],
    ]);
  });

  it("leaves state unchanged when the API rejects", async () => {
    vi.mocked(api.getPreferences).mockRejectedValueOnce(new Error("network error"));

    await expect(useStore.getState().hydrateLanguage()).resolves.toBeUndefined();

    expect(useStore.getState().language).toBe("en");
  });

  it("sets cvStructureExists from /api/config's cv_structure_exists", async () => {
    vi.mocked(api.getPreferences).mockResolvedValueOnce({ language: "en" });
    vi.mocked(api.config).mockResolvedValueOnce({
      languages: [],
      cv_structure_exists: true,
    });

    await useStore.getState().hydrateLanguage();

    expect(useStore.getState().cvStructureExists).toBe(true);
  });

  it("defaults cvStructureExists to false when /api/config omits it", async () => {
    vi.mocked(api.getPreferences).mockResolvedValueOnce({ language: "en" });
    vi.mocked(api.config).mockResolvedValueOnce({ languages: [] });

    await useStore.getState().hydrateLanguage();

    expect(useStore.getState().cvStructureExists).toBe(false);
  });
});

describe("setCvStructureExists", () => {
  it("updates cvStructureExists", () => {
    useStore.getState().setCvStructureExists(true);
    expect(useStore.getState().cvStructureExists).toBe(true);
  });
});

describe("setLanguage", () => {
  it("optimistically sets the language and persists via PUT", async () => {
    vi.mocked(api.putPreferences).mockResolvedValueOnce({ language: "ja" });

    await useStore.getState().setLanguage("ja");

    expect(useStore.getState().language).toBe("ja");
    expect(api.putPreferences).toHaveBeenCalledWith("ja");
  });

  it("reverts to the previous language when the PUT fails", async () => {
    useStore.setState({ language: "en" });
    vi.mocked(api.putPreferences).mockRejectedValueOnce(new Error("422"));

    await useStore.getState().setLanguage("xx");

    expect(useStore.getState().language).toBe("en");
  });
});
