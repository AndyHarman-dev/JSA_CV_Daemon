/**
 * Tests for Phase BF-6: Cancel button in JobDetail.
 *
 * Covers:
 * 1. Cancel button shown for running job
 * 2. Cancel button NOT shown for non-running states (pending, failed, review, approved)
 * 3. Dismiss button still shown alongside Cancel for running job
 * 4. handleCancel calls api.cancel with the correct job id
 * 5. cancelError displayed on api.cancel rejection
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useStore } from "../store";
import { api } from "../api";
import { JobDetail } from "../components/JobDetail";
import type { JobDTO, LogEntry } from "../types";

// ---------------------------------------------------------------------------
// Mock the api module — include all methods used by JobDetail
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
      state: "running",
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
    cancel: vi.fn().mockResolvedValue({}),
  },
}));

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "j1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "Some JD text",
    state: "running",
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
  // Clear call counts and reset implementations before each test
  vi.clearAllMocks();
  vi.mocked(api.cancel).mockResolvedValue({} as never);
  vi.mocked(api.dismiss).mockResolvedValue({} as never);
  vi.mocked(api.reset).mockResolvedValue({} as never);
  vi.mocked(api.getJobs).mockResolvedValue([]);
});

// ===========================================================================
// 1. Cancel button shown for running job
// ===========================================================================
describe("JobDetail — Cancel button visibility for running job", () => {
  it('renders "Cancel" button when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeInTheDocument();
  });
});

// ===========================================================================
// 2. Cancel button NOT shown for non-running states
// ===========================================================================
describe("JobDetail — Cancel button hidden for non-running states", () => {
  it('does NOT render "Cancel" button when state is "pending"', () => {
    const job = makeJob({ id: "j1", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /^Cancel$/ })).toBeNull();
  });

  it('does NOT render "Cancel" button when state is "failed"', () => {
    const job = makeJob({ id: "j1", state: "failed" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.queryByRole("button", { name: /^Cancel$/ })).toBeNull();
  });

  it('does NOT render "Cancel" button when state is "review"', async () => {
    const job = makeJob({ id: "j1", state: "review" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // review state mounts ReviewPane — wait for initial renders to settle
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /^Cancel$/ })).toBeNull();
    });
  });

  it('does NOT render "Cancel" button when state is "approved"', async () => {
    const job = makeJob({ id: "j1", state: "approved" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /^Cancel$/ })).toBeNull();
    });
  });
});

// ===========================================================================
// 3. Dismiss button still shown alongside Cancel for running job
// ===========================================================================
describe("JobDetail — Dismiss coexists with Cancel for running job", () => {
  it('renders both "Cancel" and "Dismiss" buttons when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Cancel$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Dismiss$/ })).toBeInTheDocument();
  });
});

// ===========================================================================
// 4. handleCancel calls api.cancel with the correct job id
// ===========================================================================
describe("JobDetail — handleCancel calls api.cancel", () => {
  it("calls api.cancel with the job id when Cancel is clicked", async () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const cancelBtn = screen.getByRole("button", { name: /^Cancel$/ });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(vi.mocked(api.cancel)).toHaveBeenCalledWith("j1");
    });
  });

  it("calls api.cancel exactly once per click", async () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const cancelBtn = screen.getByRole("button", { name: /^Cancel$/ });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(vi.mocked(api.cancel)).toHaveBeenCalledTimes(1);
    });
  });
});

// ===========================================================================
// 5. cancelError displayed on api.cancel rejection
// ===========================================================================
describe("JobDetail — cancelError display on failure", () => {
  it("shows error text when api.cancel rejects", async () => {
    vi.mocked(api.cancel).mockRejectedValue(new Error("HTTP 400: not running"));

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const cancelBtn = screen.getByRole("button", { name: /^Cancel$/ });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(screen.getByText("HTTP 400: not running")).toBeInTheDocument();
    });
  });

  it("error text is displayed with red styling (text-red-600 class)", async () => {
    vi.mocked(api.cancel).mockRejectedValue(new Error("HTTP 400: not running"));

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const cancelBtn = screen.getByRole("button", { name: /^Cancel$/ });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      const errorEl = screen.getByText("HTTP 400: not running");
      expect(errorEl).toHaveClass("text-red-600");
    });
  });

  it("does not show cancel error when api.cancel succeeds", async () => {
    vi.mocked(api.cancel).mockResolvedValue({} as never);

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const cancelBtn = screen.getByRole("button", { name: /^Cancel$/ });
    fireEvent.click(cancelBtn);

    await waitFor(() => {
      expect(vi.mocked(api.cancel)).toHaveBeenCalled();
    });

    // No error text should be in the DOM
    expect(screen.queryByText(/HTTP 400/)).toBeNull();
  });
});
