// View A — Blocks (primary): a centered column of a ContactCard ("IDENTITY") + one
// SectionCard ("MOD_0N") per section. Sections reorder via drag (armed only from the grip)
// or up/down arrows. The card body dispatches on the section's editor `kind`.
import { useState, type CSSProperties } from "react";
import { useEditorStore } from "../../editorStore";
import { cornerMarks, panelBase } from "../../theme/chrome";
import { Grip, Icon, type IconName } from "../../theme/Icon";
import { EDITOR_THEME } from "../../theme/tokens";
import type { EditorEntry, EditorSection, SectionKind } from "../../types";
import { AutoTextarea, KIND_CODE, KIND_ICON, KIND_LABEL } from "./ui";

const T = EDITOR_THEME;

const ADD_KINDS: { kind: SectionKind; desc: string }[] = [
  { kind: "summary", desc: "A short professional summary paragraph." },
  { kind: "experience", desc: "Roles with company, dates, and achievement bullets." },
  { kind: "projects", desc: "Projects with a description and links." },
  { kind: "skills", desc: "Grouped keyword lists (Languages, Tools…)." },
  { kind: "education", desc: "Degrees with institution and dates." },
  { kind: "bullets", desc: "A flat list of highlights." },
];

function cardField(
  opts: { weight?: number; size?: number; color?: string; pad?: string; align?: CSSProperties["textAlign"] } = {}
): CSSProperties {
  return {
    font: `${opts.weight ?? 400} ${opts.size ?? 14}px/1.55 ${T.ui}`,
    color: opts.color ?? T.ink,
    textAlign: opts.align,
    border: "1px solid transparent",
    borderRadius: 8,
    outline: "none",
    background: "transparent",
    padding: opts.pad ?? "6px 9px",
    width: "100%",
    margin: 0,
  };
}

function ToolBtn({
  icon,
  onClick,
  title,
  disabled,
  danger,
  size = 28,
  iconSize = 15,
}: {
  icon: IconName;
  onClick: () => void;
  title: string;
  disabled?: boolean;
  danger?: boolean;
  size?: number;
  iconSize?: number;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className="cvbtn"
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: size,
        height: size,
        border: "none",
        background: "transparent",
        color: danger ? T.danger : T.ink2,
        borderRadius: T.btnRadius,
        cursor: disabled ? "default" : "pointer",
        padding: 0,
        flex: "none",
      }}
    >
      <Icon name={icon} size={iconSize} />
    </button>
  );
}

// --- contact ----------------------------------------------------------------------------

function ContactRow({
  label,
  field,
  value,
  onChange,
}: {
  label: string;
  field: "email" | "phone" | "location";
  value: string;
  onChange: (field: "email" | "phone" | "location", v: string) => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        background: T.subtle,
        border: `1px solid ${T.bd}`,
        borderRadius: T.btnRadius,
        padding: "2px 6px",
      }}
    >
      <span style={{ font: `500 11px ${T.mono}`, color: T.ink3, width: 58, flex: "none", paddingLeft: 4, letterSpacing: ".04em" }}>
        {label.toUpperCase()}
      </span>
      <input
        className="cvf cvf-card"
        style={cardField({ pad: "4px 6px" })}
        value={value}
        placeholder="—"
        onChange={(e) => onChange(field, e.target.value)}
      />
    </div>
  );
}

function ContactCard() {
  const cv = useEditorStore((s) => s.cv)!;
  const updateContact = useEditorStore((s) => s.updateContact);
  const c = cv.contact;

  return (
    <div
      className="cvsec"
      style={{ position: "relative", ...panelBase(T, { chamfer: 16 }), padding: T.pad + 3, marginBottom: T.secGap, boxShadow: T.shadowSm }}
    >
      {cornerMarks(T, T.bd2)}
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
        <span style={{ font: `600 10.5px ${T.mono}`, letterSpacing: ".16em", color: T.ink3, textTransform: "uppercase" }}>IDENTITY</span>
        <span style={{ flex: 1, height: 1, background: T.bd }} />
        <span style={{ font: `400 10px ${T.mono}`, color: T.ink3, letterSpacing: ".08em" }}>OPERATOR_ID</span>
      </div>
      <input
        className="cvf cvf-card"
        style={cardField({ weight: 600, size: 23, pad: "2px 8px" })}
        value={c.name}
        placeholder="Full name"
        onChange={(e) => updateContact({ name: e.target.value })}
      />
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 8 }}>
        <ContactRow label="email" field="email" value={c.email ?? ""} onChange={(f, v) => updateContact({ [f]: v })} />
        <ContactRow label="phone" field="phone" value={c.phone ?? ""} onChange={(f, v) => updateContact({ [f]: v })} />
        <ContactRow label="location" field="location" value={c.location ?? ""} onChange={(f, v) => updateContact({ [f]: v })} />
      </div>
      <div style={{ marginTop: 10, paddingTop: 10, borderTop: `1px solid ${T.bd}` }}>
        <div style={{ font: `600 11px ${T.disp}`, letterSpacing: ".06em", color: T.ink3, marginBottom: 4 }}>CHANNELS</div>
        <LinkList links={c.links} onChange={(links) => updateContact({ links })} />
      </div>
    </div>
  );
}

