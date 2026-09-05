import { describe, it, expect, beforeEach, vi } from "vitest";

vi.mock("../api", () => ({
  api: {
    saveCvStructure: vi.fn(),
    listCvDecks: vi.fn(),
    getCvDeck: vi.fn(),
    createCvDeck: vi.fn(),
    saveCvDeck: vi.fn(),
    patchCvDeck: vi.fn(),
    duplicateCvDeck: vi.fn(),
    deleteCvDeck: vi.fn(),
  },
}));

import { useEditorStore, exportJson, inferKind, toEditor } from "../editorStore";
import { useStore } from "../store";
import { api } from "../api";
import type { CVDocument, CvDeckDTO } from "../types";

const SAMPLE: CVDocument = {
  contact: { name: "Jane Doe", email: "jane@x.com", location: "Berlin", links: ["github.com/jane"] },
  sections: [
    { name: "Summary", text: "Backend engineer.", items: [], entries: [] },
    {
      name: "Experience",
      items: [],
      entries: [
        {
          heading: "Senior Engineer",
          subheading: "Acme",
          dates: "2020–Present",
          location: "Remote",
          bullets: ["Built X", "Cut latency"],
          links: [],
        },
      ],
    },
    { name: "Skills", items: ["Python", "Go"], entries: [] },
  ],
};

function loadSample() {
  useEditorStore.getState().load(structuredClone(SAMPLE));
}

beforeEach(() => {
  useEditorStore.getState().reset();
  useStore.setState({ cvStructureExists: null });
  vi.clearAllMocks();
});

describe("kind inference + round-trip", () => {
  it("classifies sections by name", () => {
    expect(inferKind({ name: "Summary", items: [], entries: [] })).toBe("summary");
    expect(inferKind({ name: "Skills", items: ["Go"], entries: [] })).toBe("skills");
    expect(inferKind({ name: "Experience", items: [], entries: [{ bullets: ["x"], links: [] }] })).toBe(
      "experience"
    );
    expect(inferKind({ name: "Education", items: [], entries: [] })).toBe("education");
    expect(inferKind({ name: "Projects", items: [], entries: [] })).toBe("projects");
  });

  it("kinds survive an export → re-import round-trip", () => {
    loadSample();
    const kindsBefore = useEditorStore.getState().cv!.sections.map((s) => s.kind);
    const exported = exportJson(useEditorStore.getState().cv!);
    const reimported = toEditor(exported);
    const kindsAfter = reimported.sections.map((s) => s.kind);
    expect(kindsAfter).toEqual(kindsBefore);
    expect(kindsAfter).toEqual(["summary", "experience", "skills"]);
  });
});

describe("exportJson filtering", () => {
  it("strips id/kind and omits empty fields/arrays", () => {
    loadSample();
    const out = exportJson(useEditorStore.getState().cv!) as Record<string, any>;
    // No transient identity leaks.
    expect(JSON.stringify(out)).not.toContain('"id"');
    expect(JSON.stringify(out)).not.toContain('"kind"');
    // Summary keeps text, no empty items/entries arrays.
    expect(out.sections[0]).toEqual({ name: "Summary", text: "Backend engineer." });
    // Skills keeps items only.
    expect(out.sections[2]).toEqual({ name: "Skills", items: ["Python", "Go"] });
    // Contact keeps populated fields, omits the absent phone.
    expect(out.contact).toEqual({
      name: "Jane Doe",
      email: "jane@x.com",
      location: "Berlin",
      links: ["github.com/jane"],
    });
  });

  it("drops fully-empty entries", () => {
    useEditorStore.getState().load({
      contact: { name: "X", links: [] },
      sections: [
        {
          name: "Experience",
          items: [],
          entries: [
            { heading: "Real", bullets: [], links: [] },
            { heading: "  ", subheading: "", bullets: ["   "], links: [] },
          ],
        },
      ],
    });
    const out = exportJson(useEditorStore.getState().cv!) as Record<string, any>;
    expect(out.sections[0].entries).toEqual([{ heading: "Real" }]);
  });
});

