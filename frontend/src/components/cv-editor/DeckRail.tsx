// Deck rail — a slim 26px collapsed strip (left edge of the CV editor body) that reveals a
// 196px flyout of base-CV decks on hover. Absolutely positioned so opening it never shifts
// the rest of the layout. Backed entirely by useEditorStore's deck slice/actions — see
// editorStore.ts's "--- decks (the editor rail) ---" section for the state shape and
// CLAUDE.md-adjacent notes on why `defaultDeckId` (not the DTO's `is_default`) is the
// rail's source of truth for which deck is default.
import { useRef, useState, type CSSProperties } from "react";
import { useEditorStore } from "../../editorStore";
import { useT } from "../../i18n/useT";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import type { CvDeckDTO } from "../../types";

const T = EDITOR_THEME;

interface DeckRowProps {
  deck: CvDeckDTO;
  isActive: boolean;
  isDefault: boolean;
  isRenaming: boolean;
  renameDraft: string;
  hovered: boolean;
  canDelete: boolean;
  label: string;
  onHoverEnter: () => void;
  onHoverLeave: () => void;
  onSwitch: () => void;
  onSetDefault: () => void;
  onBeginRename: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onRenameDraftChange: (v: string) => void;
  onRenameCommit: () => void;
  onRenameCancel: () => void;
}

function actionBtnStyle(color: string): CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    width: 20,
    height: 20,
    border: "none",
    background: "transparent",
    color,
    borderRadius: T.btnRadius,
    cursor: "pointer",
    padding: 0,
    flexShrink: 0,
  };
}

