// The overhanging "EDIT" corner trigger for the CV Editor AI chat (design hand-off's
// `triggerStyle: "corner"`). It is a SIBLING of the chamfered card it decorates, never a
// descendant: every card's chamfer is a clipPath (theme/chrome.tsx::panelBase), which clips
// away anything outside its polygon, and this pill deliberately overhangs the card's top
// edge (top: -10). Callers must render it inside a `.cvunit` wrapper (position: relative,
// NOT chamfered) alongside the card -- never inside the card itself. See the plan's Phase 5
// "5a/5b" for the trap this works around.
import type { ChatScope } from "../../cvChatStore";
import { useCvChatStore } from "../../cvChatStore";
import { useT } from "../../i18n/useT";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";

const T = EDITOR_THEME;

export function ChatCorner({ scope, label = "EDIT" }: { scope: ChatScope; label?: string }) {
  const t = useT();
  const openChat = useCvChatStore((s) => s.openChat);
  return (
    <button
      type="button"
      className="aitrig"
      data-testid="chat-corner-trigger"
      onClick={(e) => {
        e.stopPropagation();
        openChat(scope);
      }}
      title={t("cvChat.triggerTitle")}
      style={{
        position: "absolute",
        top: -10,
        right: 10,
        zIndex: 3,
        display: "inline-flex",
        alignItems: "center",
        gap: 5,
        padding: "3px 9px 3px 7px",
        background: T.a,
        color: "#1A0A0D",
        border: `1px solid ${T.a}`,
        borderRadius: 999,
        font: `700 10px ${T.mono}`,
        letterSpacing: ".08em",
        cursor: "pointer",
        boxShadow: T.shadowSm,
      }}
    >
      <Icon name="chat" size={11} />
      {label}
    </button>
  );
}