describe("reorder", () => {
  it("moveSection swaps with the neighbor", () => {
    loadSample();
    const ids = useEditorStore.getState().cv!.sections.map((s) => s.id);
    useEditorStore.getState().moveSection(ids[0], 1);
    const after = useEditorStore.getState().cv!.sections.map((s) => s.name);
    expect(after).toEqual(["Experience", "Summary", "Skills"]);
  });

  it("moveSection is a no-op at the ends", () => {
    loadSample();
    const ids = useEditorStore.getState().cv!.sections.map((s) => s.id);
    useEditorStore.getState().moveSection(ids[0], -1);
    expect(useEditorStore.getState().cv!.sections.map((s) => s.name)).toEqual([
      "Summary",
      "Experience",
      "Skills",
    ]);
  });

  it("reorderSection moves a section to an arbitrary index", () => {
    loadSample();
    const ids = useEditorStore.getState().cv!.sections.map((s) => s.id);
    useEditorStore.getState().reorderSection(ids[2], 0); // Skills → front
    expect(useEditorStore.getState().cv!.sections.map((s) => s.name)).toEqual([
      "Skills",
      "Summary",
      "Experience",
    ]);
  });
});

describe("undo/redo with coalescing", () => {
  it("collapses a typing burst into a single undo step", () => {
    loadSample();
    const start = useEditorStore.getState().history.length; // 1
    useEditorStore.getState().updateContact({ name: "J" });
    useEditorStore.getState().updateContact({ name: "Ja" });
    useEditorStore.getState().updateContact({ name: "Jane Q" });
    // Still coalescing — no new committed snapshot yet.
    expect(useEditorStore.getState().history.length).toBe(start);

    useEditorStore.getState().commit(); // flush the burst
    expect(useEditorStore.getState().history.length).toBe(start + 1);

    // One undo reverts the entire burst back to the original name.
    useEditorStore.getState().undo();
    expect(useEditorStore.getState().cv!.contact.name).toBe("Jane Doe");

    // Redo restores the burst result.
    useEditorStore.getState().redo();
    expect(useEditorStore.getState().cv!.contact.name).toBe("Jane Q");
  });

  it("structural ops commit immediately and are individually undoable", () => {
    loadSample();
    const startLen = useEditorStore.getState().cv!.sections.length;
    useEditorStore.getState().addSection("projects", null);
    expect(useEditorStore.getState().cv!.sections.length).toBe(startLen + 1);
    expect(useEditorStore.getState().canUndo).toBe(true);

    useEditorStore.getState().undo();
    expect(useEditorStore.getState().cv!.sections.length).toBe(startLen);
  });

  it("undo flushes a pending typing burst before stepping back", () => {
    loadSample();
    // Type without an explicit commit, then undo directly.
    useEditorStore.getState().updateContact({ name: "Typed" });
    useEditorStore.getState().undo();
    expect(useEditorStore.getState().cv!.contact.name).toBe("Jane Doe");
  });

  it("a new edit after undo truncates the redo tail", () => {
    loadSample();
    useEditorStore.getState().addSection("skills", null);
    useEditorStore.getState().undo();
    expect(useEditorStore.getState().canRedo).toBe(true);
    useEditorStore.getState().addSection("education", null);
    expect(useEditorStore.getState().canRedo).toBe(false);
  });
});

describe("save", () => {
  it("flips the global cvStructureExists flag to true on a successful commit", async () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.saveCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    const ok = await useEditorStore.getState().save();

    expect(ok).toBe(true);
    expect(useStore.getState().cvStructureExists).toBe(true);
  });

  it("does not flip cvStructureExists when the save fails", async () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(new Error("HTTP 422: {\"detail\":\"bad\"}"));

    const ok = await useEditorStore.getState().save();

    expect(ok).toBe(false);
    expect(useStore.getState().cvStructureExists).toBe(null);
  });
});

// --- decks ---------------------------------------------------------------------------

function deck(id: string, over: Partial<CvDeckDTO> = {}): CvDeckDTO {
  return { id, name: null, auto_title: null, has_cv: true, is_default: false, ...over };
}

function stubIndex(decks: CvDeckDTO[], defaultId: string | null) {
  vi.mocked(api.listCvDecks).mockResolvedValue({ decks, default_id: defaultId });
}

