import { useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobDTO } from "../types";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase, cornerMarks } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

interface UnfitModalProps {
  job: JobDTO;
}

/**
 * Centered overlay shown when a job is parked in the `unfit` state by the
 * fit-assessment stage. Presents the agent's reason and two choices:
 *   - Dismiss Job (red)  → same as the existing dismiss action (→ dismissed)
 *   - Ignore (gray)      → proceed through the remaining stages (→ fit_done)
 */
export function UnfitModal({ job }: UnfitModalProps) {
  const refetchAll = useStore((s) => s.refetchAll);
  const [busy, setBusy] = useState<null | "dismiss" | "ignore">(null);
  const [error, setError] = useState<string | null>(null);

  async function run(action: "dismiss" | "ignore") {
    setBusy(action);
    setError(null);
    try {
      if (action === "dismiss") {
        await api.dismiss(job.id);
      } else {
        await api.ignoreFit(job.id);
      }
      await refetchAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(null);
    }
  }

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 50,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(3,5,8,.72)",
        backdropFilter: "blur(4px)",
        padding: 16,
      }}
    >
      <div
        style={{
          position: "relative",
          width: 460,
          maxWidth: "100%",
          ...panelBase(T, { chamfer: 16 }),
          boxShadow: T.shadowMd,
          padding: "24px 26px",
        }}
      >
        {cornerMarks(T, T.a, 11)}
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 4 }}>
          <span style={{ color: T.a }}>
            <Icon name="alert" size={18} />
          </span>
          <div style={{ font: `600 16px ${T.disp}`, color: T.ink, letterSpacing: ".02em" }}>
            FIT_ASSESSMENT: MISMATCH
          </div>
        </div>
        <div style={{ font: `400 11.5px ${T.mono}`, color: T.ink3, marginBottom: 14 }}>
          {job.company} — {job.role}
        </div>

        <div
          style={{
            ...panelBase(T, { bg: T.aSoft, border: `1px solid ${T.aBorder}`, chamfer: 8 }),
            padding: "11px 13px",
            font: `400 13.5px/1.55 ${T.ui}`,
            color: T.ink,
            marginBottom: 14,
          }}
        >
          {job.fit_reason || "The assessment flagged a significant mismatch."}
        </div>

        <p style={{ font: `400 13px/1.5 ${T.ui}`, color: T.ink2, margin: "0 0 18px" }}>
          Dismiss this job, or override the assessment and tailor your application anyway.
        </p>

        {error && <p style={{ font: `400 13px ${T.ui}`, color: T.danger, marginBottom: 12 }}>{error}</p>}

        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <button
            type="button"
            className="jghost"
            disabled={busy !== null}
            onClick={() => {
              run("ignore").catch(console.error);
            }}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 12px",
              border: `1px solid ${T.bd2}`,
              borderRadius: T.btnRadius,
              background: "transparent",
              color: T.ink,
              font: `600 11.5px ${T.disp}`,
              letterSpacing: ".03em",
              cursor: busy !== null ? "default" : "pointer",
              opacity: busy !== null ? 0.5 : 1,
            }}
          >
            {busy === "ignore" ? "Continuing…" : "Ignore & Continue"}
          </button>
          <button
            type="button"
            className="jdanger jbtn"
            disabled={busy !== null}
            onClick={() => {
              run("dismiss").catch(console.error);
            }}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "6px 12px",
              border: "1px solid rgba(255,70,85,.4)",
              borderRadius: T.btnRadius,
              background: "transparent",
              color: T.danger,
              font: `600 11.5px ${T.disp}`,
              letterSpacing: ".03em",
              cursor: busy !== null ? "default" : "pointer",
              opacity: busy !== null ? 0.5 : 1,
            }}
          >
            {busy === "dismiss" ? "Dismissing…" : "Dismiss Job"}
          </button>
        </div>
      </div>
    </div>
  );
}
