// The "paper sheet" CV renderer used by View B (Document) and View C (Split). Editable in
// place; mirrors the serializer's layout (summary paragraph, bullets, `Category: a, b, c`
// skills lines, entries with right-aligned dates). In `selectable` mode the selected section
// gets an accent focus ring and clicking a section selects it (drives the Split outline).
import { useEditorStore } from "../../editorStore";
import type { EditorSection } from "../../types";
import { AutoTextarea, ContentInput, IconChevDown, IconChevUp, IconTrash } from "./ui";

function SectionTools({ section, index, total }: { section: EditorSection; index: number; total: number }) {
  const st = useEditorStore();
  return (
    <div className="absolute top-2 right-0 flex flex-col opacity-0 group-hover:opacity-100 transition-opacity">
      <button
        type="button"
        title="Move up"
        disabled={index === 0}
        onClick={() => st.moveSection(section.id, -1)}
        className="p-1 text-cv-ink3 hover:text-cv-ink2 disabled:opacity-30"
      >
        <IconChevUp className="w-4 h-4" />
      </button>
      <button
        type="button"
        title="Move down"
        disabled={index === total - 1}
        onClick={() => st.moveSection(section.id, 1)}
        className="p-1 text-cv-ink3 hover:text-cv-ink2 disabled:opacity-30"
      >
        <IconChevDown className="w-4 h-4" />
      </button>
      <button
        type="button"
        title="Delete section"
        onClick={() => st.deleteSection(section.id)}
        className="p-1 text-cv-ink3 hover:text-cv-danger"
      >
        <IconTrash className="w-4 h-4" />
      </button>
    </div>
  );
}

