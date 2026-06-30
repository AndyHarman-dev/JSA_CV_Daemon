import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "../components/StatusBadge";

describe("StatusBadge", () => {
  it('renders "PENDING" for state "pending"', () => {
    render(<StatusBadge state="pending" />);
    expect(screen.getByText("PENDING")).toBeInTheDocument();
  });

  it('renders "RUNNING" for state "running"', () => {
    render(<StatusBadge state="running" />);
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it('renders a spinning indicator dot for state "running"', () => {
    const { container } = render(<StatusBadge state="running" />);
    const dot = container.querySelector('span > span');
    expect(dot).not.toBeNull();
    expect(dot).toHaveStyle({ animation: "jsspin .7s linear infinite" });
  });

  it('renders "NEEDS INPUT" for state "awaiting_input"', () => {
    render(<StatusBadge state="awaiting_input" />);
    expect(screen.getByText("NEEDS INPUT")).toBeInTheDocument();
  });

  it('renders "APPROVED" for state "approved"', () => {
    render(<StatusBadge state="approved" />);
    expect(screen.getByText("APPROVED")).toBeInTheDocument();
  });

  it('renders "FAILED" for state "failed"', () => {
    render(<StatusBadge state="failed" />);
    expect(screen.getByText("FAILED")).toBeInTheDocument();
  });

  it('renders "CV DONE" for state "cv_done"', () => {
    render(<StatusBadge state="cv_done" />);
    expect(screen.getByText("CV DONE")).toBeInTheDocument();
  });

  it('renders "CL DONE" for state "cl_done"', () => {
    render(<StatusBadge state="cl_done" />);
    expect(screen.getByText("CL DONE")).toBeInTheDocument();
  });

  it('renders "REVIEW" for state "review"', () => {
    render(<StatusBadge state="review" />);
    expect(screen.getByText("REVIEW")).toBeInTheDocument();
  });

  it("does not render a spinning indicator for non-running states", () => {
    const { container } = render(<StatusBadge state="pending" />);
    const dot = container.querySelector('span > span');
    expect(dot).not.toHaveStyle({ animation: "jsspin .7s linear infinite" });
  });

  it("applies additional className prop to the span", () => {
    const { container } = render(
      <StatusBadge state="pending" className="extra-class" />
    );
    const span = container.querySelector("span");
    expect(span).toHaveClass("extra-class");
  });
});
