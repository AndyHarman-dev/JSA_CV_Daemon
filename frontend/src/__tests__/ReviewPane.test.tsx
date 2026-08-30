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
    approveCv: vi.fn(),
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
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting", viewedStage: null });
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

  describe('mode="cv-gate"', () => {
    it("shows only the CV tab and an APPROVE CV button, and calls api.approveCv", async () => {
      const user = userEvent.setup();
      (api.approveCv as ReturnType<typeof vi.fn>).mockResolvedValue({
        pdf_path: "/out/cv.pdf",
        docx_path: "/out/cv.docx",
      });
      const job = makeJob({ state: "cv_review" });
      useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      expect(screen.getByRole("button", { name: "CV / RESUME" })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "COVER_LETTER" })).toBeNull();

      const approveButton = await screen.findByRole("button", { name: /approve cv/i });
      await user.click(approveButton);

      await waitFor(() => {
        expect(api.approveCv).toHaveBeenCalledWith("job1");
      });
      expect(api.approve).not.toHaveBeenCalled();
    });

    it("restricts the revise ChatBox to the CV target (no cv/cl selector)", async () => {
      const job = makeJob({ state: "cv_review" });
      useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      await waitFor(() => {
        expect(screen.getByRole("button", { name: /Request Revision/i })).toBeInTheDocument();
      });
      expect(screen.queryByRole("combobox")).toBeNull();
    });

    it("hides the approve button and revise box, and shows a read-only notice, when the job is running/awaiting_input", async () => {
      const job = makeJob({ state: "running", current_stage: "cover_letter" });
      useStore.setState({ jobs: { job1: job }, selectedId: "job1", viewedStage: "cv" });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      await waitFor(() => {
        expect(screen.queryByRole("button", { name: /approve cv/i })).toBeNull();
      });
      expect(screen.queryByRole("button", { name: /Request Revision/i })).toBeNull();
      expect(screen.getByText(/cover letter lane is running/i)).toBeInTheDocument();
    });

    it("shows the CV-lane read-only notice (not the cover-letter one) when clicking CV_ADJUST during cv_adjust's own run", async () => {
      const job = makeJob({ state: "running", current_stage: "cv_adjust" });
      useStore.setState({ jobs: { job1: job }, selectedId: "job1", viewedStage: "cv" });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      await waitFor(() => {
        expect(screen.queryByRole("button", { name: /approve cv/i })).toBeNull();
      });
      expect(screen.queryByRole("button", { name: /Request Revision/i })).toBeNull();
      // The bug: this used to say "COVER LETTER LANE IS RUNNING" even though cv_adjust is running.
      expect(screen.getByText(/cv lane is running/i)).toBeInTheDocument();
      expect(screen.queryByText(/cover letter lane is running/i)).toBeNull();
    });
  });

  // The regression this whole feature could introduce: StageTimeline's dots write to the
  // same shared `viewedStage` the final ReviewPane reads for its tab. Looking at the CV tab
  // via viewedStage must never be mistaken for the CV-gate's read-only condition — a job at
  // state === "review" stays fully interactive regardless of which tab is being viewed.
  it('mode="final" with viewedStage "cv" still shows APPROVE & EXPORT and the revise box at state "review"', async () => {
    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1", viewedStage: "cv" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /approve & export/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: /Request Revision/i })).toBeInTheDocument();
  });
});
