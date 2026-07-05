import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatBox } from "../components/ChatBox";
import { api } from "../api";
import { useStore } from "../store";
import { useScratchStore, type ScratchEntry } from "../scratchStore";

vi.mock("../api", () => ({
  api: {
    answerFollowUp: vi.fn(),
    revise: vi.fn(),
    getJobs: vi.fn().mockResolvedValue([]),
  },
}));

function makeEntry(overrides: Partial<ScratchEntry> = {}): ScratchEntry {
  return {
    id: overrides.id ?? "e1",
    text: overrides.text ?? "Visa status: not required",
    tag: overrides.tag ?? "#visa",
    pinned: overrides.pinned ?? false,
    ts: overrides.ts ?? Date.now(),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  // Reset store so refetchAll doesn't blow up
  useStore.setState({ jobs: {}, selectedId: undefined, wsStatus: "connecting" });
  (api.getJobs as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  // ChatBox subscribes to the scratch store for the @-mention dropdown; reset it so seeded
  // entries never leak across tests (it's a module-level singleton).
  localStorage.clear();
  useScratchStore.setState({ entries: [] });
});

describe("ChatBox kind='answer'", () => {
  it("renders a textarea and Submit Answer button", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /SUBMIT_ANSWER/i })).toBeInTheDocument();
  });

  it("does not render a target select for kind='answer'", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.queryByRole("combobox")).toBeNull();
  });

  it("submit button is disabled when textarea is empty", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.getByRole("button", { name: /SUBMIT_ANSWER/i })).toBeDisabled();
  });

  it("submit button is enabled after typing text", async () => {
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    await user.type(screen.getByRole("textbox"), "my answer");
    expect(screen.getByRole("button", { name: /SUBMIT_ANSWER/i })).not.toBeDisabled();
  });

  it("calls api.answerFollowUp with correct args on submit", async () => {
    const user = userEvent.setup();
    (api.answerFollowUp as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    await user.type(screen.getByRole("textbox"), "my answer");
    await user.click(screen.getByRole("button", { name: /SUBMIT_ANSWER/i }));

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
    await user.click(screen.getByRole("button", { name: /SUBMIT_ANSWER/i }));

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
    await user.click(screen.getByRole("button", { name: /SUBMIT_ANSWER/i }));

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
    await user.click(screen.getByRole("button", { name: /SUBMIT_ANSWER/i }));

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
    expect(screen.getByRole("button", { name: /REQUEST_REVISION/i })).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: /REQUEST_REVISION/i })).toBeDisabled();
  });

  it("calls api.revise with target='cv' by default", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockResolvedValue({});

    render(<ChatBox kind="revise" jobId="job1" />);
    await user.type(screen.getByRole("textbox"), "make it shorter");
    await user.click(screen.getByRole("button", { name: /REQUEST_REVISION/i }));

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
    await user.click(screen.getByRole("button", { name: /REQUEST_REVISION/i }));

    await waitFor(() => {
      expect(api.revise).toHaveBeenCalledWith("job1", "cl", "be more formal");
    });
  });

  it("shows error message when api.revise throws", async () => {
    const user = userEvent.setup();
    (api.revise as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("revise failed"));

    render(<ChatBox kind="revise" jobId="job1" />);
    await user.type(screen.getByRole("textbox"), "some text");
    await user.click(screen.getByRole("button", { name: /REQUEST_REVISION/i }));

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
    await user.click(screen.getByRole("button", { name: /REQUEST_REVISION/i }));

    await waitFor(() => {
      expect(textarea).toHaveValue("");
    });
  });
});

describe("ChatBox @-mention scratch dropdown", () => {
  it("opens the dropdown listing scratch buffer notes when @ is typed", async () => {
    useScratchStore.setState({ entries: [makeEntry({ text: "Visa status: not required" })] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    await user.type(screen.getByRole("textbox"), "Following up @");

    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();
    expect(screen.getByText("Visa status: not required")).toBeInTheDocument();
    expect(screen.getByText("#visa")).toBeInTheDocument();
  });

  it("shows the empty state when the scratch buffer has no notes", async () => {
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    await user.type(screen.getByRole("textbox"), "@");

    expect(screen.getByText("No notes in buffer.")).toBeInTheDocument();
  });

  it("does not open the dropdown before @ is typed", () => {
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("keeps the dropdown open while typing more non-space characters after @", async () => {
    useScratchStore.setState({ entries: [makeEntry()] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    await user.type(screen.getByRole("textbox"), "@vi");

    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();
  });

  it("closes the dropdown when a space is typed after @", async () => {
    useScratchStore.setState({ entries: [makeEntry()] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    await user.type(screen.getByRole("textbox"), "@ ");

    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("closes the dropdown when the triggering @ is deleted", async () => {
    useScratchStore.setState({ entries: [makeEntry()] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    const textarea = screen.getByRole("textbox");
    await user.type(textarea, "@");
    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();

    await user.type(textarea, "{backspace}");
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("clicking a note inserts its full text plus a trailing space and closes the dropdown", async () => {
    useScratchStore.setState({ entries: [makeEntry({ text: "Visa status: not required" })] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    await user.type(textarea, "Following up @");
    await user.click(screen.getByText("Visa status: not required"));

    expect(textarea).toHaveValue("Following up Visa status: not required ");
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("refocuses the textarea after inserting a note", async () => {
    useScratchStore.setState({ entries: [makeEntry({ text: "Visa status: not required" })] });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    const textarea = screen.getByRole("textbox") as HTMLTextAreaElement;
    await user.type(textarea, "@");
    await user.click(screen.getByText("Visa status: not required"));

    await waitFor(() => expect(textarea).toHaveFocus());
  });

  it("lists pinned notes first, then newest first", async () => {
    useScratchStore.setState({
      entries: [
        makeEntry({ id: "old", text: "old unpinned", ts: 1 }),
        makeEntry({ id: "pinned", text: "pinned note", ts: 0, pinned: true }),
        makeEntry({ id: "new", text: "new unpinned", ts: 2 }),
      ],
    });
    const user = userEvent.setup();
    render(<ChatBox kind="answer" jobId="job1" followUpId={42} />);

    await user.type(screen.getByRole("textbox"), "@");

    const texts = screen.getAllByText(/unpinned|pinned note/).map((el) => el.textContent);
    expect(texts).toEqual(["pinned note", "new unpinned", "old unpinned"]);
  });
});
