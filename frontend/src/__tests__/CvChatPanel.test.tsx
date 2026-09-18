// First render harness for CvChatPanel/CvDiffCard -- the render/click layer on top of
// cvChatStore's already-exhaustively-tested pure logic (see cvChatStore.test.ts). These
// tests confirm the DOM wiring: a button click reaches the right store action end to end.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CvChatPanel } from "../components/cv-editor/CvChatPanel";
import { useCvChatStore, hashCv, type ClientChatTurn } from "../cvChatStore";
import { useEditorStore, exportJson } from "../editorStore";
import { useStore } from "../store";
import type { CVDocument } from "../types";

vi.mock("../api", () => ({
  api: { getDeckChat: vi.fn(), sendDeckChat: vi.fn(), clearDeckChat: vi.fn() },
}));

const SAMPLE: CVDocument = {
  contact: { name: "Jane Doe", email: "jane@x.com", links: [] },
  sections: [{ name: "Summary", text: "Backend engineer.", items: [], entries: [] }],
};

const mainRef = { current: null };

function agentTurn(overrides: Partial<ClientChatTurn> = {}): ClientChatTurn {
  return {
    id: "agent1",
    role: "agent",
    scope: { type: "cv" },
    text: "Tightened it.",
    question: null,
    reasoning: "",
    items: [{ label: "Contact · name", before: "Jane Doe", after: "J. Doe" }],
    document: null,
    status: "pending",
    files: [],
    base_hash: "",
    created_at: "2026-09-16T00:00:00Z",
    deckId: "deck1",
    ...overrides,
  };
}

beforeEach(() => {
  useStore.setState({ language: "en" });
  useEditorStore.getState().reset();
  useEditorStore.getState().load(structuredClone(SAMPLE));
  useEditorStore.setState({ activeDeckId: "deck1" });
  useCvChatStore.setState({
    open: true,
    collapsed: false,
    scope: { type: "cv" },
    deckId: "deck1",
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
});

describe("CvChatPanel", () => {
  it("renders nothing when there is no CV loaded", () => {
    useEditorStore.getState().reset();
    const { container } = render(<CvChatPanel mainRef={mainRef} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the collapsed pill when the panel is closed", () => {
    useCvChatStore.setState({ open: false });
    render(<CvChatPanel mainRef={mainRef} />);
    expect(screen.getByTestId("cv-chat-pill")).toBeInTheDocument();
    expect(screen.queryByTestId("cv-chat-panel")).not.toBeInTheDocument();
  });

  it("clicking APPLY on a matching, fresh diff writes the proposed document into the editor buffer", () => {
    const cv = useEditorStore.getState().cv!;
    const proposed: CVDocument = { ...structuredClone(SAMPLE), contact: { ...SAMPLE.contact, name: "J. Doe" } };
    useCvChatStore.setState({
      turns: [agentTurn({ document: proposed, base_hash: hashCv(exportJson(cv)) })],
    });

    render(<CvChatPanel mainRef={mainRef} />);
    fireEvent.click(screen.getByText("APPLY"));

    expect(useEditorStore.getState().cv!.contact.name).toBe("J. Doe");
    expect(useCvChatStore.getState().turns[0].status).toBe("applied");
  });

  it("clicking DISCARD marks the turn discarded without touching the editor buffer", () => {
    const cv = useEditorStore.getState().cv!;
    const proposed: CVDocument = { ...structuredClone(SAMPLE), contact: { ...SAMPLE.contact, name: "J. Doe" } };
    useCvChatStore.setState({
      turns: [agentTurn({ document: proposed, base_hash: hashCv(exportJson(cv)) })],
    });

    render(<CvChatPanel mainRef={mainRef} />);
    fireEvent.click(screen.getByText("DISCARD"));

    expect(useEditorStore.getState().cv!.contact.name).toBe("Jane Doe");
    expect(useCvChatStore.getState().turns[0].status).toBe("discarded");
  });

  it("a stale turn shows RE-RUN instead of APPLY/DISCARD, and APPLY does nothing if clicked anyway", () => {
    useCvChatStore.setState({
      turns: [agentTurn({ document: structuredClone(SAMPLE), status: "stale", base_hash: "deadbeef" })],
    });

    render(<CvChatPanel mainRef={mainRef} />);
    expect(screen.getByText("RE-RUN")).toBeInTheDocument();
    expect(screen.queryByText("APPLY")).not.toBeInTheDocument();
    expect(screen.queryByText("DISCARD")).not.toBeInTheDocument();
  });

  it("a quick-action chip calls send with that action's key", () => {
    const sendSpy = vi.spyOn(useCvChatStore.getState(), "send").mockResolvedValue(undefined);
    render(<CvChatPanel mainRef={mainRef} />);

    fireEvent.click(screen.getByText("ONE PAGE"));

    expect(sendSpy).toHaveBeenCalledWith("one_page");
  });

  it("the AUTO toggle flips cvChatStore.auto", () => {
    render(<CvChatPanel mainRef={mainRef} />);
    fireEvent.click(screen.getByText("AUTO"));
    expect(useCvChatStore.getState().auto).toBe(true);
  });
});
