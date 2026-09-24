import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReviewPane } from "../components/ReviewPane";
import { api } from "../api";
import { useStore } from "../store";
import type { DocumentDTO, JobDTO, FullJobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    getJob: vi.fn(),
    approve: vi.fn(),
    approveCv: vi.fn(),
    revise: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
    saveCvAsDeck: vi.fn(),
    listCvDecks: vi.fn().mockResolvedValue({ decks: [], default_id: null }),
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
    backend_name: null,
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
  describe("JOB POSTING link", () => {
    it('renders in "final" mode with the job link as href and a safe rel', async () => {
      useStore.setState({
        jobs: { job1: makeJob({ link: "https://jobs.example.com/eng-42" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" />);

      const link = await screen.findByRole("link", { name: /job posting/i });
      expect(link).toHaveAttribute("href", "https://jobs.example.com/eng-42");
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    });

    it('renders in "cv-gate" mode too — both stages share the one tab bar', async () => {
      useStore.setState({
        jobs: { job1: makeJob({ state: "cv_review", link: "https://jobs.example.com/eng-42" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      const link = await screen.findByRole("link", { name: /job posting/i });
      expect(link).toHaveAttribute("href", "https://jobs.example.com/eng-42");
    });

    it("does not render when the link is empty", async () => {
      useStore.setState({ jobs: { job1: makeJob({ link: "" }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      // Wait for the pane to finish loading so absence means absence, not "not yet rendered".
      await screen.findByRole("button", { name: /approve & export/i });
      expect(screen.queryByText(/job posting/i)).not.toBeInTheDocument();
    });

    // The CSV `link` column is free text; a `javascript:` URI there would otherwise
    // execute in the app's own origin on click. rel="noopener noreferrer" does not
    // stop that — only a scheme check does.
    it("does not render a javascript: link", async () => {
      useStore.setState({
        jobs: { job1: makeJob({ link: "javascript:alert(1)" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" />);

      await screen.findByRole("button", { name: /approve & export/i });
      expect(screen.queryByText(/job posting/i)).not.toBeInTheDocument();
    });

    it("still renders an ordinary https link", async () => {
      useStore.setState({
        jobs: { job1: makeJob({ link: "https://ok.example.com/x" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" />);

      const link = await screen.findByRole("link", { name: /job posting/i });
      expect(link).toHaveAttribute("href", "https://ok.example.com/x");
    });

    it("does not render when the link is only whitespace", async () => {
      useStore.setState({ jobs: { job1: makeJob({ link: "   " }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      await screen.findByRole("button", { name: /approve & export/i });
      expect(screen.queryByText(/job posting/i)).not.toBeInTheDocument();
    });
  });

  // Gated on "a cv_adjust Document exists", never on job state — so it must also show while
  // the pane is read-only, and never before the CV is written.
  describe("SAVE AS BASE CV", () => {
    const cvDoc: DocumentDTO = {
      id: 1,
      job_id: "job1",
      stage: "cv_adjust",
      version: 2,
      markdown: "# CV",
      pdf_path: "/out/acme/cv.pdf",
      docx_path: "/out/acme/cv.docx",
    };

    function withCvDoc(overrides: Partial<JobDTO> = {}) {
      (api.getJob as ReturnType<typeof vi.fn>).mockResolvedValue({
        ...makeFullJob(overrides),
        documents: [cvDoc],
      });
    }

    it("saves the CV as a new deck, refreshes the deck list, and confirms with its name", async () => {
      const user = userEvent.setup();
      withCvDoc();
      (api.saveCvAsDeck as ReturnType<typeof vi.fn>).mockResolvedValue({
        id: "d".repeat(32),
        name: "Acme · Engineer",
        auto_title: "Jane Doe",
        has_cv: true,
        is_default: false,
        in_use_by: 0,
      });
      useStore.setState({ jobs: { job1: makeJob({ state: "review" }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      await user.click(await screen.findByRole("button", { name: /save as base cv/i }));

      await waitFor(() => {
        expect(screen.getByText(/saved as base cv “Acme · Engineer”/i)).toBeInTheDocument();
      });
      expect(api.saveCvAsDeck).toHaveBeenCalledWith("job1");
      expect(api.listCvDecks).toHaveBeenCalled();
      expect(useStore.getState().cvStructureExists).toBe(true);
      // A pure copy — it must not approve anything.
      expect(api.approve).not.toHaveBeenCalled();
    });

    it("shows the server's error when the save fails", async () => {
      const user = userEvent.setup();
      withCvDoc();
      (api.saveCvAsDeck as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error("This CV was stored without its structured form")
      );
      useStore.setState({ jobs: { job1: makeJob({ state: "review" }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      await user.click(await screen.findByRole("button", { name: /save as base cv/i }));

      await waitFor(() => {
        expect(screen.getByText(/stored without its structured form/i)).toBeInTheDocument();
      });
    });

    it("is available while the pane is read-only (a lane is running)", async () => {
      withCvDoc({ state: "running", current_stage: "cover_letter" });
      useStore.setState({
        jobs: { job1: makeJob({ state: "running", current_stage: "cover_letter" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      expect(await screen.findByRole("button", { name: /save as base cv/i })).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /approve cv/i })).toBeNull();
    });

    it("is available on an approved job", async () => {
      withCvDoc({ state: "approved" });
      useStore.setState({ jobs: { job1: makeJob({ state: "approved" }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      expect(await screen.findByRole("button", { name: /save as base cv/i })).toBeInTheDocument();
    });

    it("is hidden before the CV is written (no cv_adjust document)", async () => {
      useStore.setState({
        jobs: { job1: makeJob({ state: "running", current_stage: "cv_adjust" }) },
        selectedId: "job1",
      });

      render(<ReviewPane jobId="job1" mode="cv-gate" />);

      await waitFor(() => expect(api.getJob).toHaveBeenCalled());
      await screen.findByText(/cv lane is running/i);
      expect(screen.queryByRole("button", { name: /save as base cv/i })).toBeNull();
    });

    it("is hidden on the COVER_LETTER tab", async () => {
      const user = userEvent.setup();
      withCvDoc();
      useStore.setState({ jobs: { job1: makeJob({ state: "review" }) }, selectedId: "job1" });

      render(<ReviewPane jobId="job1" />);

      await screen.findByRole("button", { name: /save as base cv/i });
      await user.click(screen.getByRole("button", { name: "COVER_LETTER" }));
      expect(screen.queryByRole("button", { name: /save as base cv/i })).toBeNull();
    });
  });
});
