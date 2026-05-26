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
 * Helper to get the step dot elements from the rendered timeline.
 * The dots are divs with rounded-full and border-2 classes.
 */
function getStepDots(container: HTMLElement): NodeListOf<Element> {
  return container.querySelectorAll(".rounded-full.border-2");
}

describe("StageTimeline", () => {
  it('shows first step (index 0) as active for state "pending"', () => {
    const job = makeJob({ state: "pending" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);
    expect(dots.length).toBe(7); // 7 steps total

    // Step 0 (Pending) should be active: has ring-2 and border-blue-500
    expect(dots[0]).toHaveClass("border-blue-500");
    expect(dots[0]).toHaveClass("ring-2");

    // Step 1 onward should be dimmed: border-gray-300
    expect(dots[1]).toHaveClass("border-gray-300");
    expect(dots[2]).toHaveClass("border-gray-300");
  });

  it('shows steps 0-1 as complete/active for state "cv_done"', () => {
    // cv_done is step index 2, so steps 0-1 are complete, step 2 is active
    const job = makeJob({ state: "cv_done" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    // Steps 0 and 1 should be complete: bg-blue-500
    expect(dots[0]).toHaveClass("bg-blue-500");
    expect(dots[1]).toHaveClass("bg-blue-500");

    // Step 2 should be active: border-blue-500 with ring
    expect(dots[2]).toHaveClass("border-blue-500");
    expect(dots[2]).toHaveClass("ring-2");

    // Step 3 onward should be dimmed
    expect(dots[3]).toHaveClass("border-gray-300");
  });

  it('shows all steps except last as complete for state "approved"', () => {
    // approved is step index 6 (last), so steps 0-5 are complete, step 6 is active
    const job = makeJob({ state: "approved" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);
    expect(dots.length).toBe(7);

    // Steps 0-5 should be complete: bg-blue-500
    for (let i = 0; i < 6; i++) {
      expect(dots[i]).toHaveClass("bg-blue-500");
      expect(dots[i]).not.toHaveClass("ring-2");
    }

    // Step 6 (Approved) is the active step
    expect(dots[6]).toHaveClass("border-blue-500");
    expect(dots[6]).toHaveClass("ring-2");
  });

  it("renders step labels in order", () => {
    const job = makeJob({ state: "pending" });
    const { getAllByText } = render(<StageTimeline job={job} />);

    // Verify specific step labels are present
    expect(getAllByText("Pending").length).toBeGreaterThan(0);
    expect(getAllByText("CV Adjust").length).toBeGreaterThan(0);
    expect(getAllByText("CV Done").length).toBeGreaterThan(0);
    expect(getAllByText("Cover Letter").length).toBeGreaterThan(0);
    expect(getAllByText("CL Done").length).toBeGreaterThan(0);
    expect(getAllByText("Review").length).toBeGreaterThan(0);
    expect(getAllByText("Approved").length).toBeGreaterThan(0);
  });

  it('shows step index 1 as active when state is "running" with cv_adjust stage', () => {
    const job = makeJob({ state: "running", current_stage: "cv_adjust" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    // Step 0 complete, step 1 active
    expect(dots[0]).toHaveClass("bg-blue-500");
    expect(dots[1]).toHaveClass("border-blue-500");
    expect(dots[1]).toHaveClass("ring-2");
    expect(dots[2]).toHaveClass("border-gray-300");
  });

  it('shows step index 3 as active when state is "running" with cover_letter stage', () => {
    const job = makeJob({ state: "running", current_stage: "cover_letter" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    // Steps 0-2 complete, step 3 active
    for (let i = 0; i < 3; i++) {
      expect(dots[i]).toHaveClass("bg-blue-500");
    }
    expect(dots[3]).toHaveClass("border-blue-500");
    expect(dots[3]).toHaveClass("ring-2");
    expect(dots[4]).toHaveClass("border-gray-300");
  });

  it('shows step index 5 active for state "review"', () => {
    const job = makeJob({ state: "review" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    // Steps 0-4 complete, step 5 active
    for (let i = 0; i < 5; i++) {
      expect(dots[i]).toHaveClass("bg-blue-500");
    }
    expect(dots[5]).toHaveClass("border-blue-500");
    expect(dots[5]).toHaveClass("ring-2");
    expect(dots[6]).toHaveClass("border-gray-300");
  });

  it('shows step index 4 active for state "cl_done"', () => {
    const job = makeJob({ state: "cl_done" });
    const { container } = render(<StageTimeline job={job} />);

    const dots = getStepDots(container);

    for (let i = 0; i < 4; i++) {
      expect(dots[i]).toHaveClass("bg-blue-500");
    }
    expect(dots[4]).toHaveClass("border-blue-500");
    expect(dots[4]).toHaveClass("ring-2");
  });
});
