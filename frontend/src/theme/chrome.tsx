// Shared chrome helpers — chamfer/panel/corner-mark primitives and the job-state badge,
// ported from the design handoff's `chamferPath`/`panelBase`/`cornerMarks`/`badge`.
import type { CSSProperties, ReactNode } from "react";
import type { JobState } from "../types";
import { useT } from "../i18n/useT";
import type { Theme } from "./tokens";

export function chamferPath(c: number): string {
  return `polygon(0 0, calc(100% - ${c}px) 0, 100% ${c}px, 100% 100%, ${c}px 100%, 0 calc(100% - ${c}px))`;
}

export interface PanelOpts {
  bg?: string;
  border?: string;
  chamfer?: number;
  radius?: number;
}

// Returns the background/border/[clipPath|borderRadius] style for a panel under the
// current skin. Under "hud" (chamfer=true) this clips a corner instead of rounding.
export function panelBase(T: Theme, o: PanelOpts = {}): CSSProperties {
  const st: CSSProperties = {
    background: o.bg || T.surface,
    border: `1px solid ${o.border || T.bd}`,
  };
  if (T.chamfer) {
    st.clipPath = chamferPath(o.chamfer ?? 12);
    st.borderRadius = 0;
  } else {
    st.borderRadius = o.radius ?? T.radius;
  }
  return st;
}

// L-shaped corner brackets for the "terminal" skin. Returns [] under "hud" — the chamfer
// itself reads as the tech cue, so brackets would double up. Kept for completeness /
// in case a future skin toggle is reintroduced.
export function cornerMarks(T: Theme, color: string, sz = 9, thick = 2): ReactNode[] {
  if (T.chamfer) return [];
  const base: CSSProperties = { position: "absolute", width: sz, height: sz, pointerEvents: "none" };
  return [
    <span key="cm-tl" style={{ ...base, top: -1, left: -1, borderTop: `${thick}px solid ${color}`, borderLeft: `${thick}px solid ${color}` }} />,
    <span key="cm-tr" style={{ ...base, top: -1, right: -1, borderTop: `${thick}px solid ${color}`, borderRight: `${thick}px solid ${color}` }} />,
    <span key="cm-bl" style={{ ...base, bottom: -1, left: -1, borderBottom: `${thick}px solid ${color}`, borderLeft: `${thick}px solid ${color}` }} />,
    <span key="cm-br" style={{ ...base, bottom: -1, right: -1, borderBottom: `${thick}px solid ${color}`, borderRight: `${thick}px solid ${color}` }} />,
  ];
}

export interface StateMeta {
  label: string;
  code: string;
  color: string;
  spin?: boolean;
}

// State -> {label, code, color, spin} map (the design's STATE_META). `color` is a function
// of the theme so the badge glows the right accent on whichever surface renders a job
// (only the Jobs shell does today, but JobState is shared so this lives here). `label` is a
// translation key (resolved by the caller via `useT()`, since this is a plain function, not
// a component, and can't call hooks itself) — `code` stays a literal terminal-style
// abbreviation, intentional HUD flavor rather than user-facing prose.
export function stateMeta(T: Theme): Record<JobState, StateMeta> {
  return {
    queued: { label: "stateMeta.queued", code: "PARK", color: T.ink3 },
    pending: { label: "stateMeta.pending", code: "STDBY", color: T.ink2 },
    running: { label: "stateMeta.running", code: "PROC", color: T.accent2, spin: true },
    awaiting_input: { label: "stateMeta.awaitingInput", code: "WAIT", color: T.a },
    fit_done: { label: "stateMeta.fitDone", code: "CHK", color: T.accent2 },
    unfit: { label: "stateMeta.unfit", code: "FLAG", color: T.a },
    cv_done: { label: "stateMeta.cvDone", code: "CV_OK", color: T.accent2 },
    cl_done: { label: "stateMeta.clDone", code: "CL_OK", color: T.accent2 },
    review: { label: "stateMeta.review", code: "RVW", color: T.violet },
    approved: { label: "stateMeta.approved", code: "DONE", color: T.green },
    failed: { label: "stateMeta.failed", code: "ERR", color: T.danger },
    dismissed: { label: "stateMeta.dismissed", code: "OFF", color: T.ink3 },
  };
}

export function Spinner({ color, size = 6 }: { color: string; size?: number }) {
  return (
    <span
      style={{
        width: size,
        height: size,
        borderRadius: size,
        border: `1.5px solid ${color}`,
        borderTopColor: "transparent",
        animation: "jsspin .7s linear infinite",
      }}
    />
  );
}

export function Badge({ T, state, className }: { T: Theme; state: JobState; className?: string }) {
  const t = useT();
  const m = stateMeta(T)[state];
  return (
    <span
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 8px",
        borderRadius: T.btnRadius,
        font: `500 10px ${T.mono}`,
        letterSpacing: ".06em",
        color: m.color,
        background: `color-mix(in srgb, ${m.color} 12%, ${T.sunk})`,
        border: `1px solid color-mix(in srgb, ${m.color} 40%, ${T.bd})`,
        flex: "none",
      }}
    >
      {m.spin ? (
        <Spinner color={m.color} />
      ) : (
        <span style={{ width: 5, height: 5, borderRadius: 5, background: m.color, boxShadow: `0 0 5px ${m.color}` }} />
      )}
      {t(m.label)}
    </span>
  );
}
