import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "../components/StatusBadge";

describe("StatusBadge", () => {
  it('renders "Pending" for state "pending"', () => {
    render(<StatusBadge state="pending" />);
    expect(screen.getByText("Pending")).toBeInTheDocument();
  });

  it('renders "Running" for state "running"', () => {
    render(<StatusBadge state="running" />);
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it('renders a spinner svg element for state "running"', () => {
    const { container } = render(<StatusBadge state="running" />);
    const svg = container.querySelector("svg");
    expect(svg).not.toBeNull();
    expect(svg).toHaveClass("animate-spin");
  });

  it('renders "Needs Input" for state "awaiting_input"', () => {
    render(<StatusBadge state="awaiting_input" />);
    expect(screen.getByText("Needs Input")).toBeInTheDocument();
  });

  it('renders "Approved" for state "approved"', () => {
    render(<StatusBadge state="approved" />);
    expect(screen.getByText("Approved")).toBeInTheDocument();
  });

  it('renders "Failed" for state "failed"', () => {
    render(<StatusBadge state="failed" />);
    expect(screen.getByText("Failed")).toBeInTheDocument();
  });

  it('renders "CV Done" for state "cv_done"', () => {
    render(<StatusBadge state="cv_done" />);
    expect(screen.getByText("CV Done")).toBeInTheDocument();
  });

  it('renders "CL Done" for state "cl_done"', () => {
    render(<StatusBadge state="cl_done" />);
    expect(screen.getByText("CL Done")).toBeInTheDocument();
  });

  it('renders "Review" for state "review"', () => {
    render(<StatusBadge state="review" />);
    expect(screen.getByText("Review")).toBeInTheDocument();
  });

  it("does not render a spinner for non-running states", () => {
    const { container } = render(<StatusBadge state="pending" />);
    expect(container.querySelector("svg")).toBeNull();
  });

  it("applies additional className prop to the span", () => {
    const { container } = render(
      <StatusBadge state="pending" className="extra-class" />
    );
    const span = container.querySelector("span");
    expect(span).toHaveClass("extra-class");
  });
});
