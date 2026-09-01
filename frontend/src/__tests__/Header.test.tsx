import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { Header } from "../components/Header";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    config: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
    getBackendModels: vi.fn().mockResolvedValue({ selected: {}, supports_model_selection: {} }),
    getBackendModelsFor: vi.fn(),
    putBackendModel: vi.fn(),
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
    backend_name: null,
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

  it("reconciles the active backend from job.backend_name on load, without a WS event", async () => {
    // Regression for the bug where a page load/reload after a BF-19 failover already
    // happened showed the stale boot-time `/api/config` backend forever, because
    // `backend_switched` only fires for switches that happen while connected and
    // `Job.backend_name` was never serialized to the frontend at all.
    (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({
      backend: "opencode-zen",
      backends: ["opencode-zen", "opencode-go"],
    });
    useStore.setState({
      jobs: {
        j1: makeJob("j1", { updated_at: "2026-01-01T00:00:00Z", backend_name: "opencode-zen" }),
        j2: makeJob("j2", { updated_at: "2026-01-02T00:00:00Z", backend_name: "opencode-go" }),
      },
    });

    render(<Header />);

    await waitFor(() => {
      expect(screen.getByText("OPENCODE GO")).toBeInTheDocument();
      expect(screen.queryByText("OPENCODE ZEN")).toBeNull();
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

  describe("per-backend model submenu", () => {
    beforeEach(() => {
      (api.config as ReturnType<typeof vi.fn>).mockResolvedValue({
        backend: "mistral",
        backends: ["mistral", "google-cli"],
      });
      (api.getBackendModels as ReturnType<typeof vi.fn>).mockResolvedValue({
        selected: { mistral: "mistral-small-2603" },
        supports_model_selection: { mistral: true, "google-cli": false },
      });
    });

    // The failover queue panel's heading is unique in the DOM; scoping row lookups to its
    // parent avoids ambiguity with the trigger button, which repeats the active backend's label.
    function queuePanel(): HTMLElement {
      return screen.getByText("BACKEND FAILOVER QUEUE").parentElement as HTMLElement;
    }

    function openBackendDropdown() {
      const trigger = screen.getByText("MISTRAL").closest("button")!;
      fireEvent.click(trigger);
    }

    it("opens the model submenu on row click, renders fetched models, and marks the selected one", async () => {
      (api.getBackendModelsFor as ReturnType<typeof vi.fn>).mockResolvedValue({
        backend: "mistral",
        models: ["mistral-small-2603", "mistral-large-latest"],
        selected: "mistral-small-2603",
        source: "live",
      });

      render(<Header />);
      await screen.findByText("MISTRAL");
      openBackendDropdown();

      const row = within(queuePanel()).getByText("MISTRAL").closest("[role='button']")!;
      fireEvent.click(row);

      const otherOption = await screen.findByRole("button", { name: "mistral-large-latest" });
      expect(api.getBackendModelsFor).toHaveBeenCalledWith("mistral");

      const selectedOption = screen.getByRole("button", { name: "mistral-small-2603" });
      expect(selectedOption.querySelector("svg")).not.toBeNull(); // check mark on the selected entry
      expect(otherOption.querySelector("svg")).toBeNull();
    });

    it("PUTs the selection on click and reverts the optimistic update on failure", async () => {
      (api.getBackendModelsFor as ReturnType<typeof vi.fn>).mockResolvedValue({
        backend: "mistral",
        models: ["mistral-small-2603", "mistral-large-latest"],
        selected: "mistral-small-2603",
        source: "live",
      });
      (api.putBackendModel as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));

      render(<Header />);
      await screen.findByText("MISTRAL");
      openBackendDropdown();

      const row = within(queuePanel()).getByText("MISTRAL").closest("[role='button']")!;
      fireEvent.click(row);

      const option = await screen.findByRole("button", { name: "mistral-large-latest" });
      fireEvent.click(option);

      expect(api.putBackendModel).toHaveBeenCalledWith("mistral", "mistral-large-latest");
      // Optimistic: the row caption flips immediately, before the PUT settles.
      expect(within(queuePanel()).getByText("mistral-large-latest")).toBeInTheDocument();

      // Once the rejected PUT resolves, the caption reverts to the prior selection --
      // assert both that the new value is gone AND the old value is back, so a revert to
      // some other (wrong) value can't pass this check.
      await waitFor(() => {
        expect(within(queuePanel()).queryByText("mistral-large-latest")).toBeNull();
      });
      expect(within(queuePanel()).getByText("mistral-small-2603")).toBeInTheDocument();
    });

    it("reverts to no selection (not a stale value) when the failed PUT was a backend's first-ever pick", async () => {
      (api.getBackendModels as ReturnType<typeof vi.fn>).mockResolvedValue({
        selected: {}, // fresh install -- matches the live probe's actual `{"selected":{}}` shape
        supports_model_selection: { mistral: true, "google-cli": false },
      });
      (api.getBackendModelsFor as ReturnType<typeof vi.fn>).mockResolvedValue({
        backend: "mistral",
        models: ["mistral-small-2603", "mistral-large-latest"],
        selected: null,
        source: "catalog",
      });
      (api.putBackendModel as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("boom"));

      render(<Header />);
      await screen.findByText("MISTRAL");
      openBackendDropdown();

      expect(within(queuePanel()).getByText("—")).toBeInTheDocument();

      const row = within(queuePanel()).getByText("MISTRAL").closest("[role='button']")!;
      fireEvent.click(row);

      const option = await screen.findByRole("button", { name: "mistral-large-latest" });
      fireEvent.click(option);

      expect(within(queuePanel()).getByText("mistral-large-latest")).toBeInTheDocument();

      await waitFor(() => {
        expect(within(queuePanel()).queryByText("mistral-large-latest")).toBeNull();
      });
      expect(within(queuePanel()).getByText("—")).toBeInTheDocument();
    });

    it("renders no submenu affordance for google-cli", async () => {
      render(<Header />);
      await screen.findByText("MISTRAL");
      openBackendDropdown();

      const panel = queuePanel();
      const googleRow = within(panel).getByText("GOOGLE CLI").closest("div")!;
      expect(googleRow.querySelector("[role='button']")).toBeNull();
      expect(within(panel).getByText("no model selection")).toBeInTheDocument();

      fireEvent.click(within(panel).getByText("GOOGLE CLI"));
      expect(api.getBackendModelsFor).not.toHaveBeenCalled();
    });
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
