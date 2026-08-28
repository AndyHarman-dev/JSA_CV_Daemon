import { useEffect, useRef, useState } from "react";
import type { SyntheticEvent } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";
import { panelBase } from "../theme/chrome";
import { useScratchStore, sortEntries, type ScratchEntry } from "../scratchStore";

const T = SHELL_THEME;

type ChatBoxProps =
  | { kind: "answer"; jobId: string; followUpId: number; onSubmitted?: () => void }
  | { kind: "revise"; jobId: string; onSubmitted?: () => void; fixedTarget?: "cv" };

// Backward-scan @-mention detection: an `@` found before any whitespace/newline makes the
// mention "active" at that index. Ported from the design handoff's `getMentionAt`
// (.claude/designs/Dropdown_design_handoff_when_@_called.zip).
export function getMentionAt(text: string, cursor: number): { active: boolean; at: number | null } {
  let i = cursor - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === "@") return { active: true, at: i };
    if (ch === "\n" || ch === " " || ch === "\t") return { active: false, at: null };
    i--;
  }
  return { active: false, at: null };
}

interface MentionState {
  open: boolean;
  pos: { x: number; y: number };
  at: number | null;
  end: number | null;
}

const CLOSED_MENTION: MentionState = { open: false, pos: { x: 0, y: 0 }, at: null, end: null };

export function ChatBox(props: ChatBoxProps) {
  const [text, setText] = useState("");
  const [targetState, setTargetState] = useState<"cv" | "cl">("cv");
  const fixedTarget = props.kind === "revise" ? props.fixedTarget : undefined;
  const target = fixedTarget ?? targetState;
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mention, setMention] = useState<MentionState>(CLOSED_MENTION);

  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const lastMouseRef = useRef({ x: 0, y: 0 });

  const scratchEntries = useScratchStore((s) => s.entries);
  const sortedEntries = sortEntries(scratchEntries);
  const t = useT();

  // Anchor point for the dropdown: tracked globally so it's available the instant the
  // mention becomes active, independent of where the textarea's own events fire.
  useEffect(() => {
    function onMove(e: MouseEvent) {
      lastMouseRef.current = { x: e.clientX, y: e.clientY };
    }
    window.addEventListener("mousemove", onMove);
    return () => window.removeEventListener("mousemove", onMove);
  }, []);

  const placeholder =
    props.kind === "answer" ? t("chatBox.answerPlaceholder") : t("chatBox.revisePlaceholder");
  const buttonLabel = props.kind === "answer" ? t("chatBox.submitAnswer") : t("chatBox.requestRevision");

  function handleDraftChange(e: SyntheticEvent<HTMLTextAreaElement>) {
    const el = e.currentTarget;
    const value = el.value;
    const cursor = el.selectionStart ?? value.length;
    const m = getMentionAt(value, cursor);
    setText(value);
    setMention((prev) => {
      if (m.active) {
        const pos = prev.open ? prev.pos : lastMouseRef.current;
        return { open: true, pos, at: m.at, end: cursor };
      }
      return { ...CLOSED_MENTION, pos: prev.pos };
    });
  }

  function insertScratchNote(entry: ScratchEntry) {
    const { at, end } = mention;
    if (at == null || end == null) return;
    const before = text.slice(0, at);
    const after = text.slice(end);
    const inserted = entry.text + " ";
    const newText = before + inserted + after;
    const newCursor = before.length + inserted.length;
    setText(newText);
    setMention((prev) => ({ ...CLOSED_MENTION, pos: prev.pos }));
    setTimeout(() => {
      const ta = textareaRef.current;
      if (ta) {
        ta.focus();
        ta.setSelectionRange(newCursor, newCursor);
      }
    }, 0);
  }

  async function handleSubmit() {
    if (!text.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      if (props.kind === "answer") {
        await api.answerFollowUp(props.jobId, props.followUpId, text);
      } else {
        await api.revise(props.jobId, target, text);
      }
      await useStore.getState().refetchAll();
      setText("");
      setMention((prev) => ({ ...CLOSED_MENTION, pos: prev.pos }));
      props.onSubmitted?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const hasText = text.trim().length > 0;

  return (
    <div
      // Sticky-footer composer: docks to the bottom of the scrolling <main> when the
      // job-detail panel overflows; sits inline when content is short (pure CSS sticky,
      // no JS). The negative margins bleed the bar to the scroll-container edges and
      // cancel JobDetail's root padding — they are COUPLED to JobDetail's
      // `padding: "20px 26px 80px"` (the only place ChatBox is rendered). See
      // .claude/designs/design_handoff_docked_chat_input.
      style={{
        position: "sticky",
        bottom: 0,
        zIndex: 8,
        marginLeft: -26,
        marginRight: -26,
        marginBottom: -80,
        paddingLeft: 26,
        paddingRight: 26,
        paddingTop: 14,
        paddingBottom: 20,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        background: `linear-gradient(${T.canvas}00, ${T.surface} 22%)`,
        borderTop: `1px solid ${T.bd}`,
        boxShadow: "0 -16px 28px -12px rgba(0,0,0,.5)",
      }}
    >
      <textarea
        ref={textareaRef}
        className="jta"
        rows={4}
        placeholder={placeholder}
        value={text}
        onChange={handleDraftChange}
        onKeyUp={handleDraftChange}
        onClick={handleDraftChange}
        disabled={submitting}
        style={{
          width: "100%",
          resize: "vertical",
          background: T.sunk,
          border: `1px solid ${T.bd2}`,
          borderRadius: T.btnRadius,
          padding: "10px 12px",
          font: `400 13.5px/1.5 ${T.ui}`,
          color: T.ink,
          outline: "none",
        }}
      />
      {mention.open && (
        <MentionDropdown pos={mention.pos} entries={sortedEntries} onSelect={insertScratchNote} />
      )}
      {props.kind === "revise" && !fixedTarget && (
        <select
          value={target}
          onChange={(e) => setTargetState(e.target.value as "cv" | "cl")}
          disabled={submitting}
          style={{
            alignSelf: "flex-start",
            background: T.sunk,
            border: `1px solid ${T.bd2}`,
            borderRadius: T.btnRadius,
            padding: "5px 8px",
            font: `400 12.5px ${T.ui}`,
            color: T.ink,
          }}
        >
          <option value="cv">{t("chatBox.cvOption")}</option>
          <option value="cl">{t("chatBox.clOption")}</option>
        </select>
      )}
      {error && <p style={{ font: `400 12.5px ${T.ui}`, color: T.danger, margin: 0 }}>{error}</p>}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <button
          type="button"
          className="jprimary"
          disabled={!hasText || submitting}
          onClick={() => {
            handleSubmit().catch((err: unknown) => {
              console.error("ChatBox submit error:", err);
            });
          }}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 7,
            padding: "9px 17px",
            border: "none",
            borderRadius: T.btnRadius,
            background: T.a,
            color: "#06080B",
            font: `600 12.5px ${T.disp}`,
            letterSpacing: ".04em",
            cursor: hasText && !submitting ? "pointer" : "default",
            opacity: hasText && !submitting ? 1 : 0.5,
            boxShadow: hasText ? `0 1px 14px ${T.a}55` : "none",
          }}
        >
          <Icon name="send" size={13} />
          {submitting ? t("chatBox.submitting") : buttonLabel}
        </button>
      </div>
    </div>
  );
}

