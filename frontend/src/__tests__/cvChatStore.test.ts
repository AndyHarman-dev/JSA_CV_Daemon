import { describe, it, expect, beforeEach, vi } from "vitest";

vi.mock("../api", () => ({
  api: {
    getDeckChat: vi.fn(),
    sendDeckChat: vi.fn(),
    clearDeckChat: vi.fn(),
  },
}));

import { useCvChatStore, hashCv } from "../cvChatStore";
import { useEditorStore, exportJson } from "../editorStore";
import { api } from "../api";
import type { CVDocument, ChatTurnDTO } from "../types";

const SAMPLE: CVDocument = {
  contact: { name: "Jane Doe", email: "jane@x.com", links: [] },
  sections: [
    { name: "Summary", text: "Backend engineer.", items: [], entries: [] },
    {
      name: "Experience",
      items: [],
      entries: [
        { heading: "Senior Engineer", subheading: "Acme", bullets: ["Did X"], links: [] },
      ],
    },
  ],
};

function loadSample(deckId = "deck1") {
  useEditorStore.getState().load(structuredClone(SAMPLE));
  useEditorStore.setState({ activeDeckId: deckId });
}

function agentTurn(overrides: Partial<ChatTurnDTO> = {}): ChatTurnDTO {
  return {
    id: "agent1",
    role: "agent",
    scope: { type: "section", section_index: 0 },
    text: "Tightened it.",
    question: null,
    reasoning: "",
    items: [{ label: "Summary · text", before: "old", after: "new" }],
    document: structuredClone(SAMPLE),
    status: "pending",
    files: [],
    base_hash: "",
    created_at: "2026-09-16T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  useEditorStore.getState().reset();
  useCvChatStore.setState({
    open: false,
    collapsed: false,
    scope: null,
    deckId: null,
    turns: [],
    input: "",
    attach: [],
    busy: false,
    elapsed: 0,
    taskId: null,
    reasoning: "",
    content: "",
    auto: false,
    flashKey: null,
    showReason: {},
    error: null,
  });
  vi.clearAllMocks();
});

describe("openChat — scope change marker turn", () => {
  it("pushes a scope marker turn only when the scope actually changes", () => {
    useCvChatStore.setState({ deckId: "deck1" });
    useCvChatStore.getState().openChat({ type: "section", sectionId: "s1" });
    expect(useCvChatStore.getState().turns).toHaveLength(0); // first open, no prior scope

    useCvChatStore.getState().openChat({ type: "entry", sectionId: "s1", entryId: "e1" });
    expect(useCvChatStore.getState().turns).toHaveLength(1);
    expect(useCvChatStore.getState().turns[0].role).toBe("scope");

    // Re-opening the SAME scope must not push another marker.
    useCvChatStore.getState().openChat({ type: "entry", sectionId: "s1", entryId: "e1" });
    expect(useCvChatStore.getState().turns).toHaveLength(1);
  });
});