function DeckRow({
  deck,
  isActive,
  isDefault,
  isRenaming,
  renameDraft,
  hovered,
  canDelete,
  label,
  onHoverEnter,
  onHoverLeave,
  onSwitch,
  onSetDefault,
  onBeginRename,
  onDuplicate,
  onDelete,
  onRenameDraftChange,
  onRenameCommit,
  onRenameCancel,
}: DeckRowProps) {
  const t = useT();
  // Escape must cancel without also letting the input's blur (fired when the row re-renders
  // back to label mode and React removes the focused input) commit the draft as a rename.
  const suppressBlurRef = useRef(false);
  const showStar = hovered || isDefault;

  return (
    <div
      data-testid={`deck-row-${deck.id}`}
      onMouseEnter={onHoverEnter}
      onMouseLeave={onHoverLeave}
      onClick={() => {
        if (!isRenaming) onSwitch();
      }}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "6px 8px 6px 10px",
        cursor: isRenaming ? "default" : "pointer",
        background: isActive ? T.surface : "transparent",
        border: `1px solid ${isActive ? T.aBorder : "transparent"}`,
      }}
    >
      {isActive && (
        <span
          data-testid={`deck-active-bar-${deck.id}`}
          style={{
            position: "absolute",
            left: 0,
            top: 6,
            bottom: 6,
            width: 2,
            background: T.a,
            boxShadow: `0 0 8px ${T.a}`,
          }}
        />
      )}

      {isRenaming ? (
        <input
          data-testid="deck-rename-input"
          autoFocus
          value={renameDraft}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => onRenameDraftChange(e.target.value)}
          onFocus={() => {
            suppressBlurRef.current = false;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              suppressBlurRef.current = true;
              onRenameCommit();
            } else if (e.key === "Escape") {
              suppressBlurRef.current = true;
              onRenameCancel();
            }
          }}
          onBlur={() => {
            if (suppressBlurRef.current) {
              suppressBlurRef.current = false;
              return;
            }
            onRenameCommit();
          }}
          style={{
            flex: 1,
            minWidth: 0,
            font: `500 12px ${T.ui}`,
            color: T.ink,
            background: T.sunk,
            border: `1px solid ${T.bd2}`,
            borderRadius: T.btnRadius,
            padding: "2px 5px",
          }}
        />
      ) : (
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            font: `500 12px ${T.ui}`,
            color: T.ink,
          }}
        >
          {label}
        </span>
      )}

      {!isRenaming && (
        <div style={{ display: "flex", alignItems: "center", gap: 1, flexShrink: 0 }}>
          {showStar && (
            <button
              type="button"
              data-testid={`deck-star-${deck.id}`}
              title={t("cvDecks.setDefault")}
              aria-label={t("cvDecks.setDefault")}
              className="cvbtn"
              onClick={(e) => {
                e.stopPropagation();
                onSetDefault();
              }}
              style={actionBtnStyle(isDefault ? T.a : T.ink3)}
            >
              <Icon name="star" size={12} />
            </button>
          )}
          {hovered && (
            <>
              <button
                type="button"
                data-testid={`deck-pencil-${deck.id}`}
                title={t("cvDecks.rename")}
                aria-label={t("cvDecks.rename")}
                className="cvbtn"
                onClick={(e) => {
                  e.stopPropagation();
                  onBeginRename();
                }}
                style={actionBtnStyle(T.ink3)}
              >
                <Icon name="pencil" size={12} />
              </button>
              <button
                type="button"
                data-testid={`deck-copy-${deck.id}`}
                title={t("cvDecks.duplicate")}
                aria-label={t("cvDecks.duplicate")}
                className="cvbtn"
                onClick={(e) => {
                  e.stopPropagation();
                  onDuplicate();
                }}
                style={actionBtnStyle(T.ink3)}
              >
                <Icon name="copy" size={12} />
              </button>
              {canDelete && (
                <button
                  type="button"
                  data-testid={`deck-trash-${deck.id}`}
                  title={t("cvDecks.delete")}
                  aria-label={t("cvDecks.delete")}
                  className="cvbtn"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDelete();
                  }}
                  style={actionBtnStyle(T.danger)}
                >
                  <Icon name="trash" size={12} />
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function DeckRail() {
  const decks = useEditorStore((s) => s.decks);
  const defaultDeckId = useEditorStore((s) => s.defaultDeckId);
  const activeDeckId = useEditorStore((s) => s.activeDeckId);
  const railOpen = useEditorStore((s) => s.railOpen);
  const renameId = useEditorStore((s) => s.renameId);
  const renameDraft = useEditorStore((s) => s.renameDraft);
  const inferring = useEditorStore((s) => s.inferring);
  const setRailOpen = useEditorStore((s) => s.setRailOpen);
  const beginRename = useEditorStore((s) => s.beginRename);
  const setRenameDraft = useEditorStore((s) => s.setRenameDraft);
  const cancelRename = useEditorStore((s) => s.cancelRename);
  const switchDeck = useEditorStore((s) => s.switchDeck);
  const newDeck = useEditorStore((s) => s.newDeck);
  const duplicateDeck = useEditorStore((s) => s.duplicateDeck);
  const renameDeck = useEditorStore((s) => s.renameDeck);
  const setDefaultDeck = useEditorStore((s) => s.setDefaultDeck);
  const deleteDeck = useEditorStore((s) => s.deleteDeck);
  const deckLabel = useEditorStore((s) => s.deckLabel);
  // deckLabel() reads the live CV through the store's get(), which creates NO subscription.
  // Without this selector the active row's label freezes at whatever it was the last time
  // the rail happened to re-render for some other reason, so the plan's "the active row
  // tracks the live buffer" rule silently does nothing. The value is deliberately unused
  // here — the label rule itself (custom name > live name > auto_title) lives in the store.
  const liveContactName = useEditorStore((s) => s.cv?.contact.name);
  void liveContactName;

  const [hoverId, setHoverId] = useState<string | null>(null);
  const t = useT();

  const open = railOpen && !inferring;

  return (
    <div
      data-testid="deck-rail"
      onMouseEnter={() => {
        if (!inferring) setRailOpen(true);
      }}
      onMouseLeave={() => setRailOpen(false)}
      style={{
        width: 26,
        flexShrink: 0,
        background: T.subtle,
        borderRight: `1px solid ${T.bd}`,
        position: "relative",
        height: "100%",
        opacity: inferring ? 0.45 : 1,
        pointerEvents: inferring ? "none" : "auto",
      }}
    >
      <div
        style={{
          height: "100%",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
        }}
      >
        <Icon name="cols" size={13} color={railOpen ? T.a : T.ink3} />
        <span
          style={{
            writingMode: "vertical-rl",
            fontFamily: '"Share Tech Mono", monospace',
            fontSize: 9.5,
            letterSpacing: ".12em",
            color: T.ink3,
          }}
        >
          {t("cvDecks.railLabel")}
        </span>
      </div>

      {open && (
        <div
          data-testid="deck-rail-flyout"
          style={{
            position: "absolute",
            left: 26,
            top: 0,
            bottom: 0,
            width: 196,
            background: T.surface,
            border: `1px solid ${T.bd}`,
            boxShadow: T.shadowMd,
            zIndex: 5,
            display: "flex",
            flexDirection: "column",
            animation: "cvfade .12s ease",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "10px 12px",
              font: `500 10px ${T.mono}`,
              color: T.ink3,
              letterSpacing: ".08em",
              borderBottom: `1px solid ${T.bd}`,
              flexShrink: 0,
            }}
          >
            <span>{t("cvDecks.railLabel")}</span>
            <span>{t("cvDecks.deckCount", { n: decks.length })}</span>
          </div>

          <div style={{ flex: 1, overflow: "auto", padding: 6, display: "flex", flexDirection: "column", gap: 3 }}>
            {decks.map((d) => (
              <DeckRow
                key={d.id}
                deck={d}
                isActive={d.id === activeDeckId}
                isDefault={d.id === defaultDeckId}
                isRenaming={renameId === d.id}
                renameDraft={renameDraft}
                hovered={hoverId === d.id}
                canDelete={decks.length > 1}
                label={deckLabel(d)}
                onHoverEnter={() => setHoverId(d.id)}
                onHoverLeave={() => setHoverId((h) => (h === d.id ? null : h))}
                onSwitch={() => void switchDeck(d.id)}
                onSetDefault={() => void setDefaultDeck(d.id)}
                onBeginRename={() => beginRename(d.id, deckLabel(d))}
                onDuplicate={() => void duplicateDeck(d.id)}
                onDelete={() => void deleteDeck(d.id)}
                onRenameDraftChange={setRenameDraft}
                onRenameCommit={() => void renameDeck(d.id, renameDraft)}
                onRenameCancel={cancelRename}
              />
            ))}
          </div>

          <button
            type="button"
            data-testid="deck-new"
            onClick={() => void newDeck()}
            style={{
              margin: 6,
              marginTop: 0,
              border: `1px dashed ${T.bd2}`,
              background: "transparent",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              gap: 6,
              padding: "8px 0",
              color: T.ink3,
              font: `500 11px ${T.ui}`,
              letterSpacing: ".03em",
              cursor: "pointer",
              flexShrink: 0,
              borderRadius: T.btnRadius,
            }}
            className="cvbtn"
          >
            <Icon name="plus" size={12} />
            {t("cvDecks.newDeck")}
          </button>
        </div>
      )}
    </div>
  );
}
