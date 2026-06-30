// Right-side read-only drawer showing the live exported JSON (exportJson → schema shape)
// with lightweight syntax highlighting. Updates on every edit; copy + close controls.
import { useState } from "react";
import { exportJson, useEditorStore } from "../../editorStore";
import { IconBraces, IconCopy, IconX } from "./ui";

// Highlight a JSON string by wrapping tokens in <span> with palette classes. Operates on the
// already-escaped pretty-printed text; regex token classes mirror the design's JSON colors.
function highlight(json: string): string {
  const esc = json
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return esc.replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (match) => {
      let cls = "text-cv-json-number";
      if (/^"/.test(match)) {
        cls = /:$/.test(match) ? "text-cv-accent" : "text-cv-json-string";
      } else if (/true|false|null/.test(match)) {
        cls = "text-cv-json-bool";
      }
      return `<span class="${cls}">${match}</span>`;
    }
  );
}

export function JsonDrawer() {
  const cv = useEditorStore((s) => s.cv);
  const toggleJson = useEditorStore((s) => s.toggleJson);
  const [copied, setCopied] = useState(false);
  if (!cv) return null;

  const text = JSON.stringify(exportJson(cv), null, 2);

  return (
    <aside className="w-[400px] shrink-0 border-l border-cv-border bg-[#FBFAF7] flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-cv-border">
        <IconBraces className="w-4 h-4 text-cv-ink2" />
        <span className="text-sm font-medium text-cv-ink">Source JSON</span>
        <span className="flex items-center gap-1 font-geist-mono text-[10px] text-cv-ink3 ml-1">
          <span className="w-1.5 h-1.5 rounded-full bg-[#3E9A6B]" /> live · read-only
        </span>
        <span className="flex-1" />
        <button
          type="button"
          title="Copy JSON"
          onClick={() => {
            void navigator.clipboard?.writeText(text);
            setCopied(true);
            setTimeout(() => setCopied(false), 1200);
          }}
          className="p-1 rounded text-cv-ink3 hover:text-cv-ink2 hover:bg-cv-sunk"
        >
          <IconCopy className="w-4 h-4" />
        </button>
        <button
          type="button"
          title="Close"
          onClick={toggleJson}
          className="p-1 rounded text-cv-ink3 hover:text-cv-ink2 hover:bg-cv-sunk"
        >
          <IconX className="w-4 h-4" />
        </button>
      </div>
      {copied && <div className="px-4 py-1 text-[11px] text-cv-accent">Copied</div>}
      <pre
        className="flex-1 overflow-auto px-4 py-3 font-geist-mono text-[12px] leading-relaxed text-cv-ink whitespace-pre"
        style={{ tabSize: 2 }}
        dangerouslySetInnerHTML={{ __html: highlight(text) }}
      />
    </aside>
  );
}
