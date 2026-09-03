import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { AgentThread } from "../components/AgentThread";
import { api } from "../api";
import { useStore } from "../store";
import type { TranscriptTurn } from "../types";

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
      streamBuffers: { job1: { stage: "cv_adjust", content: "", reasoning: "" } },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("REASONING")).toBeInTheDocument();
    });
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
  });

  it("shows the real streamed reasoning text in the REASONING card once it arrives", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: {
        job1: { stage: "cv_adjust", content: "", reasoning: "weighing the JD against the CV..." },
      },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("weighing the JD against the CV...")).toBeInTheDocument();
    });
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  });

  it("hides the REASONING card once content has started arriving with no reasoning captured", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);
    useStore.setState({
      streamBuffers: { job1: { stage: "cv_adjust", content: "Adjusted CV so far...", reasoning: "" } },
    });

    render(<AgentThread jobId="job1" mode="none" />);

    await waitFor(() => {
      expect(screen.getByText("Adjusted CV so far...")).toBeInTheDocument();
    });
    expect(screen.queryByText("REASONING")).not.toBeInTheDocument();
    expect(screen.queryByText("Thinking…")).not.toBeInTheDocument();
  });
});
