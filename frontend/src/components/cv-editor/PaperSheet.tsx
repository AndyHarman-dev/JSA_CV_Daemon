// The "paper sheet" CV renderer used by View B (Document) and View C (Split). Editable in
// place; mirrors the serializer's layout (summary paragraph, bullets, `Category: a, b, c`
// skills lines, entries with right-aligned dates). In `selectable` mode the selected section
// gets an accent focus ring and clicking a section selects it (drives the Split outline).
//
// Per the design handoff, the sheet itself stays light/print-realistic (paperT) — it is NOT
// reskinned dark. It sits inside a dark EDITOR_THEME "EXPORT_PREVIEW" viewport frame.
//
// Note: content-tracking field widths are computed inline here (not via `ui.tsx`'s
// `ContentInput`) — ContentInput's own `style={{width:...}}` would be silently clobbered by
// any caller-supplied `style` prop (JSX spread order), and these fields need custom paper
// colors/fonts via `style`. ContentInput itself is left untouched.
import type { CSSProperties } from "react";
import { useEditorStore } from "../../editorStore";
import { useT } from "../../i18n/useT";
import { chamferPath, cornerMarks } from "../../theme/chrome";
import { Icon } from "../../theme/Icon";
import { EDITOR_THEME, paperT } from "../../theme/tokens";
import type { EditorSection } from "../../types";
import { DateRangeField } from "./DateRangeField";
import { allLocations, useRecentLocations } from "./locationHistory";
import { AutoTextarea } from "./ui";

const LOCATION_LIST_ID = "cv-locations-list";

const T = EDITOR_THEME;
const PA = paperT;

function chWidth(text: string | undefined, fallback: string, min = 4): string {
  const len = Math.max((text || fallback).length, min);
  return `calc(${len}ch + 4px)`;
}

function paperStyle(opts: {
  fontFamily: string;
  weight?: number;
  size?: number;
  color?: string;
  align?: CSSProperties["textAlign"];
  italic?: boolean;
  width?: string;
}): CSSProperties {
  return {
    font: `${opts.italic ? "italic " : ""}${opts.weight ?? 400} ${opts.size ?? 13.5}px/1.5 ${opts.fontFamily}`,
    color: opts.color ?? PA.ink,
    textAlign: opts.align,
    border: "none",
    borderBottom: "1px solid transparent",
    outline: "none",
    background: "transparent",
    padding: "0 1px",
    margin: 0,
    borderRadius: 2,
    width: opts.width ?? "100%",
  };
}

function lightToolBtn({
  icon,
  onClick,
  title,
  disabled,
  danger,
}: {
  icon: "up" | "down" | "trash";
  onClick: () => void;
  title: string;
  disabled?: boolean;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className="cvbtn-light"
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 26,
        height: 26,
        border: "none",
        background: "transparent",
        color: danger ? T.danger : PA.ink2,
        borderRadius: 6,
        cursor: disabled ? "default" : "pointer",
        padding: 0,
      }}
    >
      <Icon name={icon} size={14} />
    </button>
  );
}

function SectionTools({ section, index, total }: { section: EditorSection; index: number; total: number }) {
  const st = useEditorStore();
  const t = useT();
  return (
    <div
      className="cvtools"
      style={{
        position: "absolute",
        top: -13,
        left: "50%",
        transform: "translateX(-50%)",
        display: "flex",
        flexDirection: "row",
        gap: 1,
        background: PA.surface,
        border: `1px solid ${PA.bd}`,
        borderRadius: 8,
        padding: 2,
        boxShadow: "0 2px 8px rgba(0,0,0,.15)",
        zIndex: 2,
      }}
    >
      {lightToolBtn({ icon: "up", title: t("paperSheet.moveUpTitle"), disabled: index === 0, onClick: () => st.moveSection(section.id, -1) })}
      {lightToolBtn({ icon: "down", title: t("paperSheet.moveDownTitle"), disabled: index === total - 1, onClick: () => st.moveSection(section.id, 1) })}
      {lightToolBtn({ icon: "trash", title: t("paperSheet.deleteSectionTitle"), danger: true, onClick: () => st.deleteSection(section.id) })}
    </div>
  );
}

