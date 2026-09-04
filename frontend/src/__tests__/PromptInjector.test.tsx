import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { useStore, injectorPanelHeight } from "../store";
import { JobList } from "../components/JobList";
import { PromptInjector } from "../components/PromptInjector";
import { api } from "../api";
import type { InjectionPresetDTO, JobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    putJobInjection: vi.fn(),
    getInjectionPresets: vi.fn(),
    putInjectionPresets: vi.fn(),
  },
}));

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "A great job",
    state: "queued",
    current_stage: null,
    backend_name: null,
    model_name: null,
    effective_model: null,
    language: null,
    fit_reason: null,
    injection: null,
    error: null,
    retry_count: 0,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makePreset(overrides: Partial<InjectionPresetDTO> = {}): InjectionPresetDTO {
  return {
    id: "p1",
    name: "Confident tone",
    prefix: "P",
    postfix: "S",
    first_msg: "F",
    saved_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/** Mount both surfaces the feature spans — the trigger lives in the job row, the panel is
 *  an App-level sibling (see PromptInjector.tsx's header comment). */
function renderBoth() {
  return render(
    <>
      <JobList />
      <PromptInjector />
    </>
  );
}

function seedJobs(...jobs: JobDTO[]) {
  useStore.setState({ jobs: Object.fromEntries(jobs.map((j) => [j.id, j])) });
}

function trigger(): HTMLElement {
  return screen.getByTitle("Inject a prompt for this job (pre-launch only)");
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    injectorJobId: null,
    injectorPos: { x: 0, y: 0 },
    injectionPresets: [],
  });
});

describe("InjectTrigger", () => {
  it("renders on a queued row and not on any other state", () => {
    seedJobs(makeJob({ id: "q", state: "queued" }), makeJob({ id: "r", state: "running" }));
    renderBoth();
    expect(screen.getAllByTitle("Inject a prompt for this job (pre-launch only)")).toHaveLength(1);
  });

  it("is absent entirely once the job has launched", () => {
    seedJobs(makeJob({ state: "running" }));
    renderBoth();
    expect(screen.queryByTitle("Inject a prompt for this job (pre-launch only)")).toBeNull();
  });

  it("switches its tooltip once the job carries an injection", () => {
    seedJobs(makeJob({ injection: { prefix: "hi", postfix: "", first_msg: "" } }));
    renderBoth();
    expect(screen.getByTitle("Edit prompt injection")).toBeInTheDocument();
  });

  it("opens the panel without selecting the row underneath", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 100, clientY: 100 });
    expect(useStore.getState().injectorJobId).toBe("job1");
    expect(useStore.getState().selectedId).toBeUndefined();
  });
});

describe("store.openInjector clamp", () => {
  it("keeps the panel's full height on-screen for a click near the bottom edge", () => {
    // jsdom's viewport is 1024x768. A naive fixed bottom margin would leave the footer
    // buttons off-screen here — the clamp has to reserve the panel's actual height.
    useStore.getState().openInjector("job1", 900, 760);
    const { x, y } = useStore.getState().injectorPos;
    const panelH = injectorPanelHeight(window.innerHeight);
    expect(y + panelH).toBeLessThanOrEqual(window.innerHeight - 12);
    expect(y).toBeGreaterThanOrEqual(12);
    expect(x).toBeLessThanOrEqual(window.innerWidth - 400 - 12);
  });

  it("floors a click at the very top-left at the 12px margin", () => {
    useStore.getState().openInjector("job1", 0, 0);
    expect(useStore.getState().injectorPos).toEqual({ x: 12, y: 12 });
  });
});

