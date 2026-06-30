// Decorative ambient telemetry chrome — faint grid, scanline sweep, corner session/sync
// readouts. Ported from the design handoff's `renderAmbient()`. Purely visual; no real
// telemetry is wired up. Toggle via the `telemetry` prop (default on, per design decision).
import { useEffect, useRef, useState } from "react";
import type { Theme } from "./tokens";

function randomHex(len = 6): string {
  const chars = "0123456789ABCDEF";
  return Array.from({ length: len }, () => chars[Math.floor(Math.random() * chars.length)]).join("");
}

export function Ambient({ T, telemetry = true, label }: { T: Theme; telemetry?: boolean; label: string }) {
  const sessionRef = useRef(randomHex());
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!telemetry) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [telemetry]);

  if (!telemetry) return null;

  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 0, pointerEvents: "none" }}>
      <div
        style={{
          position: "absolute",
          inset: 0,
          backgroundImage:
            "linear-gradient(rgba(51,230,230,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(51,230,230,.05) 1px,transparent 1px)",
          backgroundSize: "40px 40px",
          WebkitMaskImage: "radial-gradient(ellipse at 50% 0%, black 0%, transparent 70%)",
          maskImage: "radial-gradient(ellipse at 50% 0%, black 0%, transparent 70%)",
        }}
      />
      <div
        style={{
          position: "absolute",
          inset: 0,
          overflow: "hidden",
          backgroundImage:
            "linear-gradient(180deg, transparent 0%, transparent 38%, rgba(51,230,230,.045) 50%, transparent 62%, transparent 100%)",
          backgroundSize: "100% 160vh",
          backgroundRepeat: "repeat-y",
          animation: "jsscan 16s linear infinite",
        }}
      />
      <div
        style={{
          position: "absolute",
          right: 14,
          bottom: 10,
          font: `400 10px ${T.mono}`,
          color: "rgba(139,151,166,.32)",
          letterSpacing: ".06em",
        }}
      >
        {label} // SESSION 0x{sessionRef.current} // SYNC {8 + (tick % 7)}ms
      </div>
    </div>
  );
}
