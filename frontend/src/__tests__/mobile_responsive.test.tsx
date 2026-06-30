/**
 * Phase M-1: Mobile-responsive UI tests
 *
 * Tests for:
 *   - App.tsx panel visibility based on selectedId
 *   - JobDetail.tsx back button (render, className, selectJob(undefined) call)
 *   - Header.tsx short/full title and badge container visibility classes
 *
 * Strategy: real Zustand store driven via setState; mock only ../api and ../ws
 * so WS sockets and network calls don't fire.
 */

import { describe, it, expect, beforeAll, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useStore } from "../store";
import type { JobDTO } from "../types";

// ---------------------------------------------------------------------------
// Module-level mocks (hoisted — must cover everything used by real components)
// ---------------------------------------------------------------------------

vi.mock("../ws", () => ({
  connectWS: vi.fn(),
  disconnectWS: vi.fn(),
}));

vi.mock("../api", () => ({
  api: {
    config: vi.fn().mockReturnValue(new Promise(() => {})), // never resolves — avoids act warnings
    getJobs: vi.fn().mockResolvedValue([]),
    getJob: vi.fn().mockResolvedValue(null),
    getDocument: vi.fn().mockResolvedValue({ markdown: "# Doc", version: 1 }),
    answerFollowUp: vi.fn().mockResolvedValue({}),
    approve: vi.fn().mockResolvedValue({ cv_pdf_path: "/cv.pdf", cl_pdf_path: "/cl.pdf" }),
    revise: vi.fn().mockResolvedValue({}),
    reset: vi.fn().mockResolvedValue({}),
    dismiss: vi.fn().mockResolvedValue({}),
    deleteJob: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// scrollIntoView is not implemented in jsdom
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "A great job",
    state: "pending",
    current_stage: null,
    error: null,
    retry_count: 0,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
  });
});

// ---------------------------------------------------------------------------
// App.tsx — panel visibility on mobile
// ---------------------------------------------------------------------------

describe("App — mobile panel visibility", () => {
  // Lazy import App inside tests to ensure mocks are active
  it("no job selected: aside is block, main has 'hidden' class (mobile state)", async () => {
    const { default: App } = await import("../App");
    useStore.setState({ selectedId: undefined });

    const { container } = render(<App />);
    const aside = container.querySelector("aside");
    const main = container.querySelector("main");

    expect(aside).not.toBeNull();
    expect(main).not.toBeNull();

    // With no selection: aside class should NOT contain 'hidden'
    expect(aside!.className).not.toMatch(/\bhidden\b/);
    // With no selection: main should have 'hidden' (the hidden md:block token)
    expect(main!.className).toMatch(/\bhidden\b/);
  });

  it("job selected: aside has 'hidden' class, main does NOT have standalone 'hidden'", async () => {
    const { default: App } = await import("../App");
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    const { container } = render(<App />);
    const aside = container.querySelector("aside");
    const main = container.querySelector("main");

    expect(aside).not.toBeNull();
    expect(main).not.toBeNull();

    // With a selection: aside should have 'hidden' (hidden md:block token)
    expect(aside!.className).toMatch(/\bhidden\b/);
    // With a selection: main should NOT contain 'hidden' as a class token
    // (it uses plain 'block', no 'hidden')
    expect(main!.className).not.toMatch(/\bhidden\b/);
  });
});

// ---------------------------------------------------------------------------
// JobDetail.tsx — back button
// ---------------------------------------------------------------------------

describe("JobDetail — back button", () => {
  it("renders '← Back' button when a job is selected", async () => {
    const { JobDetail } = await import("../components/JobDetail");
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const backBtn = screen.getByRole("button", { name: /back/i });
    expect(backBtn).toBeInTheDocument();
  });

  it("back button has md:hidden class", async () => {
    const { JobDetail } = await import("../components/JobDetail");
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const backBtn = screen.getByRole("button", { name: /back/i });
    expect(backBtn).toHaveClass("md:hidden");
  });

  it("clicking back button calls selectJob(undefined) — selectedId becomes undefined", async () => {
    const { JobDetail } = await import("../components/JobDetail");
    const job = makeJob({ id: "j1" });
    useStore.setState({ jobs: { j1: job }, selectedId: "j1" });

    render(<JobDetail />);

    const backBtn = screen.getByRole("button", { name: /back/i });
    await userEvent.click(backBtn);

    expect(useStore.getState().selectedId).toBeUndefined();
  });

  it("back button is present in placeholder (md:hidden keeps it invisible on desktop)", async () => {
    const { JobDetail } = await import("../components/JobDetail");
    useStore.setState({ selectedId: undefined });

    render(<JobDetail />);

    // Placeholder "NO PROCESS SELECTED" is shown
    expect(screen.getByText(/NO PROCESS SELECTED/i)).toBeInTheDocument();

    // Back button exists in the placeholder so mobile users can escape if
    // selectedId is set but the job is missing; it carries md:hidden so it
    // is not visible on desktop
    const backBtn = screen.queryByRole("button", { name: /back/i });
    expect(backBtn).toBeInTheDocument();
    expect(backBtn?.className).toContain("md:hidden");
  });
});

// ---------------------------------------------------------------------------
// Header.tsx — responsive layout
//
// The cyberpunk redesign's header no longer hides the wordmark/cluster behind
// `md:` breakpoints; instead the header (and its center cluster) wrap via
// flex-wrap so every surface stays visible and reflows on narrow viewports.
// ---------------------------------------------------------------------------

describe("Header — responsive layout", () => {
  it("wordmark and subtitle render unconditionally (no responsive hiding)", async () => {
    const { Header } = await import("../components/Header");
    render(<Header />);

    const subtitle = screen.getByText("JOB_SEARCH_AUTOMATION · LOCAL");
    expect(subtitle).toBeInTheDocument();
    // No `hidden`/`md:*` visibility classes — always rendered, never breakpoint-gated
    expect(subtitle.closest("button")?.className ?? "").not.toMatch(/\bhidden\b/);
  });

  it("header and center cluster wrap (flex-wrap) instead of hiding content on narrow viewports", async () => {
    const { Header } = await import("../components/Header");

    // Seed a running job so the center cluster has chips/meter to wrap
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job } });

    const { container } = render(<Header />);

    const header = container.querySelector("header");
    expect(header).not.toBeNull();
    expect(header).toHaveStyle({ flexWrap: "wrap" });

    // Workers meter (always visible — replaces the old breakpoint-gated badge container)
    expect(screen.getByText("WORKERS")).toBeInTheDocument();
  });
});