describe("hydrateDecks", () => {
  it("adopts the index's default deck and loads its CV", async () => {
    stubIndex([deck("a"), deck("b", { is_default: true })], "b");
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().hydrateDecks();

    const st = useEditorStore.getState();
    expect(st.activeDeckId).toBe("b");
    expect(st.defaultDeckId).toBe("b");
    expect(api.getCvDeck).toHaveBeenCalledWith("b");
    expect(st.cv?.contact.name).toBe("Jane Doe");
  });

  it("falls back to the first deck when the index has no default", async () => {
    stubIndex([deck("a"), deck("b")], null);
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().hydrateDecks();

    expect(useEditorStore.getState().activeDeckId).toBe("a");
  });

  it("shows the empty state on a fresh install with no decks at all", async () => {
    stubIndex([], null);

    await useEditorStore.getState().hydrateDecks();

    const st = useEditorStore.getState();
    expect(st.activeDeckId).toBeNull();
    expect(st.cv).toBeNull();
    expect(api.getCvDeck).not.toHaveBeenCalled();
  });

  it("shows the empty state rather than throwing when the index cannot be read", async () => {
    vi.mocked(api.listCvDecks).mockRejectedValueOnce(new Error("HTTP 500: boom"));

    await useEditorStore.getState().hydrateDecks();

    expect(useEditorStore.getState().cv).toBeNull();
    expect(useEditorStore.getState().decks).toEqual([]);
  });
});

