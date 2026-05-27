import { useState } from "react";
import { useStore } from "../store";
import { api } from "../api";
import { StatusBadge } from "./StatusBadge";
import { StageTimeline } from "./StageTimeline";
import { LogTail } from "./LogTail";
import { FollowUpPane } from "./FollowUpPane";
import { ReviewPane } from "./ReviewPane";

const TIER_CLASSES: Record<string, string> = {
  A: "bg-green-100 text-green-700",
  B: "bg-blue-100 text-blue-700",
  C: "bg-gray-100 text-gray-600",
};

export function JobDetail() {
  const selectedId = useStore((s) => s.selectedId);
  const job = useStore((s) =>
    s.selectedId !== undefined ? s.jobs[s.selectedId] : undefined
  );
  const refetchAll = useStore((s) => s.refetchAll);

  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState<string | null>(null);
  const [requeuing, setRequeuing] = useState(false);
  const [requeueError, setRequeueError] = useState<string | null>(null);
  const [jdOpen, setJdOpen] = useState(false);

  if (selectedId === undefined || job === undefined) {
    return (
      <div className="flex flex-1 items-center justify-center text-gray-400 text-lg select-none">
        ← Select a job
      </div>
    );
  }

  async function handleDismiss() {
    if (!job) return;
    setDismissing(true);
    setDismissError(null);
    try {
      await api.dismiss(job.id);
      await refetchAll();
    } catch (err) {
      setDismissError(err instanceof Error ? err.message : String(err));
    } finally {
      setDismissing(false);
    }
  }

  async function handleRequeue() {
    if (!job) return;
    setRequeuing(true);
    setRequeueError(null);
    try {
      await api.reset(job.id);
      await refetchAll();
    } catch (err) {
      setRequeueError(err instanceof Error ? err.message : String(err));
    } finally {
      setRequeuing(false);
    }
  }

  const showDismiss = job.state !== "approved" && job.state !== "dismissed";
  const showRequeue = job.state === "dismissed";

  return (
    <div className="flex flex-col gap-4 p-6 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-xl font-bold text-gray-900">
          {job.company} — {job.role}
        </h2>
        <span
          className={`px-2 py-0.5 rounded-full text-xs font-semibold ${
            TIER_CLASSES[job.tier] ?? TIER_CLASSES["C"]
          }`}
        >
          Tier {job.tier}
        </span>
        <StatusBadge state={job.state} />
        {showDismiss && (
          <button
            type="button"
            className="text-sm px-3 py-1 rounded border border-red-300 text-red-600 hover:bg-red-50 disabled:opacity-50"
            disabled={dismissing}
            onClick={() => {
              handleDismiss().catch((err: unknown) => {
                console.error("JobDetail dismiss error:", err);
              });
            }}
          >
            {dismissing ? "Dismissing…" : "Dismiss"}
          </button>
        )}
        {showRequeue && (
          <button
            type="button"
            className="text-sm px-3 py-1 rounded border border-gray-300 text-gray-600 hover:bg-gray-50 disabled:opacity-50"
            disabled={requeuing}
            onClick={() => {
              handleRequeue().catch((err: unknown) => {
                console.error("JobDetail requeue error:", err);
              });
            }}
          >
            {requeuing ? "Re-queuing…" : "Re-queue"}
          </button>
        )}
      </div>
      {dismissError && (
        <p className="text-sm text-red-600">{dismissError}</p>
      )}
      {requeueError && (
        <p className="text-sm text-red-600">{requeueError}</p>
      )}

      {/* Stage timeline */}
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">
          Pipeline Progress
        </h3>
        <StageTimeline job={job} />
      </div>

      {/* Job Description collapsible section */}
      <div>
        <button
          type="button"
          className="text-xs font-semibold uppercase tracking-wider text-gray-500 flex items-center gap-1 mb-1 hover:text-gray-700"
          onClick={() => setJdOpen((prev) => !prev)}
        >
          {jdOpen ? "▼" : "▶"} Job Description
        </button>
        {jdOpen && (
          <pre className="whitespace-pre-wrap text-xs text-gray-700 bg-gray-50 rounded border border-gray-200 p-3 max-h-64 overflow-y-auto">
            {job.jd}
          </pre>
        )}
      </div>

      {/* Error box */}
      {job.error && (
        <div className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          <span className="font-semibold">Error: </span>
          {job.error}
        </div>
      )}

      {job.state === "awaiting_input" && (
        <FollowUpPane jobId={job.id} />
      )}
      {(job.state === "review" || job.state === "approved") && (
        <ReviewPane jobId={job.id} />
      )}

      {/* Log tail */}
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">
          Logs
        </h3>
        <LogTail jobId={job.id} />
      </div>
    </div>
  );
}
