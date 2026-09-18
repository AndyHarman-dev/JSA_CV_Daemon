// First render harness for BlocksView -- exercises the CV Editor AI chat's corner triggers
// (design hand-off's "5b/5h"): one per editable unit, opening the chat panel with the right
// scope, a scope-change pushing a marker turn, and the dim/ring carve-out for an entry scope
// not dimming its own parent section.
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { BlocksView } from "../components/cv-editor/BlocksView";
import { useEditorStore } from "../editorStore";
import { useCvChatStore } from "../cvChatStore";
import { useStore } from "../store";
import type { CVDocument } from "../types";

vi.mock("../api", () => ({
  api: {},
}));

const SAMPLE: CVDocument = {
  contact: { name: "Jane Doe", email: "jane@x.com", links: [] },
  sections: [
    { name: "Summary", text: "Backend engineer.", items: [], entries: [] },
    {
      name: "Experience",
      items: [],
      entries: [
        { heading: "Senior Engineer", subheading: "Acme", bullets: ["Did X"], links: [] },
        { heading: "Engineer", subheading: "Beta", bullets: ["Did Y"], links: [] },
      ],
    },
    {
      name: "Skills",
      items: [],
      entries: [{ heading: "Languages", bullets: ["TypeScript", "Python"], links: [] }],
    },
  ],
};

beforeEach(() => {
  useStore.setState({ language: "en" });
  useEditorStore.getState().reset();
  useEditorStore.getState().load(structuredClone(SAMPLE));
  useCvChatStore.setState({
    open: false,
    collapsed: false,
    scope: null,
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

describe("BlocksView -- CV chat corner triggers", () => {
  it("renders one corner trigger per editable unit (contact, each section, each entry, each skill group)", () => {
    render(<BlocksView />);
    const triggers = screen.getAllByTestId("chat-corner-trigger");
    // 1 contact + 3 sections + 2 experience entries + 1 skills-group row = 7
    expect(triggers.length).toBe(7);
  });

  it("clicking the contact trigger opens the panel scoped to contact", () => {
    render(<BlocksView />);
    const contactCard = screen.getByText("IDENTITY").closest(".cvunit")!;
    fireEvent.click(within(contactCard as HTMLElement).getByTestId("chat-corner-trigger"));

    expect(useCvChatStore.getState().open).toBe(true);
    expect(useCvChatStore.getState().scope).toEqual({ type: "contact" });
  });

  it("clicking a section trigger opens the panel scoped to that section", () => {
    render(<BlocksView />);
    const sectionHeading = screen.getByDisplayValue("Skills");
    const sectionCard = sectionHeading.closest(".cvunit") as HTMLElement;
    const cv = useEditorStore.getState().cv!;
    const skillsId = cv.sections[2].id;

    // The section's own trigger is a DIRECT child of the SectionCard's .cvunit wrapper --
    // an entry-level trigger nested deeper inside (e.g. a skills-group row) shares the same
    // testid, so a plain descendant query would pick up the wrong one. Using `.children`
    // (not `:scope >`, which jsdom's selector engine does not restrict correctly) to find it.
    const directTrigger = Array.from(sectionCard.children).find(
      (el) => el.getAttribute("data-testid") === "chat-corner-trigger"
    )!;
    fireEvent.click(directTrigger);

    expect(useCvChatStore.getState().scope).toEqual({ type: "section", sectionId: skillsId });
  });

  it("clicking an entry trigger opens the panel scoped to that entry", () => {
    render(<BlocksView />);
    const cv = useEditorStore.getState().cv!;
    const experienceSection = cv.sections[1];
    const firstEntryId = experienceSection.entries[0].id;

    const entryHeading = screen.getByDisplayValue("Senior Engineer");
    const entryUnit = entryHeading.closest(".cvunit")!;
    fireEvent.click(within(entryUnit as HTMLElement).getByTestId("chat-corner-trigger"));

    expect(useCvChatStore.getState().scope).toEqual({
      type: "entry",
      sectionId: experienceSection.id,
      entryId: firstEntryId,
    });
  });

  it("pushes a scope marker turn when the scope changes across two trigger clicks, not on the first", () => {
    render(<BlocksView />);
    const contactCard = screen.getByText("IDENTITY").closest(".cvunit")!;
    fireEvent.click(within(contactCard as HTMLElement).getByTestId("chat-corner-trigger"));
    expect(useCvChatStore.getState().turns).toHaveLength(0);

    const entryHeading = screen.getByDisplayValue("Senior Engineer");
    const entryUnit = entryHeading.closest(".cvunit")!;
    fireEvent.click(within(entryUnit as HTMLElement).getByTestId("chat-corner-trigger"));

    expect(useCvChatStore.getState().turns).toHaveLength(1);
    expect(useCvChatStore.getState().turns[0].role).toBe("scope");
  });

  it("dims sibling sections but not the scoped entry's own parent section (the design's carve-out)", () => {
    render(<BlocksView />);
    const cv = useEditorStore.getState().cv!;
    const experienceSection = cv.sections[1];
    act(() => {
      useCvChatStore.setState({
        open: true,
        scope: { type: "entry", sectionId: experienceSection.id, entryId: experienceSection.entries[0].id },
      });
    });

    const experienceHeading = screen.getByDisplayValue("Experience");
    const experienceUnit = experienceHeading.closest(".cvunit") as HTMLElement;
    const summaryField = screen.getByPlaceholderText("Write a short professional summary…");
    const summaryUnit = summaryField.closest(".cvunit") as HTMLElement;

    expect(experienceUnit.style.opacity).not.toBe("0.3");
    expect(summaryUnit.style.opacity).toBe("0.3");
  });
});
