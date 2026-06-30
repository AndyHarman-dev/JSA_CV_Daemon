import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { StageTimeline } from "../components/StageTimeline";
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
    expect(getAllByText("CV_DONE").length).toBeGreaterThan(0);
    expect(getAllByText("COVER_LETTER").length).toBeGreaterThan(0);
    expect(getAllByText("CL_DONE").length).toBeGreaterThan(0);
    expect(getAllByText("REVIEW").length).toBeGreaterThan(0);
    expect(getAllByText("APPROVED").length).toBeGreaterThan(0);
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
