import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useStore } from "../store";
import { UnfitModal } from "../components/UnfitModal";
import { api } from "../api";
import type { JobDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    dismiss: vi.fn().mockResolvedValue({}),
    ignoreFit: vi.fn().mockResolvedValue({}),
  },
}));

function makeJob(overrides: Partial<JobDTO> = {}): JobDTO {
  return {
    id: "job1",
    company: "Acme",
    role: "Engineer",
    link: "https://example.com",
    tier: "A",
    jd: "jd",
    state: "unfit",
    current_stage: null,
    backend_name: null,
    fit_reason: "Role needs 15+ years; CV shows 4.",
    error: null,
    retry_count: 0,
    updated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  // refetchAll hits the network in the real store; stub it for the modal's callback.
  useStore.setState({ refetchAll: vi.fn().mockResolvedValue(undefined) });
});

describe("UnfitModal", () => {
  it("renders the agent's fit reason and both buttons", () => {
    render(<UnfitModal job={makeJob()} />);
    expect(screen.getByText(/Role needs 15\+ years/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Dismiss Job/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ignore/i })).toBeInTheDocument();
  });

  it("calls api.dismiss when Dismiss Job is clicked", async () => {
    render(<UnfitModal job={makeJob()} />);
    await userEvent.click(screen.getByRole("button", { name: /Dismiss Job/i }));
    await waitFor(() => expect(api.dismiss).toHaveBeenCalledWith("job1"));
    expect(api.ignoreFit).not.toHaveBeenCalled();
  });

  it("calls api.ignoreFit when Ignore is clicked", async () => {
    render(<UnfitModal job={makeJob()} />);
    await userEvent.click(screen.getByRole("button", { name: /Ignore/i }));
    await waitFor(() => expect(api.ignoreFit).toHaveBeenCalledWith("job1"));
    expect(api.dismiss).not.toHaveBeenCalled();
  });

  it("falls back to a generic message when fit_reason is null", () => {
    render(<UnfitModal job={makeJob({ fit_reason: null })} />);
    expect(screen.getByText(/significant mismatch/i)).toBeInTheDocument();
  });
});
