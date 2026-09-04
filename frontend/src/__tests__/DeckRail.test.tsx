import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DeckRail } from "../components/cv-editor/DeckRail";
import { useEditorStore } from "../editorStore";
import { useStore } from "../store";
import type { CvDeckDTO } from "../types";

vi.mock("../api", () => ({
  api: {
    listCvDecks: vi.fn(),
    getCvDeck: vi.fn(),
    createCvDeck: vi.fn(),
    saveCvDeck: vi.fn(),
    patchCvDeck: vi.fn(),
    duplicateCvDeck: vi.fn(),
    deleteCvDeck: vi.fn(),
  },
}));

// Captured at module load, BEFORE any seed() swaps in the label stub below. The stub has no
// live-buffer behaviour by construction, so a test that wants to exercise the real
// "active row tracks what you type" rule has to reach for this.
const REAL_DECK_LABEL = useEditorStore.getState().deckLabel;

const DECKS: CvDeckDTO[] = [
  { id: "d1", name: "Backend Focus", auto_title: "Jane Doe", has_cv: true, is_default: true, in_use_by: 0 },
  { id: "d2", name: null, auto_title: "Frontend Deck", has_cv: true, is_default: false, in_use_by: 0 },
];

// Full reset of the deck slice for each test. Network-hitting deck actions (switchDeck,
// newDeck, duplicateDeck, renameDeck, setDefaultDeck, deleteDeck) are replaced with spies;
// pure UI-state actions (setRailOpen, beginRename, setRenameDraft, cancelRename, deckLabel)
// keep a real/store-faithful implementation so hover/rename flows exercise real state.
function seed(overrides: Partial<ReturnType<typeof useEditorStore.getState>> = {}) {
  useEditorStore.setState({
    decks: DECKS,
    defaultDeckId: "d1",
    activeDeckId: "d1",
    railOpen: false,
    renameId: null,
    renameDraft: "",
    deckBusy: false,
    inferring: false,
    deckLabel: (d: CvDeckDTO) => (d.name || d.auto_title || "").trim() || "Untitled",
    switchDeck: vi.fn(),
    newDeck: vi.fn(),
    duplicateDeck: vi.fn(),
    renameDeck: vi.fn(),
    setDefaultDeck: vi.fn(),
    deleteDeck: vi.fn(),
    ...overrides,
  });
}

beforeEach(() => {
  useStore.setState({ language: "en" });
  seed();
});

function openRail() {
  fireEvent.mouseEnter(screen.getByTestId("deck-rail"));
}

