import type { JobDTO, JobState, Stage } from "../types";

interface StageTimelineProps {
  job: JobDTO;
}

const STEPS: { label: string; key: string }[] = [
  { label: "Pending", key: "pending" },
  { label: "CV Adjust", key: "cv_adjust" },
  { label: "CV Done", key: "cv_done" },
  { label: "Cover Letter", key: "cover_letter" },
  { label: "CL Done", key: "cl_done" },
  { label: "Review", key: "review" },
  { label: "Approved", key: "approved" },
];

function getActiveStepIndex(state: JobState, currentStage: Stage | null): number {
  switch (state) {
    case "pending":
      return 0;
    case "running":
      if (currentStage === "cv_adjust" || currentStage === "revising_cv") return 1;
      if (currentStage === "cover_letter" || currentStage === "revising_cl") return 3;
      return 0;
    case "awaiting_input":
      if (currentStage === "cv_adjust" || currentStage === "revising_cv") return 1;
      if (currentStage === "cover_letter" || currentStage === "revising_cl") return 3;
      return 1;
    case "fit_done":
      // Fit check passed; cv_adjust is up next but hasn't produced anything yet.
      return 0;
    case "unfit":
      // Parked on the not-a-fit modal; nothing produced yet.
      return 0;
    case "cv_done":
      return 2;
    case "cl_done":
      return 4;
    case "review":
      return 5;
    case "approved":
      return 6;
    case "failed":
      // Show position where they stopped
      if (currentStage === "cv_adjust" || currentStage === "revising_cv") return 1;
      if (currentStage === "cover_letter" || currentStage === "revising_cl") return 3;
      return 0;
    case "dismissed":
      return 0;
  }
}

export function StageTimeline({ job }: StageTimelineProps) {
  const activeIdx = getActiveStepIndex(job.state, job.current_stage);

  return (
    <div className="flex items-center gap-0 w-full overflow-x-auto py-2">
      {STEPS.map((step, idx) => {
        const isComplete = idx < activeIdx;
        const isActive = idx === activeIdx;
        const isDimmed = idx > activeIdx;

        return (
          <div key={step.key} className="flex items-center">
            {/* Connector line (not before first step) */}
            {idx > 0 && (
              <div
                className={`h-0.5 w-6 flex-shrink-0 ${
                  idx <= activeIdx ? "bg-blue-500" : "bg-gray-300"
                }`}
              />
            )}
            {/* Step dot + label */}
            <div className="flex flex-col items-center flex-shrink-0">
              <div
                className={`w-4 h-4 rounded-full border-2 flex-shrink-0 ${
                  isComplete
                    ? "bg-blue-500 border-blue-500"
                    : isActive
                    ? "bg-white border-blue-500 ring-2 ring-blue-300"
                    : isDimmed
                    ? "bg-white border-gray-300"
                    : "bg-white border-gray-300"
                }`}
              />
              <span
                className={`mt-1 text-xs whitespace-nowrap ${
                  isComplete
                    ? "text-blue-600 font-medium"
                    : isActive
                    ? "text-blue-700 font-semibold"
                    : "text-gray-400"
                }`}
              >
                {step.label}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
