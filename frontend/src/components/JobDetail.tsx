import { useStore } from "../store";
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

  if (selectedId === undefined || job === undefined) {
    return (
      <div className="flex flex-1 items-center justify-center text-gray-400 text-lg select-none">
        ← Select a job
      </div>
    );
  }

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
      </div>

      {/* Stage timeline */}
      <div>
        <h3 className="text-xs font-semibold uppercase tracking-wider text-gray-500 mb-2">
          Pipeline Progress
        </h3>
        <StageTimeline job={job} />
      </div>

      {/* Error box */}
      {job.error && (
        <div className="rounded border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700">
          <span className="font-semibold">Error: </span>
          {job.error}
        </div>
      )}

      {job.state === "awaiting_input" && <FollowUpPane jobId={job.id} />}
      {(job.state === "review" || job.state === "approved") && <ReviewPane jobId={job.id} />}

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
