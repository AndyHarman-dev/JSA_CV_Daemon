import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { Toast } from "../components/Toast";
import { useStore } from "../store";
import type { ToastItem } from "../store";

function makeToast(overrides: Partial<ToastItem> = {}): ToastItem {
  return {
    id: "backend-job1-1",
    jobId: "job1",
    jobLabel: "Acme — Engineer",
    kind: "backend",
    from: "opencode-zen",
    to: "opencode-go",
    ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  useStore.setState({ toasts: [], selectedId: undefined, language: "en" });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Toast", () => {
  it("renders nothing when there are no toasts", () => {
    const { container } = render(<Toast />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders a backend-switch message with the job label and from/to backends", () => {
    useStore.setState({ toasts: [makeToast()] });
    render(<Toast />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Acme — Engineer");
    expect(alert.textContent).toContain("opencode-zen");
    expect(alert.textContent).toContain("opencode-go");
  });

  it("renders a model-switch message", () => {
    useStore.setState({
      toasts: [makeToast({ id: "model-job1-1", kind: "model", from: "glm-5.3", to: "kimi-k2.6" })],
    });
    render(<Toast />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("glm-5.3");
    expect(alert.textContent).toContain("kimi-k2.6");
  });

  it("renders an error toast's message verbatim, not through an interpolated template", () => {
    useStore.setState({
      toasts: [
        makeToast({
          id: "basecv-job1-1",
          kind: "error",
          from: "",
          to: "",
          message: "deck 'd9' is not assignable (unknown, or has no saved CV yet)",
        }),
      ],
    });
    render(<Toast />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("deck 'd9' is not assignable (unknown, or has no saved CV yet)");
  });

  it("dismiss button removes only that toast from the store", () => {
    useStore.setState({
      toasts: [makeToast({ id: "t1", jobId: "job1" }), makeToast({ id: "t2", jobId: "job2" })],
    });
    render(<Toast />);
    fireEvent.click(screen.getAllByTitle("Dismiss")[0]);
    expect(useStore.getState().toasts.map((t) => t.id)).toEqual(["t2"]);
  });

  it("clicking the card selects the job", () => {
    useStore.setState({ toasts: [makeToast({ jobId: "job1" })] });
    render(<Toast />);
    fireEvent.click(screen.getByRole("alert"));
    expect(useStore.getState().selectedId).toBe("job1");
  });

  it("auto-dismisses after the timeout elapses", () => {
    useStore.setState({ toasts: [makeToast({ id: "t1" })] });
    render(<Toast />);
    expect(useStore.getState().toasts).toHaveLength(1);

    act(() => {
      vi.advanceTimersByTime(9000);
    });

    expect(useStore.getState().toasts).toHaveLength(0);
  });
});
