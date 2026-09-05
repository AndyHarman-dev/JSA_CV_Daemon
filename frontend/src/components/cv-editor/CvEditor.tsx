// Full-screen CV Structure Editor shell. Toggled by `editorOpen` in the main store; reached
// from a Header entry. On mount it GETs the saved base CV (404 → empty state). Persists only
// on Done (PUT). View switcher / undo-redo / JSON toggle / infer all live in the top bar.
//
// Visual language: cyberpunk "daemon" HUD per the design handoff (CV Structure Editor.dc.html)
// — dark chamfered panels, red accent (EDITOR_THEME), cyan "system/live" signals. The
// Document/Split paper preview is intentionally NOT reskinned (see PaperSheet.tsx).
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useEditorStore, type EditorView } from "../../editorStore";
import { useStore } from "../../store";
import { useT } from "../../i18n/useT";
import { panelBase, cornerMarks } from "../../theme/chrome";
import { Icon, type IconName } from "../../theme/Icon";
import { EDITOR_THEME, SHELL_THEME, paperT } from "../../theme/tokens";
import { BlocksView } from "./BlocksView";
import { DocumentView } from "./PaperSheet";
import { JsonDrawer } from "./JsonDrawer";
import { LanguagePill } from "./LanguagePill";
import { SplitView } from "./SplitView";
import { DeckRail } from "./DeckRail";

const T = EDITOR_THEME;

// `accent` overrides the editor's red only for the OPEN JSON button, which the design
// carries in the shell's amber — see SHELL_THEME in theme/tokens.ts.
function tbtnStyle(primary: boolean, disabled?: boolean, accent: string = T.a): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: 7,
    padding: primary ? "8px 16px" : "7px 13px",
    border: primary ? "none" : `1px solid ${T.bd2}`,
    borderRadius: T.btnRadius,
    cursor: disabled ? "default" : "pointer",
    font: `600 12px ${T.disp}`,
    letterSpacing: ".04em",
    color: primary ? "#06080B" : T.ink,
    background: primary ? accent : T.surface,
    opacity: disabled ? 0.5 : 1,
    boxShadow: primary ? `0 1px 14px ${accent}55` : "none",
  };
}

// One hidden-input dance for both file entry points (inference and JSON import). The
// `e.target.value = ""` reset is load-bearing: without it, picking the SAME file twice in a
// row fires no change event and the second click looks dead.
function FileButton({
  label,
  primary,
  disabled,
  accept = ".pdf,.docx,.txt,.md",
  icon = "spark",
  accent,
  testId,
  onFile,
}: {
  label: string;
  primary?: boolean;
  disabled?: boolean;
  accept?: string;
  icon?: IconName;
  accent?: string;
  testId?: string;
  onFile?: (f: File) => void;
}) {
  const inferFromFile = useEditorStore((s) => s.inferFromFile);
  const ref = useRef<HTMLInputElement>(null);
  const handle = onFile ?? ((f: File) => void inferFromFile(f));
  return (
    <>
      <button
        type="button"
        onClick={() => ref.current?.click()}
        disabled={disabled}
        data-testid={testId}
        className={primary ? "cvprimary" : "cvghost"}
        style={tbtnStyle(!!primary, disabled, accent)}
      >
        <Icon name={icon} size={14} />
        {label}
      </button>
      <input
        ref={ref}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) handle(f);
          e.target.value = "";
        }}
      />
    </>
  );
}

function EmptyState() {
  const startBlank = useEditorStore((s) => s.startBlank);
  const importFromJsonFile = useEditorStore((s) => s.importFromJsonFile);
  const importError = useEditorStore((s) => s.importError);
  const t = useT();
  return (
    <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, position: "relative", zIndex: 1 }}>
      <div style={{ position: "relative", textAlign: "center", maxWidth: 460, animation: "cvfade .18s ease" }}>
        <div
          style={{
            width: 60,
            height: 60,
            ...panelBase(T, { chamfer: 14 }),
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            margin: "0 auto 20px",
            color: T.ink3,
          }}
        >
          <Icon name="doc" size={26} />
        </div>
        <h2 style={{ font: `600 22px ${T.disp}`, color: T.ink, margin: "0 0 8px", letterSpacing: ".02em" }}>
          {t("cvEditor.emptyTitle")}
        </h2>
        <p style={{ font: `400 14.5px/1.6 ${T.ui}`, color: T.ink2, margin: "0 0 24px" }}>
          {t("cvEditor.emptyBody")}
        </p>
        <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
          <FileButton label={t("cvEditor.runInference")} primary />
          <FileButton
            label={t("cvEditor.openJson")}
            primary
            accept=".json,application/json"
            icon="braces"
            accent={SHELL_THEME.a}
            testId="open-json"
            onFile={(f) => void importFromJsonFile(f)}
          />
          <button
            type="button"
            onClick={startBlank}
            className="cvghost"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
              padding: "11px 18px",
              border: `1px solid ${T.bd2}`,
              borderRadius: T.btnRadius,
              background: T.surface,
              color: T.ink,
              font: `600 13px ${T.disp}`,
              letterSpacing: ".04em",
              cursor: "pointer",
            }}
          >
            <Icon name="plus" size={16} />
            {t("cvEditor.initBlank")}
          </button>
        </div>
        {importError && (
          <div
            data-testid="import-error"
            role="alert"
            style={{
              marginTop: 16,
              padding: "8px 12px",
              border: `1px solid ${T.danger}55`,
              background: `${T.danger}1A`,
              color: T.danger,
              font: `400 13px/1.5 ${T.ui}`,
              borderRadius: T.btnRadius,
            }}
          >
            {importError}
          </div>
        )}
      </div>
    </div>
  );
}

