import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ScratchBuffer } from "../components/ScratchBuffer";
import { useScratchStore } from "../scratchStore";

const ORB_TITLE = /Scratch buffer/i;

beforeEach(() => {
  localStorage.clear();
  useScratchStore.setState({ entries: [] });
});

function rows(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(".jrow"));
}

describe("ScratchBuffer", () => {
  it("renders the orb with no count badge when empty", () => {
    render(<ScratchBuffer />);
    expect(screen.getByTitle(ORB_TITLE)).toBeInTheDocument();
    // Window is closed by default, so its header/count aren't rendered either.
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("shows the empty state when the window is opened with no entries", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    expect(screen.getByText(/No notes yet/i)).toBeInTheDocument();
  });

  it("orb click opens the window and autofocuses the input", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();
    const input = screen.getByPlaceholderText(/quick note/i);
    await waitFor(() => expect(input).toHaveFocus());
  });

  it("Cmd/Ctrl+Space toggles the window open and closed from anywhere", async () => {
    render(<ScratchBuffer />);
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();

    await userEvent.keyboard("{Meta>}{ }{/Meta}");
    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();

    await userEvent.keyboard("{Meta>}{ }{/Meta}");
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("Esc closes the window when open", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    expect(screen.getByText("SCRATCH_BUFFER")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
  });

  it("the minimize button closes the window without discarding notes", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    await userEvent.type(screen.getByPlaceholderText(/quick note/i), "keep me{Enter}");
    expect(screen.getByText("keep me")).toBeInTheDocument();

    await userEvent.click(screen.getByTitle("Minimize"));
    expect(screen.queryByText("SCRATCH_BUFFER")).not.toBeInTheDocument();
    expect(useScratchStore.getState().entries.map((e) => e.text)).toEqual(["keep me"]);
  });

  it("typing a note and pressing Enter adds it, clears the input, and updates the badge", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    const input = screen.getByPlaceholderText<HTMLInputElement>(/quick note/i);

    await userEvent.type(input, "Salary floor: $190k{Enter}");
    expect(screen.getByText("Salary floor: $190k")).toBeInTheDocument();
    expect(screen.getByText("#salary")).toBeInTheDocument();
    expect(input.value).toBe("");
    // Orb badge + header live count both now read "1".
    expect(screen.getAllByText("1").length).toBeGreaterThan(0);
  });

  it("ignores an empty submission", async () => {
    render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    const input = screen.getByPlaceholderText(/quick note/i);
    await userEvent.type(input, "{Enter}");
    expect(screen.getByText(/No notes yet/i)).toBeInTheDocument();
  });

  it("pinning an older entry floats it above a newer one", async () => {
    const { container } = render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    const input = screen.getByPlaceholderText(/quick note/i);
    await userEvent.type(input, "alpha entry{Enter}");
    await userEvent.type(input, "beta entry{Enter}");

    // Newest ("beta entry") sorts first pre-pin.
    expect(rows(container).map((r) => r.textContent?.includes("beta entry"))).toEqual([true, false]);

    // Pin the older entry — it should float above the newer, unpinned one.
    const alphaRow = rows(container).find((r) => r.textContent?.includes("alpha entry"))!;
    await userEvent.click(alphaRow.querySelector('button[title="Pin"]')!);

    expect(rows(container).map((r) => r.textContent?.includes("alpha entry"))).toEqual([true, false]);
  });

  it("delete removes an entry immediately", async () => {
    const { container } = render(<ScratchBuffer />);
    await userEvent.click(screen.getByTitle(ORB_TITLE));
    const input = screen.getByPlaceholderText(/quick note/i);
    await userEvent.type(input, "delete me{Enter}");
    expect(screen.getByText("delete me")).toBeInTheDocument();

    const row = rows(container)[0];
    await userEvent.click(row.querySelector('button[title="Delete"]')!);

    expect(screen.queryByText("delete me")).not.toBeInTheDocument();
    expect(screen.getByText(/No notes yet/i)).toBeInTheDocument();
  });
});
