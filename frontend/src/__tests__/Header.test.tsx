import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { Header } from "../components/Header";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    config: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

function makeJob(id: string, overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id,
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
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({ backend: "anthropic" });
});

describe("Header", () => {
  it("renders the app name 'JSA — Job Search Assistant'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<Header />);
    expect(screen.getByText("JSA — Job Search Assistant")).toBeInTheDocument();
  });

  it("shows the WS status dot — 'Connecting' when wsStatus is 'connecting'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "connecting" });

    render(<Header />);

    expect(screen.getByText("Connecting")).toBeInTheDocument();
  });

  it("shows 'Connected' label when wsStatus is 'open'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "open" });

    render(<Header />);

    expect(screen.getByText("Connected")).toBeInTheDocument();
  });

  it("shows 'Disconnected' label when wsStatus is 'closed'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "closed" });

    render(<Header />);

    expect(screen.getByText("Disconnected")).toBeInTheDocument();
  });

  it("shows backend badge once api.config resolves", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({ backend: "claude-cli" });

    render(<Header />);

    await waitFor(() => {
      expect(screen.getByText("claude-cli")).toBeInTheDocument();
    });
  });

  it("hides backend badge while api.config is loading (shows ellipsis)", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    render(<Header />);

    // While loading, show the "…" placeholder
    expect(screen.getByText("…")).toBeInTheDocument();
    expect(screen.queryByText("anthropic")).toBeNull();
  });

  it("hides backend badge (no error shown) when api.config rejects", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("network error"));

    render(<Header />);

    await waitFor(() => {
      // Loading ellipsis is gone, error state renders nothing for the badge
      expect(screen.queryByText("…")).toBeNull();
    });
    // No error text visible for the badge
    expect(screen.queryByText("anthropic")).toBeNull();
  });

  it("shows non-zero count badges for running jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "running" }),
      j2: makeJob("j2", { state: "pending" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText(/Running: 2/i)).toBeInTheDocument();
  });

  it("shows Inbox badge for awaiting_input jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "awaiting_input" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText(/Inbox: 1/i)).toBeInTheDocument();
  });

  it("shows Review badge for review jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "review" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText(/Review: 1/i)).toBeInTheDocument();
  });

  it("shows Done badge for approved jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "approved" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText(/Done: 1/i)).toBeInTheDocument();
  });

  it("shows Failed badge for failed jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "failed" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText(/Failed: 1/i)).toBeInTheDocument();
  });

  it("hides zero-count Running badge when no jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ jobs: {} });

    render(<Header />);

    expect(screen.queryByText(/Running:/i)).toBeNull();
    expect(screen.queryByText(/Inbox:/i)).toBeNull();
    expect(screen.queryByText(/Review:/i)).toBeNull();
    expect(screen.queryByText(/Done:/i)).toBeNull();
    expect(screen.queryByText(/Failed:/i)).toBeNull();
  });
});