// Fallback labels shown for steps the engine hasn't reached yet (mirrors
// jsa/pipeline/infer_structure.py::INFER_STEPS so a step's fallback text matches what the
// backend will eventually report for it).
const STEPS = [
  "Reading document",
  "Detecting section breaks",
  "Extracting entries & dates",
  "Structuring JSON",
  "Validating against schema",
];

function InferringState() {
  const { inferStep, inferTotal, inferLabel, inferError, inferFilename } = useEditorStore();
  const reset = useEditorStore((s) => s.reset);
  const [elapsedMs, setElapsedMs] = useState(0);
  const startRef = useRef(Date.now());

  useEffect(() => {
    startRef.current = Date.now();
    setElapsedMs(0);
    const id = setInterval(() => setElapsedMs(Date.now() - startRef.current), 100);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const elapsedS = (elapsedMs / 1000).toFixed(1);

  return (
    <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", padding: 24, position: "relative", zIndex: 1 }}>
      <div
        style={{ position: "relative", width: 460, ...panelBase(T, { chamfer: 16 }), padding: "26px 28px 24px", boxShadow: T.shadowMd, animation: "cvfade .18s ease" }}
      >
        {cornerMarks(T, T.a, 11)}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 18 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div
              style={{
                width: 38,
                height: 38,
                ...panelBase(T, { bg: T.aSoft, chamfer: 10, border: T.aBorder }),
                color: T.a,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              <Icon name="spark" size={18} />
            </div>
            <div>
              <div style={{ font: `600 15px ${T.disp}`, color: T.ink, letterSpacing: ".02em" }}>
                {inferError ? "INFERENCE FAILED" : "INFERENCE IN PROGRESS"}
              </div>
              <div className="truncate" style={{ font: `400 11.5px ${T.mono}`, color: T.ink3, maxWidth: 280 }}>
                SRC: {inferFilename}
              </div>
            </div>
          </div>
          <div style={{ font: `400 12px ${T.mono}`, color: T.accent2, flex: "none" }}>{elapsedS}s</div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {STEPS.map((fallback, i) => {
            const n = i + 1;
            const done = n < inferStep || (n === inferTotal && inferStep === inferTotal && !inferError);
            const active = n === inferStep && !inferError;
            const errored = !!inferError && n === inferStep;
            const label = (inferLabel && (active || errored) ? inferLabel : fallback).toUpperCase();
            const pct = done ? 100 : 0;
            return (
              <div key={fallback} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span
                      style={{
                        width: 14,
                        height: 14,
                        flex: "none",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        borderRadius: T.chamfer ? 0 : 7,
                        background: errored ? T.danger : done ? T.a : "transparent",
                        border: errored || done ? "none" : `1.5px solid ${active ? T.a : T.bd2}`,
                      }}
                    >
                      {errored ? (
                        <Icon name="x" size={9} color="#06080B" />
                      ) : done ? (
                        <Icon name="check" size={9} color="#06080B" />
                      ) : active ? (
                        <div
                          style={{ width: 6, height: 6, borderRadius: 6, border: `1.5px solid ${T.a}`, borderTopColor: "transparent", animation: "cvspin .7s linear infinite" }}
                        />
                      ) : null}
                    </span>
                    <span style={{ font: `500 11.5px ${T.mono}`, letterSpacing: ".06em", color: done || active || errored ? T.ink : T.ink3 }}>
                      THREAD_0{n} · {label}
                    </span>
                  </div>
                  <span style={{ font: `400 11px ${T.mono}`, color: errored ? T.danger : done ? T.accent2 : T.ink3 }}>
                    {errored ? "ERR" : `${pct}%`}
                  </span>
                </div>
                <div style={{ height: 3, background: T.sunk, borderRadius: 3, overflow: "hidden" }}>
                  <div
                    style={{
                      height: "100%",
                      width: `${errored ? 100 : active ? 100 : pct}%`,
                      background: errored ? T.danger : done ? T.accent2 : T.a,
                      transition: "width .2s ease",
                      animation: active ? "cvpulse 1s ease-in-out infinite" : undefined,
                    }}
                  />
                </div>
              </div>
            );
          })}
        </div>

        {inferError && (
          <div style={{ marginTop: 16, font: `400 12px ${T.ui}`, color: T.danger }}>
            {inferError}
            <button
              type="button"
              onClick={reset}
              style={{ marginLeft: 8, background: "none", border: "none", color: T.danger, textDecoration: "underline", cursor: "pointer", font: `inherit` }}
            >
              Try again
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

const VIEWS: { id: EditorView; label: string; icon: IconName; num: string }[] = [
  { id: "blocks", label: "BLOCKS", icon: "blocks", num: "01" },
  { id: "document", label: "DOCUMENT", icon: "doc", num: "02" },
  { id: "split", label: "SPLIT", icon: "cols", num: "03" },
];

export function CvEditor() {
  const cv = useEditorStore((s) => s.cv);
  const view = useEditorStore((s) => s.view);
  const jsonOpen = useEditorStore((s) => s.jsonOpen);
  const inferring = useEditorStore((s) => s.inferring);
  const canUndo = useEditorStore((s) => s.canUndo);
  const canRedo = useEditorStore((s) => s.canRedo);
  const saving = useEditorStore((s) => s.saving);
  const saveError = useEditorStore((s) => s.saveError);
  const st = useEditorStore();
  const setEditorOpen = useStore((s) => s.setEditorOpen);
  const t = useT();
  const [loading, setLoading] = useState(true);
  const [clock, setClock] = useState(() => new Date());
  const sessionRef = useRef(
    Array.from({ length: 6 }, () => "0123456789ABCDEF"[Math.floor(Math.random() * 16)]).join("")
  );

  // Live HUD clock.
  useEffect(() => {
    const id = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  // Load the deck index + the active deck's CV once when the editor opens. hydrateDecks
  // replaces the pre-decks single getCvStructure() fetch and handles its own failures
  // (falling back to the empty state), so there is nothing to catch here.
  useEffect(() => {
    let alive = true;
    st.hydrateDecks().finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keyboard undo/redo.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        st.undo();
      } else if ((e.key.toLowerCase() === "z" && e.shiftKey) || e.key.toLowerCase() === "y") {
        e.preventDefault();
        st.redo();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onDone() {
    if (!cv) {
      setEditorOpen(false);
      return;
    }
    const ok = await st.save();
    if (ok) setEditorOpen(false);
  }

  const sectionCount = cv?.sections.length ?? 0;

  return (
    <div style={{ position: "fixed", inset: 0, zIndex: 40, height: "100vh", display: "flex", flexDirection: "column", minHeight: 0, background: T.canvas, color: T.ink, fontFamily: T.ui, overflow: "hidden" }}>
      <style>{`:root{--a:${T.a};--pa:${paperT.a}}`}</style>

      {/* Top bar */}
      <header
        style={{
          position: "relative",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          padding: "10px 18px",
          background: "rgba(10,14,19,.85)",
          backdropFilter: "blur(10px)",
          borderBottom: `1px solid ${T.bd}`,
          boxShadow: "0 1px 14px rgba(0,0,0,.4)",
          flex: "none",
          zIndex: 20,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 14, minWidth: 0 }}>
          <button
            type="button"
            onClick={() => void onDone()}
            disabled={saving}
            title="Switch to Jobs board"
            className="cvghost"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              flex: "none",
              border: "none",
              background: "transparent",
              padding: "4px 8px 4px 4px",
              margin: 0,
              borderRadius: T.btnRadius,
              cursor: saving ? "default" : "pointer",
              textAlign: "left",
              opacity: saving ? 0.6 : 1,
            }}
          >
            <div
              style={{
                position: "relative",
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
              <Icon name="blocks" size={16} color="#06080B" />
            </div>
            <div style={{ lineHeight: 1.2 }}>
              <div style={{ font: `700 14.5px ${T.disp}`, color: T.ink, letterSpacing: ".02em" }}>
                CV<span style={{ color: T.a }}> // </span>DAEMON
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 5, font: `500 10px ${T.mono}`, color: T.ink3, letterSpacing: ".08em" }}>
                {t("cvEditor.sub")} <span style={{ color: T.accent2 }}>⇄ JOBS</span>
              </div>
            </div>
          </button>

          {cv && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "4px 4px 4px 12px", borderLeft: `1px solid ${T.bd}`, minWidth: 0 }}>
              <span style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, letterSpacing: ".1em", flex: "none" }}>{t("cvEditor.operator")}</span>
              <span
                className="truncate"
                style={{ font: `500 13px ${T.ui}`, color: T.ink2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 180 }}
              >
                {cv.contact.name || t("cvEditor.untitled")}
              </span>
              <span style={{ font: `500 10.5px ${T.mono}`, color: T.accent2, background: T.sunk, padding: "2px 7px", borderRadius: T.btnRadius, flex: "none", border: `1px solid ${T.bd}` }}>
                {t(sectionCount === 1 ? "cvEditor.moduleCountOne" : "cvEditor.moduleCountMany", { n: sectionCount })}
              </span>
            </div>
          )}
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, flex: "none" }}>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", marginRight: 4, flex: "none" }}>
            <span style={{ font: `500 11px ${T.mono}`, color: T.ink2 }}>
              {clock.toLocaleTimeString("en-US", { hour12: false })}
            </span>
            <span style={{ font: `400 9px ${T.mono}`, color: T.ink3, letterSpacing: ".06em" }}>0x{sessionRef.current}</span>
          </div>

          {cv && (
            <div style={{ display: "flex", gap: 2, padding: 3, background: T.sunk, border: `1px solid ${T.bd}`, borderRadius: T.chamfer ? 8 : 4 }}>
              {VIEWS.map((v) => {
                const on = view === v.id;
                return (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => st.setView(v.id)}
                    className={on ? undefined : "cvbtn"}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      padding: "6px 11px",
                      border: "none",
                      borderRadius: T.btnRadius,
                      cursor: "pointer",
                      font: `600 11.5px ${T.disp}`,
                      letterSpacing: ".05em",
                      color: on ? "#06080B" : T.ink2,
                      background: on ? T.a : "transparent",
                    }}
                  >
                    <span style={{ font: `500 9px ${T.mono}`, opacity: 0.65 }}>{v.num}</span>
                    <Icon name={v.icon} size={13} />
                    <span className="hidden lg:inline">{v.label}</span>
                  </button>
                );
              })}
            </div>
          )}

          {cv && (
            <div style={{ display: "flex", gap: 1, padding: 3, background: T.sunk, borderRadius: T.chamfer ? 8 : 4, border: `1px solid ${T.bd}` }}>
              <button
                type="button"
                title="Undo (⌘Z)"
                disabled={!canUndo}
                onClick={st.undo}
                className="cvbtn"
                style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
              >
                <Icon name="undo" size={15} />
              </button>
              <button
                type="button"
                title="Redo (⌘⇧Z)"
                disabled={!canRedo}
                onClick={st.redo}
                className="cvbtn"
                style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
              >
                <Icon name="redo" size={15} />
              </button>
            </div>
          )}

          {cv && (
            <button type="button" onClick={st.toggleJson} className="cvghost" style={tbtnStyle(false)}>
              <Icon name="braces" size={14} />
              <span className="hidden lg:inline">{jsonOpen ? t("cvEditor.hideSrc") : t("cvEditor.srcJson")}</span>
            </button>
          )}

          <LanguagePill />

          <FileButton label={cv ? t("cvEditor.rerun") : t("cvEditor.runInference")} primary={!cv} disabled={inferring} />

          {cv && (
            <button type="button" onClick={() => void onDone()} disabled={saving} className="cvprimary" style={tbtnStyle(true, saving)}>
              <Icon name="check" size={14} />
              {saving ? t("cvEditor.saving") : t("cvEditor.commit")}
            </button>
          )}
        </div>
      </header>

      {saveError && (
        <div style={{ padding: "8px 18px", background: `${T.danger}1A`, color: T.danger, fontSize: 13, borderBottom: `1px solid ${T.bd}`, flex: "none" }}>
          {t("cvEditor.saveError", { reason: saveError })}
        </div>
      )}

      {/* Body */}
      <div style={{ flex: 1, display: "flex", minHeight: 0, overflow: "hidden", position: "relative", zIndex: 1 }}>
        <DeckRail />
        <main style={{ flex: 1, minWidth: 0, overflow: "auto", position: "relative" }}>
          {loading ? (
            <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: T.ink3, fontSize: 13 }}>
              Loading…
            </div>
          ) : inferring ? (
            <InferringState />
          ) : !cv ? (
            <EmptyState />
          ) : view === "blocks" ? (
            <BlocksView />
          ) : view === "document" ? (
            <DocumentView />
          ) : (
            <SplitView />
          )}
        </main>
        {cv && jsonOpen && <JsonDrawer />}
      </div>
    </div>
  );
}
