import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
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
  useStore.setState({
    jobs: {},
    selectedId: undefined,
    wsStatus: "connecting",
    lastBackendSwitch: null,
  });
  (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({ backend: "anthropic" });
});

describe("Header", () => {
  it("renders the JSA // DAEMON wordmark and editor entry point", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<Header />);
    expect(screen.getByTitle("Open the CV Structure Editor")).toBeInTheDocument();
    expect(screen.getByText("JOB_SEARCH_AUTOMATION · LOCAL")).toBeInTheDocument();
  });

  it("clicking the logo opens the CV Structure Editor", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    render(<Header />);
    fireEvent.click(screen.getByTitle("Open the CV Structure Editor"));
    expect(useStore.getState().editorOpen).toBe(true);
  });

  it("shows 'UPLINK: RECONNECTING' when wsStatus is 'connecting'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "connecting" });

    render(<Header />);

    expect(screen.getByText("UPLINK: RECONNECTING")).toBeInTheDocument();
  });

  it("shows 'UPLINK: SYNCED' label when wsStatus is 'open'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "open" });

    render(<Header />);

    expect(screen.getByText("UPLINK: SYNCED")).toBeInTheDocument();
  });

  it("shows 'UPLINK: LOST' label when wsStatus is 'closed'", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ wsStatus: "closed" });

    render(<Header />);

    expect(screen.getByText("UPLINK: LOST")).toBeInTheDocument();
  });

  it("shows the mapped backend label once api.config resolves", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({ backend: "claude-cli" });

    render(<Header />);

    await waitFor(() => {
      expect(screen.getByText("CLAUDE CLI")).toBeInTheDocument();
    });
  });

  it("shows a loading ellipsis in the backend cluster while api.config is in flight", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    render(<Header />);

    expect(screen.getByText("…")).toBeInTheDocument();
  });

  it("hides backend label (no crash) when api.config rejects", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("network error"));

    render(<Header />);

    await waitFor(() => {
      expect(screen.queryByText("…")).toBeNull();
    });
    expect(screen.queryByText("ANTHROPIC API")).toBeNull();
  });

  it("opens the backend failover queue dropdown showing standby entries", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({
      backend: "claude-cli",
      backends: ["claude-cli", "google-cli", "anthropic"],
    });

    render(<Header />);

    const trigger = await screen.findByText("CLAUDE CLI");
    fireEvent.click(trigger.closest("button")!);

    expect(screen.getByText("BACKEND FAILOVER QUEUE")).toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    expect(screen.getAllByText("STANDBY").length).toBe(2);
    expect(screen.getByText("GOOGLE CLI")).toBeInTheDocument();
    expect(screen.getByText("ANTHROPIC API")).toBeInTheDocument();
  });

  it("reorders the backend cluster to the front after a backend_switched WS event", async () => {
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({
      backend: "claude-cli",
      backends: ["claude-cli", "google-cli", "anthropic"],
    });

    render(<Header />);

    await screen.findByText("CLAUDE CLI");

    useStore.getState().applyEvent({
      type: "backend_switched",
      job_id: "j1",
      from_backend: "claude-cli",
      to_backend: "google-cli",
    });

    await waitFor(() => {
      expect(screen.getByText("GOOGLE CLI")).toBeInTheDocument();
      expect(screen.queryByText("CLAUDE CLI")).toBeNull();
    });
  });

  it("shows the workers meter reflecting running-state job counts", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "running" }),
      j2: makeJob("j2", { state: "pending" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText("2/5")).toBeInTheDocument();
  });

  it("shows Inbox chip for awaiting_input jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "awaiting_input" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText("INBOX")).toBeInTheDocument();
  });

  it("shows Review chip for review jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "review" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText("REVIEW")).toBeInTheDocument();
  });

  it("shows Done chip for approved jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "approved" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText("DONE")).toBeInTheDocument();
  });

  it("shows Failed chip for failed jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));

    const jobs = {
      j1: makeJob("j1", { state: "failed" }),
    };
    useStore.setState({ jobs });

    render(<Header />);

    expect(screen.getByText("FAILED")).toBeInTheDocument();
  });

  it("hides zero-count chips when no jobs", () => {
    (api.config as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}));
    useStore.setState({ jobs: {} });

    render(<Header />);

    expect(screen.queryByText("INBOX")).toBeNull();
    expect(screen.queryByText("REVIEW")).toBeNull();
    expect(screen.queryByText("DONE")).toBeNull();
    expect(screen.queryByText("FAILED")).toBeNull();
    expect(screen.getByText("0/5")).toBeInTheDocument();
  });
});
