// The CV Editor AI chat panel -- an anchored, position:fixed overlay measured off the
// scoped unit's rect (design hand-off's "chatForm: anchored"). See the plan's Phase 5 "5c"
// for the full geometry spec, including the flip-to-left rule the design reference itself
// does not implement.
import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import { useCvChatStore, type ChatScope } from "../../cvChatStore";
import { useEditorStore } from "../../editorStore";
import { useT } from "../../i18n/useT";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import { ReasoningCard } from "../ReasoningCard";
import { CvDiffCard } from "./CvDiffCard";
import { getAnchor } from "../../lib/chatAnchors";

const T = EDITOR_THEME;
const PANEL_W = 380;
const MARGIN = 16;
const GAP = 20;
const CONNECTOR_CLEARANCE = 14;

const QUICK_ACTION_KEYS: Record<string, string> = {
  compact: "cvChat.quickCompact",
  quantify: "cvChat.quickQuantify",
  reorder: "cvChat.quickReorder",
  expand: "cvChat.quickExpand",
  one_page: "cvChat.quickOnePage",
  grammar: "cvChat.quickGrammar",
  tone: "cvChat.quickTone",
  fix_formatting: "cvChat.quickFixFormat",
};

const QUICK_ACTIONS_BY_SCOPE: Record<ChatScope["type"], string[]> = {
  contact: ["fix_formatting", "grammar"],
  entry: ["compact", "quantify", "expand", "grammar"],
  section: ["compact", "reorder", "quantify", "expand", "grammar"],
  cv: ["one_page", "compact", "tone", "grammar"],
};

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), Math.max(lo, hi));
}

interface AnchorRect {
  top: number;
  left: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
}

function readAnchorRect(scope: ChatScope | null): AnchorRect | null {
  const el = getAnchor(scope);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, right: r.right, bottom: r.bottom, width: r.width, height: r.height };
}

interface Geometry {
  left: number;
  top: number;
  maxHeight: number;
  showConnector: boolean;
  connector?: { x1: number; y1: number; x2: number; y2: number };
}

// Anchored placement, viewport-clamped, with a flip-to-left fallback: at common desktop
// widths (~1440px) the naive right-of-anchor position clamps back ONTO the anchor for a
// centered ~760px column, which would cover the very field being edited. See the plan's
// Phase 5 "5c" for the derivation.
function computeGeometry(rect: AnchorRect | null, scope: ChatScope | null, vw: number, vh: number): Geometry {
  const maxHeight = Math.min(vh * 0.7, 560);
  if (!rect) {
    return { left: vw - PANEL_W - 22, top: 80, maxHeight, showConnector: false };
  }

  const rightPlacement = clamp(rect.right + GAP, MARGIN, vw - PANEL_W - MARGIN);
  const rightOverlapsAnchor = rightPlacement < rect.right;

  let left: number;
  let flipped = false;
  if (!rightOverlapsAnchor) {
    left = rightPlacement;
  } else {
    const leftPlacement = clamp(rect.left - PANEL_W - GAP, MARGIN, vw - PANEL_W - MARGIN);
    const leftFits = leftPlacement + PANEL_W <= rect.left;
    if (leftFits) {
      left = leftPlacement;
      flipped = true;
    } else {
      left = rightPlacement; // neither side fits cleanly -- fall back, connector suppressed below
    }
  }

  const top = clamp(rect.top - 4, 62, Math.max(vh - maxHeight, 62));

  const offScreen = rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw;
  const occluded = flipped
    ? left + PANEL_W > rect.left - CONNECTOR_CLEARANCE
    : rect.right > left - CONNECTOR_CLEARANCE;
  const showConnector = scope?.type !== "cv" && !offScreen && !occluded;

  return {
    left,
    top,
    maxHeight,
    showConnector,
    connector: showConnector
      ? flipped
        ? { x1: rect.left, y1: rect.top + rect.height / 2, x2: left + PANEL_W, y2: top + 14 }
        : { x1: rect.right, y1: rect.top + rect.height / 2, x2: left, y2: top + 14 }
      : undefined,
  };
}

