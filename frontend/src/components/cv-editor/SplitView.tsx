// View C — Split: a 280px outline rail (select / reorder sections) beside the selectable
// paper sheet (View B in selectable mode).
import { useEditorStore } from "../../editorStore";
import { PaperSheet } from "./PaperSheet";
import { IconChevDown, IconChevUp, KIND_ICON, KIND_LABEL } from "./ui";

export function SplitView() {
  const cv = useEditorStore((s) => s.cv)!;
  const st = useEditorStore();
  const selectedId = useEditorStore((s) => s.selectedId);

  return (
    <div className="flex h-full">
      <aside className="w-[280px] shrink-0 border-r border-cv-border bg-cv-subtle px-3.5 py-4 overflow-y-auto">
        <div className="font-geist-mono text-[10.5px] tracking-widest text-cv-ink3 mb-2 px-1">
          OUTLINE
        </div>
        <div className="space-y-1">
          {cv.sections.map((s, i) => {
            const Icon = KIND_ICON[s.kind];
            const sel = selectedId === s.id;
            const count = s.entries.length || s.items.length || (s.text ? 1 : 0);
            return (
              <div
                key={s.id}
                onClick={() => st.setSelected(s.id)}
                className={`group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors ${
                  sel
                    ? "bg-white border border-cv-accent-border shadow-sm"
                    : "border border-transparent hover:bg-cv-sunk"
                }`}
              >
                <Icon className={`w-4 h-4 shrink-0 ${sel ? "text-cv-accent" : "text-cv-ink3"}`} />
                <span className="flex-1 min-w-0">
                  <span className="block truncate text-sm text-cv-ink">{s.name || "Untitled"}</span>
                  <span className="block font-geist-mono text-[10px] text-cv-ink3">
                    {KIND_LABEL[s.kind].toLowerCase()} · {count}
                  </span>
                </span>
                <span className="flex opacity-0 group-hover:opacity-100 transition-opacity">
                  <button
                    type="button"
                    title="Move up"
                    disabled={i === 0}
                    onClick={(e) => {
                      e.stopPropagation();
                      st.moveSection(s.id, -1);
                    }}
                    className="p-0.5 text-cv-ink3 hover:text-cv-ink2 disabled:opacity-30"
                  >
                    <IconChevUp className="w-3.5 h-3.5" />
                  </button>
                  <button
                    type="button"
                    title="Move down"
                    disabled={i === cv.sections.length - 1}
                    onClick={(e) => {
                      e.stopPropagation();
                      st.moveSection(s.id, 1);
                    }}
                    className="p-0.5 text-cv-ink3 hover:text-cv-ink2 disabled:opacity-30"
                  >
                    <IconChevDown className="w-3.5 h-3.5" />
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      </aside>
      <div className="flex-1 overflow-y-auto">
        <PaperSheet selectable />
      </div>
    </div>
  );
}
