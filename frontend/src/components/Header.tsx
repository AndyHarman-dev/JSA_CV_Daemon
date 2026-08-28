import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { JobState } from "../types";
import { useT } from "../i18n/useT";
import { useOutsideClick } from "../hooks/useOutsideClick";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase, cornerMarks } from "../theme/chrome";
import { Icon } from "../theme/Icon";

const T = SHELL_THEME;

const RUNNING_STATES: JobState[] = ["running", "pending", "cv_done", "cl_done"];
const INBOX_STATES: JobState[] = ["awaiting_input"];
const REVIEW_STATES: JobState[] = ["cv_review", "review"];
const DONE_STATES: JobState[] = ["approved"];
const FAILED_STATES: JobState[] = ["failed"];

const BACKEND_LABELS: Record<string, string> = {
  "claude-cli": "CLAUDE CLI",
  "google-cli": "GOOGLE CLI",
  anthropic: "ANTHROPIC API",
  "opencode-zen": "OPENCODE ZEN",
};

function backendLabel(id: string): string {
  return BACKEND_LABELS[id] ?? id.toUpperCase();
}

function CountChip({ label, count, color }: { label: string; count: number; color: string }) {
  if (count === 0) return null;
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 6,
        padding: "4px 10px",
        borderRadius: T.btnRadius,
        background: T.sunk,
        border: `1px solid color-mix(in srgb, ${color} 40%, ${T.bd})`,
        font: `500 11px ${T.mono}`,
        color,
        letterSpacing: ".04em",
      }}
    >
      {label}
      <span style={{ color: T.ink, fontWeight: 700 }}>{count}</span>
    </span>
  );
}

type BackendState =
  | { status: "loading" }
  | { status: "ok"; active: string; list: string[] }
  | { status: "error" };

