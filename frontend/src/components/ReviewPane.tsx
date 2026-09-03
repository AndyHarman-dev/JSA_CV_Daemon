import { useState, useEffect, useRef } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";
import { ChatBox } from "./ChatBox";
import { useT } from "../i18n/useT";
import { useOutsideClick } from "../hooks/useOutsideClick";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase, cornerMarks } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

interface Props {
  jobId: string;
  // "cv-gate" — the CV-only pane shown while a job is parked in `cv_review` (or, read-only,
  // while the cover-letter lane is running and the user jumps back to view the CV). Defaults
  // to "final" so the existing review/approved call site is unaffected.
  mode?: "cv-gate" | "final";
}

type TabKey = "cv" | "cl";

interface DocPaths {
  pdfUrl: string | null;
  docxUrl: string | null;
  version: number;
}

const emptyPaths: DocPaths = { pdfUrl: null, docxUrl: null, version: 1 };

/** Convert an absolute filesystem path stored in the DB to a /api/files/<relpath> URL. */
function toFileUrl(absPath: string | null | undefined): string | null {
  if (!absPath) return null;
  // Extract last two path segments: slug/filename
  const rel = absPath.replace(/^.*?([^/\\]+[/\\][^/\\]+)$/, "$1").replace(/\\/g, "/");
  return `/api/files/${rel}`;
}

