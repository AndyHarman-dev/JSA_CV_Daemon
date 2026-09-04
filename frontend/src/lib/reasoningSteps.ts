// Segments a raw, still-streaming reasoning buffer into discrete steps for the
// REASONING card (see the design handoff's `thinkCard`, which renders reasoning as
// separated rows with a check/spinner per row — not one continuously inflating block
// of text).
//
// THE LOAD-BEARING PROPERTY IS PREFIX STABILITY. This runs on every render against a
// buffer that only ever grows by appended tokens, so a boundary decided at char 250
// must never move once more text arrives — otherwise already-rendered rows re-flow and
// the card visibly jitters mid-stream, and index-based React keys stop being safe.
// Every rule below is therefore evaluated left-to-right against the PREFIX only, and a
// rule may only fire once the currently-available text CONFIRMS it (e.g. a trailing
// "\n" is not yet a blank line — it stays in the open chunk until a second newline
// lands). Both properties are pinned by tests/reasoningSteps.test.ts.
//
// This is also why the "obvious" rule — *use \n\n if the text has any, else fall back
// to single \n* — is deliberately NOT used: that is a decision about the whole buffer,
// not about a prefix, so a stream that opens with single newlines and later emits one
// blank line would re-segment everything already on screen.
//
// Backends vary a lot in whether they paragraph their reasoning at all (an OpenAI-compat
// `reasoning_content` stream usually does; a run-on model does not), so the rules are
// layered and degrade rather than betting on any one convention.

/** A blank line always ends a step, at any length. */
const BLANK_LINE = /\n[ \t\r]*\n/y;
/** Below this, a sentence end is just punctuation — cutting there would make one row per sentence. */
export const SENTENCE_FLOOR = 200;
/** A run-on model that never punctuates still has to be broken up somewhere. */
export const HARD_CAP = 600;

const SENTENCE_END = /[.?!…]/;
const WHITESPACE = /\s/;

/**
 * One row in the REASONING card. A discriminated union so that tool calls render
 * through the same row renderer and the same separator idiom as reasoning prose —
 * and so wiring a real backend tool channel later is purely additive here.
 */
export type ReasoningStep =
  | {
      kind: "text";
      /** A leading markdown heading (`**Bold**` / `## Heading`) promoted out of the body. */
      label: string | null;
      text: string;
    }
  | { kind: "tool"; name: string; detail: string | null };

export interface ReasoningSegments {
  /** Steps whose boundary is settled — these never change again as more text streams in. */
  closed: ReasoningStep[];
  /** The step still being written, if any. Only this one grows between renders. */
  open: ReasoningStep | null;
  /**
   * bounds[i] is the END offset in the raw string of closed[i] — the anchor
   * `lib/mergeToolSteps.ts` compares a tool mark's arrival offset against to interleave
   * it into the right position. Always the same length as `closed`. Omitted (not an
   * empty array) whenever `closed` is empty, so a degenerate `{closed: [], open: null}`
   * buffer stays exactly that shape — existing whole-object equality tests for the
   * empty/whitespace-only inputs depend on no extra key being present.
   */
  bounds?: number[];
}

// Gemini's thought summaries (and any markdown-ish reasoning stream) prefix a section
// with a bold or hash heading. Promoting it to a label makes it the row's title rather
// than leaving `**` noise inline.
const BOLD_HEADING = /^\s*\*\*(.+?)\*\*[ \t]*(?::[ \t]*|\n+|$)/;
const HASH_HEADING = /^\s*#{1,6}[ \t]+(.+?)[ \t]*(?:\n+|$)/;

function toStep(raw: string): ReasoningStep | null {
  const chunk = raw.trim();
  if (chunk.length === 0) return null;
  const bold = BOLD_HEADING.exec(chunk);
  if (bold) return { kind: "text", label: bold[1].trim(), text: chunk.slice(bold[0].length).trim() };
  const hash = HASH_HEADING.exec(chunk);
  if (hash) return { kind: "text", label: hash[1].trim(), text: chunk.slice(hash[0].length).trim() };
  return { kind: "text", label: null, text: chunk };
}

export function segmentReasoning(raw: string): ReasoningSegments {
  const closed: ReasoningStep[] = [];
  if (raw.length === 0) return { closed, open: null };

  const bounds: number[] = [];
  let start = 0;
  let i = 0;

  const close = (end: number, resumeAt: number) => {
    const step = toStep(raw.slice(start, end));
    if (step) {
      closed.push(step);
      bounds.push(end);
    }
    start = resumeAt;
    i = resumeAt;
  };

  while (i < raw.length) {
    // Rule 1 — a blank line is always a boundary. Requires BOTH newlines to already be
    // in the buffer, so a trailing "\n" keeps the chunk open until the next token lands.
    BLANK_LINE.lastIndex = i;
    const blank = BLANK_LINE.exec(raw);
    if (blank) {
      close(i, BLANK_LINE.lastIndex);
      continue;
    }

    // Rule 2 — a sentence end, but only once the open chunk has earned a break. The
    // trailing whitespace must already be present, so a "." at the very end of the
    // buffer does not close a step that the next token might continue ("v1.2").
    if (
      SENTENCE_END.test(raw[i]) &&
      i + 1 < raw.length &&
      WHITESPACE.test(raw[i + 1]) &&
      i + 1 - start >= SENTENCE_FLOOR
    ) {
      let resume = i + 1;
      while (resume < raw.length && WHITESPACE.test(raw[resume])) resume += 1;
      close(i + 1, resume);
      continue;
    }

    // Rule 3 — hard cut for a model that never punctuates or paragraphs. Fires at the
    // FIRST index where the open chunk reaches the cap, so the cut point depends only
    // on the prefix; back off to the last whitespace so words stay intact.
    if (i - start >= HARD_CAP) {
      let cut = i;
      while (cut > start && !WHITESPACE.test(raw[cut - 1])) cut -= 1;
      if (cut === start) cut = i; // one unbroken token longer than the cap — cut it anyway
      let resume = cut;
      while (resume < raw.length && WHITESPACE.test(raw[resume])) resume += 1;
      close(cut, resume);
      continue;
    }

    i += 1;
  }

  return { closed, open: toStep(raw.slice(start)), ...(bounds.length > 0 ? { bounds } : {}) };
}
