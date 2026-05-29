import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReviewPane } from "../components/ReviewPane";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    getDocument: vi.fn(),
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
    state: "review",
    current_stage: null,
    error: null,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
});

describe("ReviewPane", () => {
  it("shows 'Loading…' initially while documents are fetching", () => {
    // Never resolve so we stay in loading state
    (api.getDocument as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    expect(screen.getByText("Loading…")).toBeInTheDocument();
  });

  it("shows tab bar with CV / Resume and Cover Letter tabs after loading", async () => {
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# CV Content",
      version: 1,
    });

    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    // Tab buttons are always rendered (not behind loading state)
    expect(screen.getByRole("button", { name: "CV / Resume" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cover Letter" })).toBeInTheDocument();
  });

  it("switches to Cover Letter tab when clicked", async () => {
    const user = userEvent.setup();
    (api.getDocument as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce({ markdown: "# CV", version: 1 })      // cv_adjust
      .mockResolvedValueOnce({ markdown: "# Cover Letter", version: 1 }); // cover_letter

    const job = makeJob();
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    // Wait for CV content to load
    await waitFor(() => {
      expect(screen.queryByText("Loading…")).toBeNull();
    });

    await user.click(screen.getByRole("button", { name: "Cover Letter" }));

    // After switching, the CL content renders (or its loading/error state)
    // The CL tab button is now active
    const clButton = screen.getByRole("button", { name: "Cover Letter" });
    expect(clButton.className).toContain("border-blue-500");
  });

  it("shows 'Approve & Export PDFs' button when state is 'review'", async () => {
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });

    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /Approve & Export PDFs/i })
      ).toBeInTheDocument();
    });
  });

  it("does not show approve button when state is 'approved'", async () => {
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });

    const job = makeJob({ state: "approved" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: /Approve & Export PDFs/i })
      ).toBeNull();
    });
  });

  it("shows '✓ Approved' banner when state is 'approved'", async () => {
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });

    const job = makeJob({ state: "approved" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    await waitFor(() => {
      expect(screen.getByText(/✓ Approved/)).toBeInTheDocument();
    });
  });

  it("calls api.approve with the correct jobId when Approve button is clicked", async () => {
    const user = userEvent.setup();
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });
    (api.approve as ReturnType<typeof vi.fn>).mockResolvedValue({
      cv_pdf_path: "/out/cv.pdf",
      cl_pdf_path: "/out/cl.pdf",
    });

    const job = makeJob({ id: "job1", state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    const approveButton = await screen.findByRole("button", {
      name: /Approve & Export PDFs/i,
    });
    await user.click(approveButton);

    await waitFor(() => {
      expect(api.approve).toHaveBeenCalledWith("job1");
    });
  });

  it("shows error message when api.approve throws", async () => {
    const user = userEvent.setup();
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });
    (api.approve as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("approval failed")
    );

    const job = makeJob({ state: "review" });
    useStore.setState({ jobs: { job1: job }, selectedId: "job1" });

    render(<ReviewPane jobId="job1" />);

    const approveButton = await screen.findByRole("button", {
      name: /Approve & Export PDFs/i,
    });
    await user.click(approveButton);

    await waitFor(() => {
      expect(screen.getByText("approval failed")).toBeInTheDocument();
    });
  });

  it("renders Request Revision ChatBox in review state", async () => {
    (api.getDocument as ReturnType<typeof vi.fn>).mockResolvedValue({
      markdown: "# Content",
      version: 1,
    });

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