function PaperBody({
  section,
  serif,
  onRecordLocation,
}: {
  section: EditorSection;
  serif: boolean;
  onRecordLocation: (v: string) => void;
}) {
  const st = useEditorStore();
  const t = useT();
  const bodyFont = serif ? PA.serifF : PA.ui;
  switch (section.kind) {
    case "summary":
      return (
        <AutoTextarea
          className="cvf cvf-paper"
          style={paperStyle({ fontFamily: bodyFont, size: 13.5 })}
          value={section.text ?? ""}
          placeholder={t("paperSheet.summaryPlaceholder")}
          onChange={(e) => st.updateSection(section.id, { text: e.target.value })}
        />
      );
    case "bullets":
      return (
        <ul style={{ margin: "4px 0 0", paddingLeft: 20, listStyle: "none" }}>
          {section.items.map((it, i) => (
            <li key={i} className="cvitem" style={{ marginBottom: 2, display: "flex", gap: 6, alignItems: "flex-start" }}>
              <span style={{ width: 4, height: 4, borderRadius: 4, background: PA.ink3, marginTop: 8, flex: "none" }} />
              <AutoTextarea
                className="cvf cvf-paper"
                style={paperStyle({ fontFamily: bodyFont, size: 13.5 })}
                value={it}
                onChange={(e) => st.updateItem(section.id, i, e.target.value)}
              />
              <button
                type="button"
                className="cvih cvbtn-light"
                onClick={() => st.removeItem(section.id, i)}
                title={t("paperSheet.removeItemTitle")}
                style={{ border: "none", background: "transparent", color: PA.ink3, cursor: "pointer", width: 18, height: 18, borderRadius: 4, flex: "none", display: "inline-flex", alignItems: "center", justifyContent: "center" }}
              >
                <Icon name="x" size={10} />
              </button>
            </li>
          ))}
        </ul>
      );
    case "skills":
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 3 }}>
          {section.entries.map((e) => (
            <div key={e.id} style={{ display: "flex", gap: 7, alignItems: "baseline", font: `400 13.5px ${bodyFont}`, color: PA.ink }}>
              <span style={{ flex: "none" }}>
                <input
                  className="cvf cvf-paper"
                  style={paperStyle({ fontFamily: bodyFont, weight: 600, width: chWidth(e.heading, "Group") })}
                  value={e.heading ?? ""}
                  placeholder={t("paperSheet.groupPlaceholder")}
                  onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
                />
                <span style={{ fontWeight: 600 }}>:</span>
              </span>
              <span style={{ flex: 1 }}>
                <input
                  className="cvf cvf-paper"
                  style={paperStyle({ fontFamily: bodyFont, width: "100%" })}
                  value={e.bullets.join(", ")}
                  placeholder={t("paperSheet.skillsExamplePlaceholder")}
                  onChange={(ev) =>
                    st.updateEntry(section.id, e.id, {
                      bullets: ev.target.value.split(",").map((x) => x.trim()).filter(Boolean),
                    })
                  }
                />
              </span>
            </div>
          ))}
        </div>
      );
    default:
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 4 }}>
          {section.entries.map((e) => {
            const showSub = e.subheading !== undefined || section.kind !== "projects";
            return (
              <div key={e.id} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10 }}>
                  <input
                    className="cvf cvf-paper"
                    style={paperStyle({ fontFamily: bodyFont, weight: 600, size: 14, width: "100%" })}
                    value={e.heading ?? ""}
                    placeholder={t("paperSheet.titlePlaceholder")}
                    onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
                  />
                  {e.dates !== undefined && (
                    <span style={{ flex: "0 1 auto", minWidth: 0, marginLeft: "auto" }}>
                      <DateRangeField
                        value={e.dates ?? ""}
                        onChange={(v) => st.updateEntry(section.id, e.id, { dates: v })}
                      />
                    </span>
                  )}
                </div>
                {showSub && (
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 10, font: `400 12.5px ${bodyFont}`, fontStyle: "italic", color: PA.ink2 }}>
                    <input
                      className="cvf cvf-paper"
                      style={paperStyle({ fontFamily: bodyFont, size: 12.5, color: PA.ink2, italic: true, width: "100%" })}
                      value={e.subheading ?? ""}
                      placeholder={t("paperSheet.subheadingPlaceholder")}
                      onChange={(ev) => st.updateEntry(section.id, e.id, { subheading: ev.target.value })}
                    />
                    {e.location !== undefined && (
                      <input
                        className="cvf cvf-paper"
                        list={LOCATION_LIST_ID}
                        style={{ ...paperStyle({ fontFamily: bodyFont, size: 12, color: PA.ink2, align: "right", width: chWidth(e.location, "location") }), flex: "none" }}
                        value={e.location ?? ""}
                        placeholder={t("paperSheet.locationPlaceholder")}
                        onChange={(ev) => st.updateEntry(section.id, e.id, { location: ev.target.value })}
                        onBlur={(ev) => onRecordLocation(ev.target.value)}
                      />
                    )}
                  </div>
                )}
                {e.text !== undefined && (
                  <AutoTextarea
                    className="cvf cvf-paper"
                    style={paperStyle({ fontFamily: bodyFont, size: 13 })}
                    value={e.text ?? ""}
                    placeholder={t("paperSheet.descriptionPlaceholder")}
                    onChange={(ev) => st.updateEntry(section.id, e.id, { text: ev.target.value })}
                  />
                )}
                {e.bullets.length > 0 && (
                  <ul style={{ margin: "3px 0 0", paddingLeft: 20, listStyle: "none" }}>
                    {e.bullets.map((b, i) => (
                      <li key={i} className="cvitem" style={{ marginBottom: 1, display: "flex", gap: 6, font: `400 13px ${bodyFont}`, color: PA.ink }}>
                        <span style={{ width: 4, height: 4, borderRadius: 4, background: PA.ink3, marginTop: 7, flex: "none" }} />
                        <AutoTextarea
                          className="cvf cvf-paper"
                          style={paperStyle({ fontFamily: bodyFont, size: 13 })}
                          value={b}
                          onChange={(ev) => st.updateBullet(section.id, e.id, i, ev.target.value)}
                        />
                        <button
                          type="button"
                          className="cvih cvbtn-light"
                          onClick={() => st.removeBullet(section.id, e.id, i)}
                          title={t("paperSheet.removeBulletTitle")}
                          style={{ border: "none", background: "transparent", color: PA.ink3, cursor: "pointer", width: 18, height: 18, flex: "none", borderRadius: 4, display: "inline-flex", alignItems: "center", justifyContent: "center" }}
                        >
                          <Icon name="x" size={10} />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {e.links.length > 0 && (
                  <div style={{ font: `400 12px ${PA.ui}`, color: PA.a, marginTop: 2 }}>
                    {e.links.map((l, i) => (
                      <input
                        key={i}
                        className="cvf cvf-paper"
                        style={paperStyle({ fontFamily: PA.ui, size: 12, color: PA.a, width: chWidth(l, "link") })}
                        value={l}
                        placeholder={t("paperSheet.linkPlaceholder")}
                        onChange={(ev) =>
                          st.updateEntry(section.id, e.id, { links: e.links.map((x, j) => (j === i ? ev.target.value : x)) })
                        }
                      />
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      );
  }
}

export function PaperSheet({ selectable = false }: { selectable?: boolean }) {
  const cv = useEditorStore((s) => s.cv)!;
  const st = useEditorStore();
  const serif = useEditorStore((s) => s.paperSerif);
  const selectedId = useEditorStore((s) => s.selectedId);
  const t = useT();
  const nameFont = serif ? PA.serifF : PA.ui;
  const { recent, record } = useRecentLocations();
  const locationOptions = allLocations(cv, recent);

  const sheet = (
    <div
      style={{
        width: 760,
        maxWidth: "100%",
        margin: "0 auto",
        background: PA.surface,
        border: `1px solid ${PA.bd}`,
        borderRadius: 4,
        boxShadow: "0 1px 2px rgba(0,0,0,.2), 0 24px 50px rgba(0,0,0,.45)",
        padding: "54px 60px 70px",
        minHeight: 900,
        fontFamily: nameFont,
      }}
    >
      <div style={{ textAlign: "center", marginBottom: 6 }}>
        <input
          className="cvf cvf-paper"
          style={paperStyle({ fontFamily: nameFont, weight: 500, size: 30, align: "center" })}
          value={cv.contact.name}
          placeholder={t("paperSheet.yourNamePlaceholder")}
          onChange={(e) => st.updateContact({ name: e.target.value })}
        />
      </div>
      <div style={{ textAlign: "center", font: `400 12.5px ${PA.ui}`, color: PA.ink2, marginBottom: 26, display: "flex", flexWrap: "wrap", justifyContent: "center", alignItems: "center", gap: 2 }}>
        {(["email", "phone"] as const).map((f) =>
          cv.contact[f] !== undefined ? (
            <input
              key={f}
              className="cvf cvf-paper"
              style={paperStyle({ fontFamily: PA.ui, size: 12.5, color: PA.ink2, align: "center", width: chWidth(cv.contact[f], f) })}
              value={cv.contact[f] ?? ""}
              onChange={(e) => st.updateContact({ [f]: e.target.value })}
            />
          ) : null
        )}
        {cv.contact.links.map((l, i) => (
          <span key={i} style={{ color: PA.a, margin: "0 2px" }}>
            ·{" "}
            <input
              className="cvf cvf-paper"
              style={paperStyle({ fontFamily: PA.ui, size: 12.5, color: PA.a, align: "center", width: chWidth(l, "link") })}
              value={l}
              placeholder="link"
              onChange={(e) =>
                st.updateContact({ links: cv.contact.links.map((x, j) => (j === i ? e.target.value : x)) })
              }
            />
          </span>
        ))}
        {cv.contact.location !== undefined && (
          <span>
            ·{" "}
            <input
              className="cvf cvf-paper"
              list={LOCATION_LIST_ID}
              style={paperStyle({ fontFamily: PA.ui, size: 12.5, color: PA.ink2, align: "center", width: chWidth(cv.contact.location, "location") })}
              value={cv.contact.location ?? ""}
              onChange={(e) => st.updateContact({ location: e.target.value })}
              onBlur={(e) => record(e.target.value)}
            />
          </span>
        )}
      </div>

      <datalist id={LOCATION_LIST_ID}>
        {locationOptions.map((loc) => (
          <option key={loc} value={loc} />
        ))}
      </datalist>

      {cv.sections.map((s, i) => {
        const isSel = selectable && selectedId === s.id;
        return (
          <div
            key={s.id}
            className="cvsec cvpapersec"
            onClick={selectable ? () => st.setSelected(s.id) : undefined}
            style={{
              position: "relative",
              marginTop: i === 0 ? 4 : 18,
              paddingTop: 14,
              borderTop: `1px solid ${PA.bd}`,
              boxShadow: isSel ? `0 0 0 2px ${PA.a}, 0 0 0 6px color-mix(in srgb, ${PA.a} 15%, transparent)` : "none",
              borderRadius: isSel ? 6 : 0,
              padding: isSel ? "14px 10px 8px" : "14px 0 0",
              cursor: selectable ? "pointer" : "default",
            }}
          >
            <div style={{ marginBottom: 7 }}>
              <input
                className="cvf cvf-paper"
                style={paperStyle({ fontFamily: PA.ui, weight: 600, size: 11.5, color: PA.ink, width: chWidth(s.name, "Section") })}
                value={s.name}
                placeholder={t("paperSheet.sectionPlaceholder")}
                onChange={(e) => st.updateSection(s.id, { name: e.target.value })}
              />
            </div>
            <PaperBody section={s} serif={serif} onRecordLocation={record} />
            <SectionTools section={s} index={i} total={cv.sections.length} />
          </div>
        );
      })}
    </div>
  );

  return (
    <div style={{ padding: "34px 24px 120px" }}>
      <div style={{ position: "relative", maxWidth: 880, margin: "0 auto" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12, justifyContent: "center" }}>
          <span
            style={{ width: 6, height: 6, borderRadius: 6, background: T.accent2, boxShadow: `0 0 8px ${T.accent2}`, animation: "cyblink 2.4s ease-in-out infinite", flex: "none" }}
          />
          <span style={{ font: `500 10.5px ${T.mono}`, letterSpacing: ".18em", color: T.ink3, textTransform: "uppercase" }}>
            EXPORT_PREVIEW · READ-ONLY LAYOUT
          </span>
        </div>
        <div
          style={{
            position: "relative",
            padding: 18,
            border: `1px dashed ${T.bd2}`,
            borderRadius: T.chamfer ? 0 : 10,
            clipPath: T.chamfer ? chamferPath(18) : undefined,
          }}
        >
          {cornerMarks(T, T.bd2, 11)}
          {sheet}
        </div>
      </div>
    </div>
  );
}

export function DocumentView() {
  return <PaperSheet selectable={false} />;
}
