import type { JobDTO, JobState, Stage } from "../types";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

interface StageTimelineProps {
  job: JobDTO;
}

const STEPS: { label: string; key: string }[] = [
  { label: "PENDING", key: "pending" },
  { label: "CV_ADJUST", key: "cv_adjust" },
  { label: "CV_DONE", key: "cv_done" },
  { label: "COVER_LETTER", key: "cover_letter" },
  { label: "CL_DONE", key: "cl_done" },
  { label: "REVIEW", key: "review" },
  { label: "APPROVED", key: "approved" },
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
    <div style={{ display: "flex", alignItems: "flex-start", gap: 0, overflowX: "auto", padding: "6px 2px 2px" }}>
      {STEPS.map((step, idx) => {
        const isComplete = idx < activeIdx;
        const isActive = idx === activeIdx;
        const color = isComplete ? T.accent2 : isActive ? T.a : T.ink3;

        return (
          <div key={step.key} style={{ display: "flex", alignItems: "flex-start", flex: "none" }}>
            {/* Connector line (not before first step) */}
            {idx > 0 && (
              <div
                style={{
                  height: 2,
                  width: 28,
                  marginTop: 7,
                  flex: "none",
                  background: idx <= activeIdx ? T.accent2 : T.bd,
                  boxShadow: idx <= activeIdx ? `0 0 4px ${T.accent2}` : "none",
                }}
              />
            )}
            {/* Step node + label */}
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", flex: "none", width: 86 }}>
              <div
                data-testid="stage-dot"
                data-state={isComplete ? "complete" : isActive ? "active" : "pending"}
                style={{
                  width: 16,
                  height: 16,
                  borderRadius: T.chamfer ? 3 : 16,
                  flex: "none",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  background: isComplete ? T.accent2 : "transparent",
                  border: `2px solid ${color}`,
                  boxShadow: isActive ? `0 0 8px ${T.a}` : "none",
                }}
              >
                {isComplete ? (
                  <Icon name="check" size={9} color="#06080B" />
                ) : isActive ? (
                  <span
                    style={{
                      width: 5,
                      height: 5,
                      borderRadius: 5,
                      background: T.a,
                      animation: "jsblink 1s ease-in-out infinite",
                    }}
                  />
                ) : null}
              </div>
              <span
                style={{
                  marginTop: 6,
                  font: `500 9.5px ${T.mono}`,
                  letterSpacing: ".04em",
                  color,
                  textAlign: "center",
                  whiteSpace: "nowrap",
                }}
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
