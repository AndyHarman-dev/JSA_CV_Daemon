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
    listCvDecks: vi.fn().mockResolvedValue({ decks: [], default_id: null }),
    putJobBaseCv: vi.fn(),
    putJobInjection: vi.fn(),
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
    backend_name: null,
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
    toasts: [],
    editorOpen: false,
    cvDecks: [],
    cvDecksDefaultId: null,
    cvPickerJobId: null,
    cvPickerPos: { x: 0, y: 0 },
    injectorJobId: null,
    injectorPos: { x: 0, y: 0 },
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

describe("applyEvent - model_switched event", () => {
  it("calls refetchAll (api.getJobs) when a model_switched event is received", async () => {
    const event: WSEvent = {
      type: "model_switched",
      job_id: "job1",
      backend: "opencode-go",
      from_model: "glm-5.3",
      to_model: "kimi-k2.6",
    };
    useStore.getState().applyEvent(event);

    await Promise.resolve();
    await Promise.resolve();

    expect(api.getJobs).toHaveBeenCalledTimes(1);
  });

  it("pushes a toast with the job's company/role label", async () => {
    useStore.getState().upsertJob(makeJob({ id: "job1", company: "Acme", role: "Engineer" }));

    useStore.getState().applyEvent({
      type: "model_switched",
      job_id: "job1",
      backend: "opencode-go",
      from_model: "glm-5.3",
      to_model: "kimi-k2.6",
    });

    const toasts = useStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({
      jobId: "job1",
      jobLabel: "Acme — Engineer",
      kind: "model",
      from: "glm-5.3",
      to: "kimi-k2.6",
    });
  });
});

describe("applyEvent - backend_switched event", () => {
  it("pushes a toast falling back to the raw job id when the job isn't known locally", async () => {
    useStore.getState().applyEvent({
      type: "backend_switched",
      job_id: "unknown-job",
      from_backend: "opencode-zen",
      to_backend: "opencode-go",
    });

    const toasts = useStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({
      jobId: "unknown-job",
      jobLabel: "unknown-job",
      kind: "backend",
      from: "opencode-zen",
      to: "opencode-go",
    });
  });

  it("caps the toast queue at 5 entries", () => {
    for (let i = 0; i < 8; i++) {
      useStore.getState().applyEvent({
        type: "backend_switched",
        job_id: `job${i}`,
        from_backend: "opencode-zen",
        to_backend: "opencode-go",
      });
    }
    expect(useStore.getState().toasts).toHaveLength(5);
    // Keeps the most recent, drops the oldest.
    expect(useStore.getState().toasts[4].jobId).toBe("job7");
  });
});

