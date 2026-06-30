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

/** Group headers render their label text inside an h2 (the StatusBadge text — when it
 *  happens to share a word, e.g. "Review" — is uppercase, so plain getByText is safe). */
function groupHeader(label: string): HTMLElement {
  const el = screen.getByText(label);
  expect(el.closest("h2")).not.toBeNull();
  return el;
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

    groupHeader("Running");
    expect(screen.getByText("Beta")).toBeInTheDocument();
    expect(screen.getByText("Dev")).toBeInTheDocument();
  });

  it("shows a running job under the Running section", () => {
    const job = makeJob({ id: "j1", state: "running", company: "Corp", role: "Lead" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    groupHeader("Running");
    expect(screen.getByText("Corp")).toBeInTheDocument();
    expect(screen.getByText("Lead")).toBeInTheDocument();
    // StatusBadge renders the uppercase "RUNNING" label distinct from the group header
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
  });

  it("shows an unfit job under the Needs Review section", () => {
    const job = makeJob({ id: "j1", state: "unfit", company: "Bad Robot", role: "Principal" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    groupHeader("Needs Review");
    expect(screen.getByText("Bad Robot")).toBeInTheDocument();
    expect(screen.getByText("Principal")).toBeInTheDocument();
    // StatusBadge renders the uppercase "NEEDS REVIEW" label
    expect(screen.getByText("NEEDS REVIEW")).toBeInTheDocument();
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

    groupHeader("Inbox");
    expect(screen.getByText("Inbox Co")).toBeInTheDocument();
    expect(screen.getByText("Analyst")).toBeInTheDocument();
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

    groupHeader("Done");
    expect(screen.getByText("Done Inc")).toBeInTheDocument();
    expect(screen.getByText("Manager")).toBeInTheDocument();
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

    groupHeader("Failed");
    expect(screen.getByText("Fail Ltd")).toBeInTheDocument();
    expect(screen.getByText("QA")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toBeInTheDocument();
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

    groupHeader("Review");
    expect(screen.getByText("Review Corp")).toBeInTheDocument();
    expect(screen.getByText("Scientist")).toBeInTheDocument();
    expect(screen.getByText("REVIEW")).toBeInTheDocument();
  });

  it("clicking a job row calls selectJob with the correct id", () => {
    const job = makeJob({ id: "clickable", company: "Click Co", role: "Dev" });
    useStore.setState({ jobs: { clickable: job } });

    render(<JobList />);

    const btn = screen.getByText("Click Co").closest("button");
    expect(btn).not.toBeNull();
    fireEvent.click(btn!);

    expect(useStore.getState().selectedId).toBe("clickable");
  });

  it("selected job row has an accent left-bar and accent border", () => {
    const job = makeJob({ id: "sel1", company: "Selected Co", role: "Dev" });
    useStore.setState({ jobs: { sel1: job }, selectedId: "sel1" });

    render(<JobList />);

    const btn = screen.getByText("Selected Co").closest("button");
    expect(btn).not.toBeNull();
    // Accent left-bar marker is only rendered when selected
    expect(btn!.querySelector("span")).not.toBeNull();
    expect(btn).toHaveStyle({ background: "rgb(16, 21, 29)" }); // T.surface
  });

  it("non-selected job row does not have the accent left-bar marker", () => {
    const job1 = makeJob({ id: "j1", company: "Selected Co", role: "Dev" });
    const job2 = makeJob({ id: "j2", company: "Other Co", role: "Eng" });
    useStore.setState({ jobs: { j1: job1, j2: job2 }, selectedId: "j1" });

    render(<JobList />);

    const btn = screen.getByText("Other Co").closest("button");
    expect(btn).not.toBeNull();
    expect(btn).toHaveStyle({ background: "transparent" });
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

    groupHeader("Inbox");
    groupHeader("Running");
    expect(screen.queryByText("Done")).toBeNull();
    // "Failed" is not present anywhere — no failed jobs and no group header
    expect(screen.queryByText("Failed")).toBeNull();
  });
});
