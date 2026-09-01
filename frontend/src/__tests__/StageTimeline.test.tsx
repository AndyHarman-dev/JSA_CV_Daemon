import { describe, it, expect, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StageTimeline } from "../components/StageTimeline";
import { useStore } from "../store";
import type { JobDTO } from "../types";

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    state: "pending",
    current_stage: null,
    backend_name: null,
    error: null,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/**
 * Helper to get the step node elements (`data-testid="stage-dot"`) from the rendered
 * timeline, in order. Each carries `data-state` of "complete" | "active" | "pending".
 */
function getStepDots(container: HTMLElement): Element[] {
  return Array.from(container.querySelectorAll('[data-testid="stage-dot"]'));
}

beforeEach(() => {
  useStore.setState({ viewedStage: null });
});

describe("StageTimeline", () => {
  it('shows first step (index 0) as active for state "pending"', () => {
    const job = makeJob({ state: "pending" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);
    expect(dots.length).toBe(7); // 7 steps total

    // Step 0 (Pending) should be active
    expect(dots[0]).toHaveAttribute("data-state", "active");

    // Step 1 onward should be dimmed (pending)
    expect(dots[1]).toHaveAttribute("data-state", "pending");
    expect(dots[2]).toHaveAttribute("data-state", "pending");
  });

  it('shows steps 0-1 as complete/active for state "cv_done"', () => {
    // cv_done is step index 2, so steps 0-1 are complete, step 2 is active
    const job = makeJob({ state: "cv_done" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    expect(dots[0]).toHaveAttribute("data-state", "complete");
    expect(dots[1]).toHaveAttribute("data-state", "complete");
    expect(dots[2]).toHaveAttribute("data-state", "active");
    expect(dots[3]).toHaveAttribute("data-state", "pending");
  });

  it('shows all steps except last as complete for state "approved"', () => {
    // approved is step index 6 (last), so steps 0-5 are complete, step 6 is active
    const job = makeJob({ state: "approved" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);
    expect(dots.length).toBe(7);

    for (let i = 0; i < 6; i++) {
      expect(dots[i]).toHaveAttribute("data-state", "complete");
    }
    expect(dots[6]).toHaveAttribute("data-state", "active");
  });

  it("renders step labels in order", () => {
    const job = makeJob({ state: "pending" });
    const { getAllByText } = render(<StageTimeline job={job} />);

    expect(getAllByText("PENDING").length).toBeGreaterThan(0);
    expect(getAllByText("CV_ADJUST").length).toBeGreaterThan(0);
    expect(getAllByText("CV_REVIEW").length).toBeGreaterThan(0);
    expect(getAllByText("COVER_LETTER").length).toBeGreaterThan(0);
    expect(getAllByText("CL_DONE").length).toBeGreaterThan(0);
    expect(getAllByText("REVIEW").length).toBeGreaterThan(0);
    expect(getAllByText("APPROVED").length).toBeGreaterThan(0);
  });

  it("renders only the CV_ADJUST and COVER_LETTER nodes as buttons; the other five stay plain divs", () => {
    // A "review" job so the CL button is enabled too — this test is about element type,
    // not disabled state.
    const job = makeJob({ state: "review" });
    render(<StageTimeline job={job} />);

    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /COVER_LETTER/ })).toBeInTheDocument();
    // Only those two — no other step label sits inside a <button>.
    expect(screen.getAllByRole("button")).toHaveLength(2);
  });

  it("disables the COVER_LETTER button while no cover-letter Document exists yet", () => {
    const job = makeJob({ state: "cv_review" });
    render(<StageTimeline job={job} />);

    expect(screen.getByRole("button", { name: /COVER_LETTER/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toBeEnabled();
  });

  it("disables the CV_ADJUST button while no cv_adjust Document exists yet", () => {
    const job = makeJob({ state: "pending" });
    render(<StageTimeline job={job} />);

    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toHaveAttribute(
      "title",
      "CV not started yet"
    );
  });

  it("enables the COVER_LETTER button while the cover-letter lane is actively running — the click-back in verification step (f) must work mid-run, not just after review", () => {
    const job = makeJob({ state: "running", current_stage: "cover_letter" });
    render(<StageTimeline job={job} />);

    expect(screen.getByRole("button", { name: /COVER_LETTER/ })).toBeEnabled();
  });

  it("enables the COVER_LETTER button once the job has reached review", () => {
    const job = makeJob({ state: "review" });
    render(<StageTimeline job={job} />);

    expect(screen.getByRole("button", { name: /COVER_LETTER/ })).toBeEnabled();
  });

  it("clicking a dot sets the store's viewedStage, and clicking it again clears it", async () => {
    const user = userEvent.setup();
    const job = makeJob({ state: "review" });
    render(<StageTimeline job={job} />);

    await user.click(screen.getByRole("button", { name: /CV_ADJUST/ }));
    expect(useStore.getState().viewedStage).toBe("cv");
    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toHaveAttribute("aria-pressed", "true");

    await user.click(screen.getByRole("button", { name: /COVER_LETTER/ }));
    expect(useStore.getState().viewedStage).toBe("cl");
    expect(screen.getByRole("button", { name: /CV_ADJUST/ })).toHaveAttribute("aria-pressed", "false");

    await user.click(screen.getByRole("button", { name: /COVER_LETTER/ }));
    expect(useStore.getState().viewedStage).toBeNull();
  });

  it('shows step index 1 as active when state is "running" with cv_adjust stage', () => {
    const job = makeJob({ state: "running", current_stage: "cv_adjust" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    expect(dots[0]).toHaveAttribute("data-state", "complete");
    expect(dots[1]).toHaveAttribute("data-state", "active");
    expect(dots[2]).toHaveAttribute("data-state", "pending");
  });

  it('shows step index 3 as active when state is "running" with cover_letter stage', () => {
    const job = makeJob({ state: "running", current_stage: "cover_letter" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    for (let i = 0; i < 3; i++) {
      expect(dots[i]).toHaveAttribute("data-state", "complete");
    }
    expect(dots[3]).toHaveAttribute("data-state", "active");
    expect(dots[4]).toHaveAttribute("data-state", "pending");
  });

  it('shows step index 5 active for state "review"', () => {
    const job = makeJob({ state: "review" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    for (let i = 0; i < 5; i++) {
      expect(dots[i]).toHaveAttribute("data-state", "complete");
    }
    expect(dots[5]).toHaveAttribute("data-state", "active");
    expect(dots[6]).toHaveAttribute("data-state", "pending");
  });

  it('shows step index 4 active for state "cl_done"', () => {
    const job = makeJob({ state: "cl_done" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    for (let i = 0; i < 4; i++) {
      expect(dots[i]).toHaveAttribute("data-state", "complete");
    }
    expect(dots[4]).toHaveAttribute("data-state", "active");
  });
});
