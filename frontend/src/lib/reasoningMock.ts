// Dev-only fixture for the REASONING card's tool row.
//
// The backend has NO tool channel today — `AgentChunk.kind` is `"content" | "reasoning"`
// (jsa/agents/base.py) and nothing anywhere emits a tool call. The row is built now so
// the UI is ready, and this fixture is the only way to actually look at it in the
// browser against a live reasoning stream. It is reachable exclusively from the
// `import.meta.env.DEV`-guarded toggle in ReasoningCard's header, so it never reaches a
// production bundle and never sits in the real render path.
//
// When a real channel lands, it replaces this function and nothing else: the card
// already renders `ReasoningStep[]`, and the tool variant is part of that union.
import type { ReasoningStep } from "./reasoningSteps";

export const MOCK_TOOL_STEPS: ReasoningStep[] = [
  { kind: "tool", name: "read_file", detail: "~/.jsa/cv_structure.json" },
  { kind: "tool", name: "web_search", detail: '"Acme Corp engineering culture"' },
  { kind: "tool", name: "render_pdf", detail: null },
];

/** Splices the fixture's tool rows in after every other text step, preserving order. */
export function interleaveMockTools(steps: ReasoningStep[]): ReasoningStep[] {
  const out: ReasoningStep[] = [];
  let tool = 0;
  steps.forEach((step, i) => {
    out.push(step);
    if (i % 2 === 1 && tool < MOCK_TOOL_STEPS.length) {
      out.push(MOCK_TOOL_STEPS[tool]);
      tool += 1;
    }
  });
  return out;
}
