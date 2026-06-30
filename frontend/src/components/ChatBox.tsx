import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

type ChatBoxProps =
  | { kind: "answer"; jobId: string; followUpId: number; onSubmitted?: () => void }
  | { kind: "revise"; jobId: string; onSubmitted?: () => void };

export function ChatBox(props: ChatBoxProps) {
  const [text, setText] = useState("");
  const [target, setTarget] = useState<"cv" | "cl">("cv");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const placeholder =
    props.kind === "answer" ? "Type your answer…" : "Describe the revision you want…";
  const buttonLabel = props.kind === "answer" ? "SUBMIT_ANSWER" : "REQUEST_REVISION";

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
      props.onSubmitted?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  const hasText = text.trim().length > 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <textarea
        className="jta"
        rows={4}
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
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
      {props.kind === "revise" && (
        <select
          value={target}
          onChange={(e) => setTarget(e.target.value as "cv" | "cl")}
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
          <option value="cv">CV / Resume</option>
          <option value="cl">Cover Letter</option>
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
          {submitting ? "Submitting…" : buttonLabel}
        </button>
      </div>
    </div>
  );
}
