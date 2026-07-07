import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { useStore } from "../store";
import { LaunchButton } from "../components/LaunchButton";

beforeEach(() => {
  vi.useFakeTimers();
  useStore.setState({ launchJob: vi.fn().mockResolvedValue(undefined) });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("LaunchButton", () => {
  it("renders the LAUNCH label initially", () => {
    render(<LaunchButton jobId="job1" />);
    expect(screen.getByText("LAUNCH")).toBeInTheDocument();
  });

  it("shows LAUNCHING immediately on click, before the API call fires", () => {
    render(<LaunchButton jobId="job1" />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("LAUNCHING")).toBeInTheDocument();
    expect(useStore.getState().launchJob).not.toHaveBeenCalled();
  });

  it("calls store.launchJob(jobId) only after the arm + dematerialize timers elapse", () => {
    render(<LaunchButton jobId="job1" />);
    fireEvent.click(screen.getByRole("button"));

    // Arm phase (150ms) not yet elapsed.
    act(() => {
      vi.advanceTimersByTime(100);
    });
    expect(useStore.getState().launchJob).not.toHaveBeenCalled();

    // Arm phase elapses; dematerialize (380ms) begins but hasn't finished.
    act(() => {
      vi.advanceTimersByTime(100); // total 200ms > 150ms arm
    });
    expect(useStore.getState().launchJob).not.toHaveBeenCalled();

    // Dematerialize finishes (150 + 380 = 530ms total).
    act(() => {
      vi.advanceTimersByTime(400); // total 600ms
    });
    expect(useStore.getState().launchJob).toHaveBeenCalledWith("job1");
  });

  it("ignores a second click while already launching (no confirmation, but no double-fire)", () => {
    render(<LaunchButton jobId="job1" />);
    const btn = screen.getByRole("button");
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(useStore.getState().launchJob).toHaveBeenCalledTimes(1);
  });

  it("stops click propagation so it doesn't also trigger a parent row's onSelect", () => {
    const onSelect = vi.fn();
    render(
      <button type="button" onClick={onSelect}>
        <LaunchButton jobId="job1" />
      </button>
    );
    fireEvent.click(screen.getByText("LAUNCH"));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("activates on Enter/Space keydown", () => {
    render(<LaunchButton jobId="job1" />);
    fireEvent.keyDown(screen.getByRole("button"), { key: "Enter" });
    expect(screen.getByText("LAUNCHING")).toBeInTheDocument();
  });

  it("resets to LAUNCH (instead of staying stuck invisible) when launchJob rejects", async () => {
    useStore.setState({ launchJob: vi.fn().mockRejectedValue(new Error("409")) });
    render(<LaunchButton jobId="job1" />);
    fireEvent.click(screen.getByRole("button"));

    await act(async () => {
      vi.advanceTimersByTime(600);
      // Let the rejected launchJob promise's .catch handler run.
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("LAUNCH")).toBeInTheDocument();
  });
});
