// Per-row LAUNCH control (design_handoff_launch_and_boot/JSA App Shell.dc.html
// `launchSlot`/`launchJob`). Replaces the usual StatusBadge slot for a `queued` job.
// Rendered as a role="button" span (not a real <button>) because it sits inside
// JobList's outer row <button> — nesting a real button there would be invalid HTML.
import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { useT } from "../i18n/useT";
import { Icon } from "../theme/Icon";
import { Spinner } from "../theme/chrome";
import { SHELL_THEME } from "../theme/tokens";

const T = SHELL_THEME;

type Phase = "idle" | "arm" | "exit";

// Timings per the design handoff: 150ms armed (spinner) before the dematerialize
// animation starts, then 380ms for the dematerialize itself to finish.
const ARM_MS = 150;
const EXIT_MS = 380;

export function LaunchButton({ jobId }: { jobId: string }) {
  const launchJob = useStore((s) => s.launchJob);
  const t = useT();
  const [phase, setPhase] = useState<Phase>("idle");
  const timeoutsRef = useRef<number[]>([]);

  useEffect(() => {
    return () => {
      timeoutsRef.current.forEach((id) => window.clearTimeout(id));
    };
  }, []);

  const handleActivate = () => {
    if (phase !== "idle") return; // no double-click / no confirmation step, by design
    setPhase("arm");
    const armTimeout = window.setTimeout(() => {
      setPhase("exit");
      const exitTimeout = window.setTimeout(() => {
        launchJob(jobId).catch(() => {
          // launchJob already reverted the job to `queued` server-side; reset the
          // local animation state too, or this control stays stuck invisible/inert.
          setPhase("idle");
        });
      }, EXIT_MS);
      timeoutsRef.current.push(exitTimeout);
    }, ARM_MS);
    timeoutsRef.current.push(armTimeout);
  };

  const launching = phase !== "idle";

  return (
    <span
      role="button"
      tabIndex={launching ? -1 : 0}
      aria-disabled={launching}
      onClick={(e) => {
        e.stopPropagation(); // don't also trigger the row's onSelect
        handleActivate();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          handleActivate();
        }
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 9px",
        borderRadius: T.btnRadius,
        font: `600 10px ${T.disp}`,
        letterSpacing: ".04em",
        textTransform: "uppercase",
        cursor: launching ? "default" : "pointer",
        flex: "none",
        background: launching ? T.sunk : T.a,
        color: launching ? T.a : "#06080B",
        animation: phase === "exit" ? `jslaunchexit ${EXIT_MS}ms ease forwards` : undefined,
      }}
    >
      {launching ? (
        <Spinner color={T.a} />
      ) : (
        <Icon name="play" size={9} color="#06080B" />
      )}
      {launching ? t("launch.launching") : t("launch.button")}
    </span>
  );
}
