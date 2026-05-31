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

    // Placeholder "← Select a job" is shown
    expect(screen.getByText(/Select a job/i)).toBeInTheDocument();

    // Back button exists in the placeholder so mobile users can escape if
    // selectedId is set but the job is missing; it carries md:hidden so it
    // is not visible on desktop
    const backBtn = screen.queryByRole("button", { name: /back/i });
    expect(backBtn).toBeInTheDocument();
    expect(backBtn?.className).toContain("md:hidden");
  });
});

// ---------------------------------------------------------------------------
// Header.tsx — responsive title and badge container
// ---------------------------------------------------------------------------

describe("Header — responsive title and badge container", () => {
  it("short title 'JSA' span is present with md:hidden class", async () => {
    const { Header } = await import("../components/Header");
    render(<Header />);

    // getByText with exact match will find only the short span ("JSA"), not the full span
    const shortTitle = screen.getByText("JSA");
    expect(shortTitle).toBeInTheDocument();
    expect(shortTitle).toHaveClass("md:hidden");
  });

  it("full title 'JSA — Job Search Assistant' span is present with hidden md:inline class", async () => {
    const { Header } = await import("../components/Header");
    render(<Header />);

    const fullTitle = screen.getByText("JSA — Job Search Assistant");
    expect(fullTitle).toBeInTheDocument();
    expect(fullTitle).toHaveClass("hidden");
    expect(fullTitle).toHaveClass("md:inline");
  });

  it("badges container has hidden and md:flex classes", async () => {
    const { Header } = await import("../components/Header");

    // Seed a running job so at least one CountBadge renders inside the container
    const job = makeJob({ id: "j1", state: "running" });
    useStore.setState({ jobs: { j1: job } });

    render(<Header />);

    // Find the badge text and walk up to its container div
    const badgeText = screen.getByText(/Running:/i);
    // CountBadge renders a <span>; the container div wraps all badges
    // Go up until we find the div with hidden md:flex
    let el: HTMLElement | null = badgeText.parentElement;
    let containerFound = false;
    while (el !== null) {
      if (el.tagName === "DIV" && el.className.includes("hidden") && el.className.includes("md:flex")) {
        containerFound = true;
        break;
      }
      el = el.parentElement;
    }

    expect(containerFound).toBe(true);
    expect(el).not.toBeNull();
    expect(el).toHaveClass("hidden");
    expect(el).toHaveClass("md:flex");
  });
});
