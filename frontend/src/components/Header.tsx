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
  mistral: "MISTRAL",
  openrouter: "OPENROUTER",
  gemini: "GEMINI API",
  "opencode-go": "OPENCODE GO",
};

function backendLabel(id: string): string {
  return BACKEND_LABELS[id] ?? id.toUpperCase();
}

// Filter input is only worth its footprint once a catalog gets long (e.g. OpenRouter's
// namespaced `vendor/model` list) -- see plan Phase 6 solution 5b.
const MODEL_FILTER_THRESHOLD = 12;

type ModelMenuState =
  | { status: "loading"; models: [] }
  | { status: "ok"; models: string[]; source: "live" | "catalog" }
  | { status: "error"; models: [] };

// Width of the failover-queue panel this submenu anchors beside (Header's panel is a fixed
// 240px) plus a small gap -- kept as a sibling of that panel, not a child, because the panel's
// own `panelBase` sets `clip-path` for the chamfered-corner skin, which clips any absolutely
// positioned descendant that extends past its own 240px box (confirmed via a live Playwright
// screenshot during Phase 6 verification: a submenu nested inside the panel rendered zero
// pixels). Positioning by a fixed left offset from the shared `backendMenuRef` ancestor sidesteps
// that clip entirely.
const QUEUE_PANEL_WIDTH = 240;
const SUBMENU_GAP = 6;

