import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { useStore } from "../store";
import { JobDetail } from "../components/JobDetail";
import type { JobDTO, LogEntry } from "../types";

vi.mock("../api", () => ({
  api: {
    getJob: vi.fn().mockResolvedValue({
      id: "j1",
      company: "Acme",
      role: "Engineer",
      link: "https://example.com",
      tier: "A",
      state: "awaiting_input",
      current_stage: null,
      error: null,
      updated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      follow_ups: [],
      documents: [],
    }),
    getDocument: vi.fn().mockResolvedValue({ markdown: "# Doc", version: 1 }),
    answerFollowUp: vi.fn().mockResolvedValue({}),
    approve: vi.fn().mockResolvedValue({ cv_pdf_path: "/cv.pdf", cl_pdf_path: "/cl.pdf" }),
    revise: vi.fn().mockResolvedValue({}),
    config: vi.fn().mockResolvedValue({ backend: "anthropic" }),
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
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
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
    logs: [] as LogEntry[],
  });
});

describe("JobDetail", () => {
  it("shows placeholder text when no job is selected", () => {
    render(<JobDetail />);
    expect(screen.getByText(/Select a job/i)).toBeInTheDocument();
  });

  it("shows placeholder when selectedId is set but job is not in the map", () => {
    useStore.setState({ selectedId: "nonexistent" });
    render(<JobDetail />);
    expect(screen.getByText(/Select a job/i)).toBeInTheDocument();
  });

  it("shows company and role for a selected pending job", () => {
    const job = makeJob({ id: "j1", company: "TechCorp", role: "SWE", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByText("TechCorp — SWE")).toBeInTheDocument();
  });

  it("shows the tier badge for the selected job", () => {
    const job = makeJob({ id: "j1", tier: "B" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByText("Tier B")).toBeInTheDocument();
  });

  it("shows status badge for the selected job", () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("shows error message in red box when job has failed with an error", () => {
    const job = makeJob({
      id: "j1",
      state: "failed",
      error: "agent timed out after 30s",
    });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const errorMsg = screen.getByText("agent timed out after 30s");
    expect(errorMsg).toBeInTheDocument();

    // Error box should have red styling
    const errorBox = errorMsg.closest("div");
    expect(errorBox).not.toBeNull();
    expect(errorBox).toHaveClass("bg-red-50");
  });

  it("does not show the error box when job has no error", () => {
    const job = makeJob({ id: "j1", state: "pending", error: null });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByText(/Error:/)).toBeNull();
  });

  it("shows the stage timeline for a selected job", () => {
    const job = makeJob({ id: "j1", state: "cv_done" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // Pipeline Progress heading should be present
    expect(screen.getByText("Pipeline Progress")).toBeInTheDocument();
  });

  it("renders FollowUpPane (not placeholder) for awaiting_input state", () => {
    const job = makeJob({ id: "j1", state: "awaiting_input" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // Phase 10 placeholder is gone; FollowUpPane mounts (shows loading state initially)
    expect(screen.queryByText(/Follow-up pane coming in Phase 10/)).toBeNull();
    expect(screen.queryByText(/coming in Phase 10/)).toBeNull();
    // FollowUpPane renders its loading state
    expect(screen.getByText("Loading follow-up…")).toBeInTheDocument();
  });

  it("renders ReviewPane (not placeholder) for review state", () => {
    const job = makeJob({ id: "j1", state: "review" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // Phase 10 placeholder is gone; ReviewPane mounts with tab bar
    expect(screen.queryByText(/Review pane coming in Phase 10/)).toBeNull();
    expect(screen.queryByText(/coming in Phase 10/)).toBeNull();
    // Tab buttons rendered by ReviewPane (use getAllByText since option also has same text)
    const cvResumeElements = screen.getAllByText("CV / Resume");
    expect(cvResumeElements.length).toBeGreaterThan(0);
  });

  it("renders ReviewPane (not placeholder) for approved state", () => {
    const job = makeJob({ id: "j1", state: "approved" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // Phase 10 placeholder is gone; ReviewPane mounts with tab bar
    expect(screen.queryByText(/Review pane coming in Phase 10/)).toBeNull();
    expect(screen.queryByText(/coming in Phase 10/)).toBeNull();
    // Tab buttons rendered by ReviewPane
    const cvResumeElements = screen.getAllByText("CV / Resume");
    expect(cvResumeElements.length).toBeGreaterThan(0);
  });

  it("shows Logs section heading for a selected job", () => {
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByText("Logs")).toBeInTheDocument();
  });

  it("shows 'No log entries yet.' when no logs for the job", () => {
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1", logs: [] });

    render(<JobDetail />);

    expect(screen.getByText("No log entries yet.")).toBeInTheDocument();
  });

  it("shows log entries for the selected job", () => {
    const job = makeJob({ id: "j1" });
    const logEntry: LogEntry = {
      job_id: "j1",
      level: "info",
      text: "pipeline started",
      ts: Date.now(),
    };
    useStore.setState({ jobs: { j1: job }, selectedId: "j1", logs: [logEntry] });

    render(<JobDetail />);

    expect(screen.getByText("pipeline started")).toBeInTheDocument();
  });
});
