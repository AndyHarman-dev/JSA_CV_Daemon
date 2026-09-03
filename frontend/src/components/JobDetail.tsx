import { useState } from "react";
import { useStore } from "../store";
import { api } from "../api";
import { StatusBadge } from "./StatusBadge";
import { StageTimeline } from "./StageTimeline";
import { FollowUpPane } from "./FollowUpPane";
import { AgentThread } from "./AgentThread";
import { ReviewPane } from "./ReviewPane";
import { UnfitModal } from "./UnfitModal";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase, cornerMarks } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

const TIER_COLOR: Record<string, string> = { A: T.green, B: T.accent2, C: T.ink3 };

interface ActionBtnProps {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  danger?: boolean;
  icon?: "refresh" | "x" | "trash";
}

function ActionBtn({ label, onClick, disabled, danger, icon }: ActionBtnProps) {
  return (
    <button
      type="button"
      className={danger ? "jdanger jbtn" : "jghost"}
      disabled={disabled}
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "6px 12px",
        border: `1px solid ${danger ? "rgba(255,70,85,.4)" : T.bd2}`,
        borderRadius: T.btnRadius,
        background: "transparent",
        color: danger ? T.danger : T.ink,
        font: `600 11.5px ${T.disp}`,
        letterSpacing: ".03em",
        cursor: disabled ? "default" : "pointer",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {icon && <Icon name={icon} size={12} />}
      {label}
    </button>
  );
}