function LinkList({ links, onChange }: { links: string[]; onChange: (links: string[]) => void }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {links.map((l, i) => (
        <div key={i} className="cvitem" style={{ display: "flex", alignItems: "center", gap: 7 }}>
          <Icon name="link" size={13} color={T.accent2} />
          <input
            className="cvf cvf-card"
            style={{ ...cardField({ size: 13, color: T.accent2, pad: "4px 7px" }), flex: 1 }}
            value={l}
            placeholder="github.com/…"
            onChange={(e) => onChange(links.map((x, j) => (j === i ? e.target.value : x)))}
          />
          <button
            type="button"
            className="cvih cvbtn"
            title="Remove link"
            onClick={() => onChange(links.filter((_, j) => j !== i))}
            style={{
              border: "none",
              background: "transparent",
              color: T.ink3,
              cursor: "pointer",
              width: 22,
              height: 24,
              borderRadius: 6,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "none",
            }}
          >
            <Icon name="x" size={11} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="cvbtn"
        onClick={() => onChange([...links, ""])}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          border: "none",
          background: "transparent",
          color: T.ink2,
          cursor: "pointer",
          font: `500 12.5px ${T.ui}`,
          padding: "4px 7px",
          borderRadius: 7,
          alignSelf: "flex-start",
        }}
      >
        <Icon name="plus" size={12} /> Add link
      </button>
    </div>
  );
}

// --- skills tag editor ------------------------------------------------------------------

function TagEditor({ tags, onChange }: { tags: string[]; onChange: (t: string[]) => void }) {
  const [draft, setDraft] = useState("");
  const commit = () => {
    const v = draft.trim();
    if (v) onChange([...tags, v]);
    setDraft("");
  };
  return (
    <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 6 }}>
      {tags.map((t, i) => (
        <span
          key={i}
          className="cvchip"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 5,
            padding: "3px 4px 3px 9px",
            background: T.aSoft,
            border: `1px solid ${T.aBorder}`,
            borderRadius: T.btnRadius,
            font: `500 12.5px ${T.ui}`,
            color: T.ink,
          }}
        >
          {t}
          <button
            type="button"
            className="cvx cvbtn"
            onClick={() => onChange(tags.filter((_, j) => j !== i))}
            title="Remove"
            style={{
              border: "none",
              background: "transparent",
              color: T.ink3,
              cursor: "pointer",
              padding: 0,
              width: 16,
              height: 16,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              borderRadius: 4,
            }}
          >
            <Icon name="x" size={10} />
          </button>
        </span>
      ))}
      <input
        className="cvf"
        style={{ border: "none", outline: "none", background: "transparent", font: `400 13px ${T.ui}`, color: T.ink, padding: "3px 2px", minWidth: 64, flex: 1 }}
        value={draft}
        placeholder="Add skill…"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") {
            e.preventDefault();
            commit();
          } else if (e.key === "Backspace" && draft === "" && tags.length) {
            onChange(tags.slice(0, -1));
          }
        }}
        onBlur={commit}
      />
    </div>
  );
}

// --- bullet list (entry-level) -----------------------------------------------------------