describe("send — FormData shape and scope→index resolution", () => {
  it("resolves the client-id scope to indices and posts the exported CV + base_hash", async () => {
    loadSample();
    const cv = useEditorStore.getState().cv!;
    const sectionId = cv.sections[1].id;
    useCvChatStore.setState({ deckId: "deck1", scope: { type: "section", sectionId }, input: "tighten" });

    vi.mocked(api.sendDeckChat).mockResolvedValue({
      task_id: "t1",
      turns: [
        { id: "u1", role: "user", scope: {}, text: "tighten", question: null, reasoning: "", items: [], document: null, status: "none", files: [], base_hash: "", created_at: "x" },
        agentTurn({ status: "pending" }),
      ],
      rejected: [],
    });

    await useCvChatStore.getState().send();

    expect(api.sendDeckChat).toHaveBeenCalledTimes(1);
    const [deckId, payload, files] = vi.mocked(api.sendDeckChat).mock.calls[0];
    expect(deckId).toBe("deck1");
    expect(payload.scope).toEqual({ type: "section", section_index: 1 });
    expect(payload.instruction).toBe("tighten");
    expect(payload.cv).toEqual(exportJson(cv));
    expect(payload.base_hash).toBe(hashCv(exportJson(cv)));
    expect(files).toEqual([]);
    expect(useCvChatStore.getState().turns).toHaveLength(2);
    expect(useCvChatStore.getState().busy).toBe(false);
  });

  it("surfaces a per-op rejection instead of silently dropping it (turn still succeeds)", async () => {
    // Regression: jsa/pipeline/cv_chat.py::_apply_ops rejects a bad/unknown-id op
    // without failing the whole turn, and the server reports it in the response's
    // top-level `rejected` array -- but the turn itself can still come back
    // `status: "pending"` with the model's `answer` confidently describing a change
    // that didn't happen. The client used to read `res.turns` only and drop
    // `res.rejected` entirely, leaving the user with no indication anything failed.
    loadSample();
    useCvChatStore.setState({ deckId: "deck1", scope: { type: "contact" }, input: "fix the email" });

    vi.mocked(api.sendDeckChat).mockResolvedValue({
      task_id: "t1",
      turns: [
        { id: "u1", role: "user", scope: {}, text: "fix the email", question: null, reasoning: "", items: [], document: null, status: "none", files: [], base_hash: "", created_at: "x" },
        agentTurn({ status: "pending", items: [] }),
      ],
      rejected: ["replace_contact: contact must name at least one of ('name', 'email', 'phone', 'location', 'links')"],
    });

    await useCvChatStore.getState().send();

    expect(useCvChatStore.getState().error).toContain("replace_contact");
  });
});

describe("auto-mode", () => {
  it("auto-applies a fresh (non-stale) turn", async () => {
    loadSample();
    const cv = useEditorStore.getState().cv!;
    useCvChatStore.setState({ deckId: "deck1", scope: { type: "cv" }, input: "x", auto: true });

    vi.mocked(api.sendDeckChat).mockResolvedValue({
      task_id: "t1",
      turns: [
        { id: "u1", role: "user", scope: {}, text: "x", question: null, reasoning: "", items: [], document: null, status: "none", files: [], base_hash: "", created_at: "x" },
        agentTurn({ status: "pending", base_hash: hashCv(exportJson(cv)) }),
      ],
      rejected: [],
    });

    await useCvChatStore.getState().send();

    const applied = useCvChatStore.getState().turns.find((t) => t.role === "agent")!;
    expect(applied.status).toBe("auto");
    // The document actually landed in the editor buffer.
    expect(useEditorStore.getState().unsaved).toBe(true);
  });

  it("refuses to auto-apply when the buffer changed while the request was in flight (D4)", async () => {
    loadSample();
    const cv = useEditorStore.getState().cv!;
    const baseHashAtSendTime = hashCv(exportJson(cv));
    useCvChatStore.setState({ deckId: "deck1", scope: { type: "cv" }, input: "x", auto: true });

    vi.mocked(api.sendDeckChat).mockImplementation(async () => {
      // Simulate a concurrent edit landing while the request is in flight.
      useEditorStore.getState().updateContact({ name: "Changed mid-flight" });
      useEditorStore.getState().commit();
      return {
        task_id: "t1",
        turns: [
          { id: "u1", role: "user", scope: {}, text: "x", question: null, reasoning: "", items: [], document: null, status: "none", files: [], base_hash: "", created_at: "x" },
          agentTurn({ status: "pending", base_hash: baseHashAtSendTime }),
        ],
        rejected: [],
      };
    });

    await useCvChatStore.getState().send();

    const turn = useCvChatStore.getState().turns.find((t) => t.role === "agent")!;
    expect(turn.status).toBe("stale");
    // The stale document must NOT have been written into the live buffer.
    expect(useEditorStore.getState().cv!.contact.name).toBe("Changed mid-flight");
  });
});

