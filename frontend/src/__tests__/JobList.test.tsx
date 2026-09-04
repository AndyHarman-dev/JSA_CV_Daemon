import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { useStore } from "../store";
import { JobList } from "../components/JobList";
import type { JobDTO } from "../types";
import { SHELL_THEME } from "../theme/tokens";

const T = SHELL_THEME;

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
    base_cv_id: null,
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

/** A job row's StatusBadge text (e.g. "RUNNING") can collide with the queue filter chips,
 *  which deliberately reuse the same short uppercase words — so scope to inside a .jrow. */
function jobRowBadge(label: string): HTMLElement | undefined {
  return screen.queryAllByText(label).find((el) => el.closest("button.jrow"));
}

/** The filter chip button itself (as opposed to a same-worded StatusBadge, which is a
 *  <span> inside a .jrow, never a top-level .jbtn button). */
function filterChip(label: string): HTMLElement {
  const el = screen
    .getAllByText(label)
    .find((e) => e.tagName === "BUTTON" && e.className.includes("jbtn"));
  expect(el).not.toBeUndefined();
  return el!;
}

beforeEach(() => {
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
    cvStructureExists: null,
    cvPickerJobId: null,
    cvPickerPos: { x: 0, y: 0 },
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
    expect(jobRowBadge("RUNNING")).not.toBeUndefined();
  });

  it("shows the effective model on the job row when set", () => {
    const job = makeJob({
      id: "j1",
      state: "running",
      company: "Corp",
      role: "Lead",
      effective_model: "kimi-k2.6",
    });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByText("kimi-k2.6")).toBeInTheDocument();
  });

  it("shows no model text on the job row when effective_model is absent", () => {
    const job = makeJob({ id: "j1", state: "running", company: "Corp", role: "Lead" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    // Only the status badge and company/role text should render — no stray model chip.
    expect(jobRowBadge("RUNNING")).not.toBeUndefined();
  });

  it("shows an unfit job under the Needs Review section", () => {
    const job = makeJob({ id: "j1", state: "unfit", company: "Bad Robot", role: "Principal" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    groupHeader("Needs Review");
    expect(screen.getByText("Bad Robot")).toBeInTheDocument();
    expect(screen.getByText("Principal")).toBeInTheDocument();
    // StatusBadge renders the uppercase "NEEDS REVIEW" label
    expect(jobRowBadge("NEEDS REVIEW")).not.toBeUndefined();
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
    expect(jobRowBadge("FAILED")).not.toBeUndefined();
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
    expect(jobRowBadge("REVIEW")).not.toBeUndefined();
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

  it("shows a queued job under its own 'Queued — Not Started' section with a LAUNCH control", () => {
    const job = makeJob({ id: "j1", state: "queued", company: "Fresh Co", role: "Grad" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    groupHeader("Queued — Not Started");
    expect(screen.getByText("Fresh Co")).toBeInTheDocument();
    // The LAUNCH pill replaces the usual StatusBadge for a queued row.
    expect(screen.getByText("LAUNCH")).toBeInTheDocument();
    expect(jobRowBadge("QUEUED")).toBeUndefined();
  });

  it("a single queued job does not show the Launch All action", () => {
    const job = makeJob({ id: "j1", state: "queued", company: "Solo Co", role: "Dev" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.queryByText("LAUNCH ALL")).not.toBeInTheDocument();
  });

  it("multiple queued jobs show a Launch All action that calls store.launchAll", () => {
    const launchAll = vi.fn().mockResolvedValue(undefined);
    const job1 = makeJob({ id: "j1", state: "queued", company: "Co A", role: "Dev" });
    const job2 = makeJob({ id: "j2", state: "queued", company: "Co B", role: "Eng" });
    useStore.setState({ jobs: { j1: job1, j2: job2 }, launchAll });

    render(<JobList />);

    const btn = screen.getByText("LAUNCH ALL");
    fireEvent.click(btn);
    expect(launchAll).toHaveBeenCalledTimes(1);
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

  it("filters jobs by company name (case-insensitive)", () => {
    const acme = makeJob({ id: "a1", company: "Acme Corp", role: "Engineer" });
    const beta = makeJob({ id: "b1", company: "Beta Inc", role: "Designer" });
    useStore.setState({ jobs: { a1: acme, b1: beta } });

    render(<JobList />);
    fireEvent.change(screen.getByPlaceholderText("Search company or role…"), {
      target: { value: "acme" },
    });

    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.queryByText("Beta Inc")).toBeNull();
  });

  it("filters jobs by role", () => {
    const acme = makeJob({ id: "a1", company: "Acme Corp", role: "Engineer" });
    const beta = makeJob({ id: "b1", company: "Beta Inc", role: "Designer" });
    useStore.setState({ jobs: { a1: acme, b1: beta } });

    render(<JobList />);
    fireEvent.change(screen.getByPlaceholderText("Search company or role…"), {
      target: { value: "design" },
    });

    expect(screen.getByText("Beta Inc")).toBeInTheDocument();
    expect(screen.queryByText("Acme Corp")).toBeNull();
  });

  it("shows a 'no matches' message when the search excludes every job", () => {
    const acme = makeJob({ id: "a1", company: "Acme Corp", role: "Engineer" });
    useStore.setState({ jobs: { a1: acme } });

    render(<JobList />);
    fireEvent.change(screen.getByPlaceholderText("Search company or role…"), {
      target: { value: "nonexistent" },
    });

    expect(screen.getByText("No jobs match your search.")).toBeInTheDocument();
    expect(screen.queryByText("Acme Corp")).toBeNull();
  });

  it("clears the search via the clear button and restores all jobs", () => {
    const acme = makeJob({ id: "a1", company: "Acme Corp", role: "Engineer" });
    const beta = makeJob({ id: "b1", company: "Beta Inc", role: "Designer" });
    useStore.setState({ jobs: { a1: acme, b1: beta } });

    render(<JobList />);
    const input = screen.getByPlaceholderText("Search company or role…");
    fireEvent.change(input, { target: { value: "acme" } });
    expect(screen.queryByText("Beta Inc")).toBeNull();

    fireEvent.click(screen.getByTitle("Clear"));

    expect(input).toHaveValue("");
    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.getByText("Beta Inc")).toBeInTheDocument();
  });

  it("shows a filter chip with a count for each group that has jobs, and none for empty groups", () => {
    const running = makeJob({ id: "r1", state: "running", company: "Run Co", role: "Dev" });
    const failed = makeJob({ id: "f1", state: "failed", company: "Fail Co", role: "Dev" });
    useStore.setState({ jobs: { r1: running, f1: failed } });

    render(<JobList />);

    const runningChip = filterChip("RUNNING");
    expect(runningChip).toHaveTextContent("1");
    filterChip("FAILED");
    expect(screen.queryByText("DONE")).toBeNull();
    expect(screen.queryByText("DISMISSED")).toBeNull();
  });

  it("clicking a filter chip narrows the list to that group only", () => {
    const running = makeJob({ id: "r1", state: "running", company: "Run Co", role: "Dev" });
    const failed = makeJob({ id: "f1", state: "failed", company: "Fail Co", role: "Dev" });
    useStore.setState({ jobs: { r1: running, f1: failed } });

    render(<JobList />);
    fireEvent.click(filterChip("FAILED"));

    expect(screen.getByText("Fail Co")).toBeInTheDocument();
    expect(screen.queryByText("Run Co")).toBeNull();
    expect(screen.queryByText("Running")).toBeNull(); // group header gone
  });

  it("clicking an active filter chip again deselects it and restores all groups", () => {
    const running = makeJob({ id: "r1", state: "running", company: "Run Co", role: "Dev" });
    const failed = makeJob({ id: "f1", state: "failed", company: "Fail Co", role: "Dev" });
    useStore.setState({ jobs: { r1: running, f1: failed } });

    render(<JobList />);
    fireEvent.click(filterChip("FAILED"));
    fireEvent.click(filterChip("FAILED"));

    expect(screen.getByText("Fail Co")).toBeInTheDocument();
    expect(screen.getByText("Run Co")).toBeInTheDocument();
  });

  it("selecting multiple filter chips shows the union of their groups", () => {
    const running = makeJob({ id: "r1", state: "running", company: "Run Co", role: "Dev" });
    const failed = makeJob({ id: "f1", state: "failed", company: "Fail Co", role: "Dev" });
    const done = makeJob({ id: "d1", state: "approved", company: "Done Co", role: "Dev" });
    useStore.setState({ jobs: { r1: running, f1: failed, d1: done } });

    render(<JobList />);
    fireEvent.click(filterChip("FAILED"));
    fireEvent.click(filterChip("RUNNING"));

    expect(screen.getByText("Fail Co")).toBeInTheDocument();
    expect(screen.getByText("Run Co")).toBeInTheDocument();
    expect(screen.queryByText("Done Co")).toBeNull();
  });

  it("shows a CLEAR chip only once a filter is active, and it resets all filters", () => {
    const running = makeJob({ id: "r1", state: "running", company: "Run Co", role: "Dev" });
    const failed = makeJob({ id: "f1", state: "failed", company: "Fail Co", role: "Dev" });
    useStore.setState({ jobs: { r1: running, f1: failed } });

    render(<JobList />);
    expect(screen.queryByText("CLEAR")).toBeNull();

    fireEvent.click(filterChip("FAILED"));
    expect(screen.getByText("CLEAR")).toBeInTheDocument();
    expect(screen.queryByText("Run Co")).toBeNull();

    fireEvent.click(screen.getByText("CLEAR"));
    expect(screen.queryByText("CLEAR")).toBeNull();
    expect(screen.getByText("Run Co")).toBeInTheDocument();
    expect(screen.getByText("Fail Co")).toBeInTheDocument();
  });

  it("combines an active filter with the search box (both must match)", () => {
    const runA = makeJob({ id: "r1", state: "running", company: "Acme Corp", role: "Dev" });
    const runB = makeJob({ id: "r2", state: "running", company: "Beta Inc", role: "Dev" });
    const failedA = makeJob({ id: "f1", state: "failed", company: "Acme Corp", role: "QA" });
    useStore.setState({ jobs: { r1: runA, r2: runB, f1: failedA } });

    render(<JobList />);
    fireEvent.click(filterChip("RUNNING"));
    fireEvent.change(screen.getByPlaceholderText("Search company or role…"), {
      target: { value: "acme" },
    });

    expect(screen.getByText("Acme Corp")).toBeInTheDocument();
    expect(screen.queryByText("Beta Inc")).toBeNull(); // excluded by search
    expect(screen.queryByText("QA")).toBeNull(); // excluded by the RUNNING filter
  });

  it("shows the CV setup gate banner when cvStructureExists is false", () => {
    useStore.setState({ cvStructureExists: false });

    render(<JobList />);

    expect(screen.getByText("Set up your CV first")).toBeInTheDocument();
    expect(screen.getByText("Open CV editor")).toBeInTheDocument();
  });

  it("hides the CV setup gate banner when cvStructureExists is true", () => {
    useStore.setState({ cvStructureExists: true });

    render(<JobList />);

    expect(screen.queryByText("Set up your CV first")).toBeNull();
  });

  it("hides the CV setup gate banner when cvStructureExists is still unknown (null)", () => {
    useStore.setState({ cvStructureExists: null });

    render(<JobList />);

    expect(screen.queryByText("Set up your CV first")).toBeNull();
  });

  it("clicking the gate banner's CTA opens the CV editor", () => {
    useStore.setState({ cvStructureExists: false });

    render(<JobList />);

    fireEvent.click(screen.getByText("Open CV editor"));
    expect(useStore.getState().editorOpen).toBe(true);
  });
});

describe("JobList — base-CV trigger (Phase 6)", () => {
  it("renders the base-CV trigger on a queued row", () => {
    const job = makeJob({ id: "j1", state: "queued" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByTitle("Assign base CV")).toBeInTheDocument();
  });

  it("does not render the base-CV trigger on non-queued rows", () => {
    for (const state of ["pending", "running", "review", "approved"] as const) {
      const job = makeJob({ id: `j-${state}`, state });
      useStore.setState({ jobs: { [`j-${state}`]: job } });

      const { unmount } = render(<JobList />);
      expect(screen.queryByTitle("Assign base CV")).toBeNull();
      expect(screen.queryByTitle("Change base CV")).toBeNull();
      unmount();
    }
  });

  it("shows 'Change base CV' and accent styling once a deck is assigned", () => {
    const job = makeJob({ id: "j1", state: "queued", base_cv_id: "deck-1" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    const trigger = screen.getByTitle("Change base CV");
    expect(trigger).toBeInTheDocument();
    expect(screen.queryByTitle("Assign base CV")).toBeNull();
    expect(trigger).toHaveStyle({ color: T.a });
  });

  it("shows no accent styling when unassigned", () => {
    const job = makeJob({ id: "j1", state: "queued", base_cv_id: null });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    expect(screen.getByTitle("Assign base CV")).toHaveStyle({ color: T.ink3 });
  });

  it("clicking the trigger opens the picker for that job without selecting the row", () => {
    const job = makeJob({ id: "j1", state: "queued" });
    useStore.setState({ jobs: { j1: job } });

    render(<JobList />);

    fireEvent.click(screen.getByTitle("Assign base CV"));

    expect(useStore.getState().cvPickerJobId).toBe("j1");
    expect(useStore.getState().selectedId).toBeUndefined();
  });
});
