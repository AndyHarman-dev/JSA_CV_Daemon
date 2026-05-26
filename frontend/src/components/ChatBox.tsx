import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";

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
  const buttonLabel = props.kind === "answer" ? "Submit Answer" : "Request Revision";

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

  return (
    <div className="flex flex-col gap-2">
      <textarea
        className="w-full rounded border border-gray-300 px-3 py-2 text-sm resize-y focus:outline-none focus:ring-2 focus:ring-blue-400 disabled:opacity-50"
        rows={4}
        placeholder={placeholder}
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={submitting}
      />
      {props.kind === "revise" && (
        <select
          className="self-start rounded border border-gray-300 px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 disabled:opacity-50"
          value={target}
          onChange={(e) => setTarget(e.target.value as "cv" | "cl")}
          disabled={submitting}
        >
          <option value="cv">CV / Resume</option>
          <option value="cl">Cover Letter</option>
        </select>
      )}
      {error && (
        <p className="text-sm text-red-600">{error}</p>
      )}
      <button
        type="button"
        className="self-start rounded bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        disabled={!text.trim() || submitting}
        onClick={() => {
          handleSubmit().catch((err: unknown) => {
            console.error("ChatBox submit error:", err);
          });
        }}
      >
        {submitting ? "Submitting…" : buttonLabel}
      </button>
    </div>
  );
}
