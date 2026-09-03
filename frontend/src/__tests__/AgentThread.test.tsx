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
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting", transcripts: {} });
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
});