function PaperBody({ section, serif }: { section: EditorSection; serif: boolean }) {
  const st = useEditorStore();
  const bodyFont = serif ? "font-newsreader" : "font-geist";
  switch (section.kind) {
    case "summary":
      return (
        <AutoTextarea
          className={`cv-paper-field w-full ${bodyFont} text-[13.5px] leading-relaxed text-cv-ink`}
          value={section.text ?? ""}
          placeholder="Summary…"
          onChange={(e) => st.updateSection(section.id, { text: e.target.value })}
        />
      );
    case "bullets":
      return (
        <ul className="list-disc pl-5 space-y-0.5">
          {section.items.map((it, i) => (
            <li key={i} className={`${bodyFont} text-[13.5px] text-cv-ink`}>
              <input
                className="cv-paper-field w-full"
                value={it}
                onChange={(e) => st.updateItem(section.id, i, e.target.value)}
              />
            </li>
          ))}
        </ul>
      );
    case "skills":
      return (
        <div className="space-y-0.5">
          {section.entries.map((e) => (
            <div key={e.id} className={`${bodyFont} text-[13.5px] text-cv-ink`}>
              <input
                className="cv-paper-field font-semibold"
                value={e.heading ?? ""}
                placeholder="Category"
                onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
              />
              <span>: </span>
              <input
                className="cv-paper-field w-3/5"
                value={e.bullets.join(", ")}
                placeholder="a, b, c"
                onChange={(ev) =>
                  st.updateEntry(section.id, e.id, {
                    bullets: ev.target.value.split(",").map((x) => x.trim()).filter(Boolean),
                  })
                }
              />
            </div>
          ))}
        </div>
      );
    default:
      return (
        <div className="space-y-2">
          {section.entries.map((e) => (
            <div key={e.id}>
              <div className="flex justify-between items-baseline gap-2">
                <input
                  className={`cv-paper-field font-semibold ${bodyFont} text-[14px] text-cv-ink flex-1`}
                  value={e.heading ?? ""}
                  placeholder="Title"
                  onChange={(ev) => st.updateEntry(section.id, e.id, { heading: ev.target.value })}
                />
                <input
                  className="cv-paper-field font-geist-mono text-[11px] text-cv-ink2 text-right"
                  value={e.dates ?? ""}
                  placeholder="Dates"
                  onChange={(ev) => st.updateEntry(section.id, e.id, { dates: ev.target.value })}
                />
              </div>
              {(e.subheading !== undefined || section.kind !== "projects") && (
                <div className="flex justify-between items-baseline gap-2">
                  <input
                    className={`cv-paper-field italic ${bodyFont} text-[13px] text-cv-ink2 flex-1`}
                    value={e.subheading ?? ""}
                    placeholder="Subheading"
                    onChange={(ev) => st.updateEntry(section.id, e.id, { subheading: ev.target.value })}
                  />
                  <input
                    className="cv-paper-field text-[12px] text-cv-ink2 text-right"
                    value={e.location ?? ""}
                    placeholder="Location"
                    onChange={(ev) => st.updateEntry(section.id, e.id, { location: ev.target.value })}
                  />
                </div>
              )}
              {e.text !== undefined && (
                <AutoTextarea
                  className={`cv-paper-field w-full ${bodyFont} text-[13px] text-cv-ink`}
                  value={e.text ?? ""}
                  placeholder="Description"
                  onChange={(ev) => st.updateEntry(section.id, e.id, { text: ev.target.value })}
                />
              )}
              {e.bullets.length > 0 && (
                <ul className="list-disc pl-5">
                  {e.bullets.map((b, i) => (
                    <li key={i} className={`${bodyFont} text-[13px] text-cv-ink`}>
                      <input
                        className="cv-paper-field w-full"
                        value={b}
                        onChange={(ev) => st.updateBullet(section.id, e.id, i, ev.target.value)}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
        </div>
      );
  }
}

export function PaperSheet({ selectable = false }: { selectable?: boolean }) {
  const cv = useEditorStore((s) => s.cv)!;
  const st = useEditorStore();
  const serif = useEditorStore((s) => s.paperSerif);
  const selectedId = useEditorStore((s) => s.selectedId);
  const nameFont = serif ? "font-newsreader" : "font-geist";

  return (
    <div className="flex justify-center px-6 py-9 pb-32">
      <div
        className="w-[760px] bg-white rounded shadow-[0_1px_2px_rgba(40,34,24,.05),0_16px_40px_rgba(40,34,24,.07)] px-[60px] py-[54px] min-h-[900px]"
      >
        <input
          className={`cv-paper-field w-full text-center ${nameFont} text-[30px] font-medium text-cv-ink`}
          value={cv.contact.name}
          placeholder="Your Name"
          onChange={(e) => st.updateContact({ name: e.target.value })}
        />
        <div className="flex flex-wrap justify-center items-center gap-x-2 gap-y-1 mt-1 text-[12.5px] text-cv-ink2">
          {(["email", "phone"] as const).map((f) =>
            cv.contact[f] !== undefined ? (
              <ContentInput
                key={f}
                className="cv-paper-field text-center"
                value={cv.contact[f] ?? ""}
                onChange={(e) => st.updateContact({ [f]: e.target.value })}
              />
            ) : null
          )}
          {cv.contact.links.map((l, i) => (
            <span key={i} className="text-cv-accent">
              · {l}
            </span>
          ))}
          {cv.contact.location !== undefined && (
            <ContentInput
              className="cv-paper-field text-center"
              value={cv.contact.location ?? ""}
              onChange={(e) => st.updateContact({ location: e.target.value })}
            />
          )}
        </div>

        {cv.sections.map((s, i) => {
          const isSel = selectable && selectedId === s.id;
          return (
            <div
              key={s.id}
              onClick={selectable ? () => st.setSelected(s.id) : undefined}
              className={`group relative mt-5 pt-4 border-t border-cv-border2 ${
                selectable ? "cursor-pointer rounded px-2 -mx-2 transition-shadow" : ""
              } ${isSel ? "shadow-[0_0_0_2px_#3B5BD9,0_0_0_6px_#E2E6F9]" : ""}`}
            >
              <input
                className="cv-paper-field font-geist uppercase tracking-[0.13em] text-[11.5px] font-semibold text-cv-ink2 mb-1"
                style={{ width: `calc(${(s.name || "").length}ch + ${(s.name || "").length * 0.14}em + 10px)` }}
                value={s.name}
                placeholder="SECTION"
                onChange={(e) => st.updateSection(s.id, { name: e.target.value })}
              />
              <PaperBody section={s} serif={serif} />
              <SectionTools section={s} index={i} total={cv.sections.length} />
            </div>
          );
        })}
      </div>
    </div>
  );
}

export function DocumentView() {
  return <PaperSheet selectable={false} />;
}