export function Header() {
  const wsStatus = useStore((s) => s.wsStatus);
  const jobs = useStore((s) => s.jobs);
  const setEditorOpen = useStore((s) => s.setEditorOpen);
  const lastBackendSwitch = useStore((s) => s.lastBackendSwitch);

  const [backendState, setBackendState] = useState<BackendState>({ status: "loading" });
  const [backendMenuOpen, setBackendMenuOpen] = useState(false);
  const backendMenuRef = useRef<HTMLDivElement>(null);
  const t = useT();

  useEffect(() => {
    api
      .config()
      .then((cfg) => {
        const b = cfg["backend"];
        const rawList = cfg["backends"];
        if (typeof b !== "string") {
          setBackendState({ status: "error" });
          return;
        }
        const list = Array.isArray(rawList)
          ? rawList.filter((x): x is string => typeof x === "string")
          : [b];
        setBackendState({ status: "ok", active: b, list: list.length > 0 ? list : [b] });
      })
      .catch(() => {
        setBackendState({ status: "error" });
      });
  }, []);

  // Keep the active-backend indicator fresh after a runtime failover, without altering
  // applyEvent's per-event-type behavior — the store just records the latest event.
  useEffect(() => {
    if (!lastBackendSwitch) return;
    setBackendState((prev) => {
      if (prev.status !== "ok") return prev;
      const reordered = [
        lastBackendSwitch.to_backend,
        ...prev.list.filter((id) => id !== lastBackendSwitch.to_backend),
      ];
      return { status: "ok", active: lastBackendSwitch.to_backend, list: reordered };
    });
  }, [lastBackendSwitch]);

  // Close the backend dropdown when clicking outside of it.
  useOutsideClick(backendMenuRef, backendMenuOpen, () => setBackendMenuOpen(false));

  const jobList = Object.values(jobs);

  function countByStates(states: JobState[]): number {
    return jobList.filter((j) => states.includes(j.state)).length;
  }

  const running = countByStates(RUNNING_STATES);
  const inbox = countByStates(INBOX_STATES);
  const review = countByStates(REVIEW_STATES);
  const done = countByStates(DONE_STATES);
  const failed = countByStates(FAILED_STATES);
  const workers = Math.min(running, 5);

  async function nuclearReload() {
    if ("caches" in window) {
      const keys = await caches.keys();
      await Promise.all(keys.map((k) => caches.delete(k)));
    }
    if ("serviceWorker" in navigator) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map((r) => r.unregister()));
    }
    window.location.href = `/?v=${Date.now()}`;
  }

  const uplink =
    wsStatus === "open"
      ? { label: t("header.uplinkSynced"), color: T.accent2 }
      : wsStatus === "connecting"
      ? { label: t("header.uplinkReconnecting"), color: T.a }
      : { label: t("header.uplinkLost"), color: T.danger };

  return (
    <header
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: 18,
        padding: "10px 18px",
        background: "rgba(10,14,19,.85)",
        backdropFilter: "blur(10px)",
        borderBottom: `1px solid ${T.bd}`,
        boxShadow: "0 1px 14px rgba(0,0,0,.4)",
        flexShrink: 0,
        zIndex: 20,
        flexWrap: "wrap",
      }}
    >
      {/* Left: logo / wordmark — opens the CV Structure Editor */}
      <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
        <button
          type="button"
          className="jghost"
          onClick={() => setEditorOpen(true)}
          title={t("header.openEditorTitle")}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            border: "none",
            background: "transparent",
            padding: "4px 8px 4px 4px",
            margin: 0,
            borderRadius: T.btnRadius,
            cursor: "pointer",
            textAlign: "left",
          }}
        >
          <div
            style={{
              width: 32,
              height: 32,
              ...panelBase(T, { bg: T.a, chamfer: 8 }),
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "#06080B",
              boxShadow: `0 0 16px ${T.a}66`,
              flex: "none",
            }}
          >
            <Icon name="bolt" size={16} />
          </div>
          <div style={{ lineHeight: 1.2 }}>
            <div style={{ font: `700 14.5px ${T.disp}`, color: T.ink, letterSpacing: ".02em" }}>
              JSA<span style={{ color: T.a }}> // </span>DAEMON
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 5,
                font: `500 10px ${T.mono}`,
                color: T.ink3,
                letterSpacing: ".08em",
              }}
            >
              JOB_SEARCH_AUTOMATION · LOCAL
              <span style={{ color: T.a }}>⇄ EDITOR</span>
            </div>
          </div>
        </button>
      </div>

      {/* Center: backend cluster + workers meter + count chips */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <div style={{ position: "relative" }} ref={backendMenuRef}>
          <button
            type="button"
            className="jghost"
            onClick={() => setBackendMenuOpen((open) => !open)}
            title={t("header.backendMenuTitle")}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 7,
              padding: "5px 10px",
              border: `1px solid ${T.bd}`,
              borderRadius: T.btnRadius,
              background: T.sunk,
              color: T.ink2,
              cursor: "pointer",
            }}
          >
            <span style={{ color: T.accent2, display: "flex" }}>
              <Icon name="server" size={13} />
            </span>
            <span style={{ font: `500 10.5px ${T.mono}`, color: T.ink3, letterSpacing: ".06em" }}>
              {t("header.backendLabel")}
            </span>
            {backendState.status === "ok" && (
              <>
                <span style={{ font: `600 11px ${T.mono}`, color: T.ink }}>
                  {backendLabel(backendState.active)}
                </span>
                {backendState.list.length > 1 && (
                  <span
                    style={{
                      font: `500 9.5px ${T.mono}`,
                      color: T.ink3,
                      background: T.surface,
                      border: `1px solid ${T.bd}`,
                      borderRadius: T.btnRadius,
                      padding: "1px 5px",
                    }}
                  >
                    +{backendState.list.length - 1}
                  </span>
                )}
              </>
            )}
            {backendState.status === "loading" && (
              <span style={{ font: `500 11px ${T.mono}`, color: T.ink3 }}>…</span>
            )}
            <span
              style={{
                color: T.ink3,
                transform: backendMenuOpen ? "rotate(180deg)" : "none",
                transition: "transform .12s",
                display: "flex",
              }}
            >
              <Icon name="chevron" size={9} />
            </span>
          </button>
          {backendMenuOpen && backendState.status === "ok" && (
            <div
              style={{
                position: "absolute",
                top: "100%",
                left: 0,
                marginTop: 6,
                width: 240,
                zIndex: 30,
                ...panelBase(T, { chamfer: 10 }),
                boxShadow: T.shadowMd,
                padding: 5,
              }}
            >
              {cornerMarks(T, T.bd2, 8)}
              <div
                style={{
                  font: `600 9.5px ${T.mono}`,
                  letterSpacing: ".12em",
                  color: T.ink3,
                  textTransform: "uppercase",
                  padding: "5px 9px 7px",
                }}
              >
                {t("header.backendFailoverQueue")}
              </div>
              {backendState.list.map((id, i) => {
                const isActive = i === 0;
                const color = isActive ? T.accent2 : T.ink3;
                return (
                  <div
                    key={id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 9,
                      padding: "7px 9px",
                      borderRadius: T.btnRadius,
                      background: isActive ? T.aSoft : "transparent",
                    }}
                  >
                    <span style={{ font: `600 10px ${T.mono}`, color: T.ink3, width: 14, flex: "none" }}>
                      {i + 1}
                    </span>
                    {isActive ? (
                      <span
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: 6,
                          background: color,
                          boxShadow: `0 0 6px ${color}`,
                          animation: "jsblink 2s ease-in-out infinite",
                          flex: "none",
                        }}
                      />
                    ) : (
                      <span
                        style={{
                          width: 6,
                          height: 6,
                          borderRadius: 6,
                          border: `1.5px solid ${color}`,
                          flex: "none",
                        }}
                      />
                    )}
                    <span style={{ font: `500 12px ${T.ui}`, color: isActive ? T.ink : T.ink2, flex: 1 }}>
                      {backendLabel(id)}
                    </span>
                    <span style={{ font: `500 9px ${T.mono}`, color, letterSpacing: ".05em" }}>
                      {isActive ? t("header.active") : t("header.standby")}
                    </span>
                  </div>
                );
              })}
              <div
                style={{
                  font: `400 10.5px/1.5 ${T.ui}`,
                  color: T.ink3,
                  padding: "8px 9px 4px",
                  borderTop: `1px solid ${T.bd}`,
                  marginTop: 3,
                }}
              >
                {t("header.failoverExplain")}
              </div>
            </div>
          )}
        </div>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            padding: "4px 10px",
            background: T.sunk,
            border: `1px solid ${T.bd}`,
            borderRadius: T.btnRadius,
          }}
        >
          <Icon name="bolt" size={12} color={T.a} />
          <span style={{ font: `500 10px ${T.mono}`, color: T.ink3, letterSpacing: ".06em" }}>
            {t("header.workersLabel")}
          </span>
          <div style={{ display: "flex", gap: 2 }}>
            {Array.from({ length: 5 }, (_, i) => (
              <span
                key={i}
                style={{
                  width: 5,
                  height: 11,
                  borderRadius: 1,
                  background: i < workers ? T.a : T.bd,
                  boxShadow: i < workers ? `0 0 5px ${T.a}` : "none",
                }}
              />
            ))}
          </div>
          <span style={{ font: `500 10px ${T.mono}`, color: T.ink2 }}>{workers}/5</span>
        </div>

        <CountChip label={t("header.inboxLabel")} count={inbox} color={T.a} />
        <CountChip label={t("header.reviewLabel")} count={review} color={T.violet} />
        <CountChip label={t("header.doneLabel")} count={done} color={T.green} />
        <CountChip label={t("header.failedLabel")} count={failed} color={T.danger} />
      </div>

      {/* Right: UPLINK status + hard reload */}
      <div style={{ display: "flex", alignItems: "center", gap: 10, flex: "none" }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "5px 10px",
            background: T.sunk,
            border: `1px solid ${T.bd}`,
            borderRadius: T.btnRadius,
          }}
        >
          <span
            style={{
              width: 7,
              height: 7,
              borderRadius: 7,
              background: uplink.color,
              boxShadow: `0 0 6px ${uplink.color}`,
              animation: "jsblink 2.4s ease-in-out infinite",
              flex: "none",
            }}
          />
          <span style={{ font: `500 10.5px ${T.mono}`, color: T.ink2, letterSpacing: ".06em" }}>
            {uplink.label}
          </span>
        </div>
        <button
          type="button"
          className="jbtn"
          onClick={() => {
            void nuclearReload();
          }}
          title={t("header.hardReloadTitle")}
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            border: `1px solid ${T.bd}`,
            borderRadius: T.btnRadius,
            background: "transparent",
            color: T.ink2,
            cursor: "pointer",
          }}
        >
          <Icon name="refresh" size={13} />
        </button>
      </div>
    </header>
  );
}
