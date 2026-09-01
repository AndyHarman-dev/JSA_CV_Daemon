import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { FollowUpPane } from "../components/FollowUpPane";
import { api } from "../api";
import { useStore } from "../store";
import type { FullJobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    getJob: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
    answerFollowUp: vi.fn().mockResolvedValue({}),
  },
}));

function makeFullJob(overrides: Partial<FullJobDTO> = {}): FullJobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    state: "awaiting_input",
    current_stage: null,
    backend_name: null,
    error: null,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    follow_ups: [],
    documents: [],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
});

describe("FollowUpPane", () => {
  it("shows 'Loading follow-up…' while the API call is in flight", () => {
    // Never resolve so we stay in loading state
    (api.getJob as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    render(<FollowUpPane jobId="job1" />);

    expect(screen.getByText("Loading follow-up…")).toBeInTheDocument();
  });

  it("shows the question text once loaded with an open follow-up", async () => {
    const job = makeFullJob({
      follow_ups: [
        {
          id: 7,
          job_id: "job1",
          stage: "cv_adjust",
          question: "What is your target role level?",
          answer: null,
          asked_at: "2026-01-01T00:00:00Z",
          answered_at: null,
        },
      ],
    });
    (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue(job);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("What is your target role level?")).toBeInTheDocument();
    });
  });

  it("shows 'Waiting for follow-up data…' when all follow-ups are answered", async () => {
    const job = makeFullJob({
      follow_ups: [
        {
          id: 8,
          job_id: "job1",
          stage: "cv_adjust",
          question: "Old question",
          answer: "My answer",
          asked_at: "2026-01-01T00:00:00Z",
          answered_at: "2026-01-01T01:00:00Z",
        },
      ],
    });
    (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue(job);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("Waiting for follow-up data…")).toBeInTheDocument();
    });
  });

  it("shows 'Waiting for follow-up data…' when follow_ups array is empty", async () => {
    const job = makeFullJob({ follow_ups: [] });
    (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue(job);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("Waiting for follow-up data…")).toBeInTheDocument();
    });
  });

  it("renders a ChatBox below the question when an open follow-up is present", async () => {
    const job = makeFullJob({
      follow_ups: [
        {
          id: 9,
          job_id: "job1",
          stage: "cv_adjust",
          question: "Describe your experience.",
          answer: null,
          asked_at: "2026-01-01T00:00:00Z",
          answered_at: null,
        },
      ],
    });
    (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue(job);

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      // ChatBox with kind="answer" renders a Submit Answer button
      expect(screen.getByRole("button", { name: /Submit Answer/i })).toBeInTheDocument();
    });
  });

  it("shows an error message when api.getJob rejects", async () => {
    (api.getJob as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("not found"));

    render(<FollowUpPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText("not found")).toBeInTheDocument();
    });
  });
});