describe("dismissToast", () => {
  it("removes the toast with the given id", () => {
    useStore.getState().applyEvent({
      type: "backend_switched",
      job_id: "job1",
      from_backend: "opencode-zen",
      to_backend: "opencode-go",
    });
    const id = useStore.getState().toasts[0].id;

    useStore.getState().dismissToast(id);

    expect(useStore.getState().toasts).toHaveLength(0);
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

describe("applyEvent - agent_tool event", () => {
  beforeEach(() => {
    useStore.setState({ streamBuffers: {} });
  });

  it("appends a tool mark anchored at the reasoning buffer's current length", () => {
    useStore.setState({
      streamBuffers: {
        job1: { stage: "cv_adjust", content: "", reasoning: "Reading the JD.", tools: [] },
      },
    });

    useStore.getState().applyEvent({
      type: "agent_tool",
      job_id: "job1",
      stage: "cv_adjust",
      seq: 1,
      call_id: "call_1",
      name: "read_file",
      summary: "~/.jsa/cv_structure.json",
      status: "ok",
      detail: "",
    });

    expect(useStore.getState().streamBuffers.job1.tools).toEqual([
      { name: "read_file", detail: "~/.jsa/cv_structure.json", ok: true, at: "Reading the JD.".length },
    ]);
    // content/reasoning are untouched by an agent_tool event.
    expect(useStore.getState().streamBuffers.job1.reasoning).toBe("Reading the JD.");
  });

  it("falls back to `detail` when `summary` is empty, and records ok:false on a failed call", () => {
    useStore.getState().applyEvent({
      type: "agent_tool",
      job_id: "job1",
      stage: "cv_adjust",
      seq: 1,
      call_id: "call_1",
      name: "run_patch",
      summary: "",
      status: "error",
      detail: "patch rejected: hunk mismatch",
    });

    expect(useStore.getState().streamBuffers.job1.tools).toEqual([
      { name: "run_patch", detail: "patch rejected: hunk mismatch", ok: false, at: 0 },
    ]);
  });

  it("resets the buffer (and its tools) when the event's stage differs from the existing one", () => {
    useStore.setState({
      streamBuffers: {
        job1: { stage: "cv_adjust", content: "", reasoning: "stale", tools: [{ name: "old", detail: "", ok: true, at: 0 }] },
      },
    });

    useStore.getState().applyEvent({
      type: "agent_tool",
      job_id: "job1",
      stage: "cover_letter",
      seq: 1,
      call_id: "call_2",
      name: "web_search",
      summary: "query",
      status: "ok",
      detail: "",
    });

    const buf = useStore.getState().streamBuffers.job1;
    expect(buf.stage).toBe("cover_letter");
    expect(buf.reasoning).toBe("");
    expect(buf.tools).toEqual([{ name: "web_search", detail: "query", ok: true, at: 0 }]);
  });

  it("preserves accumulated tools across a subsequent agent_chunk event", () => {
    useStore.getState().applyEvent({
      type: "agent_tool",
      job_id: "job1",
      stage: "cv_adjust",
      seq: 1,
      call_id: "call_1",
      name: "read_file",
      summary: "cv_structure.json",
      status: "ok",
      detail: "",
    });
    useStore.getState().applyEvent({
      type: "agent_chunk",
      job_id: "job1",
      stage: "cv_adjust",
      kind: "reasoning",
      text: "more thinking",
    });

    const buf = useStore.getState().streamBuffers.job1;
    expect(buf.reasoning).toBe("more thinking");
    expect(buf.tools).toEqual([{ name: "read_file", detail: "cv_structure.json", ok: true, at: 0 }]);
  });
});

describe("hydrateCvDecks", () => {
  it("populates cvDecks and cvDecksDefaultId from GET /api/cv-decks", async () => {
    vi.mocked(api.listCvDecks).mockResolvedValueOnce({
      decks: [{ id: "d1", name: null, auto_title: "Jane Doe", has_cv: true, is_default: true }],
      default_id: "d1",
    });

    await useStore.getState().hydrateCvDecks();

    expect(useStore.getState().cvDecks).toHaveLength(1);
    expect(useStore.getState().cvDecksDefaultId).toBe("d1");
  });

  it("leaves the existing deck list untouched on failure", async () => {
    useStore.setState({ cvDecks: [{ id: "d1", name: null, auto_title: null, has_cv: true, is_default: true }] });
    vi.mocked(api.listCvDecks).mockRejectedValueOnce(new Error("network error"));

    await useStore.getState().hydrateCvDecks();

    expect(useStore.getState().cvDecks).toHaveLength(1);
  });
});

describe("setEditorOpen", () => {
  it("re-hydrates the deck list when the editor closes", async () => {
    vi.mocked(api.listCvDecks).mockResolvedValueOnce({
      decks: [{ id: "d1", name: null, auto_title: "Jane Doe", has_cv: true, is_default: true }],
      default_id: "d1",
    });

    useStore.getState().setEditorOpen(false);
    // Flush the fire-and-forget hydrateCvDecks() call (setEditorOpen doesn't await it) —
    // a macrotask tick, not just one microtask, since the mocked promise's own resolution
    // is itself a queued microtask ahead of hydrateCvDecks's `await` continuation.
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(api.listCvDecks).toHaveBeenCalled();
    expect(useStore.getState().cvDecks).toHaveLength(1);
  });

  it("does not re-hydrate when the editor opens", () => {
    useStore.getState().setEditorOpen(true);
    expect(api.listCvDecks).not.toHaveBeenCalled();
  });
});

describe("openCvPicker / closeCvPicker", () => {
  const job = makeJob({ id: "job1", state: "queued" });

  it("opens the picker for a queued job, clamped to the viewport", () => {
    useStore.getState().openCvPicker(job, { clientX: 100, clientY: 200 });
    const state = useStore.getState();
    expect(state.cvPickerJobId).toBe("job1");
    expect(state.cvPickerPos).toEqual({ x: 100, y: 214 });
  });

  it("is a no-op for a non-queued job", () => {
    useStore.getState().openCvPicker(makeJob({ id: "job2", state: "pending" }), {
      clientX: 0,
      clientY: 0,
    });
    expect(useStore.getState().cvPickerJobId).toBeNull();
  });

  it("closes the picker", () => {
    useStore.getState().openCvPicker(job, { clientX: 0, clientY: 0 });
    useStore.getState().closeCvPicker();
    expect(useStore.getState().cvPickerJobId).toBeNull();
  });
});

describe("assignBaseCv", () => {
  it("patches the job in the store and closes the picker on success", async () => {
    const updated = makeJob({ id: "job1", base_cv_id: "d2" });
    vi.mocked(api.putJobBaseCv).mockResolvedValueOnce(updated);
    useStore.setState({ cvPickerJobId: "job1" });

    await useStore.getState().assignBaseCv("job1", "d2");

    expect(api.putJobBaseCv).toHaveBeenCalledWith("job1", "d2");
    expect(useStore.getState().jobs["job1"]).toEqual(updated);
    expect(useStore.getState().cvPickerJobId).toBeNull();
  });

  it("closes the picker and toasts the server's detail on failure", async () => {
    vi.mocked(api.putJobBaseCv).mockRejectedValueOnce(
      new Error('HTTP 422: {"detail":"deck is not assignable"}')
    );
    useStore.setState({ cvPickerJobId: "job1", jobs: { job1: makeJob({ id: "job1" }) } });

    await useStore.getState().assignBaseCv("job1", "bad-deck");

    expect(useStore.getState().cvPickerJobId).toBeNull();
    const toasts = useStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].kind).toBe("error");
    expect(toasts[0].message).toBe("deck is not assignable");
  });
});

describe("saveInjection", () => {
  it("patches the job in the store and closes the panel on success", async () => {
    const updated = makeJob({ id: "job1", injection: { prefix: "p", postfix: "", first_msg: "" } });
    vi.mocked(api.putJobInjection).mockResolvedValueOnce(updated as never);
    useStore.setState({ injectorJobId: "job1" });

    await useStore.getState().saveInjection("job1", { prefix: "p", postfix: "", first_msg: "" });

    expect(api.putJobInjection).toHaveBeenCalledWith("job1", {
      prefix: "p",
      postfix: "",
      first_msg: "",
    });
    expect(useStore.getState().jobs["job1"]).toEqual(updated);
    expect(useStore.getState().injectorJobId).toBeNull();
  });

  it("toasts the server's detail on failure instead of failing silently", async () => {
    // The real 400: LAUNCH ALL moved the job off `queued` between opening the vial and
    // pressing SAVE. Leaving the panel open is not enough — the WS state change unmounts
    // it (PromptInjector returns null off `queued`), so without this toast the rejected
    // write is invisible. Mirrors assignBaseCv's gate-rejection toast above.
    vi.mocked(api.putJobInjection).mockRejectedValueOnce(
      new Error('HTTP 400: {"detail":"Job \'job1\' is in state \'pending\', expected \'queued\'"}')
    );
    useStore.setState({ injectorJobId: "job1", jobs: { job1: makeJob({ id: "job1" }) } });

    await useStore.getState().saveInjection("job1", { prefix: "p", postfix: "", first_msg: "" });

    const toasts = useStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].kind).toBe("error");
    expect(toasts[0].jobId).toBe("job1");
    expect(toasts[0].message).toBe("Job 'job1' is in state 'pending', expected 'queued'");
    // The panel deliberately stays open so the draft survives when it is still mountable.
    expect(useStore.getState().injectorJobId).toBe("job1");
  });
});