describe("save targets the active deck", () => {
  it("PUTs to the active deck, not the legacy default-deck alias", async () => {
    stubIndex([deck("d1")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.saveCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().save();

    expect(api.saveCvDeck).toHaveBeenCalledWith("d1", expect.objectContaining({
      contact: expect.objectContaining({ name: "Jane Doe" }),
    }));
    expect(api.saveCvStructure).not.toHaveBeenCalled();
  });

  it("mints a deck first when nothing is active yet, and adopts it", async () => {
    stubIndex([deck("new1")], "new1");
    loadSample();
    useEditorStore.setState({ activeDeckId: null });
    vi.mocked(api.createCvDeck).mockResolvedValueOnce(deck("new1"));
    vi.mocked(api.saveCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    const ok = await useEditorStore.getState().save();

    expect(ok).toBe(true);
    expect(api.createCvDeck).toHaveBeenCalledWith(null);
    expect(api.saveCvDeck).toHaveBeenCalledWith("new1", expect.anything());
    expect(useEditorStore.getState().activeDeckId).toBe("new1");
  });
});

describe("flushAndPersist gates every deck navigation", () => {
  it("persists a structural edit that left `dirty` false — the whole reason `unsaved` exists", async () => {
    // add/delete/reorder commit synchronously, so `dirty` is already false by the time the
    // user clicks another deck. Gating the flush on `dirty` would drop this edit silently.
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().deleteSection(useEditorStore.getState().cv!.sections[0].id);

    expect(useEditorStore.getState().dirty).toBe(false);
    expect(useEditorStore.getState().unsaved).toBe(true);

    vi.mocked(api.saveCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().switchDeck("d2");

    expect(api.saveCvDeck).toHaveBeenCalledWith("d1", expect.anything());
    expect(useEditorStore.getState().activeDeckId).toBe("d2");
  });

  it("switches without saving when nothing has changed since the last load", async () => {
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().switchDeck("d2");

    expect(api.saveCvDeck).not.toHaveBeenCalled();
    expect(useEditorStore.getState().activeDeckId).toBe("d2");
  });

  it("prompts on an unsavable buffer and stays put when the user cancels", async () => {
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "" });
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(
      new Error('HTTP 422: {"detail":"contact.name is required"}')
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    await useEditorStore.getState().switchDeck("d2");

    expect(confirmSpy).toHaveBeenCalled();
    expect(useEditorStore.getState().activeDeckId).toBe("d1");
    expect(api.getCvDeck).not.toHaveBeenCalled();
    expect(useEditorStore.getState().saveError).toBe("contact.name is required");
    confirmSpy.mockRestore();
  });

  it("discards and moves on when the user confirms", async () => {
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "" });
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(new Error('HTTP 422: {"detail":"bad"}'));
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await useEditorStore.getState().switchDeck("d2");

    expect(useEditorStore.getState().activeDeckId).toBe("d2");
    expect(useEditorStore.getState().cv?.contact.name).toBe("Jane Doe");
    confirmSpy.mockRestore();
  });
});

describe("deckBusy is claimed before the first await", () => {
  // Regression: switchDeck/newDeck/duplicateDeck used to set deckBusy only *after*
  // awaiting flushAndPersist(). A second click landing inside that await is a separate
  // task, so it sailed past the busy guard: both switches set activeDeckId and raced
  // their loadDeckInto, and whichever getCvDeck resolved last won the buffer. The buffer
  // could end up holding deck B's document while activeDeckId named deck C — and the next
  // save would then write B's content into C.
  //
  // A clean (not `unsaved`) buffer is what makes this a real discriminator: flushAndPersist
  // then returns true without issuing a PUT, so the *only* thing that can turn the second
  // click away is the busy flag itself, not an incidental save failure.
  it("refuses a second switchDeck issued before the first has awaited anything", async () => {
    stubIndex([deck("d1"), deck("d2"), deck("d3")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    expect(useEditorStore.getState().unsaved).toBe(false);
    vi.mocked(api.getCvDeck).mockResolvedValue(structuredClone(SAMPLE));

    const first = useEditorStore.getState().switchDeck("d2");
    // The flag must already be up here — this is the assertion the fix is about.
    expect(useEditorStore.getState().deckBusy).toBe(true);
    const second = useEditorStore.getState().switchDeck("d3"); // must be refused
    await Promise.all([first, second]);

    expect(api.saveCvDeck).not.toHaveBeenCalled();
    expect(api.getCvDeck).toHaveBeenCalledTimes(1);
    expect(api.getCvDeck).toHaveBeenCalledWith("d2");
    expect(useEditorStore.getState().activeDeckId).toBe("d2");
    expect(useEditorStore.getState().deckBusy).toBe(false);
  });

  it("claims the flag synchronously in newDeck and duplicateDeck too", async () => {
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.createCvDeck).mockResolvedValue({
      id: "new1", name: null, auto_title: null, has_cv: false, is_default: false,
    } as CvDeckDTO);

    const p1 = useEditorStore.getState().newDeck();
    expect(useEditorStore.getState().deckBusy).toBe(true);
    await p1;
    expect(useEditorStore.getState().deckBusy).toBe(false);

    vi.mocked(api.duplicateCvDeck).mockResolvedValue({
      id: "dup1", name: null, auto_title: null, has_cv: true, is_default: false,
    } as CvDeckDTO);
    vi.mocked(api.getCvDeck).mockResolvedValue(structuredClone(SAMPLE));
    const p2 = useEditorStore.getState().duplicateDeck("d2");
    expect(useEditorStore.getState().deckBusy).toBe(true);
    await p2;
    expect(useEditorStore.getState().deckBusy).toBe(false);
  });

  it("releases deckBusy when the switch is abandoned at the discard prompt", async () => {
    // The early return now sits inside the try, so the finally must still clear the flag —
    // otherwise one cancelled switch would wedge the whole rail for the rest of the session.
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "" });
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(new Error('HTTP 422: {"detail":"bad"}'));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    await useEditorStore.getState().switchDeck("d2");

    expect(useEditorStore.getState().activeDeckId).toBe("d1");
    expect(useEditorStore.getState().deckBusy).toBe(false);

    // ...and a later switch still works, i.e. the rail is not wedged.
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));
    confirmSpy.mockReturnValue(true);
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(new Error('HTTP 422: {"detail":"bad"}'));
    await useEditorStore.getState().switchDeck("d2");
    expect(useEditorStore.getState().activeDeckId).toBe("d2");
    confirmSpy.mockRestore();
  });
});

describe("newDeck", () => {
  it("adopts the fresh slot and shows the empty state without saving into it", async () => {
    stubIndex([deck("d1"), deck("n1", { has_cv: false })], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.createCvDeck).mockResolvedValueOnce(deck("n1", { has_cv: false }));

    await useEditorStore.getState().newDeck();

    const st = useEditorStore.getState();
    expect(st.activeDeckId).toBe("n1");
    expect(st.cv).toBeNull();
    expect(api.saveCvDeck).not.toHaveBeenCalled();
  });

  it("leaves an untouched new slot clean, so switching straight back out PUTs nothing", async () => {
    stubIndex([deck("d1"), deck("n1", { has_cv: false })], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    vi.mocked(api.createCvDeck).mockResolvedValueOnce(deck("n1", { has_cv: false }));
    await useEditorStore.getState().newDeck();

    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));
    await useEditorStore.getState().switchDeck("d1");

    expect(useEditorStore.getState().unsaved).toBe(false);
    expect(api.saveCvDeck).not.toHaveBeenCalled();
    expect(useEditorStore.getState().activeDeckId).toBe("d1");
  });
});