describe("applyTurn — deck identity and staleness guards", () => {
  it("refuses to apply a turn whose deckId no longer matches the active deck", () => {
    loadSample("deck1");
    useCvChatStore.setState({
      deckId: "deck1",
      turns: [{ ...agentTurn(), deckId: "deck-old", base_hash: "" }],
    });
    useEditorStore.setState({ activeDeckId: "deck-new" });

    useCvChatStore.getState().applyTurn("agent1");

    expect(useCvChatStore.getState().turns[0].status).toBe("error");
  });

  it("fails a rehydrated turn explicitly when there is no live editor buffer to apply into", () => {
    useEditorStore.getState().reset();
    useEditorStore.setState({ activeDeckId: "deck1" }); // deck identity must match -- this
    // test targets the null-cv guard specifically, not the deck-identity guard above it.
    useCvChatStore.setState({
      deckId: "deck1",
      turns: [{ ...agentTurn(), deckId: "deck1", base_hash: "" }],
    });

    useCvChatStore.getState().applyTurn("agent1");

    expect(useCvChatStore.getState().turns[0].status).toBe("error");
  });

  it("marks a turn stale (not applied) when base_hash no longer matches the live buffer", () => {
    loadSample();
    useCvChatStore.setState({
      deckId: "deck1",
      turns: [{ ...agentTurn(), deckId: "deck1", base_hash: "deadbeef" }],
    });

    useCvChatStore.getState().applyTurn("agent1");

    expect(useCvChatStore.getState().turns[0].status).toBe("stale");
  });

  it("applies a matching, in-scope turn as one undoable edit", () => {
    loadSample();
    const cv = useEditorStore.getState().cv!;
    const proposed: CVDocument = { ...structuredClone(SAMPLE), contact: { ...SAMPLE.contact, name: "New Name" } };
    useCvChatStore.setState({
      deckId: "deck1",
      turns: [{ ...agentTurn({ document: proposed }), deckId: "deck1", base_hash: hashCv(exportJson(cv)) }],
    });

    useCvChatStore.getState().applyTurn("agent1");

    expect(useCvChatStore.getState().turns[0].status).toBe("applied");
    expect(useEditorStore.getState().cv!.contact.name).toBe("New Name");
    expect(useCvChatStore.getState().flashKey).toBe("agent1");
  });
});

describe("discardTurn", () => {
  it("marks a turn discarded without touching the editor buffer", () => {
    loadSample();
    const nameBefore = useEditorStore.getState().cv!.contact.name;
    useCvChatStore.setState({
      deckId: "deck1",
      turns: [{ ...agentTurn(), deckId: "deck1" }],
    });

    useCvChatStore.getState().discardTurn("agent1");

    expect(useCvChatStore.getState().turns[0].status).toBe("discarded");
    expect(useEditorStore.getState().cv!.contact.name).toBe(nameBefore);
  });
});

describe("onChunk — single-flight gating", () => {
  it("accumulates content/reasoning only while busy", () => {
    useCvChatStore.getState().onChunk({ type: "chat_chunk", task_id: "t1", kind: "reasoning", text: "ignored" });
    expect(useCvChatStore.getState().reasoning).toBe("");

    useCvChatStore.setState({ busy: true });
    useCvChatStore.getState().onChunk({ type: "chat_chunk", task_id: "t1", kind: "reasoning", text: "thinking" });
    expect(useCvChatStore.getState().reasoning).toBe("thinking");
  });
});

describe("newThread", () => {
  it("clears the thread via DELETE and empties local turns", async () => {
    useCvChatStore.setState({ deckId: "deck1", turns: [{ ...agentTurn(), deckId: "deck1" }] });
    vi.mocked(api.clearDeckChat).mockResolvedValue(undefined);

    await useCvChatStore.getState().newThread();

    expect(api.clearDeckChat).toHaveBeenCalledWith("deck1");
    expect(useCvChatStore.getState().turns).toEqual([]);
  });
});
