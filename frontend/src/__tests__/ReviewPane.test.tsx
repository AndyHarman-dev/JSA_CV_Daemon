import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReviewPane } from "../components/ReviewPane";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO, FullJobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    getJob: vi.fn(),
    approve: vi.fn(),
    revise: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "Job description",
    state: "review",
    current_stage: null,
    language: null,
    fit_reason: null,
    error: null,
    retry_count: 0,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function makeFullJob(overrides: Partial<JobDTO> = {}): FullJobDTO {
  return { ...makeJob(overrides), follow_ups: [], documents: [] };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue(makeFullJob());
});

describe("ReviewPane", () => {
  it("shows 'Loading preview…' initially while documents are fetching", () => {
    // Never resolve so we stay in loading state
    (api.getJob as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    expect(screen.getByText("Loading preview…")).toBeInTheDocument();
  });

  it("shows tab bar with CV / RESUME and COVER_LETTER tabs after loading", async () => {
    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    // Tab buttons are always rendered (not behind loading state)
    expect(screen.getByRole("button", { name: "CV / RESUME" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "COVER_LETTER" })).toBeInTheDocument();

    // Let the getJob effect resolve so React state settles before the test exits.
    await waitFor(() => expect(api.getJob).toHaveBeenCalled());
  });

  it("switches to Cover Letter tab when clicked", async () => {
    const user = userEvent.setup();

    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await user.click(screen.getByRole("button", { name: "COVER_LETTER" }));

    // The preview header derives directly from activeTab — that's the state change
    // under test. (The border-bottom color is also a signal, but jsdom's shorthand→
    // longhand expansion for it is incomplete — don't assert on style there.)
    await screen.findByText(/— COVER_LETTER/);
  });

  it("shows 'APPROVE & EXPORT' button when state is 'review'", async () => {
    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /approve & export/i })
      ).toBeInTheDocument();
    });
  });

  it("does not show approve button when state is 'approved'", async () => {
    const job = makeJob({ state: "approved" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: /approve & export/i })
      ).toBeNull();
    });
  });

  it("shows approved banner when state is 'approved'", async () => {
    const job = makeJob({ state: "approved" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText(/approved · pdfs written/i)).toBeInTheDocument();
    });
  });

  it("calls api.approve with the correct jobId when Approve button is clicked", async () => {
    const user = userEvent.setup();
    (api.approve as ReturnType<typeof vi.fn>).mockResolvedValue({
      cv_pdf_path: "/out/cv.pdf",
      cl_pdf_path: "/out/cl.pdf",
    });

    const job = makeJob({ id: "job1", state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    const approveButton = await screen.findByRole("button", {
      name: /approve & export/i,
    });
    await user.click(approveButton);

    await waitFor(() => {
      expect(api.approve).toHaveBeenCalledWith("job1");
    });
  });

  it("shows error message when api.approve throws", async () => {
    const user = userEvent.setup();
    (api.approve as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("approval failed")
    );

    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    const approveButton = await screen.findByRole("button", {
      name: /approve & export/i,
    });
    await user.click(approveButton);

    await waitFor(() => {
      expect(screen.getByText("approval failed")).toBeInTheDocument();
    });
  });

  it("renders Request Revision ChatBox in review state", async () => {
    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Request Revision/i })
      ).toBeInTheDocument();
    });
  });
});
