// View A — Blocks (primary): a centered column of a ContactCard + one SectionCard per
// section. Sections reorder via drag (armed only from the grip) or up/down arrows. The card
// body dispatches on the section's editor `kind`.
import { useState } from "react";
import { useEditorStore } from "../../editorStore";
import type { EditorEntry, EditorSection, SectionKind } from "../../types";
import {
  AutoTextarea,
  IconChevDown,
  IconChevUp,
  IconGrip,
  IconPlus,
  IconTrash,
  IconX,
  KIND_ICON,
  KIND_LABEL,
} from "./ui";

const ADD_KINDS: { kind: SectionKind; desc: string }[] = [
  { kind: "summary", desc: "A short professional summary paragraph." },
  { kind: "experience", desc: "Roles with company, dates, and achievement bullets." },
  { kind: "projects", desc: "Projects with a description and links." },
  { kind: "skills", desc: "Grouped keyword lists (Languages, Tools…)." },
  { kind: "education", desc: "Degrees with institution and dates." },
  { kind: "bullets", desc: "A flat list of highlights." },
];

function ToolBtn({
  onClick,
  title,
  disabled,
  danger,
  children,
}: {
  onClick: () => void;
  title: string;
  disabled?: boolean;
  danger?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`p-1 rounded-md transition-colors disabled:opacity-30 ${
        danger ? "text-cv-ink3 hover:text-cv-danger" : "text-cv-ink3 hover:text-cv-ink2"
      } hover:bg-cv-sunk`}
    >
      {children}
    </button>
  );
}

// --- contact ----------------------------------------------------------------------------

function ContactCard() {
  const cv = useEditorStore((s) => s.cv)!;
  const updateContact = useEditorStore((s) => s.updateContact);
  const c = cv.contact;

  const Row = ({ label, field }: { label: string; field: "email" | "phone" | "location" }) => (
    <label className="flex items-center gap-2 bg-cv-subtle rounded-lg px-2.5 py-1.5">
      <span className="font-geist-mono text-[10.5px] text-cv-ink3 w-14 shrink-0">{label}</span>
      <input
        className="flex-1 bg-transparent outline-none text-sm text-cv-ink placeholder:text-cv-ink3"
        value={c[field] ?? ""}
        placeholder="—"
        onChange={(e) => updateContact({ [field]: e.target.value })}
      />
    </label>
  );

  return (
    <div className="bg-cv-surface border border-cv-border rounded-2xl p-5 shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        <span className="font-geist-mono text-[10.5px] tracking-widest text-cv-ink3">CONTACT</span>
        <span className="flex-1 h-px bg-cv-border" />
      </div>
      <input
        className="w-full bg-transparent outline-none font-geist font-semibold text-[23px] text-cv-ink placeholder:text-cv-ink3 mb-3"
        value={c.name}
        placeholder="Your name"
        onChange={(e) => updateContact({ name: e.target.value })}
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <Row label="email" field="email" />
        <Row label="phone" field="phone" />
        <Row label="location" field="location" />
      </div>
      <LinkList
        links={c.links}
        onChange={(links) => updateContact({ links })}
        label="Links"
      />
    </div>
  );
}

