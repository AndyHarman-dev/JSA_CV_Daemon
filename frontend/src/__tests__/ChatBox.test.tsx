import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatBox } from "../components/ChatBox";
import { api } from "../api";
import { useStore } from "../store";

vi.mock("../api", () => ({
  api: {
    answerFollowUp: vi.fn(),
    revise: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  // Reset store so refetchAll doesn't blow up
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
});

describe("ChatBox kind='answer'", () => {
  it("renders a textarea and Submit Answer button", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Submit Answer/i })).toBeInTheDocument();
  });

  it("does not render a target select for kind='answer'", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("submit button is disabled when textarea is empty", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.getByRole("button", { name: /Submit Answer/i })).toBeDisabled();
  });

  it("submit button is enabled after typing text", async () => {
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    await user.type(screen.getByRole("textbox"), "my answer");
    expect(screen.getByRole("button", { name: /Submit Answer/i })).not.toBeDisabled();
  });

  it("calls api.answerFollowUp with correct args on submit", async () => {
    const user = userEvent.setup();
    (api.answerFollowUp as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    await user.type(screen.getByRole("textbox"), "my answer");
    await user.click(screen.getByRole("button", { name: /Submit Answer/i }));

    await waitFor(() => {
      expect(api.answerFollowUp).toHaveBeenCalledWith("job1", 42, "my answer");
    });
  });

  it("clears textarea on successful submit", async () => {
    const user = userEvent.setup();
    (api.answerFollowUp as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "my answer");
    await user.click(screen.getByRole("button", { name: /Submit Answer/i }));

    await waitFor(() => {
      expect(textarea).toHaveValue("");
    });
  });

  it("shows error message when api.answerFollowUp throws", async () => {
    const user = userEvent.setup();
    (api.answerFollowUp as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("server error")
    );

    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    await user.type(screen.getByRole("textbox"), "my answer");
    await user.click(screen.getByRole("button", { name: /Submit Answer/i }));

    await waitFor(() => {
      expect(screen.getByText("server error")).toBeInTheDocument();
    });
  });

  it("calls onSubmitted callback after successful submit", async () => {
    const user = userEvent.setup();
    (api.answerFollowUp as ReturnType<typeof vi.fn>).mockResolvedValue({});
    const onSubmitted = vi.fn();

    render(<ChatBox kind="answer" jobId="job1" followUpId={42} onSubmitted={onSubmitted} />);
    await user.type(screen.getByRole("textbox"), "some answer");
    await user.click(screen.getByRole("button", { name: /Submit Answer/i }));

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledOnce();
    });
  });
});

describe("ChatBox kind='revise'", () => {
  it("renders a textarea, a target select, and Request Revision button", () => {
    render(<ChatBox kind="revise" jobId="job1" />);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Request Revision/i })).toBeInTheDocument();
  });

  it("target select has CV / Resume and Cover Letter options", () => {
    render(<ChatBox kind="revise" jobId="job1" />);
    const select = screen.getByRole("combobox");
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toContain("CV / Resume");
    expect(options).toContain("Cover Letter");
  });

  it("submit button is disabled when textarea is empty", () => {
    render(<ChatBox kind="revise" jobId="job1" />);
    expect(screen.getByRole("button", { name: /Request Revision/i })).toBeDisabled();
  });

  it("calls api.revise with target='cv' by default", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="revise" jobId="job1" />);
    await user.type(screen.getByRole("textbox"), "make it shorter");
    await user.click(screen.getByRole("button", { name: /Request Revision/i }));

    await waitFor(() => {
      expect(api.revise).toHaveBeenCalledWith("job1", "cv", "make it shorter");
    });
  });

  it("calls api.revise with target='cl' after selecting Cover Letter", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="revise" jobId="job1" />);
    await user.selectOptions(screen.getByRole("combobox"), "cl");
    await user.type(screen.getByRole("textbox"), "be more formal");
    await user.click(screen.getByRole("button", { name: /Request Revision/i }));

    await waitFor(() => {
      expect(api.revise).toHaveBeenCalledWith("job1", "cl", "be more formal");
    });
  });

  it("shows error message when api.revise throws", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("revise failed"));

    render(<ChatBox kind="revise" jobId="job1" />);
    await user.type(screen.getByRole("textbox"), "some text");
    await user.click(screen.getByRole("button", { name: /Request Revision/i }));

    await waitFor(() => {
      expect(screen.getByText("revise failed")).toBeInTheDocument();
    });
  });

  it("clears textarea on successful revise submit", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="revise" jobId="job1" />);
    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "adjust the tone");
    await user.click(screen.getByRole("button", { name: /Request Revision/i }));

    await waitFor(() => {
      expect(textarea).toHaveValue("");
    });
  });
});