function BulletList({ section, entry }: { section: EditorSection; entry: EditorEntry }) {
  const st = useEditorStore();
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      {entry.bullets.map((b, i) => (
        <div key={i} className="cvitem" style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
          <span style={{ width: 5, height: 5, borderRadius: 5, background: T.a, marginTop: 10, flex: "none", boxShadow: `0 0 4px ${T.a}` }} />
          <AutoTextarea
            className="cvf cvf-card"
            style={{ ...cardField({ pad: "4px 7px" }), flex: 1 }}
            value={b}
            placeholder="Achievement…"
            onChange={(e) => st.updateBullet(section.id, entry.id, i, e.target.value)}
          />
          <button
            type="button"
            className="cvih cvbtn"
            title="Remove bullet"
            onClick={() => st.removeBullet(section.id, entry.id, i)}
            style={{
              border: "none",
              background: "transparent",
              color: T.ink3,
              cursor: "pointer",
              width: 24,
              height: 26,
              borderRadius: 6,
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "none",
              marginTop: 2,
            }}
          >
            <Icon name="x" size={12} />
          </button>
        </div>
      ))}
      <button
        type="button"
        className="cvbtn"
        onClick={() => st.addBullet(section.id, entry.id)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          border: "none",
          background: "transparent",
          color: T.ink2,
          cursor: "pointer",
          font: `500 12.5px ${T.ui}`,
          padding: "4px 7px",
          borderRadius: 7,
          marginLeft: 13,
        }}
      >
        <Icon name="plus" size={12} /> Add point
      </button>
    </div>
  );
}

// --- entry editor (experience / projects / education) -----------------------------------

const ENTRY_FIELDS: Record<
  string,
  { sub: boolean; dates: boolean; loc: boolean; bul: boolean; text: boolean; links: boolean; ph: string; subph?: string }
> = {
  experience: { sub: true, dates: true, loc: true, bul: true, text: false, links: false, ph: "Role / title", subph: "Company" },
  projects: { sub: false, dates: false, loc: false, bul: true, text: true, links: true, ph: "Project name" },
  education: { sub: true, dates: true, loc: true, bul: false, text: false, links: false, ph: "Degree", subph: "Institution" },
};

function EntryEditor({ section, entry }: { section: EditorSection; entry: EditorEntry }) {
  const st = useEditorStore();
  const { id: sid } = section;
  const set = (patch: Partial<EditorEntry>) => st.updateEntry(sid, entry.id, patch);
  const idx = section.entries.findIndex((e) => e.id === entry.id);
  const f = ENTRY_FIELDS[section.kind] ?? ENTRY_FIELDS.experience;

  return (
    <div
      className="cvsec"
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        gap: 7,
        padding: "11px 12px",
        paddingLeft: 14,
        ...panelBase(T, { bg: T.subtle, chamfer: 9 }),
        borderLeft: `2px solid ${T.aBorder}`,
      }}
    >
      <div style={{ display: "flex", alignItems: "flex-start", gap: 10, paddingRight: 76 }}>
        <div style={{ flex: 1, minWidth: 0 }}>
          <input
            className="cvf cvf-card"
            style={cardField({ weight: 600, size: 14.5, pad: "4px 8px" })}
            value={entry.heading ?? ""}
            placeholder={f.ph}
            onChange={(e) => set({ heading: e.target.value })}
          />
        </div>
        {f.dates && (
          <div style={{ flex: "none", width: 138 }}>
            <input
              className="cvf cvf-card"
              style={cardField({ size: 12.5, color: T.ink2, align: "right", pad: "5px 8px" })}
              value={entry.dates ?? ""}
              placeholder="Dates"
              onChange={(e) => set({ dates: e.target.value })}
            />
          </div>
        )}
      </div>

      {(f.sub || f.loc) && (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {f.sub ? (
            <div style={{ flex: 1, minWidth: 0 }}>
              <input
                className="cvf cvf-card"
                style={cardField({ size: 13, color: T.ink2, pad: "4px 8px" })}
                value={entry.subheading ?? ""}
                placeholder={f.subph}
                onChange={(e) => set({ subheading: e.target.value })}
              />
            </div>
          ) : (
            <div style={{ flex: 1 }} />
          )}
          {f.loc && (
            <div style={{ flex: "none", width: 138 }}>
              <input
                className="cvf cvf-card"
                style={cardField({ size: 12.5, color: T.ink2, align: "right", pad: "4px 8px" })}
                value={entry.location ?? ""}
                placeholder="Location"
                onChange={(e) => set({ location: e.target.value })}
              />
            </div>
          )}
        </div>
      )}

      {f.text && (
        <AutoTextarea
          className="cvf cvf-card"
          style={cardField({ size: 13.5, color: T.ink2, pad: "5px 8px" })}
          value={entry.text ?? ""}
          placeholder="Short description"
          onChange={(e) => set({ text: e.target.value })}
        />
      )}

      {f.bul && <BulletList section={section} entry={entry} />}

      {f.links && <LinkList links={entry.links} onChange={(links) => set({ links })} />}

      <div
        className="cvtools"
        style={{ position: "absolute", top: 8, right: 8, display: "flex", gap: 1, background: T.surface, border: `1px solid ${T.bd}`, borderRadius: T.btnRadius, padding: 2, boxShadow: T.shadowSm }}
      >
        <ToolBtn icon="up" size={24} iconSize={13} title="Move up" disabled={idx === 0} onClick={() => st.moveEntry(sid, entry.id, -1)} />
        <ToolBtn
          icon="down"
          size={24}
          iconSize={13}
          title="Move down"
          disabled={idx === section.entries.length - 1}
          onClick={() => st.moveEntry(sid, entry.id, 1)}
        />
        <ToolBtn icon="trash" size={24} iconSize={13} title="Delete entry" danger onClick={() => st.deleteEntry(sid, entry.id)} />
      </div>
    </div>
  );
}

