import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useStore } from "../store";
import { StatusBadge } from "../components/StatusBadge";
import { JobList } from "../components/JobList";
import { JobDetail } from "../components/JobDetail";
import { StageTimeline } from "../components/StageTimeline";
import { api } from "../api";
import type { JobDTO } from "../types";

// ---------------------------------------------------------------------------
// Mock api — include all methods used by JobDetail (+ dismiss and reset)
// ---------------------------------------------------------------------------
vi.mock("../api", () => ({
  api: {
    getJob: vi.fn().mockResolvedValue({
      id: "j1",
      company: "Acme",
      role: "Engineer",
      link: "https://example.com",
      tier: "A",
      jd: "Some JD text",
      state: "pending",
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
    dismiss: vi.fn().mockResolvedValue({}),
    reset: vi.fn().mockResolvedValue({}),
  },
}));

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

// ---------------------------------------------------------------------------
// Shared fixture helpers
// ---------------------------------------------------------------------------
function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "j1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "UNIQUE_JD_BODY_TEXT for this test job",
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
  });
});

// ===========================================================================
// 1. StatusBadge — "dismissed" state
// ===========================================================================
describe('StatusBadge — dismissed state', () => {
  it('renders label "DISMISSED" for state "dismissed"', () => {
    render(<StatusBadge state="dismissed" />);
    expect(screen.getByText("DISMISSED")).toBeInTheDocument();
  });

  it('does not render a spinner for state "dismissed"', () => {
    const { container } = render(<StatusBadge state="dismissed" />);
    const dot = container.querySelector('span > span');
    expect(dot).not.toHaveStyle({ animation: "jsspin .7s linear infinite" });
  });
});

// ===========================================================================
// 2. JobList — Dismissed group
// ===========================================================================
describe('JobList — Dismissed group', () => {
  it('shows "Dismissed" group header (h2) when a dismissed job exists', () => {
    const job = makeJob({ id: "d1", state: "dismissed", company: "Dismissed Co", role: "Dev" });
    useStore.setState({ jobs: { d1: job } });

    render(<JobList />);

    // "Dismissed" group label lives inside an h2 (the badge itself reads "DISMISSED")
    const label = screen.getByText("Dismissed");
    expect(label.closest("h2")).not.toBeNull();
  });

  it('shows the dismissed job inside the Dismissed group', () => {
    const job = makeJob({ id: "d1", state: "dismissed", company: "Dismissed Co", role: "Dev" });
    useStore.setState({ jobs: { d1: job } });

    render(<JobList />);

    expect(screen.getByText("Dismissed Co")).toBeInTheDocument();
    expect(screen.getByText("Dev")).toBeInTheDocument();
  });

  it('does NOT show "Dismissed" group when there are no dismissed jobs', () => {
    const job = makeJob({ id: "p1", state: "pending" });
    useStore.setState({ jobs: { p1: job } });

    render(<JobList />);

    // No "Dismissed" text anywhere — neither as group header nor as badge
    expect(screen.queryByText("Dismissed")).toBeNull();
  });
});

// ===========================================================================
// 3. JobDetail — Dismiss button visibility
// ===========================================================================
describe('JobDetail — Dismiss button', () => {
  it('renders "Dismiss" button when state is "pending"', () => {
    const job = makeJob({ id: "j1", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Dismiss$/ })).toBeInTheDocument();
  });

  it('renders "Dismiss" button when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Dismiss$/ })).toBeInTheDocument();
  });

  it('renders "Dismiss" button when state is "review"', async () => {
    const job = makeJob({ id: "j1", state: "review" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // review state mounts ReviewPane — wait for initial renders to settle
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^Dismiss$/ })).toBeInTheDocument();
    });
  });

  it('does NOT render "Dismiss" button when state is "approved"', async () => {
    const job = makeJob({ id: "j1", state: "approved" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /^Dismiss$/ })).toBeNull();
    });
  });

  it('does NOT render "Dismiss" button when state is "dismissed"', () => {
    const job = makeJob({ id: "j1", state: "dismissed" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /^Dismiss$/ })).toBeNull();
  });
});