// @-mention dropdown: read-only list of Scratch Buffer notes, anchored at the mouse position
// captured when the mention became active (not the text caret). Position is clamped to stay
// within the viewport. Ported from the design handoff's `renderMentionDropdown`.
function MentionDropdown({
  pos,
  entries,
  onSelect,
}: {
  pos: { x: number; y: number };
  entries: ScratchEntry[];
  onSelect: (entry: ScratchEntry) => void;
}) {
  const t = useT();
  const w = 272;
  const maxH = 320;
  const left = Math.min(Math.max(8, pos.x), window.innerWidth - w - 8);
  const top = Math.min(Math.max(8, pos.y + 14), window.innerHeight - maxH - 8);

  return (
    <div
      style={{
        position: "fixed",
        left,
        top,
        width: w,
        maxHeight: maxH,
        zIndex: 90,
        display: "flex",
        flexDirection: "column",
        ...panelBase(T, { chamfer: 10 }),
        boxShadow: T.shadowMd,
        animation: "jsfade .1s ease",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 7,
          padding: "8px 10px",
          borderBottom: `1px solid ${T.bd}`,
          flex: "none",
        }}
      >
        <span style={{ color: T.a, display: "flex" }}>
          <Icon name="pin" size={12} />
        </span>
        <span
          style={{
            font: `600 10px ${T.mono}`,
            letterSpacing: ".1em",
            color: T.ink,
            textTransform: "uppercase",
          }}
        >
          SCRATCH_BUFFER
        </span>
        <span style={{ marginLeft: "auto", font: `400 9px ${T.mono}`, color: T.ink3 }}>
          {t("chatBox.mentionHint")}
        </span>
      </div>
      <div style={{ overflow: "auto", flex: 1, minHeight: 0 }}>
        {entries.length ? (
          entries.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className="jbtn"
              onMouseDown={(ev) => {
                ev.preventDefault();
                onSelect(entry);
              }}
              style={{
                display: "flex",
                alignItems: "flex-start",
                gap: 8,
                width: "100%",
                textAlign: "left",
                padding: "8px 10px",
                border: "none",
                borderBottom: `1px solid ${T.bd}`,
                background: "transparent",
                cursor: "pointer",
              }}
            >
              <span
                style={{ font: `500 9px ${T.mono}`, color: T.accent2, flex: "none", marginTop: 1 }}
              >
                {entry.tag}
              </span>
              <span style={{ font: `400 12px/1.45 ${T.ui}`, color: T.ink, wordBreak: "break-word" }}>
                {entry.text}
              </span>
            </button>
          ))
        ) : (
          <div
            style={{
              padding: "16px 12px",
              font: `400 12px ${T.ui}`,
              color: T.ink3,
              fontStyle: "italic",
              textAlign: "center",
            }}
          >
            {t("chatBox.noNotesInBuffer")}
          </div>
        )}
      </div>
    </div>
  );
}
