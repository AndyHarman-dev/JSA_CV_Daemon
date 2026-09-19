// One turn's proposed/settled diff card in the CV chat thread (design hand-off's
// "PROPOSED DIFF" card). Shows EVERY changed field as a full-text BEFORE/AFTER row, then
// the status-appropriate action row.
//
// Nothing here may abbreviate: no per-field character cap, no visible-item cap. The card is
// the only thing the user reads before pressing APPLY, so any cut hides part of what that
// button will write into the editor buffer. Both cuts existed and both were removed — a
// 140-char `truncate()` on each side, and a 4-item slice with a "+N more changes" line. The
// panel scrolls (CvChatPanel's `maxHeight`), so length is a scrolling concern, never a
// truncation one. `whiteSpace: "pre-wrap"` is what makes long text wrap instead of clip.
//
// The STALE state is new relative to the design (D4): it
// replaces APPLY/DISCARD with a short explanation and a RE-RUN affordance, since applying a
// diff computed against a buffer that has since changed would silently overwrite newer work.
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import type { ClientChatTurn } from "../../cvChatStore";
import { useCvChatStore } from "../../cvChatStore";
import { useT } from "../../i18n/useT";

const T = EDITOR_THEME;

const STATUS_META: Record<ClientChatTurn["status"], { label: string; color: string }> = {
  pending: { label: "PROPOSED DIFF", color: T.a },
  applied: { label: "APPLIED", color: T.green },
  auto: { label: "AUTO-APPLIED", color: T.green },
  discarded: { label: "DISCARDED", color: T.ink3 },
  stale: { label: "STALE", color: T.danger },
  error: { label: "ERROR", color: T.danger },
  none: { label: "NO CHANGES", color: T.ink3 },
};

export function CvDiffCard({ turn }: { turn: ClientChatTurn }) {
  const t = useT();
  const applyTurn = useCvChatStore((s) => s.applyTurn);
  const discardTurn = useCvChatStore((s) => s.discardTurn);
  const flashKey = useCvChatStore((s) => s.flashKey);
  const meta = STATUS_META[turn.status];
  const items = turn.items ?? [];
  const flashing = flashKey === turn.id;
  const canAct = turn.status === "pending" && !!turn.document;

  return (
    <div
      data-testid="cv-diff-card"
      data-status={turn.status}
      style={{
        border: `1px solid ${meta.color}55`,
        borderLeft: `3px solid ${meta.color}`,
        borderRadius: T.btnRadius,
        background: T.subtle,
        padding: "10px 11px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        animation: flashing ? "aiflash .6s ease-out" : undefined,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
        <span style={{ font: `700 10px ${T.mono}`, letterSpacing: ".08em", color: meta.color }}>{meta.label}</span>
        {items.length > 0 && (
          <span style={{ font: `400 10.5px ${T.mono}`, color: T.ink3 }}>
            {t(items.length === 1 ? "cvChat.changeCountOne" : "cvChat.changeCountMany", { n: items.length })}
          </span>
        )}
      </div>

      {turn.text && <div style={{ font: `400 12.5px/1.5 ${T.ui}`, color: T.ink2 }}>{turn.text}</div>}

      {items.map((item, i) => (
        <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <div style={{ font: `600 10px ${T.mono}`, letterSpacing: ".05em", color: T.ink3 }}>{item.label}</div>
          <div
            style={{
              font: `400 11.5px/1.45 ${T.mono}`,
              color: T.danger,
              textDecoration: "line-through",
              opacity: 0.75,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {item.before || "—"}
          </div>
          <div
            style={{
              font: `400 11.5px/1.45 ${T.mono}`,
              color: T.green,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {item.after || "—"}
          </div>
        </div>
      ))}

      {turn.status === "stale" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ font: `400 12px/1.5 ${T.ui}`, color: T.ink2 }}>
            {t("cvChat.staleExplain")}
          </div>
          <button
            type="button"
            className="cvbtn"
            onClick={() => useCvChatStore.getState().send()}
            style={{
              alignSelf: "flex-start",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: `1px solid ${T.bd2}`,
              background: T.surface,
              color: T.ink,
              cursor: "pointer",
              font: `600 11px ${T.disp}`,
              padding: "5px 10px",
              borderRadius: T.btnRadius,
            }}
          >
            <Icon name="refresh" size={12} />
            RE-RUN
          </button>
        </div>
      )}

      {turn.status === "error" && (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div style={{ font: `400 12px/1.5 ${T.ui}`, color: T.ink2 }}>
            {t("cvChat.errorExplain")}
          </div>
          <button
            type="button"
            className="cvghost"
            onClick={() => discardTurn(turn.id)}
            style={{
              alignSelf: "flex-start",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: `1px solid ${T.bd2}`,
              background: "transparent",
              color: T.ink2,
              cursor: "pointer",
              font: `600 11px ${T.disp}`,
              padding: "5px 10px",
              borderRadius: T.btnRadius,
            }}
          >
            <Icon name="x" size={12} />
            DISCARD
          </button>
        </div>
      )}

      {canAct && (
        <div style={{ display: "flex", gap: 6, marginTop: 2 }}>
          <button
            type="button"
            className="cvprimary"
            onClick={() => applyTurn(turn.id)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: "none",
              background: T.a,
              color: "#1A0A0D",
              cursor: "pointer",
              font: `700 11px ${T.disp}`,
              padding: "6px 12px",
              borderRadius: T.btnRadius,
            }}
          >
            <Icon name="check" size={12} />
            APPLY
          </button>
          <button
            type="button"
            className="cvghost"
            onClick={() => discardTurn(turn.id)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: `1px solid ${T.bd2}`,
              background: "transparent",
              color: T.ink2,
              cursor: "pointer",
              font: `600 11px ${T.disp}`,
              padding: "6px 12px",
              borderRadius: T.btnRadius,
            }}
          >
            <Icon name="x" size={12} />
            DISCARD
          </button>
        </div>
      )}
    </div>
  );
}