// --- section body dispatch --------------------------------------------------------------

function SectionBody({ section }: { section: EditorSection }) {
  const st = useEditorStore();
  switch (section.kind) {
    case "summary":
      return (
        <AutoTextarea
          className="cvf cvf-card"
          style={cardField({ size: 14, pad: "8px 9px" })}
          value={section.text ?? ""}
          placeholder="Write a short professional summary…"
          onChange={(e) => st.updateSection(section.id, { text: e.target.value })}
        />
      );
    case "bullets":
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          {section.items.map((it, i) => (
            <div key={i} className="cvitem" style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
              <span style={{ width: 5, height: 5, borderRadius: 5, background: T.a, marginTop: 10, flex: "none", boxShadow: `0 0 4px ${T.a}` }} />
              <AutoTextarea
                className="cvf cvf-card"
                style={{ ...cardField({ pad: "4px 7px" }), flex: 1 }}
                value={it}
                placeholder="Highlight"
                onChange={(e) => st.updateItem(section.id, i, e.target.value)}
              />
              <button
                type="button"
                className="cvih cvbtn"
                title="Remove item"
                onClick={() => st.removeItem(section.id, i)}
                style={{
                  border: "none",
                  background: "transparent",
                  color: T.ink3,
                  cursor: "pointer",
                  width: 24,
                  height: 26,
                  borderRadius: 6,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flex: "none",
                  marginTop: 2,
                }}
              >
                <Icon name="x" size={12} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="cvbtn"
            onClick={() => st.addItem(section.id)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: "none",
              background: "transparent",
              color: T.ink2,
              cursor: "pointer",
              font: `500 12.5px ${T.ui}`,
              padding: "4px 7px",
              borderRadius: 7,
              marginLeft: 13,
            }}
          >
            <Icon name="plus" size={12} /> Add item
          </button>
        </div>
      );
    case "skills":
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {section.entries.map((e) => (
            <div
              key={e.id}
              className="cvitem"
              style={{ display: "flex", alignItems: "flex-start", gap: 10, padding: "7px 8px", borderRadius: T.btnRadius, background: T.subtle, border: `1px solid ${T.bd}` }}
            >
              <div style={{ flex: "none", width: 132 }}>
                <input
                  className="cvf cvf-card"
                  style={cardField({ weight: 600, size: 13, pad: "3px 7px" })}
                  value={e.heading ?? ""}
                  placeholder="Category"
                  onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
                />
              </div>
              <div style={{ flex: 1, minWidth: 0, paddingTop: 1 }}>
                <TagEditor tags={e.bullets} onChange={(bullets) => st.updateEntry(section.id, e.id, { bullets })} />
              </div>
              <button
                type="button"
                className="cvih cvbtn"
                title="Remove category"
                onClick={() => st.deleteEntry(section.id, e.id)}
                style={{
                  border: "none",
                  background: "transparent",
                  color: T.ink3,
                  cursor: "pointer",
                  width: 24,
                  height: 24,
                  borderRadius: 6,
                  display: "inline-flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flex: "none",
                }}
              >
                <Icon name="trash" size={12} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="cvbtn"
            onClick={() => st.addEntry(section.id)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              border: "none",
              background: "transparent",
              color: T.ink2,
              cursor: "pointer",
              font: `500 12.5px ${T.ui}`,
              padding: "5px 7px",
              borderRadius: 7,
              alignSelf: "flex-start",
            }}
          >
            <Icon name="plus" size={12} /> Add category
          </button>
        </div>
      );
    default:
      return (
        <div style={{ display: "flex", flexDirection: "column", gap: 9 }}>
          {section.entries.map((e) => (
            <EntryEditor key={e.id} section={section} entry={e} />
          ))}
          <button
            type="button"
            className="cvbtn"
            onClick={() => st.addEntry(section.id)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 7,
              border: `1px dashed ${T.bd2}`,
              background: "transparent",
              color: T.ink2,
              cursor: "pointer",
              font: `600 12.5px ${T.ui}`,
              padding: "9px",
              borderRadius: T.btnRadius,
              justifyContent: "center",
            }}
          >
            <Icon name="plus" size={13} />
            Add {section.kind === "education" ? "education" : section.kind === "projects" ? "project" : "role"}
          </button>
        </div>
      );
  }
}

// --- section card -----------------------------------------------------------------------

function SectionCard({ section, index, total }: { section: EditorSection; index: number; total: number }) {
  const st = useEditorStore();
  const dragId = useEditorStore((s) => s.dragId);
  const overId = useEditorStore((s) => s.overId);
  const armed = useEditorStore((s) => s.armed);
  const isDrag = dragId === section.id;
  const isOver = overId === section.id && !!dragId && dragId !== section.id;

  // Armed only from the grip (store state, so the `draggable` attribute actually re-renders).
  return (
    <div style={{ display: "flex", flexDirection: "column" }}>
      <div
        className="cvsec"
        draggable={armed && dragId === section.id}
        onDragStart={(e) => {
          st.setDrag(section.id);
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={(e) => {
          e.preventDefault();
          if (overId !== section.id) st.setOver(section.id);
        }}
        onDrop={(e) => {
          e.preventDefault();
          if (dragId && dragId !== section.id) st.reorderSection(dragId, index);
          st.endDrag();
        }}
        onDragEnd={() => st.endDrag()}
        style={{
          position: "relative",
          ...panelBase(T, { chamfer: 14, border: isOver ? T.a : T.bd }),
          padding: T.pad + 1,
          boxShadow: isOver ? `0 0 0 3px ${T.aSoft2}, 0 0 22px ${T.aSoft2}` : T.shadowSm,
          opacity: isDrag ? 0.4 : 1,
          transition: "box-shadow .12s,border-color .12s,opacity .12s",
        }}
      >
        {cornerMarks(T, isOver ? T.a : T.bd2)}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <div
            onMouseDown={() => st.arm(section.id)}
            onMouseUp={() => st.endDrag()}
            title="Drag to reorder"
            style={{ cursor: "grab", padding: "6px 3px", marginLeft: -4, display: "flex", flex: "none" }}
          >
            <Grip color={T.ink3} size={16} />
          </div>
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 9px 4px 7px",
              background: T.aSoft,
              color: T.a,
              borderRadius: T.btnRadius,
              font: `600 11px ${T.disp}`,
              letterSpacing: ".04em",
              flex: "none",
              border: `1px solid ${T.aBorder}`,
            }}
          >
            <Icon name={KIND_ICON[section.kind]} size={13} />
            {KIND_LABEL[section.kind].toUpperCase()}
          </span>
          <div style={{ flex: 1, minWidth: 0 }}>
            <input
              className="cvf cvf-card"
              style={cardField({ weight: 600, size: 16, pad: "4px 8px" })}
              value={section.name}
              placeholder="Section title"
              onChange={(e) => st.updateSection(section.id, { name: e.target.value })}
            />
          </div>
          <span style={{ font: `400 10px ${T.mono}`, color: T.ink3, letterSpacing: ".08em", flex: "none", marginRight: 4 }}>
            MOD_{String(index + 1).padStart(2, "0")}
          </span>
          <div className="cvtools" style={{ display: "flex", gap: 1, flex: "none" }}>
            <ToolBtn icon="up" title="Move section up" disabled={index === 0} onClick={() => st.moveSection(section.id, -1)} />
            <ToolBtn icon="down" title="Move section down" disabled={index === total - 1} onClick={() => st.moveSection(section.id, 1)} />
            <ToolBtn icon="trash" title="Delete section" danger onClick={() => st.deleteSection(section.id)} />
          </div>
        </div>
        <SectionBody section={section} />
      </div>
      <div className="cvgap" style={{ display: "flex", justifyContent: "center", height: T.secGap, alignItems: "center", position: "relative" }}>
        <div className="cvadd">
          <AddButton anchor={section.id} />
        </div>
      </div>
    </div>
  );
}

// --- add-section menu -------------------------------------------------------------------

function AddSectionMenu({ anchor }: { anchor: string | null }) {
  const st = useEditorStore();
  const addOpen = useEditorStore((s) => s.addOpen);
  if (addOpen !== anchor) return null;
  return (
    <div
      className="animate-cvfade"
      style={{
        position: "absolute",
        zIndex: 30,
        top: "100%",
        marginTop: 6,
        left: anchor === "end" ? "50%" : 0,
        transform: anchor === "end" ? "translateX(-50%)" : "none",
        width: 300,
        ...panelBase(T, { chamfer: 14 }),
        boxShadow: T.shadowMd,
        padding: 6,
      }}
    >
      {cornerMarks(T, T.a, 9)}
      <div style={{ font: `600 10px ${T.mono}`, letterSpacing: ".12em", color: T.ink3, textTransform: "uppercase", padding: "6px 9px 7px" }}>
        SELECT MODULE TYPE
      </div>
      {ADD_KINDS.map(({ kind, desc }) => (
        <button
          key={kind}
          type="button"
          className="cvkindbtn"
          onClick={() => st.addSection(kind, anchor === "end" ? null : anchor)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 11,
            width: "100%",
            textAlign: "left",
            padding: "9px 11px",
            border: "1px solid transparent",
            borderRadius: T.btnRadius,
            background: "transparent",
            cursor: "pointer",
          }}
        >
          <div
            style={{
              width: 30,
              height: 30,
              ...panelBase(T, { bg: T.aSoft, chamfer: 8, border: T.aBorder }),
              color: T.a,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              flex: "none",
            }}
          >
            <Icon name={KIND_ICON[kind]} size={15} />
          </div>
          <div style={{ minWidth: 0 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, font: `600 13px ${T.disp}`, color: T.ink }}>
              {KIND_LABEL[kind].toUpperCase()}
              <span style={{ font: `400 9.5px ${T.mono}`, color: T.ink3 }}>{KIND_CODE[kind]}</span>
            </div>
            <div style={{ font: `400 11.5px ${T.ui}`, color: T.ink3 }}>{desc}</div>
          </div>
        </button>
      ))}
    </div>
  );
}

