/**
 * Tests for the Delete button in JobDetail (originally Phase BF-6: Cancel button;
 * updated in BF-12 to reflect that Cancel was replaced by a hard Delete action).
 *
 * Covers:
 * 1. Delete button shown for running job
 * 2. Delete button NOT shown only for "approved" state; shown for all other states
 * 3. Dismiss button still shown alongside Delete for running job
 * 4. handleDelete calls api.deleteJob with the correct job id
 * 5. deleteError displayed on api.deleteJob rejection
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { useStore } from "../store";
import { api } from "../api";
import { JobDetail } from "../components/JobDetail";
import type { JobDTO } from "../types";

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
    backend_name: null,
      error: null,
      retry_count: 0,
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
    deleteJob: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
  // handleDelete is gated by window.confirm — default jsdom returns false which
  // causes the handler to bail out before calling api.deleteJob.  Stub it to
  // return true so click tests exercise the real code path.
  vi.spyOn(window, "confirm").mockReturnValue(true);
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
    backend_name: null,
    error: null,
    retry_count: 0,
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
  // Clear call counts and reset implementations before each test
  vi.clearAllMocks();
  vi.mocked(api.deleteJob).mockResolvedValue({ ok: true } as never);
  vi.mocked(api.dismiss).mockResolvedValue({} as never);
  vi.mocked(api.reset).mockResolvedValue({} as never);
  vi.mocked(api.getJobs).mockResolvedValue([]);
  // Restore confirm stub after clearAllMocks (clearAllMocks resets spy return values)
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

// ===========================================================================
// 1. Delete button shown for running job
// ===========================================================================
describe("JobDetail — Delete button visibility for running job", () => {
  it('renders "Delete" button when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Delete$/ })).toBeInTheDocument();
  });
});

// ===========================================================================
// 2. Delete button visibility across non-approved states
//
// The component shows Delete for ALL states except "approved" (showCancel =
// job.state !== "approved").  So the old "Cancel NOT shown for pending/failed/
// review" expectation is now inverted — Delete IS shown for those states.
// Only "approved" hides the button.
// ===========================================================================
describe("JobDetail — Delete button visibility across states", () => {
  it('renders "Delete" button when state is "pending"', () => {
    const job = makeJob({ id: "j1", state: "pending" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Delete$/ })).toBeInTheDocument();
  });

  it('renders "Delete" button when state is "failed"', () => {
    const job = makeJob({ id: "j1", state: "failed" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Delete$/ })).toBeInTheDocument();
  });

  it('renders "Delete" button when state is "review"', async () => {
    const job = makeJob({ id: "j1", state: "review" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    // review state mounts ReviewPane — wait for initial renders to settle
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^Delete$/ })).toBeInTheDocument();
    });
  });

  it('does NOT render "Delete" button when state is "approved"', async () => {
    const job = makeJob({ id: "j1", state: "approved" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /^Delete$/ })).toBeNull();
    });
  });
});

// ===========================================================================
// 3. Dismiss button still shown alongside Delete for running job
// ===========================================================================
describe("JobDetail — Dismiss coexists with Delete for running job", () => {
  it('renders both "Delete" and "Dismiss" buttons when state is "running"', () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    expect(screen.getByRole("button", { name: /^Delete$/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Dismiss$/ })).toBeInTheDocument();
  });
});

// ===========================================================================
// 4. handleDelete calls api.deleteJob with the correct job id
// ===========================================================================
describe("JobDetail — handleDelete calls api.deleteJob", () => {
  it("calls api.deleteJob with the job id when Delete is clicked", async () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(vi.mocked(api.deleteJob)).toHaveBeenCalledWith("j1");
    });
  });

  it("calls api.deleteJob exactly once per click", async () => {
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(vi.mocked(api.deleteJob)).toHaveBeenCalledTimes(1);
    });
  });
});

// ===========================================================================
// 5. deleteError displayed on api.deleteJob rejection
// ===========================================================================
describe("JobDetail — deleteError display on failure", () => {
  it("shows error text when api.deleteJob rejects", async () => {
    vi.mocked(api.deleteJob).mockRejectedValue(new Error("HTTP 400: not running"));

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(screen.getByText("HTTP 400: not running")).toBeInTheDocument();
    });
  });

  it("error text is displayed with danger (red) styling", async () => {
    vi.mocked(api.deleteJob).mockRejectedValue(new Error("HTTP 400: not running"));

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      const errorEl = screen.getByText("HTTP 400: not running");
      // Inline-styled (theme-driven) red — SHELL_THEME.danger === #FF4655
      expect(errorEl).toHaveStyle({ color: "rgb(255, 70, 85)" });
    });
  });

  it("does not show delete error when api.deleteJob succeeds", async () => {
    vi.mocked(api.deleteJob).mockResolvedValue({ ok: true } as never);

    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const deleteBtn = screen.getByRole("button", { name: /^Delete$/ });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(vi.mocked(api.deleteJob)).toHaveBeenCalled();
    });

    // No error text should be in the DOM
    expect(screen.queryByText(/HTTP 400/)).toBeNull();
  });
});