export function JobDetail() {
  const selectedId = useStore((s) => s.selectedId);
  const job = useStore((s) =>
    s.selectedId !== undefined ? s.jobs[s.selectedId] : undefined
  );
  const refetchAll = useStore((s) => s.refetchAll);
  const removeJob = useStore((s) => s.removeJob);
  const selectJob = useStore((s) => s.selectJob);
  const viewedStage = useStore((s) => s.viewedStage);

  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [dismissing, setDismissing] = useState(false);
  const [dismissError, setDismissError] = useState<string | null>(null);
  const [requeuing, setRequeuing] = useState(false);
  const [requeueError, setRequeueError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [showNuclearConfirm, setShowNuclearConfirm] = useState(false);
  const [jdOpen, setJdOpen] = useState(false);
  const t = useT();

  if (selectedId === undefined || job === undefined) {
    return (
      <div style={{ display: "flex", flexDirection: "column", flex: 1, height: "100%", padding: 16, position: "relative", zIndex: 1 }}>
        <button
          type="button"
          onClick={() => selectJob(undefined)}
          className="md:hidden"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginBottom: 16,
            marginLeft: -4,
            border: "none",
            background: "transparent",
            color: T.a,
            font: `500 13px ${T.ui}`,
            cursor: "pointer",
          }}
        >
          <Icon name="back" size={13} /> {t("jobDetail.back")}
        </button>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ textAlign: "center", color: T.ink3 }}>
            <div
              style={{
                width: 50,
                height: 50,
                ...panelBase(T, { chamfer: 12 }),
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 14px",
                color: T.ink3,
              }}
            >
              <Icon name="work" size={22} />
            </div>
            <div style={{ font: `600 14px ${T.disp}`, letterSpacing: ".04em", color: T.ink2 }}>
              {t("jobDetail.noProcessSelected")}
            </div>
            <div style={{ font: `400 11.5px ${T.mono}`, marginTop: 6 }}>{t("jobDetail.chooseJobHint")}</div>
          </div>
        </div>
      </div>
    );
  }

  async function handleDelete() {
    if (!job) return;
    if (!window.confirm(t("jobDetail.confirmDeleteMessage"))) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.deleteJob(job.id);
      removeJob(job.id);  // immediately remove from store; WS job_removed event is a no-op
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeleting(false);
    }
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

  async function handleRetry() {
    if (!job) return;
    if (job.retry_count > 0) {
      // Show confirmation modal instead of acting immediately (nuclear reset)
      setShowNuclearConfirm(true);
      return;
    }
    setRetrying(true);
    setRetryError(null);
    try {
      await api.reset(job.id);
      await refetchAll();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : String(err));
    } finally {
      setRetrying(false);
    }
  }

  async function handleNuclearConfirm() {
    if (!job) return;
    setShowNuclearConfirm(false);
    setRetrying(true);
    setRetryError(null);
    try {
      await api.reset(job.id);
      await refetchAll();
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : String(err));
    } finally {
      setRetrying(false);
    }
  }

  const showCancel = job.state !== "approved";
  const showDismiss = job.state !== "approved" && job.state !== "dismissed";
  const showRequeue = job.state === "dismissed";
  const showRetry = job.state === "failed";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16, padding: "20px 26px", position: "relative", zIndex: 1 }}>
      {/* Mobile back button */}
      <button
        type="button"
        onClick={() => selectJob(undefined)}
        className="md:hidden"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: -4,
          marginLeft: -4,
          border: "none",
          background: "transparent",
          color: T.a,
          font: `500 13px ${T.ui}`,
          cursor: "pointer",
        }}
      >
        <Icon name="back" size={13} /> {t("jobDetail.back")}
      </button>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <h2 style={{ font: `600 19px ${T.disp}`, color: T.ink, margin: 0, letterSpacing: ".01em" }}>
          {job.company}
          <span style={{ color: T.ink3, fontWeight: 400 }}> — </span>
          {job.role}
        </h2>
        <span
          style={{
            font: `600 10px ${T.mono}`,
            color: TIER_COLOR[job.tier] ?? T.ink3,
            border: `1px solid ${T.bd2}`,
            borderRadius: T.btnRadius,
            padding: "3px 7px",
          }}
        >
          {t("jobDetail.tierLabel", { tier: job.tier })}
        </span>
        <StatusBadge state={job.state} />
        <div style={{ display: "flex", gap: 7, marginLeft: "auto", flexWrap: "wrap" }}>
          {showRetry && (
            <ActionBtn
              label={retrying ? t("jobDetail.retrying") : t("jobDetail.retry")}
              icon="refresh"
              disabled={retrying}
              onClick={() => {
                handleRetry().catch((err: unknown) => {
                  console.error("JobDetail retry error:", err);
                });
              }}
            />
          )}
          {showRequeue && (
            <ActionBtn
              label={requeuing ? t("jobDetail.requeuing") : t("jobDetail.requeue")}
              icon="refresh"
              disabled={requeuing}
              onClick={() => {
                handleRequeue().catch((err: unknown) => {
                  console.error("JobDetail requeue error:", err);
                });
              }}
            />
          )}
          {showDismiss && (
            <ActionBtn
              label={dismissing ? t("jobDetail.dismissing") : t("jobDetail.dismiss")}
              icon="x"
              danger
              disabled={dismissing}
              onClick={() => {
                handleDismiss().catch((err: unknown) => {
                  console.error("JobDetail dismiss error:", err);
                });
              }}
            />
          )}
          {showCancel && (
            <ActionBtn
              label={deleting ? t("jobDetail.deleting") : t("jobDetail.delete")}
              icon="trash"
              danger
              disabled={deleting}
              onClick={() => {
                handleDelete().catch((err: unknown) => {
                  console.error("JobDetail delete error:", err);
                });
              }}
            />
          )}
        </div>
      </div>

      {showNuclearConfirm && (
        <div
          style={{
            position: "relative",
            padding: "12px 14px",
            ...panelBase(T, {
              bg: `color-mix(in srgb, ${T.danger} 8%, ${T.surface})`,
              border: `1px solid ${T.danger}`,
              chamfer: 10,
            }),
          }}
        >
          <div style={{ font: `500 13px ${T.ui}`, color: T.ink }}>
            {t("jobDetail.nuclearWarning")}
          </div>
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            <ActionBtn
              label={t("jobDetail.yesRestart")}
              danger
              onClick={() => { handleNuclearConfirm().catch(console.error); }}
            />
            <ActionBtn label={t("jobDetail.cancel")} onClick={() => setShowNuclearConfirm(false)} />
          </div>
        </div>
      )}

      {dismissError && <p style={{ font: `400 13px ${T.ui}`, color: T.danger, margin: 0 }}>{dismissError}</p>}
      {requeueError && <p style={{ font: `400 13px ${T.ui}`, color: T.danger, margin: 0 }}>{requeueError}</p>}
      {deleteError && <p style={{ font: `400 13px ${T.ui}`, color: T.danger, margin: 0 }}>{deleteError}</p>}
      {retryError && <p style={{ font: `400 13px ${T.ui}`, color: T.danger, margin: 0 }}>{retryError}</p>}

      {/* Pipeline progress */}
      <div>
        <div
          style={{
            font: `600 10px ${T.mono}`,
            letterSpacing: ".14em",
            color: T.ink3,
            textTransform: "uppercase",
            marginBottom: 6,
          }}
        >
          PIPELINE_PROGRESS
        </div>
        <div style={{ ...panelBase(T, { chamfer: 12 }), padding: "10px 14px", position: "relative" }}>
          {cornerMarks(T, T.bd2)}
          <StageTimeline job={job} />
        </div>
      </div>

      {/* Job Description collapsible section */}
      <div>
        <button
          type="button"
          onClick={() => setJdOpen((prev) => !prev)}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            border: "none",
            background: "transparent",
            cursor: "pointer",
            font: `600 10px ${T.mono}`,
            letterSpacing: ".14em",
            color: T.ink3,
            textTransform: "uppercase",
            padding: 0,
            marginBottom: 6,
          }}
        >
          <span
            style={{
              transform: jdOpen ? "rotate(90deg)" : "none",
              transition: "transform .12s",
              display: "flex",
            }}
          >
            <Icon name="chevron" size={11} />
          </span>
          JOB_DESCRIPTION.TXT
        </button>
        {jdOpen && (
          <pre
            style={{
              margin: 0,
              whiteSpace: "pre-wrap",
              font: `400 12px/1.6 ${T.mono}`,
              color: T.ink2,
              background: T.sunk,
              border: `1px solid ${T.bd}`,
              borderRadius: T.btnRadius,
              padding: "12px 14px",
              maxHeight: 256,
              overflow: "auto",
            }}
          >
            {job.jd}
          </pre>
        )}
      </div>

      {/* Error box */}
      {(job.error || job.state === "failed") && (
        <div
          style={{
            position: "relative",
            display: "flex",
            gap: 10,
            ...panelBase(T, {
              bg: `color-mix(in srgb, ${T.danger} 8%, ${T.surface})`,
              border: `1px solid color-mix(in srgb, ${T.danger} 45%, ${T.bd})`,
              chamfer: 10,
            }),
            padding: "11px 14px",
          }}
        >
          <span style={{ color: T.danger, flex: "none", marginTop: 1 }}>
            <Icon name="alert" size={16} />
          </span>
          <div>
            <div style={{ font: `600 11px ${T.mono}`, color: T.danger, letterSpacing: ".06em", marginBottom: 3 }}>
              {t("jobDetail.exceptionLabel")}
            </div>
            <div style={{ font: `400 13px/1.5 ${T.ui}`, color: T.ink }}>
              {job.error || t("jobDetail.unknownError")}
            </div>
          </div>
        </div>
      )}

      {/* A pipeline stage running doesn't retire the CV — clicking the CV_ADJUST dot in the
          timeline (StageTimeline.tsx) jumps to a read-only view of it (ReviewPane's own
          state==="running" check keeps it non-interactive; it degrades gracefully to
          "rendering in progress" if no cv_adjust Document exists yet). This only applies to
          `running`, never `awaiting_input` — the agent may be blocking on a question, and
          FollowUpPane (the only place that question and its answer box are shown) must
          never be suppressed while the job is actually waiting on the user. Clicking
          COVER_LETTER (or anything resetting viewedStage away from "cv") returns to the
          normal running-lane UI below. */}
      {job.state === "running" && viewedStage === "cv" && (
        // Both answer different questions ("what does the draft look like" vs "what
        // has the agent been asking") — render both rather than picking one.
        <>
          <ReviewPane jobId={job.id} mode="cv-gate" />
          <AgentThread jobId={job.id} mode="none" />
        </>
      )}
      {job.state === "running" && viewedStage !== "cv" && (
        <AgentThread jobId={job.id} mode="none" />
      )}
      {job.state === "awaiting_input" && <FollowUpPane jobId={job.id} />}
      {job.state === "cv_review" && <ReviewPane jobId={job.id} mode="cv-gate" />}
      {(job.state === "review" || job.state === "approved") && (
        <ReviewPane jobId={job.id} />
      )}
      {job.state === "cv_done" && (
        // Brief transitional state between "Approve CV" succeeding (cv_review → cv_done)
        // and the orchestrator actually dispatching cover_letter (which flips state back
        // to running). Without this, the pane matches none of the branches above and
        // renders blank with no confirmation the approval went through.
        <div style={{ font: `400 13px/1.5 ${T.ui}`, color: T.ink3, padding: "11px 14px" }}>
          {t("jobDetail.cvApprovedWaiting")}
        </div>
      )}

      {job.state === "unfit" && <UnfitModal job={job} />}
    </div>
  );
}