function LinkList({
  links,
  onChange,
  label,
}: {
  links: string[];
  onChange: (links: string[]) => void;
  label: string;
}) {
  return (
    <div className="mt-3">
      <span className="font-geist-mono text-[10.5px] tracking-wide text-cv-ink3">{label}</span>
      <div className="mt-1.5 space-y-1.5">
        {links.map((l, i) => (
          <div key={i} className="flex items-center gap-1.5 group">
            <input
              className="cv-field flex-1 px-2.5 py-1.5 text-sm text-cv-ink outline-none"
              value={l}
              placeholder="github.com/…"
              onChange={(e) => onChange(links.map((x, j) => (j === i ? e.target.value : x)))}
            />
            <ToolBtn title="Remove link" onClick={() => onChange(links.filter((_, j) => j !== i))}>
              <IconX className="w-3.5 h-3.5" />
            </ToolBtn>
          </div>
        ))}
        <button
          type="button"
          onClick={() => onChange([...links, ""])}
          className="text-xs text-cv-ink2 hover:text-cv-accent transition-colors"
        >
          + Add link
        </button>
      </div>
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
    <div className="flex flex-wrap items-center gap-1.5">
      {tags.map((t, i) => (
        <span
          key={i}
          className="group inline-flex items-center gap-1 bg-cv-accent-soft text-cv-accent rounded-md px-2 py-0.5 text-xs"
        >
          {t}
          <button
            type="button"
            onClick={() => onChange(tags.filter((_, j) => j !== i))}
            className="opacity-0 group-hover:opacity-100 transition-opacity"
            title="Remove"
          >
            <IconX className="w-3 h-3" />
          </button>
        </span>
      ))}
      <input
        className="bg-transparent outline-none text-xs text-cv-ink placeholder:text-cv-ink3 min-w-[80px] flex-1 py-0.5"
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

// --- entry editor (experience / projects / education) -----------------------------------

function EntryEditor({ section, entry }: { section: EditorSection; entry: EditorEntry }) {
  const st = useEditorStore();
  const { id: sid } = section;
  const set = (patch: Partial<EditorEntry>) => st.updateEntry(sid, entry.id, patch);
  const idx = section.entries.findIndex((e) => e.id === entry.id);

  return (
    <div className="group relative bg-cv-subtle border border-cv-border rounded-xl border-l-2 border-l-cv-border2 px-3 py-2.5">
      <div className="absolute top-1.5 right-1.5 flex opacity-0 group-hover:opacity-100 transition-opacity">
        <ToolBtn title="Move up" disabled={idx === 0} onClick={() => st.moveEntry(sid, entry.id, -1)}>
          <IconChevUp className="w-4 h-4" />
        </ToolBtn>
        <ToolBtn
          title="Move down"
          disabled={idx === section.entries.length - 1}
          onClick={() => st.moveEntry(sid, entry.id, 1)}
        >
          <IconChevDown className="w-4 h-4" />
        </ToolBtn>
        <ToolBtn title="Delete entry" danger onClick={() => st.deleteEntry(sid, entry.id)}>
          <IconTrash className="w-4 h-4" />
        </ToolBtn>
      </div>

      <div className="flex gap-2 pr-16">
        <input
          className="cv-field flex-1 px-2 py-1 text-sm font-semibold text-cv-ink outline-none"
          value={entry.heading ?? ""}
          placeholder={section.kind === "education" ? "Degree" : "Title"}
          onChange={(e) => set({ heading: e.target.value })}
        />
        {section.kind !== "projects" && (
          <input
            className="cv-field w-[138px] px-2 py-1 text-xs text-cv-ink2 outline-none"
            value={entry.dates ?? ""}
            placeholder="Dates"
            onChange={(e) => set({ dates: e.target.value })}
          />
        )}
      </div>

      {section.kind !== "projects" && (
        <div className="flex gap-2 mt-1.5">
          <input
            className="cv-field flex-1 px-2 py-1 text-sm text-cv-ink2 outline-none"
            value={entry.subheading ?? ""}
            placeholder={section.kind === "education" ? "Institution" : "Company"}
            onChange={(e) => set({ subheading: e.target.value })}
          />
          <input
            className="cv-field w-[138px] px-2 py-1 text-xs text-cv-ink2 outline-none"
            value={entry.location ?? ""}
            placeholder="Location"
            onChange={(e) => set({ location: e.target.value })}
          />
        </div>
      )}

      {section.kind === "projects" && (
        <AutoTextarea
          className="cv-field w-full px-2 py-1 mt-1.5 text-sm text-cv-ink outline-none"
          value={entry.text ?? ""}
          placeholder="Description"
          onChange={(e) => set({ text: e.target.value })}
        />
      )}

      {section.kind !== "education" && (
        <BulletList section={section} entry={entry} />
      )}

      {section.kind === "projects" && (
        <div className="mt-1.5">
          <LinkList links={entry.links} onChange={(links) => set({ links })} label="Links" />
        </div>
      )}
    </div>
  );
}

function BulletList({ section, entry }: { section: EditorSection; entry: EditorEntry }) {
  const st = useEditorStore();
  return (
    <div className="mt-1.5 space-y-1">
      {entry.bullets.map((b, i) => (
        <div key={i} className="group/b flex items-start gap-1.5">
          <span className="mt-2 w-1 h-1 rounded-full bg-cv-ink3 shrink-0" />
          <AutoTextarea
            className="cv-field flex-1 px-2 py-1 text-sm text-cv-ink outline-none"
            value={b}
            placeholder="Achievement…"
            onChange={(e) => st.updateBullet(section.id, entry.id, i, e.target.value)}
          />
          <ToolBtn
            title="Remove bullet"
            onClick={() => st.removeBullet(section.id, entry.id, i)}
          >
            <IconX className="w-3.5 h-3.5 opacity-0 group-hover/b:opacity-100 transition-opacity" />
          </ToolBtn>
        </div>
      ))}
      <button
        type="button"
        onClick={() => st.addBullet(section.id, entry.id)}
        className="text-xs text-cv-ink2 hover:text-cv-accent transition-colors ml-2.5"
      >
        + Add bullet
      </button>
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
          className="cv-field w-full px-3 py-2 text-sm text-cv-ink outline-none"
          value={section.text ?? ""}
          placeholder="Write a 2–3 sentence summary…"
          onChange={(e) => st.updateSection(section.id, { text: e.target.value })}
        />
      );
    case "bullets":
      return (
        <div className="space-y-1">
          {section.items.map((it, i) => (
            <div key={i} className="group/i flex items-start gap-1.5">
              <span className="mt-2 w-1 h-1 rounded-full bg-cv-ink3 shrink-0" />
              <AutoTextarea
                className="cv-field flex-1 px-2 py-1 text-sm text-cv-ink outline-none"
                value={it}
                placeholder="Highlight…"
                onChange={(e) => st.updateItem(section.id, i, e.target.value)}
              />
              <ToolBtn title="Remove item" onClick={() => st.removeItem(section.id, i)}>
                <IconX className="w-3.5 h-3.5 opacity-0 group-hover/i:opacity-100 transition-opacity" />
              </ToolBtn>
            </div>
          ))}
          <button
            type="button"
            onClick={() => st.addItem(section.id)}
            className="text-xs text-cv-ink2 hover:text-cv-accent transition-colors ml-2.5"
          >
            + Add item
          </button>
        </div>
      );
    case "skills":
      return (
        <div className="space-y-1.5">
          {section.entries.map((e) => (
            <div key={e.id} className="group/g flex items-start gap-2 bg-cv-subtle rounded-lg p-2">
              <input
                className="w-[132px] shrink-0 bg-transparent outline-none text-sm font-semibold text-cv-ink placeholder:text-cv-ink3"
                value={e.heading ?? ""}
                placeholder="Category"
                onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
              />
              <div className="flex-1">
                <TagEditor
                  tags={e.bullets}
                  onChange={(bullets) => st.updateEntry(section.id, e.id, { bullets })}
                />
              </div>
              <ToolBtn title="Remove category" danger onClick={() => st.deleteEntry(section.id, e.id)}>
                <IconTrash className="w-4 h-4 opacity-0 group-hover/g:opacity-100 transition-opacity" />
              </ToolBtn>
            </div>
          ))}
          <button
            type="button"
            onClick={() => st.addEntry(section.id)}
            className="text-xs text-cv-ink2 hover:text-cv-accent transition-colors"
          >
            + Add category
          </button>
        </div>
      );
    default:
      return (
        <div className="space-y-2">
          {section.entries.map((e) => (
            <EntryEditor key={e.id} section={section} entry={e} />
          ))}
          <button
            type="button"
            onClick={() => st.addEntry(section.id)}
            className="w-full text-sm text-cv-ink2 hover:text-cv-accent border border-dashed border-cv-border2 rounded-lg py-1.5 transition-colors"
          >
            + Add{" "}
            {section.kind === "education" ? "education" : section.kind === "projects" ? "project" : "role"}
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
  const KindIcon = KIND_ICON[section.kind];

  // Armed only from the grip (store state, so the `draggable` attribute actually re-renders).
  return (
    <div
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
      className={`bg-cv-surface border rounded-2xl p-[18px] shadow-sm transition-shadow ${
        dragId === section.id ? "opacity-40" : ""
      } ${overId === section.id && dragId && dragId !== section.id ? "border-cv-accent ring-[3px] ring-cv-accent-soft2" : "border-cv-border"}`}
    >
      <div className="group flex items-center gap-2">
        <button
          type="button"
          title="Drag to reorder"
          onMouseDown={() => st.arm(section.id)}
          onMouseUp={() => st.endDrag()}
          className="cursor-grab active:cursor-grabbing text-cv-ink3 hover:text-cv-ink2 shrink-0"
        >
          <IconGrip className="w-4 h-4" />
        </button>
        <span className="inline-flex items-center gap-1 bg-cv-accent-soft text-cv-accent rounded-md px-1.5 py-0.5 text-[11px] shrink-0">
          <KindIcon className="w-3.5 h-3.5" />
          {KIND_LABEL[section.kind]}
        </span>
        <input
          className="flex-1 bg-transparent outline-none font-geist font-semibold text-base text-cv-ink placeholder:text-cv-ink3"
          value={section.name}
          placeholder="Section name"
          onChange={(e) => st.updateSection(section.id, { name: e.target.value })}
        />
        <div className="flex opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
          <ToolBtn title="Move up" disabled={index === 0} onClick={() => st.moveSection(section.id, -1)}>
            <IconChevUp className="w-4 h-4" />
          </ToolBtn>
          <ToolBtn
            title="Move down"
            disabled={index === total - 1}
            onClick={() => st.moveSection(section.id, 1)}
          >
            <IconChevDown className="w-4 h-4" />
          </ToolBtn>
          <ToolBtn title="Delete section" danger onClick={() => st.deleteSection(section.id)}>
            <IconTrash className="w-4 h-4" />
          </ToolBtn>
        </div>
      </div>
      <div className="mt-3">
        <SectionBody section={section} />
      </div>
    </div>
  );
}

// --- add-section menu -------------------------------------------------------------------

export function AddSectionMenu({ anchor }: { anchor: string | null }) {
  const st = useEditorStore();
  const addOpen = useEditorStore((s) => s.addOpen);
  if (addOpen !== anchor) return null;
  return (
    <div className="absolute z-30 mt-1 w-72 bg-cv-surface border border-cv-border rounded-2xl shadow-xl p-1.5 animate-cvfade">
      {ADD_KINDS.map(({ kind, desc }) => {
        const Icon = KIND_ICON[kind];
        return (
          <button
            key={kind}
            type="button"
            onClick={() => st.addSection(kind, anchor === "end" ? null : anchor)}
            className="w-full flex items-start gap-2.5 text-left rounded-xl px-2.5 py-2 hover:bg-cv-subtle transition-colors"
          >
            <span className="mt-0.5 inline-flex items-center justify-center w-7 h-7 rounded-lg bg-cv-accent-soft text-cv-accent shrink-0">
              <Icon className="w-4 h-4" />
            </span>
            <span>
              <span className="block text-sm font-medium text-cv-ink">{KIND_LABEL[kind]}</span>
              <span className="block text-xs text-cv-ink3">{desc}</span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

// --- view -------------------------------------------------------------------------------

export function BlocksView() {
  const cv = useEditorStore((s) => s.cv)!;
  const st = useEditorStore();
  const addOpen = useEditorStore((s) => s.addOpen);

  return (
    <div className="max-w-[760px] mx-auto px-6 pt-7 pb-32 space-y-[18px]">
      <ContactCard />
      {cv.sections.map((s, i) => (
        <SectionCard key={s.id} section={s} index={i} total={cv.sections.length} />
      ))}
      <div className="relative">
        <button
          type="button"
          onClick={() => st.setAddOpen(addOpen === "end" ? null : "end")}
          className="w-full flex items-center justify-center gap-1.5 text-sm text-cv-ink2 hover:text-cv-accent border border-dashed border-cv-border2 rounded-2xl py-3 transition-colors"
        >
          <IconPlus className="w-4 h-4" /> Add section
        </button>
        <AddSectionMenu anchor="end" />
      </div>
    </div>
  );
}
