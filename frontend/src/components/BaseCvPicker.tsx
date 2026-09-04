// Job-row base-CV picker (design_handoff_cv_decks_feature). Mounted once in App.tsx next to
// <Toast/>, store-driven (JobList's per-row doc-icon trigger just calls openCvPicker). Fixed
// position, clamped to the viewport by the store at open time (ChatBox's MentionDropdown
// clamp math) — this component only ever reads the already-clamped `cvPickerPos`.
import { useRef } from "react";
import { useStore } from "../store";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase } from "../theme/chrome";
import { Icon } from "../theme/Icon";
import { useOutsideClick } from "../hooks/useOutsideClick";

const T = SHELL_THEME;

export function BaseCvPicker() {
  const t = useT();
  const jobId = useStore((s) => s.cvPickerJobId);
  const pos = useStore((s) => s.cvPickerPos);
  const job = useStore((s) => (jobId ? s.jobs[jobId] : undefined));
  const decks = useStore((s) => s.cvDecks);
  const defaultId = useStore((s) => s.cvDecksDefaultId);
  const closeCvPicker = useStore((s) => s.closeCvPicker);
  const assignBaseCv = useStore((s) => s.assignBaseCv);
  const setEditorOpen = useStore((s) => s.setEditorOpen);
  const ref = useRef<HTMLDivElement>(null);

  useOutsideClick(ref, jobId !== null, closeCvPicker);

  if (!jobId || !job) return null;

  // A has_cv:false slot (a just-created, never-saved deck) is not assignable — the server
  // 422s it (see routes_jobs.py's set_job_base_cv) — so offering it here would be a trap.
  const assignable = decks.filter((d) => d.has_cv);
  const assignedId = job.base_cv_id;

  return (
    <div
      ref={ref}
      data-testid="base-cv-picker"
      style={{
        position: "fixed",
        left: pos.x,
        top: pos.y,
        width: 280,
        maxHeight: 360,
        zIndex: 90,
        display: "flex",
        flexDirection: "column",
        ...panelBase(T, { chamfer: 12 }),
        boxShadow: T.shadowMd,
        animation: "jsfade .1s ease",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "9px 10px",
          borderBottom: `1px solid ${T.bd}`,
          flex: "none",
        }}
      >
        <span style={{ color: T.a, display: "flex", flex: "none" }}>
          <Icon name="doc" size={13} />
        </span>
        <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
          <span style={{ font: `600 11px ${T.ui}`, color: T.ink }}>{t("baseCvPicker.title")}</span>
          <span
            style={{
              font: `400 9.5px ${T.mono}`,
              color: T.ink3,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {job.company} — {job.role}
          </span>
        </div>
        <button
          type="button"
          onClick={closeCvPicker}
          title={t("toast.dismissTitle")}
          style={{
            flex: "none",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 20,
            height: 20,
            border: "none",
            background: "transparent",
            color: T.ink3,
            cursor: "pointer",
            padding: 0,
          }}
        >
          <Icon name="x" size={13} />
        </button>
      </div>

      {assignable.length === 0 ? (
        <div
          data-testid="base-cv-picker-empty"
          style={{
            padding: "18px 14px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
            alignItems: "center",
            textAlign: "center",
          }}
        >
          <span style={{ font: `400 12px/1.5 ${T.ui}`, color: T.ink3 }}>{t("baseCvPicker.empty")}</span>
          <button
            type="button"
            className="jbtn"
            data-testid="base-cv-picker-open-editor"
            onClick={() => {
              closeCvPicker();
              setEditorOpen(true);
            }}
            style={{
              border: `1px dashed ${T.bd2}`,
              background: "transparent",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              padding: "8px 12px",
              color: T.ink3,
              font: `500 10.5px ${T.ui}`,
              letterSpacing: ".03em",
              cursor: "pointer",
              borderRadius: T.btnRadius,
              width: "100%",
            }}
          >
            <Icon name="doc" size={12} />
            {t("baseCvPicker.openEditor")}
          </button>
        </div>
      ) : (
        <div style={{ overflow: "auto", flex: 1, minHeight: 0 }}>
          {assignable.map((d) => {
            const label = d.name ?? d.auto_title ?? t("cvDecks.untitled");
            const isAssigned = assignedId === d.id;
            const isDefault = d.id === defaultId;
            return (
              <button
                key={d.id}
                type="button"
                className="jbtn"
                data-testid={`base-cv-picker-deck-${d.id}`}
                onClick={() => void assignBaseCv(job.id, d.id)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  width: "100%",
                  textAlign: "left",
                  padding: "8px 10px",
                  border: "none",
                  borderBottom: `1px solid ${T.bd}`,
                  background: isAssigned ? T.aSoft : "transparent",
                  cursor: "pointer",
                }}
              >
                <span style={{ color: isAssigned ? T.a : T.ink3, display: "flex", flex: "none" }}>
                  <Icon name="doc" size={12} />
                </span>
                <span
                  style={{
                    flex: 1,
                    minWidth: 0,
                    font: `500 12px ${T.ui}`,
                    color: T.ink,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {label}
                </span>
                {isDefault && (
                  <span
                    style={{
                      font: `600 8.5px ${T.mono}`,
                      letterSpacing: ".08em",
                      color: T.ink3,
                      flex: "none",
                    }}
                  >
                    {t("baseCvPicker.default")}
                  </span>
                )}
                {isAssigned && (
                  <span
                    data-testid={`base-cv-picker-check-${d.id}`}
                    style={{ color: T.a, display: "flex", flex: "none" }}
                  >
                    <Icon name="check" size={12} />
                  </span>
                )}
              </button>
            );
          })}
        </div>
      )}

      {assignedId && (
        <button
          type="button"
          className="jbtn"
          data-testid="base-cv-picker-unassign"
          onClick={() => void assignBaseCv(job.id, null)}
          style={{
            flex: "none",
            border: "none",
            borderTop: `1px solid ${T.bd}`,
            background: "transparent",
            padding: "10px",
            color: T.ink3,
            font: `600 10px ${T.disp}`,
            letterSpacing: ".05em",
            textTransform: "uppercase",
            cursor: "pointer",
            width: "100%",
          }}
        >
          {t("baseCvPicker.unassign")}
        </button>
      )}
    </div>
  );
}
