// Interleaves tool-call marks (jsa/events/schema.py::AgentToolEvent, accumulated in
// store.ts's streamBuffers / persisted on a settled TranscriptTurn) into the closed text
// steps produced by `segmentReasoning`. A pure function, kept in its own module and unit
// tested independently of reasoningSteps.test.ts's prefix-stability suite.
import type { ReasoningSegments, ReasoningStep } from "./reasoningSteps";

/** One tool call's row data, already resolved to the anchor offset it arrived at. */
export interface ToolMark {
  name: string;
  detail: string;
  ok: boolean;
  /** The reasoning buffer's length when this tool call arrived — the interleave anchor. */
  at: number;
}

function toToolStep(mark: ToolMark): ReasoningStep {
  return { kind: "tool", name: mark.name, detail: mark.detail || null };
}

/**
 * Merges `marks` into `segments.closed`, using `bounds` (the END offset in the raw
 * string of each closed step, `segments.bounds`) as the interleave anchor: a mark whose
 * `at` is <= bounds[i] belongs right after closed[i] (i.e. before closed[i+1]) — it
 * arrived no later than that step finished. Marks are flushed in `at` order; ties keep
 * arrival order (Array.prototype.sort is stable). Marks left over after the last closed
 * step (arrived during the still-open step) are appended at the end, so the caller can
 * append the open step after this result unaffected.
 */
export function mergeToolSteps(segments: ReasoningSegments, bounds: number[], marks: ToolMark[]): ReasoningStep[] {
  const result: ReasoningStep[] = [];
  const sorted = [...marks].sort((a, b) => a.at - b.at);
  let mi = 0;
  for (let ci = 0; ci < segments.closed.length; ci += 1) {
    result.push(segments.closed[ci]);
    const bound = bounds[ci];
    while (mi < sorted.length && sorted[mi].at <= bound) {
      result.push(toToolStep(sorted[mi]));
      mi += 1;
    }
  }
  while (mi < sorted.length) {
    result.push(toToolStep(sorted[mi]));
    mi += 1;
  }
  return result;
}
