// Right-side read-only drawer showing the live exported JSON (exportJson → schema shape)
// with lightweight syntax highlighting. Updates on every edit; copy + close controls.
import { useState } from "react";
import { exportJson, useEditorStore } from "../../editorStore";
import { useT } from "../../i18n/useT";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";

const T = EDITOR_THEME;

// Highlight a JSON string by wrapping tokens in inline-colored <span>s. Operates on the
// already-escaped pretty-printed text; token colors mirror the design's JSON palette
// (string ~green, key cyan, number/bool/null ~pink).
function highlight(json: string): string {
  const esc = json
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return esc.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      // strings → green; object keys (string followed by `:`) → cyan; numbers/bool/null → pink.
      const color = /^"/.test(match) ? (/:$/.test(match) ? T.accent2 : "#8FE3A0") : "#FF6FD8";
      return `<span style="color:${color}">${match}</span>`;
    }
  );
}

export function JsonDrawer() {
  const cv = useEditorStore((s) => s.cv);
  const toggleJson = useEditorStore((s) => s.toggleJson);
  const [copied, setCopied] = useState(false);
  const t = useT();
  if (!cv) return null;

  const text = JSON.stringify(exportJson(cv), null, 2);

  return (
    <aside
      style={{
        width: 400,
        flex: "none",
        borderLeft: `1px solid ${T.bd}`,
        background: "#0A0E13",
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        position: "relative",
        zIndex: 1,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 14px", borderBottom: `1px solid ${T.bd}`, flex: "none" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <span style={{ color: T.accent2 }}>
            <Icon name="braces" size={16} />
          </span>
          <div>
            <div style={{ font: `600 13px ${T.disp}`, color: T.ink, letterSpacing: ".03em" }}>{t("jsonDrawer.title")}</div>
            <div style={{ display: "flex", alignItems: "center", gap: 5, font: `500 10px ${T.mono}`, color: T.ink3, letterSpacing: ".04em" }}>
              <span
                style={{ width: 6, height: 6, borderRadius: 6, background: T.accent2, boxShadow: `0 0 6px ${T.accent2}`, animation: "cyblink 2s ease-in-out infinite" }}
              />
              {t("jsonDrawer.subtitle")}
            </div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 2 }}>
          <button
            type="button"
            title={t("jsonDrawer.copyTitle")}
            onClick={() => {
              void navigator.clipboard?.writeText(text);
              setCopied(true);
              setTimeout(() => setCopied(false), 1200);
            }}
            className="cvbtn"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
          >
            <Icon name="copy" size={15} />
          </button>
          <button
            type="button"
            title={t("jsonDrawer.closeTitle")}
            onClick={toggleJson}
            className="cvbtn"
            style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 28, height: 28, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
          >
            <Icon name="x" size={15} />
          </button>
        </div>
      </div>
      {copied && <div style={{ padding: "4px 14px 0", font: `400 11px ${T.ui}`, color: T.accent2 }}>{t("jsonDrawer.copiedLabel")}</div>}
      <pre
        style={{ margin: 0, flex: 1, overflow: "auto", padding: "14px 16px", font: `400 12px/1.6 ${T.mono}`, color: T.ink, whiteSpace: "pre", tabSize: 2 }}
        dangerouslySetInnerHTML={{ __html: highlight(text) }}
      />
    </aside>
  );
}
