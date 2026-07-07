// --select-language boot sequence (design_handoff_launch_and_boot/JSA App Shell.dc.html
// `renderBootGate`/`renderBootLangPicker`/`renderBootLog`). Only mounts when the CLI was
// invoked with --select-language (jsa/config.py Settings.select_language, surfaced via
// /api/config's `select_language` key -> store.selectLanguageMode). A full-viewport overlay
// on top of the already-mounted app shell — dismissing it (bootStage -> "app") just reveals
// the dashboard underneath rather than navigating to it.
import { useEffect, useRef, useState } from "react";
import { useStore } from "../store";
import { useT } from "../i18n/useT";
import { Icon } from "../theme/Icon";
import { chamferPath } from "../theme/chrome";
import { SHELL_THEME } from "../theme/tokens";

const T = SHELL_THEME;

// Boot log copy is intentionally left as literal, HUD-style terminal lines (CLAUDE.md's
// "i18n" convention explicitly carves these out — same category as StateMeta.code) rather
// than run through useT(). Only the interpolated locale-pack line varies at runtime.
function bootLines(languageLabel: string): string[] {
  return [
    "daemon.init() … OK",
    "loading module registry … OK",
    "connecting backend: CLAUDE CLI … OK",
    "mounting job queue store … OK",
    "restoring scratch buffer … OK",
    `applying locale pack: ${languageLabel.toUpperCase()} … OK`,
    "verifying output/ directory … OK",
    "starting workers [1/5 .. 5/5] … OK",
    "daemon ready.",
  ];
}

const BOOT_DURATION_MS = 2300;
const HOLD_MS = 500;

function Logo() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
      <div
        style={{
          width: 32,
          height: 32,
          background: T.a,
          clipPath: chamferPath(8),
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flex: "none",
        }}
      >
        <Icon name="bolt" size={17} color="#06080B" />
      </div>
      <span style={{ font: `700 17px ${T.disp}`, color: T.ink }}>
        JSA <span style={{ color: T.a }}>//</span> DAEMON
      </span>
    </div>
  );
}

function LanguagePickerView() {
  const languages = useStore((s) => s.languages);
  const bootLang = useStore((s) => s.bootLang);
  const setBootLang = useStore((s) => s.setBootLang);
  const setLanguage = useStore((s) => s.setLanguage);
  const setBootStage = useStore((s) => s.setBootStage);
  const t = useT();
  const [error, setError] = useState(false);

  const handleConfirm = async () => {
    setError(false);
    const ok = await setLanguage(bootLang);
    if (ok) {
      setBootStage("boot");
    } else {
      setError(true);
    }
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 22,
        maxWidth: 560,
        padding: 24,
        textAlign: "center",
      }}
    >
      <Logo />
      <div
        style={{
          font: `500 11px ${T.mono}`,
          letterSpacing: ".14em",
          color: T.ink3,
          textTransform: "uppercase",
        }}
      >
        {t("boot.pickerCaption")}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8, justifyContent: "center" }}>
        {languages.map(([code, english]) => {
          const selected = code === bootLang;
          return (
            <button
              key={code}
              type="button"
              onClick={() => setBootLang(code)}
              style={{
                padding: "7px 14px",
                borderRadius: T.btnRadius,
                border: `1px solid ${selected ? T.a : T.bd2}`,
                background: selected ? T.aSoft : "transparent",
                color: selected ? T.a : T.ink2,
                font: `500 12.5px ${T.ui}`,
                cursor: "pointer",
              }}
            >
              {english}
            </button>
          );
        })}
      </div>
      {error && (
        <div style={{ font: `500 11px ${T.mono}`, color: T.danger }}>
          {t("boot.setLanguageFailed")}
        </div>
      )}
      <button
        type="button"
        onClick={() => void handleConfirm()}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          padding: "10px 22px",
          borderRadius: T.btnRadius,
          border: "none",
          background: T.a,
          color: "#06080B",
          font: `600 13px ${T.disp}`,
          cursor: "pointer",
          marginTop: 6,
        }}
      >
        <Icon name="bolt" size={14} color="#06080B" />
        {t("boot.confirmAndBoot")}
      </button>
    </div>
  );
}

function BootLogView() {
  const bootLang = useStore((s) => s.bootLang);
  const languages = useStore((s) => s.languages);
  const setBootStage = useStore((s) => s.setBootStage);

  const languageLabel = languages.find(([code]) => code === bootLang)?.[1] ?? bootLang;
  const lines = bootLines(languageLabel);

  const [pct, setPct] = useState(0);
  const startRef = useRef<number>(performance.now());
  const dismissedRef = useRef(false);

  // Elapsed-wall-clock-driven progress (NOT tick-counted) — a late-firing frame under
  // throttling (backgrounded tab, low-power mode) still snaps to the correct, possibly
  // 100%, progress instead of stalling. See the design handoff's explicit warning.
  useEffect(() => {
    let raf = 0;
    const tick = () => {
      const elapsed = performance.now() - startRef.current;
      const p = Math.min(100, (elapsed / BOOT_DURATION_MS) * 100);
      setPct(p);
      if (p >= 100) {
        if (!dismissedRef.current) {
          dismissedRef.current = true;
          window.setTimeout(() => setBootStage("app"), HOLD_MS);
        }
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [setBootStage]);

  const visibleCount = Math.max(1, Math.ceil((pct / 100) * lines.length));
  const visibleLines = lines.slice(0, visibleCount);

  return (
    <div style={{ width: 520, maxWidth: "90vw", padding: 24 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 18,
          font: `600 12px ${T.mono}`,
          letterSpacing: ".14em",
          color: T.a,
          textTransform: "uppercase",
        }}
      >
        <Icon name="server" size={15} color={T.a} />
        JSA_DAEMON // BOOT SEQUENCE
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 18, minHeight: 168 }}>
        {visibleLines.map((line, i) => (
          <div
            key={i}
            style={{ font: `400 13px ${T.mono}`, color: T.ink2, animation: "jsfade .2s ease" }}
          >
            <span style={{ color: T.accent2 }}>{"> "}</span>
            {line}
            {i === visibleLines.length - 1 && pct < 100 && (
              <span
                style={{
                  display: "inline-block",
                  width: 7,
                  height: 13,
                  marginLeft: 4,
                  transform: "translateY(2px)",
                  background: T.ink2,
                  animation: "jsblink 1s step-start infinite",
                }}
              />
            )}
          </div>
        ))}
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <div style={{ flex: 1, height: 6, background: T.sunk, borderRadius: 3, overflow: "hidden" }}>
          <div style={{ width: `${pct}%`, height: "100%", background: T.a, transition: "width .1s linear" }} />
        </div>
        <span style={{ font: `400 11px ${T.mono}`, color: T.ink3, minWidth: 32, textAlign: "right" }}>
          {Math.round(pct)}%
        </span>
      </div>
    </div>
  );
}

export function BootGate() {
  const bootStage = useStore((s) => s.bootStage);

  if (bootStage === "app") return null;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 200,
        background: T.canvas,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {bootStage === "lang" ? <LanguagePickerView /> : <BootLogView />}
    </div>
  );
}