describe("duplicateDeck refuses rather than prompting", () => {
  it("never copies stale disk content when the live buffer will not save", async () => {
    // The server duplicates the deck *file*. flushAndPersist's discard branch would copy
    // what is on disk while the user believes they copied what is on screen.
    stubIndex([deck("d1")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "" });
    vi.mocked(api.saveCvDeck).mockRejectedValueOnce(new Error('HTTP 422: {"detail":"no name"}'));
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    await useEditorStore.getState().duplicateDeck("d1");

    expect(api.duplicateCvDeck).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
    expect(useEditorStore.getState().saveError).toBe("no name");
    confirmSpy.mockRestore();
  });

  it("flushes a valid dirty buffer first, then copies and switches to the copy", async () => {
    stubIndex([deck("d1"), deck("d2")], "d1");
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "Jane Q. Doe" });
    vi.mocked(api.saveCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));
    vi.mocked(api.duplicateCvDeck).mockResolvedValueOnce(deck("d2"));
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().duplicateDeck("d1");

    expect(api.saveCvDeck).toHaveBeenCalledWith("d1", expect.anything());
    expect(api.duplicateCvDeck).toHaveBeenCalledWith("d1", expect.stringContaining("Jane Doe"));
    expect(useEditorStore.getState().activeDeckId).toBe("d2");
  });
});

describe("deleteDeck", () => {
  it("lands on the new default when the active deck is the one deleted", async () => {
    loadSample();
    useEditorStore.setState({
      activeDeckId: "d1",
      decks: [deck("d1"), deck("d2")],
      defaultDeckId: "d1",
    });
    stubIndex([deck("d2", { is_default: true })], "d2");
    vi.mocked(api.deleteCvDeck).mockResolvedValueOnce(undefined);
    vi.mocked(api.getCvDeck).mockResolvedValueOnce(structuredClone(SAMPLE));

    await useEditorStore.getState().deleteDeck("d1");

    const st = useEditorStore.getState();
    expect(st.activeDeckId).toBe("d2");
    expect(st.defaultDeckId).toBe("d2");
    expect(api.getCvDeck).toHaveBeenCalledWith("d2");
  });

  it("leaves the buffer alone when some other deck is deleted", async () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1", decks: [deck("d1"), deck("d2")] });
    stubIndex([deck("d1", { is_default: true })], "d1");
    vi.mocked(api.deleteCvDeck).mockResolvedValueOnce(undefined);

    await useEditorStore.getState().deleteDeck("d2");

    expect(useEditorStore.getState().activeDeckId).toBe("d1");
    expect(useEditorStore.getState().cv?.contact.name).toBe("Jane Doe");
    expect(api.getCvDeck).not.toHaveBeenCalled();
  });

  it("falls back to the empty state when the last deck goes", async () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1", decks: [deck("d1")], defaultDeckId: "d1" });
    stubIndex([], null);
    vi.mocked(api.deleteCvDeck).mockResolvedValueOnce(undefined);

    await useEditorStore.getState().deleteDeck("d1");

    const st = useEditorStore.getState();
    expect(st.activeDeckId).toBeNull();
    expect(st.cv).toBeNull();
  });
});

describe("deckLabel", () => {
  it("prefers the custom name over everything else", () => {
    useEditorStore.setState({ activeDeckId: "d1" });
    expect(useEditorStore.getState().deckLabel(deck("d1", { name: "Backend CV", auto_title: "Jane Doe" })))
      .toBe("Backend CV");
  });

  it("tracks the live buffer for the active row, so a contact rename retitles it as you type", () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    useEditorStore.getState().updateContact({ name: "Renamed Live" });

    expect(useEditorStore.getState().deckLabel(deck("d1", { auto_title: "Jane Doe" })))
      .toBe("Renamed Live");
  });

  it("uses the server's auto_title for every non-active row", () => {
    loadSample();
    useEditorStore.setState({ activeDeckId: "d1" });
    expect(useEditorStore.getState().deckLabel(deck("d2", { auto_title: "John Smith" })))
      .toBe("John Smith");
  });

  it("falls back to the untitled string when a slot has no name at all", () => {
    useEditorStore.setState({ activeDeckId: null });
    expect(useEditorStore.getState().deckLabel(deck("d9", { has_cv: false }))).toBe("Untitled");
  });
});

