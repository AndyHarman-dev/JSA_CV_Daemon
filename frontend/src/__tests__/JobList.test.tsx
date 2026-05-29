import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useStore } from "../store";
import { JobList } from "../components/JobList";
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

beforeEach(() => {
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
  });
});

describe("JobList", () => {
  it("shows 'No jobs loaded.' message when there are no jobs", () => {
    render(<JobList />);
    expect(screen.getByText("No jobs loaded.")).toBeInTheDocument();
  });

  it("shows no group headers when there are no jobs", () => {
    render(<JobList />);
    expect(screen.queryByText("Inbox")).toBeNull();
    expect(screen.queryByText("Running")).toBeNull();
    expect(screen.queryByText("Done")).toBeNull();
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("shows a pending job under the Running section", () => {
    const job = makeJob({ id: "j1", state: "pending", company: "Beta", role: "Dev" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByText("Running")).toBeInTheDocument();
    expect(screen.getByText("Beta — Dev")).toBeInTheDocument();
  });

  it("shows a running job under the Running section", () => {
    const job = makeJob({ id: "j1", state: "running", company: "Corp", role: "Lead" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    // "Running" appears both as group header (h2) and as StatusBadge text
    const runningElements = screen.getAllByText("Running");
    expect(runningElements.length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Corp — Lead")).toBeInTheDocument();
  });

  it("shows an awaiting_input job under the Inbox section", () => {
    const job = makeJob({
      id: "j1",
      state: "awaiting_input",
      company: "Inbox Co",
      role: "Analyst",
    });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByText("Inbox")).toBeInTheDocument();
    expect(screen.getByText("Inbox Co — Analyst")).toBeInTheDocument();
  });

  it("shows an approved job under the Done section", () => {
    const job = makeJob({
      id: "j1",
      state: "approved",
      company: "Done Inc",
      role: "Manager",
    });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByText("Done")).toBeInTheDocument();
    expect(screen.getByText("Done Inc — Manager")).toBeInTheDocument();
  });

  it("shows a failed job under the Failed section", () => {
    const job = makeJob({
      id: "j1",
      state: "failed",
      company: "Fail Ltd",
      role: "QA",
    });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    // "Failed" appears both as group header (h2) and as StatusBadge text
    const failedElements = screen.getAllByText("Failed");
    expect(failedElements.length).toBeGreaterThanOrEqual(1);
    // Verify the group header is an h2
    const h2 = failedElements.find((el) => el.tagName === "H2");
    expect(h2).toBeDefined();
    expect(screen.getByText("Fail Ltd — QA")).toBeInTheDocument();
  });

  it("shows a review job under the Review section", () => {
    const job = makeJob({
      id: "j1",
      state: "review",
      company: "Review Corp",
      role: "Scientist",
    });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    // "Review" appears both as group header (h2) and as StatusBadge text
    const reviewElements = screen.getAllByText("Review");
    expect(reviewElements.length).toBeGreaterThanOrEqual(1);
    const h2 = reviewElements.find((el) => el.tagName === "H2");
    expect(h2).toBeDefined();
    expect(screen.getByText("Review Corp — Scientist")).toBeInTheDocument();
  });

  it("clicking a job row calls selectJob with the correct id", () => {
    const job = makeJob({ id: "clickable", company: "Click Co", role: "Dev" });
    useStore.setState({ jobs: { clickable: job } });

    render(<JobList />);

    const btn = screen.getByText("Click Co — Dev").closest("button");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);

    expect(useStore.getState().selectedId).toBe("clickable");
  });

  it("selected job row has highlighted background classes", () => {
    const job = makeJob({ id: "sel1", company: "Selected Co", role: "Dev" });
    useStore.setState({ jobs: { sel1: job }, selectedId: "sel1" });

    render(<JobList />);

    const btn = screen.getByText("Selected Co — Dev").closest("button");
    expect(btn).not.toBeNull();
    expect(btn).toHaveClass("bg-blue-50");
    expect(btn).toHaveClass("border-blue-200");
  });

  it("non-selected job row does not have highlighted background classes", () => {
    const job1 = makeJob({ id: "j1", company: "Selected Co", role: "Dev" });
    const job2 = makeJob({ id: "j2", company: "Other Co", role: "Eng" });
    useStore.setState({ jobs: { j1: job1, j2: job2 }, selectedId: "j1" });

    render(<JobList />);

    const btn = screen.getByText("Other Co — Eng").closest("button");
    expect(btn).not.toBeNull();
    expect(btn).not.toHaveClass("bg-blue-50");
  });

  it("jobs are grouped correctly when multiple states are present", () => {
    const pendingJob = makeJob({ id: "p1", state: "pending", company: "P Co", role: "Dev" });
    const inboxJob = makeJob({
      id: "i1",
      state: "awaiting_input",
      company: "I Co",
      role: "Eng",
    });
    useStore.setState({ jobs: { p1: pendingJob, i1: inboxJob } });

    render(<JobList />);

    expect(screen.getByText("Inbox")).toBeInTheDocument();
    // "Running" may appear as group header + badge; verify the h2 header is present
    const runningEls = screen.getAllByText("Running");
    const h2Running = runningEls.find((el) => el.tagName === "H2");
    expect(h2Running).toBeDefined();
    expect(screen.queryByText("Done")).toBeNull();
    // "Failed" is only in the badge, not as a group header — so query specifically
    const failedH2 = screen.queryAllByText("Failed").find((el) => el.tagName === "H2");
    expect(failedH2).toBeUndefined();
  });
});
