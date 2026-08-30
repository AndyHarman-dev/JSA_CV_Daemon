import type { JobDTO, JobState, Stage } from "../types";
import { useT } from "../i18n/useT";
import { useStore } from "../store";
import { SHELL_THEME } from "../theme/tokens";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

interface StageTimelineProps {
  job: JobDTO;
}

// `labelKey` is a translation key, resolved by the component below via `t()`. `viewKey` is
// set only on the two nodes that are clickable — they select which artifact ReviewPane
// shows (via the store's `viewedStage`), independent of which stage is actually running.
const STEPS: { labelKey: string; key: string; viewKey?: "cv" | "cl" }[] = [
  { labelKey: "stageTimeline.pending", key: "pending" },
  { labelKey: "stageTimeline.cvAdjust", key: "cv_adjust", viewKey: "cv" },
  { labelKey: "stageTimeline.cvReview", key: "cv_review" },
  { labelKey: "stageTimeline.coverLetter", key: "cover_letter", viewKey: "cl" },
  { labelKey: "stageTimeline.clDone", key: "cl_done" },
  { labelKey: "stageTimeline.review", key: "review" },
  { labelKey: "stageTimeline.approved", key: "approved" },
];

function getActiveStepIndex(state: JobState, currentStage: Stage | null): number {
  switch (state) {
    case "queued":
      // Parked, never launched — nothing produced yet.
      return 0;
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
    case "cv_review":
      // Parked at the CV gate, awaiting approve/revise.
      return 2;
    case "cv_done":
      // CV approved; same node position — cover_letter is up next but hasn't produced
      // anything yet. This is also the BF-19 rewind target, so it must stay reachable
      // from running(cover_letter) — see jsa/pipeline/state_machine.py.
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
  const viewedStage = useStore((s) => s.viewedStage);
  const setViewedStage = useStore((s) => s.setViewedStage);
  const t = useT();

  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 0, overflowX: "auto", padding: "6px 2px 2px" }}>
      {STEPS.map((step, idx) => {
        const isComplete = idx < activeIdx;
        const isActive = idx === activeIdx;
        const color = isComplete ? T.accent2 : isActive ? T.a : T.ink3;
        const interactive = step.viewKey !== undefined;
        // A node is disabled until the pipeline has actually reached it — before that,
        // clicking it would set viewedStage to an artifact that doesn't exist yet, and
        // JobDetail.tsx has no render branch for that combination (a dead click with no
        // visible effect). cv_adjust (index 1) has nothing to show before activeIdx
        // reaches it; cover_letter (index 3) likewise before the CL lane starts running.
        const disabled =
          (step.viewKey === "cv" && activeIdx < 1) ||
          (step.viewKey === "cl" && activeIdx < 3);
        const selected = interactive && viewedStage === step.viewKey;
        const viewKey = step.viewKey;

        const nodeStyle = {
          display: "flex" as const,
          flexDirection: "column" as const,
          alignItems: "center" as const,
          flex: "none" as const,
          width: 86,
          border: "none",
          background: selected ? `color-mix(in srgb, ${T.a} 12%, transparent)` : "transparent",
          borderRadius: T.chamfer ? 4 : 10,
          padding: "2px 0 4px",
          cursor: interactive ? (disabled ? "default" : "pointer") : "default",
          opacity: disabled ? 0.45 : 1,
          font: "inherit",
        };

        const nodeContent = (
          <>
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
                border: `2px solid ${selected ? T.a : color}`,
                boxShadow: isActive ? `0 0 8px ${T.a}` : selected ? `0 0 6px ${T.a}` : "none",
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
              {t(step.labelKey)}
            </span>
          </>
        );

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
            {/* Step node + label — only the cv_adjust/cover_letter nodes are interactive */}
            {interactive ? (
              <button
                type="button"
                disabled={disabled}
                aria-pressed={selected}
                title={
                  disabled
                    ? step.viewKey === "cv"
                      ? t("stageTimeline.cvNotStarted")
                      : t("stageTimeline.clNotStarted")
                    : undefined
                }
                onClick={() => setViewedStage(viewKey === viewedStage ? null : (viewKey ?? null))}
                style={nodeStyle}
              >
                {nodeContent}
              </button>
            ) : (
              <div style={nodeStyle}>{nodeContent}</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
