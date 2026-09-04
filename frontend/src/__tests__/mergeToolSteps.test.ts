import { describe, it, expect } from "vitest";
import { segmentReasoning } from "../lib/reasoningSteps";
import { mergeToolSteps, type ToolMark } from "../lib/mergeToolSteps";

const namesOf = (steps: ReturnType<typeof mergeToolSteps>) =>
  steps.map((s) => (s.kind === "tool" ? `tool:${s.name}` : `text:${s.text}`));

const mark = (name: string, at: number, overrides: Partial<ToolMark> = {}): ToolMark => ({
  name,
  detail: "",
  ok: true,
  at,
  ...overrides,
});

// A trailing "\n\nTail" forces every paragraph before it to CLOSE (segmentReasoning
// leaves the very last chunk open when nothing follows it) — see reasoningSteps.ts's
// prefix-stability rules. Using this consistently gives every test below a known,
// fully-closed `closed`/`bounds` pair to interleave marks into.
const THREE_CLOSED = "First thought.\n\nSecond thought.\n\nThird thought.\n\nTail";

describe("mergeToolSteps", () => {
  it("returns the closed steps unchanged when there are no marks", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const merged = mergeToolSteps(segments, segments.bounds ?? [], []);
    expect(namesOf(merged)).toEqual(["text:First thought.", "text:Second thought.", "text:Third thought."]);
  });

  it("places a mark right after the closed step it arrived no later than", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    // bounds[0] is the end offset of "First thought." — a mark at exactly that offset
    // belongs right after it (before "Second thought.").
    const merged = mergeToolSteps(segments, bounds, [mark("read_file", bounds[0])]);
    expect(namesOf(merged)).toEqual([
      "text:First thought.",
      "tool:read_file",
      "text:Second thought.",
      "text:Third thought.",
    ]);
  });

  it("places a mark before the first closed step when it arrived before any text", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    // A mark whose `at` is 0 is <= bounds[0] (the first step's own end offset), so per
    // the documented rule it still lands right after closed[0], not before it — there is
    // no boundary earlier than bounds[0] to anchor against.
    const merged = mergeToolSteps(segments, bounds, [mark("early_tool", 0)]);
    expect(namesOf(merged)).toEqual([
      "text:First thought.",
      "tool:early_tool",
      "text:Second thought.",
      "text:Third thought.",
    ]);
  });

  it("appends marks that arrive after the last closed step's bound at the end", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    const lastBound = bounds[bounds.length - 1];
    const merged = mergeToolSteps(segments, bounds, [mark("late_tool", lastBound)]);
    expect(namesOf(merged)).toEqual([
      "text:First thought.",
      "text:Second thought.",
      "text:Third thought.",
      "tool:late_tool",
    ]);
  });

  it("keeps arrival order for marks with equal offsets (stable)", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    const merged = mergeToolSteps(segments, bounds, [
      mark("alpha", bounds[0]),
      mark("beta", bounds[0]),
      mark("gamma", bounds[0]),
    ]);
    expect(namesOf(merged)).toEqual([
      "text:First thought.",
      "tool:alpha",
      "tool:beta",
      "tool:gamma",
      "text:Second thought.",
      "text:Third thought.",
    ]);
  });

  it("sorts out-of-arrival-order marks by offset before placing them", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    const merged = mergeToolSteps(segments, bounds, [mark("late", bounds[1]), mark("early", bounds[0])]);
    expect(namesOf(merged)).toEqual([
      "text:First thought.",
      "tool:early",
      "text:Second thought.",
      "tool:late",
      "text:Third thought.",
    ]);
  });

  it("drops `ok` and empty detail — the tool ReasoningStep variant carries only name/detail", () => {
    const segments = segmentReasoning(THREE_CLOSED);
    const bounds = segments.bounds ?? [];
    const merged = mergeToolSteps(segments, bounds, [mark("read_file", bounds[0], { detail: "", ok: false })]);
    const toolStep = merged.find((s) => s.kind === "tool");
    expect(toolStep).toEqual({ kind: "tool", name: "read_file", detail: null });
  });

  it("returns an empty array for empty input with no marks", () => {
    const segments = segmentReasoning("");
    expect(mergeToolSteps(segments, segments.bounds ?? [], [])).toEqual([]);
  });
});