function scopeLabel(scope: ChatScope, cv: ReturnType<typeof useEditorStore.getState>["cv"]): string {
  if (!cv) return "";
  if (scope.type === "cv") return "ENTIRE CV";
  if (scope.type === "contact") return "IDENTITY";
  if (scope.type === "section") {
    const s = cv.sections.find((s) => s.id === scope.sectionId);
    return s?.name ? s.name.toUpperCase() : "SECTION";
  }
  const s = cv.sections.find((s) => s.id === scope.sectionId);
  const e = s?.entries.find((e) => e.id === scope.entryId);
  return e?.heading ? e.heading.toUpperCase() : "ENTRY";
}

function CollapsedPill() {
  const busy = useCvChatStore((s) => s.busy);
  const elapsed = useCvChatStore((s) => s.elapsed);
  const open = useCvChatStore((s) => s.open);
  const collapsed = useCvChatStore((s) => s.collapsed);
  const st = useCvChatStore();

  return (
    <button
      type="button"
      data-testid="cv-chat-pill"
      onClick={() => (open ? st.setCollapsed(false) : st.widenToCv())}
      style={{
        position: "fixed",
        right: 22,
        bottom: 22,
        zIndex: 45,
        display: "inline-flex",
        alignItems: "center",
        gap: 8,
        padding: "10px 16px",
        border: `1px solid ${T.aBorder}`,
        borderRadius: 999,
        background: T.surface,
        color: T.ink,
        boxShadow: T.shadowMd,
        cursor: "pointer",
        font: `600 12px ${T.disp}`,
        letterSpacing: ".04em",
      }}
    >
      {busy ? (
        <span style={{ display: "flex", color: T.accent2, animation: "aiwork 1.2s ease-in-out infinite" }}>
          <Icon name="chat" size={14} />
        </span>
      ) : (
        <span style={{ display: "flex", color: T.a }}>
          <Icon name="chat" size={14} />
        </span>
      )}
      {busy ? `WORKING · ${elapsed}s` : collapsed ? "RESUME CHAT" : "ASK DAEMON · ENTIRE CV"}
    </button>
  );
}