describe("PromptInjector panel", () => {
  it("renders nothing while closed", () => {
    seedJobs(makeJob());
    render(<PromptInjector />);
    expect(screen.queryByText("PROMPT_INJECTOR")).toBeNull();
  });

  it("loads the job's saved injection into the draft on open", () => {
    seedJobs(makeJob({ injection: { prefix: "PRE", postfix: "POST", first_msg: "MSG" } }));
    renderBoth();
    fireEvent.click(screen.getByTitle("Edit prompt injection"), { clientX: 10, clientY: 10 });
    expect(screen.getByLabelText("PRE-FIX PROMPT")).toHaveValue("PRE");
    expect(screen.getByLabelText("POST-FIX PROMPT")).toHaveValue("POST");
    expect(screen.getByLabelText("FIRST USER MESSAGE")).toHaveValue("MSG");
  });

  it("edit + SAVE INJECTION PUTs the three snake_case fields and lights the trigger", async () => {
    const saved = makeJob({ injection: { prefix: "lead with payments", postfix: "", first_msg: "" } });
    vi.mocked(api.putJobInjection).mockResolvedValue(saved as never);
    seedJobs(makeJob());
    renderBoth();

    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.change(screen.getByLabelText("PRE-FIX PROMPT"), {
      target: { value: "lead with payments" },
    });
    fireEvent.click(screen.getByText("SAVE INJECTION"));

    await waitFor(() => expect(api.putJobInjection).toHaveBeenCalledTimes(1));
    expect(api.putJobInjection).toHaveBeenCalledWith("job1", {
      prefix: "lead with payments",
      postfix: "",
      first_msg: "",
    });
    await waitFor(() => expect(screen.getByTitle("Edit prompt injection")).toBeInTheDocument());
    expect(useStore.getState().injectorJobId).toBeNull();
  });

  it("sends the all-blank clearing shape when every field is empty", async () => {
    vi.mocked(api.putJobInjection).mockResolvedValue(makeJob() as never);
    seedJobs(makeJob({ injection: { prefix: "old", postfix: "", first_msg: "" } }));
    renderBoth();

    fireEvent.click(screen.getByTitle("Edit prompt injection"), { clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByText("CLEAR"));
    fireEvent.click(screen.getByText("SAVE INJECTION"));

    await waitFor(() =>
      expect(api.putJobInjection).toHaveBeenCalledWith("job1", {
        prefix: "",
        postfix: "",
        first_msg: "",
      })
    );
  });

  it("CLEAR empties the draft without closing or touching what's persisted", () => {
    seedJobs(makeJob({ injection: { prefix: "old", postfix: "keep", first_msg: "" } }));
    renderBoth();
    fireEvent.click(screen.getByTitle("Edit prompt injection"), { clientX: 10, clientY: 10 });
    fireEvent.click(screen.getByText("CLEAR"));

    expect(screen.getByLabelText("PRE-FIX PROMPT")).toHaveValue("");
    expect(screen.getByLabelText("POST-FIX PROMPT")).toHaveValue("");
    expect(useStore.getState().injectorJobId).toBe("job1");
    expect(api.putJobInjection).not.toHaveBeenCalled();
    expect(useStore.getState().jobs.job1.injection).toEqual({
      prefix: "old",
      postfix: "keep",
      first_msg: "",
    });
  });

  it("backdrop click closes and discards the draft", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.change(screen.getByLabelText("PRE-FIX PROMPT"), { target: { value: "typed" } });
    fireEvent.click(screen.getByTestId("injector-backdrop"));

    expect(useStore.getState().injectorJobId).toBeNull();
    expect(api.putJobInjection).not.toHaveBeenCalled();

    // Reopening starts from the persisted (still empty) injection, not the discarded draft.
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    expect(screen.getByLabelText("PRE-FIX PROMPT")).toHaveValue("");
  });

  it("Escape closes the panel", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(useStore.getState().injectorJobId).toBeNull();
  });

  it("closes itself if the job leaves `queued` while open", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    expect(screen.getByText("PROMPT_INJECTOR")).toBeInTheDocument();

    act(() => {
      useStore.getState().upsertJob(makeJob({ state: "running" }));
    });
    expect(screen.queryByText("PROMPT_INJECTOR")).toBeNull();
  });
});

describe("PromptInjector presets", () => {
  it("shows the empty state with no saved doses", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    expect(screen.getByText("No saved doses yet.")).toBeInTheDocument();
  });

  it("pasting a preset overwrites all three draft fields and leaves the panel open", () => {
    useStore.setState({
      injectionPresets: [makePreset({ prefix: "A", postfix: "B", first_msg: "C" })],
    });
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.change(screen.getByLabelText("PRE-FIX PROMPT"), { target: { value: "stale" } });
    fireEvent.click(screen.getByText("Confident tone"));

    expect(screen.getByLabelText("PRE-FIX PROMPT")).toHaveValue("A");
    expect(screen.getByLabelText("POST-FIX PROMPT")).toHaveValue("B");
    expect(screen.getByLabelText("FIRST USER MESSAGE")).toHaveValue("C");
    expect(useStore.getState().injectorJobId).toBe("job1");
  });

  it("saving a preset prepends it and PUTs the whole list", async () => {
    vi.mocked(api.putInjectionPresets).mockResolvedValue({ presets: [] } as never);
    useStore.setState({ injectionPresets: [makePreset({ id: "old", name: "Older" })] });
    seedJobs(makeJob());
    renderBoth();

    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.change(screen.getByLabelText("PRE-FIX PROMPT"), { target: { value: "text" } });
    fireEvent.change(screen.getByPlaceholderText("Name this dose to save…"), {
      target: { value: "New dose" },
    });
    fireEvent.click(screen.getByText("SAVE"));

    await waitFor(() => expect(api.putInjectionPresets).toHaveBeenCalledTimes(1));
    const sent = vi.mocked(api.putInjectionPresets).mock.calls[0][0];
    expect(sent.map((p) => p.name)).toEqual(["New dose", "Older"]);
    expect(sent[0]).toMatchObject({ prefix: "text", postfix: "", first_msg: "" });
    expect(sent[0].saved_at).not.toBe("");
  });

  it("ignores a preset save with a blank name or with all fields empty", () => {
    seedJobs(makeJob());
    renderBoth();
    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });

    // Blank name.
    fireEvent.click(screen.getByText("SAVE"));
    // Named, but nothing to save.
    fireEvent.change(screen.getByPlaceholderText("Name this dose to save…"), {
      target: { value: "Empty" },
    });
    fireEvent.click(screen.getByText("SAVE"));

    expect(api.putInjectionPresets).not.toHaveBeenCalled();
  });

  it("deletes the chip that was clicked even when two presets share an id", async () => {
    // `InjectionPresetDTO.id` is NOT unique server-side, so deleting by id would remove
    // the wrong (first-matching) chip. The handle is the array index.
    vi.mocked(api.putInjectionPresets).mockResolvedValue({ presets: [] } as never);
    useStore.setState({
      injectionPresets: [
        makePreset({ id: "dup", name: "First" }),
        makePreset({ id: "dup", name: "Second" }),
      ],
    });
    seedJobs(makeJob());
    renderBoth();

    fireEvent.click(trigger(), { clientX: 10, clientY: 10 });
    fireEvent.click(screen.getAllByTitle("Delete")[1]); // the SECOND of the two "dup" chips

    await waitFor(() => expect(api.putInjectionPresets).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.putInjectionPresets).mock.calls[0][0].map((p) => p.name)).toEqual([
      "First",
    ]);
    expect(useStore.getState().injectionPresets.map((p) => p.name)).toEqual(["First"]);
  });
});