// ===========================================================================
// 4. JobDetail — Re-queue button visibility
// ===========================================================================
describe('JobDetail — Re-queue button', () => {
  it('renders "Re-queue" button when state is "dismissed"', () => {
    const job = makeJob({ id: "j1", state: "dismissed" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /Re-queue/ })).toBeInTheDocument();
  });

  it('does NOT render "Re-queue" button when state is "pending"', () => {
    const job = makeJob({ id: "j1", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /Re-queue/ })).toBeNull();
  });

  it('does NOT render "Re-queue" button when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /Re-queue/ })).toBeNull();
  });

  it('does NOT render "Re-queue" button when state is "approved"', async () => {
    const job = makeJob({ id: "j1", state: "approved" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /Re-queue/ })).toBeNull();
    });
  });

  it('does NOT render "Re-queue" button when state is "failed"', () => {
    const job = makeJob({ id: "j1", state: "failed" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /Re-queue/ })).toBeNull();
  });
});

// ===========================================================================
// 5. JobDetail — JD collapsible section
// ===========================================================================
describe('JobDetail — JD collapsible', () => {
  // The list/summary job (from the store) no longer carries `jd` — see CLAUDE.md →
  // "API request hardening". JobDetail now fetches it lazily via api.getJob (same
  // pattern as ReviewPane/FollowUpPane), so these tests mock that per-test and wait
  // for the effect to resolve before asserting.
  function mockJdFetch(jd: string) {
    (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      id: "j1",
      company: "Acme",
      role: "Engineer",
      link: "https://example.com",
      tier: "A",
      jd,
      state: "pending",
      current_stage: null,
      error: null,
      updated_at: "2026-01-01T00:00:00Z",
      created_at: "2026-01-01T00:00:00Z",
      follow_ups: [],
      documents: [],
    });
  }

  it('shows the "Job Description" section header button on initial render', () => {
    const job = makeJob({ id: "j1", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /JOB_DESCRIPTION/ })).toBeInTheDocument();
  });

  it('does NOT show JD text initially (collapsed)', async () => {
    const job = makeJob({ id: "j1", state: "pending" });
    mockJdFetch("UNIQUE_JD_BODY_TEXT for this test job");
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);
    await waitFor(() => expect(api.getJob).toHaveBeenCalledWith("j1"));

    expect(screen.queryByText("UNIQUE_JD_BODY_TEXT for this test job")).toBeNull();
  });

  it('shows JD text after clicking the "Job Description" toggle button', async () => {
    const job = makeJob({ id: "j1", state: "pending" });
    mockJdFetch("UNIQUE_JD_BODY_TEXT for this test job");
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);
    await waitFor(() => expect(api.getJob).toHaveBeenCalledWith("j1"));

    const toggleBtn = screen.getByRole("button", { name: /JOB_DESCRIPTION/ });
    fireEvent.click(toggleBtn);

    expect(await screen.findByText("UNIQUE_JD_BODY_TEXT for this test job")).toBeInTheDocument();
  });

  it('hides JD text again after clicking the toggle twice (toggle off)', async () => {
    const job = makeJob({ id: "j1", state: "pending" });
    mockJdFetch("UNIQUE_JD_BODY_TEXT for this test job");
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);
    await waitFor(() => expect(api.getJob).toHaveBeenCalledWith("j1"));

    const toggleBtn = screen.getByRole("button", { name: /JOB_DESCRIPTION/ });
    fireEvent.click(toggleBtn); // open
    fireEvent.click(toggleBtn); // close

    expect(screen.queryByText("UNIQUE_JD_BODY_TEXT for this test job")).toBeNull();
  });
});

// ===========================================================================
// 6. StageTimeline — dismissed state does not crash
// ===========================================================================
describe('StageTimeline — dismissed state', () => {
  it('does not throw when job.state === "dismissed"', () => {
    const job = makeJob({ state: "dismissed" });
    expect(() => render(<StageTimeline job={job} />)).not.toThrow();
  });

  it('renders all 7 step dots when state is "dismissed"', () => {
    const job = makeJob({ state: "dismissed" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = container.querySelectorAll('[data-testid="stage-dot"]');
    expect(dots.length).toBe(7);
  });

  it('shows first step (index 0) as active for state "dismissed"', () => {
    const job = makeJob({ state: "dismissed" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = container.querySelectorAll('[data-testid="stage-dot"]');
    // dismissed maps to activeIdx=0, so step 0 is active
    expect(dots[0]).toHaveAttribute("data-state", "active");
  });
});