function ModelSubmenu({
  top,
  menu,
  selected,
  filter,
  onFilterChange,
  onSelect,
  t,
}: {
  top: number;
  menu: ModelMenuState | undefined;
  selected: string | undefined;
  filter: string;
  onFilterChange: (value: string) => void;
  onSelect: (model: string) => void;
  t: (key: string) => string;
}) {
  const models = menu?.models ?? [];
  const q = filter.trim().toLowerCase();
  const filtered = q ? models.filter((m) => m.toLowerCase().includes(q)) : models;
  const showFilter = models.length > MODEL_FILTER_THRESHOLD;

  return (
    <div
      style={{
        position: "absolute",
        left: QUEUE_PANEL_WIDTH + SUBMENU_GAP,
        top,
        width: 240,
        zIndex: 31,
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
        {t("header.modelMenuTitle")}
      </div>
      {showFilter && (
        <div style={{ padding: "0 6px 6px" }}>
          <input
            autoFocus
            value={filter}
            onChange={(e) => onFilterChange(e.target.value)}
            placeholder={t("header.modelFilterPlaceholder")}
            style={{
              width: "100%",
              boxSizing: "border-box",
              padding: "5px 8px",
              background: T.sunk,
              border: `1px solid ${T.bd}`,
              borderRadius: T.btnRadius,
              font: `400 12px ${T.ui}`,
              color: T.ink,
              outline: "none",
            }}
          />
        </div>
      )}
      <div style={{ maxHeight: 220, overflowY: "auto" }}>
        {menu?.status === "loading" && (
          <div style={{ padding: "10px 9px", font: `400 11px ${T.ui}`, color: T.ink3 }}>…</div>
        )}
        {menu?.status === "error" && (
          <div style={{ padding: "10px 9px", font: `400 11px ${T.ui}`, color: T.danger }}>
            {t("header.modelSelectFailed")}
          </div>
        )}
        {menu?.status === "ok" &&
          filtered.map((model) => {
            const isSelected = model === selected;
            return (
              <button
                key={model}
                type="button"
                className="jghost"
                onClick={() => onSelect(model)}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  width: "100%",
                  padding: "6px 9px",
                  border: "none",
                  borderRadius: T.btnRadius,
                  background: isSelected ? T.aSoft : "transparent",
                  cursor: "pointer",
                  textAlign: "left",
                }}
              >
                <span
                  style={{
                    font: `500 11.5px ${T.mono}`,
                    color: T.ink,
                    flex: 1,
                    overflowWrap: "anywhere",
                  }}
                >
                  {model}
                </span>
                {isSelected && <Icon name="check" size={12} color={T.accent2} />}
              </button>
            );
          })}
      </div>
      {menu?.status === "ok" && menu.source === "catalog" && (
        <div
          style={{
            font: `400 10px/1.4 ${T.ui}`,
            color: T.ink3,
            padding: "6px 9px 3px",
            borderTop: `1px solid ${T.bd}`,
            marginTop: 3,
          }}
        >
          {t("header.modelsFromCatalog")}
        </div>
      )}
    </div>
  );
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

  // Per-backend runtime model selection (Phase 6 of the multi-backend-model-select plan).
  const [supportsModelSelection, setSupportsModelSelection] = useState<Record<string, boolean>>({});
  const [selectedModels, setSelectedModels] = useState<Record<string, string>>({});
  const [openModelMenuFor, setOpenModelMenuFor] = useState<string | null>(null);
  const [modelMenus, setModelMenus] = useState<Record<string, ModelMenuState>>({});
  const [modelFilter, setModelFilter] = useState("");
  const [submenuTop, setSubmenuTop] = useState(0);
  const rowRefs = useRef<Record<string, HTMLDivElement | null>>({});

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
    // The queue reorders around the switched-to backend -- a submenu measured against the
    // old row position would now be pinned at a stale offset. Closing it is simpler and
    // safer than re-measuring, and matches every other close path's behavior.
    setOpenModelMenuFor(null);
  }, [lastBackendSwitch]);

  // Reconcile the active-backend indicator against ground truth (job.backend_name),
  // not just live WS events. `lastBackendSwitch` above only catches a failover that
  // happens while this client is connected -- a page reload, or a WS gap spanning a
  // switch, left the indicator pinned at the boot-time `/api/config` snapshot forever,
  // since `settings.backend` is never mutated after startup and `Job.backend_name` used
  // to never reach the frontend at all. Reconciling from the most recently updated job
  // that has actually dispatched on a backend closes that gap on every jobs refresh
  // (initial load, WS reconnect's refetchAll, or just a running job's own updates).
  useEffect(() => {
    if (backendState.status !== "ok") return;
    const withBackend = Object.values(jobs).filter((j) => j.backend_name);
    if (withBackend.length === 0) return;
    const latest = withBackend.reduce((a, b) => (a.updated_at > b.updated_at ? a : b));
    const trueActive = latest.backend_name as string;
    if (trueActive === backendState.active) return;
    const reordered = [trueActive, ...backendState.list.filter((id) => id !== trueActive)];
    setBackendState({ status: "ok", active: trueActive, list: reordered });
    setOpenModelMenuFor(null);
  }, [jobs, backendState]);

  useEffect(() => {
    api
      .getBackendModels()
      .then((res) => {
        setSupportsModelSelection(res.supports_model_selection);
        setSelectedModels(res.selected);
      })
      .catch(() => {
        // Leave both maps empty -- every row then renders as selection-unsupported,
        // which is the safe (fail-closed) default rather than a crash.
      });
  }, []);

  // Close the backend dropdown when clicking outside of it -- also collapses any open
  // model submenu, since it renders as a sibling within this same ref'd wrapper.
  useOutsideClick(backendMenuRef, backendMenuOpen, () => {
    setBackendMenuOpen(false);
    setOpenModelMenuFor(null);
  });

  function toggleModelMenu(backend: string) {
    setModelFilter("");
    setOpenModelMenuFor((prev) => {
      const next = prev === backend ? null : backend;
      if (next) {
        const rowEl = rowRefs.current[next];
        const containerEl = backendMenuRef.current;
        if (rowEl && containerEl) {
          setSubmenuTop(rowEl.getBoundingClientRect().top - containerEl.getBoundingClientRect().top);
        }
      }
      if (next && !modelMenus[next]) {
        setModelMenus((m) => ({ ...m, [next]: { status: "loading", models: [] } }));
        api
          .getBackendModelsFor(next)
          .then((res) => {
            setModelMenus((m) => ({
              ...m,
              [next]: { status: "ok", models: res.models, source: res.source },
            }));
          })
          .catch(() => {
            setModelMenus((m) => ({ ...m, [next]: { status: "error", models: [] } }));
          });
      }
      return next;
    });
  }

  async function selectModel(backend: string, model: string) {
    const previous = selectedModels[backend];
    setSelectedModels((s) => ({ ...s, [backend]: model }));
    setOpenModelMenuFor(null);
    try {
      await api.putBackendModel(backend, model);
    } catch (err) {
      console.error("putBackendModel failed, reverting:", err);
      setSelectedModels((s) => {
        const next = { ...s };
        if (previous === undefined) delete next[backend];
        else next[backend] = previous;
        return next;
      });
    }
  }

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
            onClick={() =>
              setBackendMenuOpen((open) => {
                if (open) setOpenModelMenuFor(null);
                return !open;
              })
            }
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
                const canSelect = supportsModelSelection[id] === true;
                const isMenuOpen = openModelMenuFor === id;
                return (
                  <div
                    key={id}
                    ref={(el) => {
                      rowRefs.current[id] = el;
                    }}
                    role={canSelect ? "button" : undefined}
                    tabIndex={canSelect ? 0 : undefined}
                    title={canSelect ? t("header.modelMenuTitle") : undefined}
                    onClick={canSelect ? () => toggleModelMenu(id) : undefined}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 9,
                      padding: "7px 9px",
                      borderRadius: T.btnRadius,
                      background: isActive ? T.aSoft : isMenuOpen ? T.sunk : "transparent",
                      cursor: canSelect ? "pointer" : "default",
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
                    {canSelect ? (
                      <>
                        <span
                          style={{
                            font: `500 9.5px ${T.mono}`,
                            color: T.ink3,
                            maxWidth: 78,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {selectedModels[id] ?? "—"}
                        </span>
                        <span
                          style={{
                            color: T.ink3,
                            display: "flex",
                            transform: isMenuOpen ? "rotate(90deg)" : "none",
                            transition: "transform .12s",
                          }}
                        >
                          <Icon name="chevron" size={9} />
                        </span>
                      </>
                    ) : (
                      <span
                        style={{
                          font: `400 9.5px ${T.mono}`,
                          color: T.ink3,
                          fontStyle: "italic",
                        }}
                      >
                        {t("header.noModelSelection")}
                      </span>
                    )}
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
          {backendMenuOpen && openModelMenuFor && (
            <ModelSubmenu
              top={submenuTop}
              menu={modelMenus[openModelMenuFor]}
              selected={selectedModels[openModelMenuFor]}
              filter={modelFilter}
              onFilterChange={setModelFilter}
              onSelect={(model) => {
                void selectModel(openModelMenuFor, model);
              }}
              t={t}
            />
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
