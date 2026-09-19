// Regression gate for "the proposed diff must be readable IN FULL before APPLY".
//
// CvDiffCard is the only thing the user sees before pressing APPLY, so it may not
// abbreviate in any way. It used to abbreviate twice over: each side of a field was cut to
// 140 chars with a "…", and only the first 4 changed fields were rendered at all, the rest
// collapsed behind a "+N more changes" line. Both are removed; this pins them out.
//
// The shape matters: many items AND long text. A fixture with <=5 short items structurally
// cannot detect either cap, which is exactly how the item cap survived the first fix.
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { CvDiffCard } from "../components/cv-editor/CvDiffCard";
import type { ClientChatTurn } from "../cvChatStore";
import type { ChatDiffItemDTO } from "../types";

const ITEM_COUNT = 12;
const SIDE_CHARS = 2000;

// Distinct per item and per side, so a rendered row can only match the item it belongs to.
function longText(marker: string): string {
  const body = `${marker} `.repeat(Math.ceil(SIDE_CHARS / (marker.length + 1)));
  return `${body.slice(0, SIDE_CHARS - marker.length)}${marker}`;
}

const ITEMS: ChatDiffItemDTO[] = Array.from({ length: ITEM_COUNT }, (_, i) => ({
  label: `Experience › Role ${i} · bullets`,
  before: longText(`BEFORE${i}`),
  after: longText(`AFTER${i}`),
}));

function turn(overrides: Partial<ClientChatTurn> = {}): ClientChatTurn {
  return {
    id: "t1",
    role: "agent",
    scope: { type: "cv" },
    text: "Rewrote every bullet.",
    question: null,
    reasoning: "",
    items: ITEMS,
    document: { contact: { name: "Jane", links: [] }, sections: [] },
    status: "pending",
    files: [],
    base_hash: "abc",
    created_at: new Date().toISOString(),
    deckId: "deck1",
    ...overrides,
  };
}

describe("CvDiffCard renders the full diff", () => {
  it("renders every changed field, not just the first few", () => {
    render(<CvDiffCard turn={turn()} />);
    for (const item of ITEMS) {
      expect(screen.getByText(item.label)).toBeTruthy();
    }
    expect(screen.queryByText(/more changes/i)).toBeNull();
  });

  it("renders each side's text in full, with no ellipsis", () => {
    const { container } = render(<CvDiffCard turn={turn()} />);
    const text = container.textContent ?? "";
    for (const item of ITEMS) {
      expect(text).toContain(item.before);
      expect(text).toContain(item.after);
    }
    expect(text).not.toContain("…");
  });

  it("wraps long text instead of clipping it", () => {
    const { container } = render(<CvDiffCard turn={turn()} />);
    const rows = Array.from(container.querySelectorAll("div")).filter((d) =>
      (d.textContent ?? "").startsWith("BEFORE0"),
    );
    expect(rows.length).toBeGreaterThan(0);
    for (const row of rows) {
      expect(row.style.whiteSpace).toBe("pre-wrap");
      expect(row.style.wordBreak).toBe("break-word");
    }
  });
});