describe("DeckRail", () => {
  it("renders one row per deck with the store's deckLabel", () => {
    render(<DeckRail />);
    openRail();
    expect(screen.getByTestId("deck-row-d1")).toBeInTheDocument();
    expect(screen.getByTestId("deck-row-d2")).toBeInTheDocument();
    expect(screen.getByText("Backend Focus")).toBeInTheDocument();
    expect(screen.getByText("Frontend Deck")).toBeInTheDocument();
  });

  it("disables the trash icon on a deck jobs still hold, and explains why", async () => {
    // The server refuses this with a 409 regardless (routes_cv_decks.delete_cv_deck);
    // this is the matching affordance. It stays rendered rather than hidden so the user
    // can see *why* it is unavailable instead of hunting for a missing control.
    const user = userEvent.setup();
    seed({
      decks: [
        { ...DECKS[0], in_use_by: 2 },
        DECKS[1],
      ],
    });
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d1"));

    const trash = screen.getByTestId("deck-trash-d1");
    expect(trash).toBeDisabled();
    expect(trash).toHaveAttribute(
      "title",
      "In use by 2 job(s) — approve, dismiss or delete them first"
    );

    await user.click(trash);
    expect(useEditorStore.getState().deleteDeck).not.toHaveBeenCalled();
  });

  it("leaves the trash icon live on a deck no job holds", async () => {
    const user = userEvent.setup();
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d2"));

    const trash = screen.getByTestId("deck-trash-d2");
    expect(trash).not.toBeDisabled();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    await user.click(trash);
    expect(useEditorStore.getState().deleteDeck).toHaveBeenCalledWith("d2");
  });

  it("marks the active deck's row with the accent bar", () => {
    render(<DeckRail />);
    openRail();
    expect(screen.getByTestId("deck-active-bar-d1")).toBeInTheDocument();
    expect(screen.queryByTestId("deck-active-bar-d2")).not.toBeInTheDocument();
  });

  it("does not render trash with exactly one deck, and does with two", () => {
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d1"));
    expect(screen.getByTestId("deck-trash-d1")).toBeInTheDocument();

    // Store update alone re-renders the already-mounted component (zustand subscription) —
    // no need to unmount/remount. Wrapped in act() since the update originates outside a
    // React event handler.
    act(() => {
      useEditorStore.setState({ decks: [DECKS[0]] });
    });
    expect(screen.queryByTestId("deck-trash-d1")).not.toBeInTheDocument();
  });

  it("rename: pencil opens the input and typing + Enter commits the typed value", async () => {
    const user = userEvent.setup();
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d2"));
    await user.click(screen.getByTestId("deck-pencil-d2"));

    expect(useEditorStore.getState().renameId).toBe("d2");
    const input = screen.getByTestId("deck-rename-input") as HTMLInputElement;
    await user.clear(input);
    await user.type(input, "New Title{enter}");
    expect(useEditorStore.getState().renameDeck).toHaveBeenCalledWith("d2", "New Title");
  });

  it("rename: Escape cancels and does not call renameDeck", async () => {
    const user = userEvent.setup();
    seed({ renameId: "d2", renameDraft: "Draft" });
    render(<DeckRail />);
    openRail();
    const input = screen.getByTestId("deck-rename-input");
    await user.type(input, "{escape}");

    expect(useEditorStore.getState().renameId).toBeNull();
    expect(useEditorStore.getState().renameDeck).not.toHaveBeenCalled();
  });

  it("locks the rail while inferring: pointerEvents none and hover does not open the flyout", () => {
    seed({ inferring: true });
    render(<DeckRail />);
    const rail = screen.getByTestId("deck-rail");
    expect(rail).toHaveStyle({ pointerEvents: "none" });
    fireEvent.mouseEnter(rail);
    expect(screen.queryByTestId("deck-rail-flyout")).not.toBeInTheDocument();
  });

  it("clicking trash asks for confirmation, then calls deleteDeck only on OK", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d2"));

    await user.click(screen.getByTestId("deck-trash-d2"));

    expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining("Frontend Deck"));
    expect(useEditorStore.getState().deleteDeck).toHaveBeenCalledWith("d2");
    confirmSpy.mockRestore();
  });

  it("does not call deleteDeck when the confirmation is cancelled", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<DeckRail />);
    openRail();
    fireEvent.mouseEnter(screen.getByTestId("deck-row-d2"));

    await user.click(screen.getByTestId("deck-trash-d2"));

    expect(useEditorStore.getState().deleteDeck).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it("clicking NEW BASE CV calls newDeck", async () => {
    const user = userEvent.setup();
    render(<DeckRail />);
    openRail();
    await user.click(screen.getByTestId("deck-new"));
    expect(useEditorStore.getState().newDeck).toHaveBeenCalled();
  });

  it("clicking a non-active row calls switchDeck; clicking an icon button does not", async () => {
    const user = userEvent.setup();
    render(<DeckRail />);
    openRail();
    await user.click(screen.getByTestId("deck-row-d2"));
    expect(useEditorStore.getState().switchDeck).toHaveBeenCalledWith("d2");

    (useEditorStore.getState().switchDeck as ReturnType<typeof vi.fn>).mockClear();
    await user.click(screen.getByTestId("deck-star-d1"));
    expect(useEditorStore.getState().switchDeck).not.toHaveBeenCalled();
  });

  // Each of the four buttons carries its own stopPropagation and can lose it independently;
  // checking one (as the `star` case above does) leaves the other three — trash included —
  // free to both fire their action AND switch decks underneath the user. One `it` per
  // button, rather than a loop, because userEvent's pointer state persists across clicks
  // within a single test and masked the leak when these ran in sequence.
  it.each(["star", "pencil", "copy", "trash"])(
    "the %s button does not leak its click into the row's switchDeck",
    async (which) => {
      const user = userEvent.setup();
      // trash now opens a window.confirm() — stub it so this generic leak-check doesn't
      // depend on the dialog's outcome (jsdom's unmocked confirm() logs a console error).
      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      // Non-active row on purpose: a leaked click there is a real navigation, not a no-op.
      useEditorStore.setState({ railOpen: true });
      render(<DeckRail />);
      fireEvent.mouseEnter(screen.getByTestId("deck-row-d2"));

      await user.click(screen.getByTestId(`deck-${which}-d2`));

      expect(useEditorStore.getState().switchDeck).not.toHaveBeenCalled();
      confirmSpy.mockRestore();
    }
  );
});

describe("the active row tracks the live editor buffer", () => {
  it("re-labels the active row as the contact name is edited", () => {
    // Uses the REAL deckLabel: this is the one rule in the rail that depends on editor state
    // outside the deck slice, so it is also the one the file's label stub cannot cover.
    seed({
      deckLabel: REAL_DECK_LABEL,
      decks: [
        { id: "d1", name: null, auto_title: "Jane Doe", has_cv: true, is_default: true },
      ],
    });
    useEditorStore.getState().load({
      contact: { name: "Jane Doe", email: "jane@x.com", location: "Berlin", links: [] },
      sections: [{ name: "Summary", text: "Backend engineer.", items: [], entries: [] }],
    });

    render(<DeckRail />);
    openRail();
    expect(screen.getByText("Jane Doe")).toBeTruthy();

    act(() => {
      useEditorStore.getState().updateContact({ name: "Renamed Live" });
    });

    // Fails unless DeckRail actually subscribes to the live CV — deckLabel() reads it via
    // get(), which does not by itself make zustand re-render this component.
    expect(screen.getByText("Renamed Live")).toBeTruthy();
  });
});