export function ReviewPane({ jobId, mode = "final" }: Props) {
  const state = useStore((s) => s.jobs[jobId]?.state as JobState | undefined);
  const currentStage = useStore((s) => s.jobs[jobId]?.current_stage);
  const viewedStage = useStore((s) => s.viewedStage);
  const setViewedStage = useStore((s) => s.setViewedStage);
  const [localActiveTab, setLocalActiveTab] = useState<TabKey>("cv");
  // The CV gate only ever has a CV to show. Otherwise, StageTimeline's dots and this pane's
  // own tab buttons both write to the shared `viewedStage` — it wins whenever set, falling
  // back to local state only before either has been touched.
  const activeTab: TabKey = mode === "cv-gate" ? "cv" : viewedStage ?? localActiveTab;
  // Read-only iff the job is in flight — the only way this pane renders while running or
  // awaiting_input is the mid-pipeline jump-back to view the CV (see JobDetail.tsx). A job
  // parked awaiting the user (cv_review, review) is always fully interactive.
  const readOnly = state === "running" || state === "awaiting_input";
  // Which lane's read-only notice to show depends on which stage is actually running — clicking
  // CV_ADJUST during the CV's own run must not claim the cover letter lane is running.
  const readOnlyNoticeKey = currentStage === "cv_adjust" || currentStage === "revising_cv"
    ? "reviewPane.readOnlyNoticeCv"
    : "reviewPane.readOnlyNoticeCl";
  const [cvPaths, setCvPaths] = useState<DocPaths>(emptyPaths);
  const [clPaths, setClPaths] = useState<DocPaths>(emptyPaths);
  const [pathsLoading, setPathsLoading] = useState(true);
  const [approving, setApproving] = useState(false);
  const [approveError, setApproveError] = useState<string | null>(null);
  const [showDownloadMenu, setShowDownloadMenu] = useState(false);
  const downloadRef = useRef<HTMLDivElement>(null);
  const t = useT();

  // Close the download menu when clicking outside of it.
  useOutsideClick(downloadRef, showDownloadMenu, () => setShowDownloadMenu(false));

  // Fetch document paths whenever jobId or state changes.
  // By the time state === "review", the pipeline has already rendered both formats.
  useEffect(() => {
    let cancelled = false;
    setPathsLoading(true);

    api.getJob(jobId)
      .then((job) => {
        if (cancelled) return;

        // Find latest version per stage (documents may be unordered)
        const latestCv = job.documents
          .filter((d) => d.stage === "cv_adjust")
          .sort((a, b) => b.version - a.version)[0];
        const latestCl = job.documents
          .filter((d) => d.stage === "cover_letter")
          .sort((a, b) => b.version - a.version)[0];

        setCvPaths({
          pdfUrl: toFileUrl(latestCv?.pdf_path),
          docxUrl: toFileUrl(latestCv?.docx_path),
          version: latestCv?.version ?? 1,
        });
        setClPaths({
          pdfUrl: toFileUrl(latestCl?.pdf_path),
          docxUrl: toFileUrl(latestCl?.docx_path),
          version: latestCl?.version ?? 1,
        });
        setPathsLoading(false);
      })
      .catch(() => {
        if (!cancelled) setPathsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [jobId, state]);

  async function handleApprove() {
    setApproving(true);
    setApproveError(null);
    try {
      if (mode === "cv-gate") {
        await api.approveCv(jobId);
      } else {
        await api.approve(jobId);
      }
      await useStore.getState().refetchAll();
    } catch (err) {
      setApproveError(err instanceof Error ? err.message : String(err));
    } finally {
      setApproving(false);
    }
  }

  /** Programmatically download a rendered file, then close the menu. */
  function downloadFormat(url: string | null) {
    if (!url) return;
    const a = document.createElement("a");
    a.href = url;
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setShowDownloadMenu(false);
  }

  const activePaths = activeTab === "cv" ? cvPaths : clPaths;
  const activeVersionLabel = activeTab === "cv" ? t("reviewPane.cvTab") : t("reviewPane.clTab");

  function tabButton(key: TabKey, label: string) {
    const on = activeTab === key;
    return (
      <button
        type="button"
        className="jtab"
        onClick={() => {
          setLocalActiveTab(key);
          setViewedStage(key);
        }}
        style={{
          padding: "8px 4px",
          marginRight: 22,
          border: "none",
          borderBottom: `2px solid ${on ? T.a : "transparent"}`,
          background: "transparent",
          color: on ? T.a : T.ink2,
          font: `600 12.5px ${T.disp}`,
          letterSpacing: ".03em",
          cursor: "pointer",
        }}
      >
        {label}
      </button>
    );
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {/* Tab bar — the CV gate only ever has a CV to show */}
      <div style={{ display: "flex", borderBottom: `1px solid ${T.bd}` }}>
        {tabButton("cv", t("reviewPane.cvTab"))}
        {mode === "final" && tabButton("cl", t("reviewPane.clTab"))}
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ font: `400 10.5px ${T.mono}`, color: T.ink3, letterSpacing: ".06em" }}>
          {t("reviewPane.versionLabel")}
        </span>
        <span style={{ font: `600 11px ${T.mono}`, color: T.accent2 }}>v{activePaths.version}</span>
      </div>

      {/* Document preview — real PDF iframe wrapped in a bracketed viewport frame */}
      <div
        style={{
          position: "relative",
          ...panelBase(T, { chamfer: 16 }),
          background: T.sunk,
          border: `1px dashed ${T.bd2}`,
          padding: 10,
        }}
      >
        {cornerMarks(T, T.bd2)}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            marginBottom: 8,
            font: `500 9.5px ${T.mono}`,
            letterSpacing: ".08em",
            color: T.ink3,
            textTransform: "uppercase",
          }}
        >
          <span
            style={{
              width: 6,
              height: 6,
              borderRadius: 6,
              background: T.accent2,
              boxShadow: `0 0 6px ${T.accent2}`,
              animation: "jsblink 2s ease-in-out infinite",
              flex: "none",
            }}
          />
          EXPORT_PREVIEW · READ-ONLY LAYOUT — {activeVersionLabel}
        </div>
        {pathsLoading ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: "58vh",
              color: T.ink3,
              font: `400 13px ${T.ui}`,
            }}
          >
            {t("reviewPane.loadingPreview")}
          </div>
        ) : activePaths.pdfUrl ? (
          <iframe
            key={activePaths.pdfUrl}
            src={activePaths.pdfUrl}
            title={activeTab === "cv" ? t("reviewPane.cvPreviewTitle") : t("reviewPane.clPreviewTitle")}
            style={{ width: "100%", height: "58vh", border: `1px solid ${T.bd}`, background: "#fff" }}
          />
        ) : (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              height: 128,
              color: T.ink3,
              font: `400 13px ${T.ui}`,
              fontStyle: "italic",
            }}
          >
            {t("reviewPane.previewInProgress")}
          </div>
        )}
      </div>

      {/* Download button + format popup — shown for both review and approved states */}
      {!pathsLoading && (activePaths.pdfUrl || activePaths.docxUrl) && (
        <div style={{ position: "relative", alignSelf: "flex-start" }} ref={downloadRef}>
          <button
            type="button"
            className="jghost"
            onClick={() => setShowDownloadMenu((open) => !open)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 7,
              padding: "7px 13px",
              border: `1px solid ${T.bd2}`,
              borderRadius: T.btnRadius,
              background: T.surface,
              color: T.ink,
              font: `600 11.5px ${T.disp}`,
              letterSpacing: ".03em",
              cursor: "pointer",
            }}
          >
            <Icon name="download" size={13} />
            {t("reviewPane.downloadButton")}
            <span
              style={{
                color: T.ink3,
                transform: showDownloadMenu ? "rotate(180deg)" : "none",
                transition: "transform .12s",
                display: "flex",
              }}
            >
              <Icon name="chevron" size={9} />
            </span>
          </button>
          {showDownloadMenu && (
            <div
              style={{
                position: "absolute",
                top: "100%",
                left: 0,
                marginTop: 6,
                width: 210,
                zIndex: 30,
                ...panelBase(T, { chamfer: 10 }),
                boxShadow: T.shadowMd,
                padding: 5,
                animation: "jsfade .12s ease",
              }}
            >
              {cornerMarks(T, T.bd2, 8)}
              <div
                style={{
                  font: `600 9.5px ${T.mono}`,
                  letterSpacing: ".12em",
                  color: T.ink3,
                  textTransform: "uppercase",
                  padding: "5px 9px 6px",
                }}
              >
                {t("reviewPane.chooseFormat")}
              </div>
              {activePaths.pdfUrl && (
                <button
                  type="button"
                  className="jbtn"
                  onClick={() => downloadFormat(activePaths.pdfUrl)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 9,
                    width: "100%",
                    textAlign: "left",
                    padding: "8px 11px",
                    border: "none",
                    borderRadius: T.btnRadius,
                    background: "transparent",
                    color: T.ink,
                    font: `500 12.5px ${T.ui}`,
                    cursor: "pointer",
                  }}
                >
                  <span style={{ font: `600 10px ${T.mono}`, color: T.accent2, width: 36, flex: "none" }}>
                    PDF
                  </span>
                  <span>{activeTab === "cv" ? t("reviewPane.cvOption") : t("reviewPane.clOption")} · pdf</span>
                </button>
              )}
              {activePaths.docxUrl && (
                <button
                  type="button"
                  className="jbtn"
                  onClick={() => downloadFormat(activePaths.docxUrl)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 9,
                    width: "100%",
                    textAlign: "left",
                    padding: "8px 11px",
                    border: "none",
                    borderRadius: T.btnRadius,
                    background: "transparent",
                    color: T.ink,
                    font: `500 12.5px ${T.ui}`,
                    cursor: "pointer",
                  }}
                >
                  <span style={{ font: `600 10px ${T.mono}`, color: T.accent2, width: 36, flex: "none" }}>
                    DOCX
                  </span>
                  <span>{activeTab === "cv" ? t("reviewPane.cvOption") : t("reviewPane.clOption")} · docx</span>
                </button>
              )}
            </div>
          )}
        </div>
      )}

      {/* State-specific actions */}
      {state === "approved" ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            font: `600 12.5px ${T.disp}`,
            color: T.green,
            border: `1px solid color-mix(in srgb, ${T.green} 40%, ${T.bd})`,
            background: `color-mix(in srgb, ${T.green} 10%, ${T.surface})`,
            borderRadius: T.btnRadius,
            padding: "10px 14px",
            width: "fit-content",
          }}
        >
          <Icon name="check" size={14} />
          {t("reviewPane.approvedBanner")}
        </div>
      ) : readOnly ? (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            font: `600 12.5px ${T.disp}`,
            color: T.ink3,
            border: `1px solid ${T.bd2}`,
            background: T.sunk,
            borderRadius: T.btnRadius,
            padding: "10px 14px",
            width: "fit-content",
          }}
        >
          <Icon name="eye" size={14} />
          {t(readOnlyNoticeKey)}
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {approveError && (
            <p style={{ font: `400 12.5px ${T.ui}`, color: T.danger, margin: 0 }}>{approveError}</p>
          )}
          <button
            type="button"
            className="jprimary"
            disabled={approving}
            onClick={() => {
              handleApprove().catch((err: unknown) => {
                console.error("ReviewPane approve error:", err);
              });
            }}
            style={{
              alignSelf: "flex-start",
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 20px",
              border: "none",
              borderRadius: T.btnRadius,
              background: T.green,
              color: "#06150C",
              font: `600 13px ${T.disp}`,
              letterSpacing: ".04em",
              cursor: approving ? "default" : "pointer",
              opacity: approving ? 0.6 : 1,
              boxShadow: `0 1px 14px ${T.green}55`,
            }}
          >
            {approving ? (
              <>
                <span
                  style={{
                    width: 12,
                    height: 12,
                    borderRadius: 12,
                    border: "2px solid rgba(6,21,12,.4)",
                    borderTopColor: "#06150C",
                    animation: "jsspin .7s linear infinite",
                  }}
                />
                {t("reviewPane.approving")}
              </>
            ) : (
              <>
                <Icon name="check" size={15} />
                {mode === "cv-gate" ? t("reviewPane.approveCvButton") : t("reviewPane.approveButton")}
              </>
            )}
          </button>
          <div style={{ paddingTop: 12, borderTop: `1px solid ${T.bd}` }}>
            <div
              style={{
                font: `600 10px ${T.mono}`,
                letterSpacing: ".14em",
                color: T.ink3,
                textTransform: "uppercase",
                marginBottom: 8,
              }}
            >
              REQUEST_REVISION
            </div>
            <ChatBox kind="revise" jobId={jobId} fixedTarget={mode === "cv-gate" ? "cv" : undefined} />
          </div>
        </div>
      )}
    </div>
  );
}