describe("setDefaultDeck", () => {
  it("moves the star optimistically and rolls back when the server refuses", async () => {
    useEditorStore.setState({ decks: [deck("d1"), deck("d2")], defaultDeckId: "d1" });
    stubIndex([deck("d1", { is_default: true }), deck("d2")], "d1");
    vi.mocked(api.patchCvDeck).mockRejectedValueOnce(new Error("HTTP 500: nope"));

    await useEditorStore.getState().setDefaultDeck("d2");

    expect(useEditorStore.getState().defaultDeckId).toBe("d1");
  });
});

describe("renameDeck", () => {
  it("sends null for an empty name so the row falls back to the auto title", async () => {
    useEditorStore.setState({ renameId: "d1", renameDraft: "  " });
    stubIndex([deck("d1")], "d1");
    vi.mocked(api.patchCvDeck).mockResolvedValueOnce(deck("d1"));

    await useEditorStore.getState().renameDeck("d1", "  ");

    expect(api.patchCvDeck).toHaveBeenCalledWith("d1", { name: null });
    expect(useEditorStore.getState().renameId).toBeNull();
  });
});

describe("importFromJsonFile — adopting an existing CV .json", () => {
  function jsonFile(body: string, name = "cv.json"): File {
    return new File([body], name, { type: "application/json" });
  }

  it("fills the buffer from a valid CV document without touching the deck API", async () => {
    await useEditorStore.getState().importFromJsonFile(jsonFile(JSON.stringify(SAMPLE)));

    const st = useEditorStore.getState();
    expect(st.importError).toBeNull();
    expect(st.cv?.contact.name).toBe("Jane Doe");
    expect(st.cv?.sections.map((s) => s.name)).toEqual(["Summary", "Experience", "Skills"]);
    // The import is buffer-only — COMMIT is still what writes it. A deck minted here on a
    // file the server later rejects would leave an unassignable `has_cv: false` row behind.
    expect(api.createCvDeck).not.toHaveBeenCalled();
    expect(api.saveCvDeck).not.toHaveBeenCalled();
    // Unlike INIT BLANK's pristine skeleton, this is content: a deck switch must flush it.
    expect(st.unsaved).toBe(true);
  });

  it("round-trips the imported document back out unchanged", async () => {
    await useEditorStore.getState().importFromJsonFile(jsonFile(JSON.stringify(SAMPLE)));
    expect(exportJson(useEditorStore.getState().cv!)).toEqual(exportJson(toEditor(SAMPLE)));
  });

  it("reports unparseable JSON and leaves the buffer alone", async () => {
    await useEditorStore.getState().importFromJsonFile(jsonFile("{not json at all"));

    const st = useEditorStore.getState();
    expect(st.cv).toBeNull();
    expect(st.importError).toBe("That file isn't valid JSON — it couldn't be parsed.");
    expect(st.unsaved).toBe(false);
  });

  it("reports well-formed JSON that isn't a CV structure, instead of throwing", async () => {
    // The load-bearing case: toEditor() dereferences `cv.contact` unconditionally, so
    // without the catch this rejects and the buffer is left mid-import. Removing the
    // try/catch in importFromJsonFile must fail THIS test.
    for (const body of ["[]", "{}", "null", '{"sections": []}', '{"contact": {"name": "x"}, "sections": 5}']) {
      useEditorStore.getState().reset();
      await expect(
        useEditorStore.getState().importFromJsonFile(jsonFile(body))
      ).resolves.toBeUndefined();
      const st = useEditorStore.getState();
      expect(st.cv, `body=${body}`).toBeNull();
      expect(st.importError, `body=${body}`).toBe(
        'That JSON isn\'t a CV structure. It needs a "contact" object and a "sections" list.'
      );
    }
  });

  it("clears a previous import error once a good file lands", async () => {
    await useEditorStore.getState().importFromJsonFile(jsonFile("nope"));
    expect(useEditorStore.getState().importError).not.toBeNull();

    await useEditorStore.getState().importFromJsonFile(jsonFile(JSON.stringify(SAMPLE)));
    expect(useEditorStore.getState().importError).toBeNull();
  });
});
