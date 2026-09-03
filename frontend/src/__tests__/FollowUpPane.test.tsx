import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { FollowUpPane } from "../components/FollowUpPane";
import { api } from "../api";
import { useStore } from "../store";
import type { TranscriptTurn } from "../types";

vi.mock("../api", () => ({
  api: {
    getJobs: vi.fn().mockResolvedValue([]),
    getTranscript: vi.fn(),
    answerFollowUp: vi.fn().mockResolvedValue({}),
  },
}));

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

function questionTurn(overrides: Partial<TranscriptTurn> = {}): TranscriptTurn {
  return {
    seq: 0,
    kind: "question",
    role: "assistant",
    stage: "cv_adjust",
    text: "What is your target role level?",
    created_at: "2026-01-01T00:00:00Z",
    follow_up_id: 7,
    suggested_replies: null,
    reasoning: null,
    ...overrides,
  };
}

function answerTurn(overrides: Partial<TranscriptTurn> = {}): TranscriptTurn {
  return {
    seq: 1,
    kind: "answer",
    role: "user",
    stage: "cv_adjust",
    text: "My answer",
    created_at: "2026-01-01T01:00:00Z",
    follow_up_id: 8,
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

describe("FollowUpPane", () => {
  it("shows a loading state while the transcript fetch is in flight", () => {
    // Never resolve so we stay in loading state
    (api.getTranscript as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    render(<FollowUpPane jobId="job1" />);

    expect(screen.getByText("Loading conversation…")).toBeInTheDocument();
  });

  it("shows the question text once loaded with an open follow-up", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      questionTurn({ follow_up_id: 7, text: "What is your target role level?" }),
    ]);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("What is your target role level?")).toBeInTheDocument();
    });
  });

  it("renders both an answered question and answer, and still offers a composer for a later open question", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      questionTurn({ seq: 0, follow_up_id: 8, text: "Old question" }),
      answerTurn({ seq: 1, follow_up_id: 8, text: "My answer" }),
      questionTurn({ seq: 2, follow_up_id: 9, text: "New open question" }),
    ]);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("Old question")).toBeInTheDocument();
    });
    expect(screen.getByText("My answer")).toBeInTheDocument();
    expect(screen.getByText("New open question")).toBeInTheDocument();
    // Exactly one textbox — the composer for the still-open question.
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("shows the empty-conversation state when the transcript is empty", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([]);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("No conversation yet.")).toBeInTheDocument();
    });
  });

  it("renders a ChatBox below the question when an open follow-up is present", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockResolvedValue([
      questionTurn({ follow_up_id: 9, text: "Describe your experience." }),
    ]);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      // ChatBox with kind="answer" renders a Submit Answer button
      expect(screen.getByRole("button", { name: /Submit Answer/i })).toBeInTheDocument();
    });
    // Exactly one textbox in answer mode.
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("shows an error message when the transcript fetch rejects", async () => {
    (api.getTranscript as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("not found"));

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("not found")).toBeInTheDocument();
    });
  });
});
