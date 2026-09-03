import { describe, it, expect } from "vitest";
import { segmentReasoning, HARD_CAP, SENTENCE_FLOOR, type ReasoningStep } from "../lib/reasoningSteps";

const textOf = (steps: ReasoningStep[]) => steps.map((s) => (s.kind === "text" ? s.text : `tool:${s.name}`));

describe("segmentReasoning — rule 1, blank lines", () => {
  it("splits paragraphs on a blank line regardless of length", () => {
    const { closed, open } = segmentReasoning("First thought.\n\nSecond thought.\n\nThird");
    expect(textOf(closed)).toEqual(["First thought.", "Second thought."]);
    expect(open).toEqual({ kind: "text", label: null, text: "Third" });
  });

  it("tolerates a blank line padded with spaces or CRLF", () => {
    const { closed } = segmentReasoning("A\n \nB\r\n\r\nC");
    expect(textOf(closed)).toEqual(["A", "B"]);
  });

  it("does NOT treat a single newline as a boundary", () => {
    const { closed, open } = segmentReasoning("line one\nline two\nline three");
    expect(closed).toEqual([]);
    expect(open).toEqual({ kind: "text", label: null, text: "line one\nline two\nline three" });
  });
});

describe("segmentReasoning — rule 2, sentence end past the floor", () => {
  it("does not cut at a sentence end below the floor", () => {
    const { closed } = segmentReasoning("Short. Also short. Still short. ");
    expect(closed).toEqual([]);
  });

  it("cuts at the first sentence end once the chunk passes the floor", () => {
    const long = "word ".repeat(50); // 250 chars, no punctuation
    const { closed } = segmentReasoning(`${long}end of it. next sentence starts here`);
    expect(closed).toHaveLength(1);
    expect(closed[0]).toMatchObject({ kind: "text" });
    expect((closed[0] as { text: string }).text.endsWith("end of it.")).toBe(true);
  });

  it("leaves a trailing period open — the next token may continue it", () => {
    const long = "word ".repeat(50);
    const { closed, open } = segmentReasoning(`${long}version 1.`);
    expect(closed).toEqual([]);
    expect(open).not.toBeNull();
  });
});

describe("segmentReasoning — rule 3, hard cap for run-on text", () => {
  it("breaks an unpunctuated run-on stream at a word boundary", () => {
    const runOn = "alpha ".repeat(400); // 2400 chars, no punctuation, no newlines
    const { closed } = segmentReasoning(runOn);
    expect(closed.length).toBeGreaterThan(2);
    for (const step of closed) {
      expect(step.kind).toBe("text");
      const { text } = step as { text: string };
      expect(text.length).toBeLessThanOrEqual(HARD_CAP);
      // backed off to a word boundary — never a torn word
      expect(text.endsWith("alpha")).toBe(true);
    }
  });

  it("still cuts a single token longer than the cap rather than growing forever", () => {
    const { closed } = segmentReasoning("x".repeat(HARD_CAP * 2 + 10));
    expect(closed.length).toBeGreaterThanOrEqual(2);
  });
});

describe("segmentReasoning — prefix stability (the load-bearing invariant)", () => {
  // Boundaries already on screen must never move as more tokens arrive, or rendered
  // rows re-flow and the card jitters mid-stream.
  const samples = [
    "Reading the revision note.\n\nChecking it against the approved sections.\n\nRewriting the bullet.",
    `${"word ".repeat(60)}done here. ${"more ".repeat(60)}and finished. tail`,
    "alpha ".repeat(300),
    `**Plan**\nDo the thing.\n\n## Check\n${"verify ".repeat(40)}ok.`,
    "no boundaries at all in this short buffer",
  ];

  for (const [n, sample] of samples.entries()) {
    it(`sample ${n}: every prefix's closed steps are a prefix of the final closed steps`, () => {
      const final = segmentReasoning(sample).closed;
      for (let cut = 1; cut <= sample.length; cut += 1) {
        const partial = segmentReasoning(sample.slice(0, cut)).closed;
        expect(partial.length).toBeLessThanOrEqual(final.length);
        expect(final.slice(0, partial.length)).toEqual(partial);
      }
    });
  }
});

describe("segmentReasoning — headings promoted to labels", () => {
  it("lifts a leading **bold** heading out of the body", () => {
    const { open } = segmentReasoning("**Assessing the request**\nThe user wants a chunked card.");
    expect(open).toEqual({ kind: "text", label: "Assessing the request", text: "The user wants a chunked card." });
  });

  it("lifts a leading ## heading out of the body", () => {
    const { open } = segmentReasoning("## Step one\nLook at the file.");
    expect(open).toEqual({ kind: "text", label: "Step one", text: "Look at the file." });
  });

  it("handles a bold heading followed by a colon on the same line", () => {
    const { open } = segmentReasoning("**Goal**: ship the card");
    expect(open).toEqual({ kind: "text", label: "Goal", text: "ship the card" });
  });

  it("leaves mid-sentence bold alone", () => {
    const { open } = segmentReasoning("the user wants **this** chunked");
    expect(open).toEqual({ kind: "text", label: null, text: "the user wants **this** chunked" });
  });
});

describe("segmentReasoning — degenerate input", () => {
  it("returns nothing for an empty buffer", () => {
    expect(segmentReasoning("")).toEqual({ closed: [], open: null });
  });

  it("returns nothing for whitespace only", () => {
    expect(segmentReasoning("   \n\n  \n ")).toEqual({ closed: [], open: null });
  });

  it("never emits an empty step", () => {
    const { closed, open } = segmentReasoning("A\n\n\n\n\n\nB\n\n\n\n");
    expect(closed.every((s) => s.kind !== "text" || s.text.length > 0 || s.label !== null)).toBe(true);
    // The trailing blank run closes B too — a run of newlines collapses to one
    // boundary rather than emitting empty rows between them.
    expect(textOf(closed)).toEqual(["A", "B"]);
    expect(open).toBeNull();
  });

  it("keeps the floor and cap in a sane relationship", () => {
    expect(SENTENCE_FLOOR).toBeLessThan(HARD_CAP);
  });
});
