// View C — Split: a 280px dark "INDEX" outline rail (select / reorder sections) beside the
// selectable paper sheet (View B in selectable mode).
import { useEditorStore } from "../../editorStore";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import { PaperSheet } from "./PaperSheet";
import { KIND_ICON, KIND_LABEL } from "./ui";

const T = EDITOR_THEME;

export function SplitView() {
  const cv = useEditorStore((s) => s.cv)!;
  const st = useEditorStore();
  const selectedId = useEditorStore((s) => s.selectedId);

  return (
    <div style={{ display: "flex", height: "100%", minHeight: 0 }}>
      <aside style={{ width: 280, flex: "none", borderRight: `1px solid ${T.bd}`, background: T.subtle, overflow: "auto", padding: "18px 14px", position: "relative", zIndex: 1 }}>
        <div style={{ font: `600 10px ${T.mono}`, letterSpacing: ".14em", color: T.ink3, textTransform: "uppercase", padding: "0 6px 10px" }}>
          INDEX
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {cv.sections.map((s, i) => {
            const on = selectedId === s.id;
            const count = s.kind === "summary" ? "" : s.kind === "bullets" ? `${s.items.filter(Boolean).length}` : `${s.entries.length}`;
            return (
              <div
                key={s.id}
                className="cvsec"
                onClick={() => st.setSelected(s.id)}
                style={{
                  position: "relative",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 9px",
                  borderRadius: T.btnRadius,
                  cursor: "pointer",
                  background: on ? T.surface : "transparent",
                  border: `1px solid ${on ? T.aBorder : "transparent"}`,
                }}
              >
                {on && (
                  <span style={{ position: "absolute", left: 0, top: 6, bottom: 6, width: 2, background: T.a, boxShadow: `0 0 8px ${T.a}` }} />
                )}
                <span style={{ color: on ? T.a : T.ink3, flex: "none" }}>
                  <Icon name={KIND_ICON[s.kind]} size={14} />
                </span>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    className="truncate"
                    style={{ font: `600 12.5px ${T.ui}`, color: T.ink, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                  >
                    {s.name || KIND_LABEL[s.kind]}
                  </div>
                  <div style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, letterSpacing: ".04em" }}>
                    {KIND_LABEL[s.kind].toLowerCase()}
                    {count ? ` · ${count}` : ""}
                  </div>
                </div>
                <div className="cvtools" style={{ display: "flex", gap: 0, flex: "none" }}>
                  <button
                    type="button"
                    title="Move up"
                    disabled={i === 0}
                    onClick={(e) => {
                      e.stopPropagation();
                      st.moveSection(s.id, -1);
                    }}
                    className="cvbtn"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 20, height: 20, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
                  >
                    <Icon name="up" size={11} />
                  </button>
                  <button
                    type="button"
                    title="Move down"
                    disabled={i === cv.sections.length - 1}
                    onClick={(e) => {
                      e.stopPropagation();
                      st.moveSection(s.id, 1);
                    }}
                    className="cvbtn"
                    style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", width: 20, height: 20, border: "none", background: "transparent", color: T.ink2, borderRadius: T.btnRadius, cursor: "pointer", padding: 0 }}
                  >
                    <Icon name="down" size={11} />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </aside>
      <div style={{ flex: 1, minWidth: 0, overflow: "auto", background: T.canvas, position: "relative" }}>
        <PaperSheet selectable />
      </div>
    </div>
  );
}