function AddButton({ anchor, full }: { anchor: string; full?: boolean }) {
  const st = useEditorStore();
  const addOpen = useEditorStore((s) => s.addOpen);
  const open = addOpen === anchor;
  return (
    <div style={{ position: "relative", display: full ? "block" : "inline-block" }}>
      <button
        type="button"
        className="cvghost"
        onClick={() => st.setAddOpen(open ? null : anchor)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 8,
          width: full ? "100%" : "auto",
          padding: full ? "12px" : "7px 13px",
          border: `1px ${full ? "dashed" : "solid"} ${T.bd2}`,
          borderRadius: T.btnRadius,
          background: open ? T.sunk : T.surface,
          cursor: "pointer",
          font: `600 12px ${T.disp}`,
          letterSpacing: ".04em",
          color: T.ink2,
        }}
      >
        <Icon name="plus" size={14} />
        {full ? "INJECT MODULE" : "ADD"}
      </button>
      <AddSectionMenu anchor={anchor} />
    </div>
  );
}

// --- view -------------------------------------------------------------------------------

export function BlocksView() {
  const cv = useEditorStore((s) => s.cv)!;

  return (
    <div style={{ maxWidth: 760, margin: "0 auto", padding: "28px 24px 120px", position: "relative", zIndex: 1 }}>
      <ContactCard />
      {cv.sections.map((s, i) => (
        <SectionCard key={s.id} section={s} index={i} total={cv.sections.length} />
      ))}
      <AddButton anchor="end" full />
    </div>
  );
}
