import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { AgentThread } from "../components/AgentThread";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO, TranscriptTurn } from "../types";

vi.mock("../api", () => ({
  api: {
    getJobs: vi.fn().mockResolvedValue([]),
    getTranscript: vi.fn(),
    answerFollowUp: vi.fn().mockResolvedValue({}),
    revise: vi.fn().mockResolvedValue({}),
  },
}));

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

function turn(overrides: Partial<TranscriptTurn> = {}): TranscriptTurn {
  return {
    seq: 0,
    kind: "question",
    role: "assistant",
    stage: "cv_adjust",
    text: "A question",
    created_at: "2026-01-01T00:00:00Z",
    follow_up_id: null,
    suggested_replies: null,
    reasoning: null,
    ...overrides,
  };
}

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "",
    state: "running",
    current_stage: "cv_adjust",
    backend_name: "opencode-zen",
    model_name: null,
    effective_model: null,
    language: null,
    fit_reason: null,
    error: null,
    retry_count: 0,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting", transcripts: {}, streamBuffers: {} });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
});

describe("AgentThread", () => {
  it("renders both an answered question and an open question", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({ seq: 0, kind: "question", follow_up_id: 1, text: "Answered question" }),
      turn({ seq: 1, kind: "answer", role: "user", follow_up_id: 1, text: "The answer" }),
      turn({ seq: 2, kind: "question", follow_up_id: 2, text: "Open question" }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText("Answered question")).toBeInTheDocument();
    });
    expect(screen.getByText("The answer")).toBeInTheDocument();
    expect(screen.getByText("Open question")).toBeInTheDocument();
  });

  it("shows exactly one textbox in answer mode when a question is open", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({ seq: 0, kind: "question", follow_up_id: 3, text: "Open question" }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText("Open question")).toBeInTheDocument();
    });
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("shows zero textboxes in none mode (read-only)", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({ seq: 0, kind: "question", follow_up_id: 4, text: "Some question" }),
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Some question")).toBeInTheDocument();
    });
    expect(screen.queryAllByRole("textbox")).toHaveLength(0);
  });

  it("hides plumbing turns by default and reveals them via the toggle", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({ seq: 0, kind: "plumbing", role: "assistant", text: "raw internal payload" }),
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Show internal turns")).toBeInTheDocument();
    });
    expect(screen.queryByText("raw internal payload")).toBeNull();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByText("Show internal turns"));

    expect(screen.getByText("raw internal payload")).toBeInTheDocument();
  });

  it("renders the empty state for an empty transcript", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("No conversation yet.")).toBeInTheDocument();
    });
  });

  it("renders suggested-reply chips for an open FollowUp with suggestions", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({
        seq: 0,
        kind: "question",
        follow_up_id: 1,
        text: "Which dates should I use?",
        suggested_replies: ["Use 2019-present", "Let me check and get back to you"],
      }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText("SUGGESTED REPLIES")).toBeInTheDocument();
    });
    expect(screen.getByText("Use 2019-present")).toBeInTheDocument();
    expect(screen.getByText("Let me check and get back to you")).toBeInTheDocument();
  });

  it("renders no chips when suggested_replies is null or empty", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({ seq: 0, kind: "question", follow_up_id: 1, text: "Open question", suggested_replies: null }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText("Open question")).toBeInTheDocument();
    });
    expect(screen.queryByText("SUGGESTED REPLIES")).toBeNull();
  });

  it("clicking a short, decisive chip sends immediately without populating the textarea", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({
        seq: 0,
        kind: "question",
        follow_up_id: 1,
        text: "Which dates should I use?",
        suggested_replies: ["Use 2019-present"],
      }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText("Use 2019-present")).toBeInTheDocument();
    });

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByText("Use 2019-present"));

    await waitFor(() => {
      expect(api.answerFollowUp).toHaveBeenCalledWith("job1", 1, "Use 2019-present");
    });
    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    expect(textarea.value).toBe("");
  });

  it("clicking a long chip populates the textarea without sending", async () => {
    const longSuggestion =
      "Let me double-check my offer letter and get back to you with the exact dates tomorrow";
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      turn({
        seq: 0,
        kind: "question",
        follow_up_id: 1,
        text: "Which dates should I use?",
        suggested_replies: [longSuggestion],
      }),
    ]);

    render(<AgentThread jobId="job1" mode="answer" />);

    await waitFor(() => {
      expect(screen.getByText(longSuggestion)).toBeInTheDocument();
    });

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByText(longSuggestion));

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    await waitFor(() => {
      expect(textarea.value).toBe(longSuggestion);
    });
    expect(api.answerFollowUp).not.toHaveBeenCalled();
  });

  it("shows a Thinking placeholder in the REASONING card when no reasoning has streamed yet", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: { job1: { stage: "cv_adjust", content: "", reasoning: "", tools: [] } },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
  });

  it("shows the streamed reasoning expanded while the turn is live, collapsing it on click", async () => {
    // The live card is expanded by default (the design mock's `open = ... ?? !m.done`).
    // The old wall-of-text objection to that is gone: the text is now chunked into
    // separated step rows and the live view is windowed to the last few of them.
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: {
        job1: { stage: "cv_adjust", content: "", reasoning: "weighing the JD against the CV...", tools: [] },
      },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
    expect(screen.getByText(/weighing the JD against the CV/)).toBeInTheDocument();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("button", { name: /REASONING/ }));

    expect(screen.queryByText(/weighing the JD against the CV/)).not.toBeInTheDocument();
  });

  it("chunks a multi-paragraph reasoning stream into separate step rows", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: {
        job1: {
          stage: "cv_adjust",
          content: "",
          reasoning: "Reading the JD.\n\nComparing it to the CV.\n\nDrafting the change",
          tools: [],
        },
      },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.getAllByTestId("reasoning-step")).toHaveLength(3);
    expect(screen.getByText("3 steps")).toBeInTheDocument();
  });

  it("windows a long live reasoning stream instead of letting the card inflate", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    const reasoning = Array.from({ length: 12 }, (_, i) => `Step number ${i}.`).join("\n\n");
    useStore.setState({
      streamBuffers: { job1: { stage: "cv_adjust", content: "", reasoning, tools: [] } },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.getAllByTestId("reasoning-step")).toHaveLength(5);
    expect(screen.getByText("Step number 11.")).toBeInTheDocument();
    expect(screen.queryByText("Step number 0.")).not.toBeInTheDocument();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByText("+7 earlier steps"));
    expect(screen.getAllByTestId("reasoning-step")).toHaveLength(12);
    expect(screen.getByText("Step number 0.")).toBeInTheDocument();
  });

  it("renders a settled turn's persisted reasoning, collapsed behind a step count", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        seq: 1,
        kind: "question",
        role: "assistant",
        stage: "cv_adjust",
        text: "Which title should I lead with?",
        created_at: "2026-09-03T10:00:00Z",
        follow_up_id: null,
        suggested_replies: null,
        reasoning: "Checked the JD.\n\nChecked the CV.",
      },
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Which title should I lead with?")).toBeInTheDocument();
    });
    expect(screen.getByText("REASONING")).toBeInTheDocument();
    expect(screen.getByText("2 steps")).toBeInTheDocument();
    // Settled = collapsed by default; the user opens it deliberately.
    expect(screen.queryByTestId("reasoning-step")).not.toBeInTheDocument();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("button", { name: /REASONING/ }));
    expect(screen.getAllByTestId("reasoning-step")).toHaveLength(2);
  });

  it("renders live tool-call rows from the streamBuffers channel through the same step list", async () => {
    // The backend now emits a real `agent_tool` WS event, accumulated into
    // streamBuffers[jobId].tools by store.ts's reducer. It renders through the same
    // ReasoningStep union and the same row/separator idiom as reasoning prose.
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: {
        job1: {
          stage: "cv_adjust",
          content: "",
          reasoning: "Reading the JD.\n\nComparing to the CV.",
          tools: [{ name: "read_file", detail: "~/.jsa/cv_structure.json", ok: true, at: 0 }],
        },
      },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });

    // Running card is expanded by default — no click needed.
    expect(screen.getAllByTestId("reasoning-tool-step")).toHaveLength(1);
    expect(screen.getByText("read_file")).toBeInTheDocument();
    expect(screen.getByText("~/.jsa/cv_structure.json")).toBeInTheDocument();
    expect(screen.getByText("TOOL")).toBeInTheDocument();
  });

  it("renders a settled turn's persisted tool rows the same way", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        seq: 1,
        kind: "question",
        role: "assistant",
        stage: "cv_adjust",
        text: "Which title should I lead with?",
        created_at: "2026-09-03T10:00:00Z",
        follow_up_id: null,
        suggested_replies: null,
        reasoning: "Checked the JD.\n\nChecked the CV.",
        tools: [{ name: "web_search", detail: '"Acme Corp engineering culture"', ok: true, at: 0 }],
      },
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Which title should I lead with?")).toBeInTheDocument();
    });
    // Settled = collapsed by default; the user opens it deliberately.
    expect(screen.queryByTestId("reasoning-tool-step")).not.toBeInTheDocument();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("button", { name: /REASONING/ }));

    expect(screen.getAllByTestId("reasoning-tool-step")).toHaveLength(1);
    expect(screen.getByText("web_search")).toBeInTheDocument();
    expect(screen.getByText('"Acme Corp engineering culture"')).toBeInTheDocument();
  });

  it("shows a plumbing turn's persisted tool rows even with internals hidden", async () => {
    // This is the shape the backend actually produces: jsa/api/transcript.py folds
    // role="tool" Message rows into the FOLLOWING assistant turn, and that turn is a
    // `plumbing` turn (its text is the raw JSON envelope). Plumbing is hidden behind the
    // "show internals" toggle, so the card is hoisted OUT of that gate — otherwise a
    // revision's tool activity would vanish on reload even though it was visible live.
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        seq: 1,
        kind: "plumbing",
        role: "assistant",
        stage: "revising_cv",
        text: '{"kind":"final","payload":{}}',
        created_at: "2026-09-04T10:00:00Z",
        follow_up_id: null,
        suggested_replies: null,
        reasoning: null,
        tools: [
          { name: "get_cv", detail: "get_cv", ok: true, at: 0 },
          { name: "replace_summary", detail: "replace_summary", ok: true, at: 1 },
          { name: "finalize", detail: "finalize", ok: true, at: 2 },
        ],
      },
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });

    // The raw plumbing text stays gated — only the card was hoisted.
    expect(screen.queryByText('{"kind":"final","payload":{}}')).not.toBeInTheDocument();

    const user = (await import("@testing-library/user-event")).default.setup();
    await user.click(screen.getByRole("button", { name: /REASONING/ }));

    // All three rows, in the persisted call order.
    const rows = screen.getAllByTestId("reasoning-tool-step");
    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining("get_cv"),
      expect.stringContaining("replace_summary"),
      expect.stringContaining("finalize"),
    ]);
  });

  it("renders no card for a plumbing turn with no tool rows", async () => {
    // Every pre-existing plumbing turn carries tools: null and must keep behaving
    // exactly as before — gated, no card.
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        seq: 1,
        kind: "plumbing",
        role: "assistant",
        stage: "cv_adjust",
        text: "machine plumbing text",
        created_at: "2026-09-04T10:00:00Z",
        follow_up_id: null,
        suggested_replies: null,
        reasoning: null,
        tools: null,
      },
    ]);

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(api.getTranscript).toHaveBeenCalled();
    });
    expect(screen.queryByText("REASONING")).not.toBeInTheDocument();
    expect(screen.queryByText("machine plumbing text")).not.toBeInTheDocument();
  });

  it("hides the REASONING card once content has started arriving with no reasoning captured", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: { job1: { stage: "cv_adjust", content: "Adjusted CV so far...", reasoning: "", tools: [] } },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Adjusted CV so far...")).toBeInTheDocument();
    });
    expect(screen.queryByText("REASONING")).not.toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  });

  it("shows the Thinking placeholder while the job is running even with ZERO chunks ever streamed", async () => {
    // The real gap this pins: a backend can be actively running a turn (structured
    // mode filtering out every content chunk, and the routed model never sending a
    // reasoning_content delta at all) without a single agent_chunk WS event ever
    // arriving -- streamBuffers[jobId] then never gets created. The live bubble
    // must still show, driven by job.state === "running" alone, not by streamBuffer
    // presence.
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({ jobs: { job1: makeJob({ state: "running" }) } });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
  });

  it("shows no live bubble at all when the job isn't running and nothing has streamed", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({ jobs: { job1: makeJob({ state: "awaiting_input" }) } });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("No conversation yet.")).toBeInTheDocument();
    });
    expect(screen.queryByText("REASONING")).not.toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  });
});