export function CvChatPanel({ mainRef }: { mainRef: RefObject<HTMLElement> }) {
  const t = useT();
  const open = useCvChatStore((s) => s.open);
  const collapsed = useCvChatStore((s) => s.collapsed);
  const scope = useCvChatStore((s) => s.scope);
  const turns = useCvChatStore((s) => s.turns);
  const input = useCvChatStore((s) => s.input);
  const attach = useCvChatStore((s) => s.attach);
  const busy = useCvChatStore((s) => s.busy);
  const reasoning = useCvChatStore((s) => s.reasoning);
  const auto = useCvChatStore((s) => s.auto);
  const error = useCvChatStore((s) => s.error);
  const st = useCvChatStore();
  const cv = useEditorStore((s) => s.cv);

  const [rect, setRect] = useState<AnchorRect | null>(null);
  const rafRef = useRef<number | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const remeasure = () => {
    if (rafRef.current != null) return;
    rafRef.current = requestAnimationFrame(() => {
      rafRef.current = null;
      const next = readAnchorRect(scope);
      setRect((prev) => {
        if (!next) return prev === null ? prev : null;
        if (prev && prev.top === next.top && prev.left === next.left && prev.right === next.right && prev.bottom === next.bottom) {
          return prev;
        }
        return next;
      });
    });
  };

  useLayoutEffect(() => {
    remeasure();
    const mainEl = mainRef.current;
    mainEl?.addEventListener("scroll", remeasure, { passive: true });
    window.addEventListener("resize", remeasure);
    return () => {
      mainEl?.removeEventListener("scroll", remeasure);
      window.removeEventListener("resize", remeasure);
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope?.type, scope?.sectionId, scope?.entryId, open]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [turns.length, reasoning]);

  if (!cv) return null;
  if (!open || collapsed) return <CollapsedPill />;
  if (!scope) return null;

  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const geo = computeGeometry(rect, scope, vw, vh);
  const label = scopeLabel(scope, cv);
  const quickActions = QUICK_ACTIONS_BY_SCOPE[scope.type];

  function send() {
    if (!input.trim() && attach.length === 0) return;
    void st.send();
  }

  function onComposerKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  return (
    <>
      {geo.showConnector && geo.connector && (
        <svg
          style={{ position: "fixed", inset: 0, pointerEvents: "none", zIndex: 44 }}
          width={vw}
          height={vh}
        >
          <path
            d={`M ${geo.connector.x1} ${geo.connector.y1} C ${(geo.connector.x1 + geo.connector.x2) / 2} ${geo.connector.y1}, ${(geo.connector.x1 + geo.connector.x2) / 2} ${geo.connector.y2}, ${geo.connector.x2} ${geo.connector.y2}`}
            fill="none"
            stroke={T.a}
            strokeWidth={1.5}
            strokeDasharray="4 4"
            opacity={0.6}
          />
        </svg>
      )}

      <div
        data-testid="cv-chat-panel"
        style={{
          position: "fixed",
          left: geo.left,
          top: geo.top,
          width: PANEL_W,
          maxHeight: geo.maxHeight,
          zIndex: 45,
          display: "flex",
          flexDirection: "column",
          background: T.surface,
          border: `1px solid ${T.bd2}`,
          borderRadius: T.btnRadius,
          boxShadow: T.shadowMd,
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 12px",
            borderBottom: `1px solid ${T.bd}`,
            flex: "none",
          }}
        >
          <span style={{ display: "flex", color: T.a }}>
            <Icon name="chat" size={14} />
          </span>
          <span style={{ font: `700 11px ${T.mono}`, letterSpacing: ".08em", color: T.ink }}>DAEMON // EDIT</span>
          <span style={{ flex: 1 }} />
          <button
            type="button"
            onClick={st.toggleAuto}
            title={t("cvChat.autoToggleTitle")}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              border: `1px solid ${auto ? T.aBorder : T.bd2}`,
              background: auto ? T.aSoft : "transparent",
              color: auto ? T.a : T.ink3,
              borderRadius: 999,
              padding: "3px 8px",
              font: `700 9.5px ${T.mono}`,
              letterSpacing: ".06em",
              cursor: "pointer",
            }}
          >
            AUTO
          </button>
          <button
            type="button"
            title={t("cvChat.newThreadTitle")}
            onClick={() => void st.newThread()}
            className="cvbtn"
            style={{ border: "none", background: "transparent", color: T.ink2, cursor: "pointer", width: 24, height: 24, borderRadius: T.btnRadius, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
          >
            <Icon name="refresh" size={13} />
          </button>
          <button
            type="button"
            title={t("cvChat.collapseTitle")}
            onClick={() => st.setCollapsed(true)}
            className="cvbtn"
            style={{ border: "none", background: "transparent", color: T.ink2, cursor: "pointer", width: 24, height: 24, borderRadius: T.btnRadius, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
          >
            <Icon name="down" size={13} />
          </button>
          <button
            type="button"
            title={t("cvChat.closeTitle")}
            onClick={st.closeChat}
            className="cvbtn"
            style={{ border: "none", background: "transparent", color: T.ink2, cursor: "pointer", width: 24, height: 24, borderRadius: T.btnRadius, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
          >
            <Icon name="x" size={13} />
          </button>
        </div>

        {/* Scope bar */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 12px", borderBottom: `1px solid ${T.bd}`, flex: "none" }}>
          <span
            style={{
              font: `700 10px ${T.mono}`,
              letterSpacing: ".05em",
              color: T.a,
              background: T.aSoft,
              border: `1px solid ${T.aBorder}`,
              borderRadius: T.btnRadius,
              padding: "2px 7px",
            }}
          >
            {label}
          </span>
          {scope.type !== "cv" && (
            <button
              type="button"
              onClick={st.widenToCv}
              className="cvbtn"
              style={{ border: "none", background: "transparent", color: T.ink3, cursor: "pointer", font: `600 10px ${T.mono}`, letterSpacing: ".04em", padding: "2px 4px" }}
            >
              WIDEN → CV
            </button>
          )}
        </div>

        {/* Thread */}
        <div ref={threadRef} style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "10px 12px", display: "flex", flexDirection: "column", gap: 8 }}>
          {turns.length === 0 && !busy && (
            <div style={{ font: `400 12px/1.5 ${T.ui}`, color: T.ink3, padding: "8px 2px" }}>
              {t("cvChat.emptyThread")}
            </div>
          )}
          {turns.map((turn) => {
            if (turn.role === "scope") {
              return (
                <div key={turn.id} style={{ font: `600 10px ${T.mono}`, letterSpacing: ".05em", color: T.ink3, textAlign: "center" }}>
                  SCOPE → {scopeLabel(scope, cv)}
                </div>
              );
            }
            if (turn.role === "user") {
              return (
                <div key={turn.id} style={{ alignSelf: "flex-end", maxWidth: "88%", background: T.aSoft, border: `1px solid ${T.aBorder}`, borderRadius: T.btnRadius, padding: "7px 10px", font: `400 12.5px/1.5 ${T.ui}`, color: T.ink }}>
                  {turn.text}
                </div>
              );
            }
            return (
              <div key={turn.id} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {turn.reasoning && <ReasoningCard reasoning={turn.reasoning} running={false} theme={T} />}
                <CvDiffCard turn={turn} />
              </div>
            );
          })}
          {busy && <ReasoningCard reasoning={reasoning} running theme={T} />}
          {error && (
            <div role="alert" style={{ font: `400 12px/1.5 ${T.ui}`, color: T.danger, padding: "6px 2px" }}>
              {error}
            </div>
          )}
        </div>

        {/* Composer */}
        <div style={{ borderTop: `1px solid ${T.bd}`, padding: "8px 10px 10px", display: "flex", flexDirection: "column", gap: 6, flex: "none" }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
            {quickActions.map((key) => (
              <button
                key={key}
                type="button"
                disabled={busy}
                onClick={() => void st.send(key)}
                className="cvbtn"
                style={{
                  border: `1px solid ${T.bd2}`,
                  background: T.subtle,
                  color: T.ink2,
                  borderRadius: 999,
                  padding: "3px 9px",
                  font: `600 10px ${T.disp}`,
                  letterSpacing: ".03em",
                  cursor: busy ? "default" : "pointer",
                  opacity: busy ? 0.5 : 1,
                }}
              >
                {t(QUICK_ACTION_KEYS[key])}
              </button>
            ))}
          </div>

          {attach.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
              {attach.map((a) => (
                <span
                  key={a.id}
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 5,
                    font: `400 10.5px ${T.mono}`,
                    color: T.ink2,
                    background: T.subtle,
                    border: `1px solid ${T.bd}`,
                    borderRadius: T.btnRadius,
                    padding: "2px 6px",
                  }}
                >
                  <Icon name="file" size={11} />
                  {a.file.name}
                  <button
                    type="button"
                    onClick={() => st.removeFile(a.id)}
                    style={{ border: "none", background: "transparent", color: T.ink3, cursor: "pointer", display: "flex", padding: 0 }}
                  >
                    <Icon name="x" size={10} />
                  </button>
                </span>
              ))}
              <div style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, width: "100%" }}>
                {t("cvChat.readOnlyContextNote")}
              </div>
            </div>
          )}

          <div style={{ display: "flex", alignItems: "flex-end", gap: 6 }}>
            <button
              type="button"
              title={t("cvChat.attachTitle")}
              onClick={() => fileInputRef.current?.click()}
              className="cvbtn"
              style={{ border: `1px solid ${T.bd2}`, background: "transparent", color: T.ink2, cursor: "pointer", width: 30, height: 30, borderRadius: T.btnRadius, display: "inline-flex", alignItems: "center", justifyContent: "center", flex: "none" }}
            >
              <Icon name="clip" size={14} />
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md,.json,.rtf,.csv"
              className="hidden"
              onChange={(e) => {
                if (e.target.files) st.addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <textarea
              value={input}
              onChange={(e) => st.setInput(e.target.value)}
              onKeyDown={onComposerKeyDown}
              placeholder={t("cvChat.composerPlaceholder")}
              rows={2}
              style={{
                flex: 1,
                resize: "none",
                border: `1px solid ${T.bd}`,
                borderRadius: T.btnRadius,
                background: T.sunk,
                color: T.ink,
                font: `400 12.5px/1.4 ${T.ui}`,
                padding: "7px 9px",
                outline: "none",
              }}
            />
            <button
              type="button"
              disabled={busy || (!input.trim() && attach.length === 0)}
              onClick={send}
              className="cvprimary"
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 34,
                height: 34,
                border: "none",
                borderRadius: T.btnRadius,
                background: T.a,
                color: "#1A0A0D",
                cursor: busy ? "default" : "pointer",
                opacity: busy || (!input.trim() && attach.length === 0) ? 0.5 : 1,
                flex: "none",
              }}
            >
              <Icon name="send" size={14} />
            </button>
          </div>

          <div style={{ font: `400 10px ${T.mono}`, color: T.ink3 }}>
            {auto ? t("cvChat.footerAuto") : t("cvChat.footerProposed")}
          </div>
        </div>
      </div>
    </>
  );
}
