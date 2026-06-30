import { describe, it, expect, beforeEach } from "vitest";
import { useEditorStore, exportJson, inferKind, toEditor } from "../editorStore";
import type { CVDocument } from "../types";

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
