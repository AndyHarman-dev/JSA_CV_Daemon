// Full-screen CV Structure Editor shell. Toggled by `editorOpen` in the main store; reached
// from a Header entry. On mount it GETs the saved base CV (404 → empty state). Persists only
// on Done (PUT). View switcher / undo-redo / JSON toggle / infer all live in the top bar.
import { useEffect, useRef, useState } from "react";
import { api } from "../../api";
import { useEditorStore, type EditorView } from "../../editorStore";
import { useStore } from "../../store";
import { BlocksView } from "./BlocksView";
import { DocumentView } from "./PaperSheet";
import { JsonDrawer } from "./JsonDrawer";
import { SplitView } from "./SplitView";
import {
  IconBlocks,
  IconBraces,
  IconCheck,
  IconColumns,
  IconDoc,
  IconRedo,
  IconSpark,
  IconUndo,
} from "./ui";

const VIEWS: { id: EditorView; label: string; Icon: (p: { className?: string }) => JSX.Element }[] = [
  { id: "blocks", label: "Blocks", Icon: IconBlocks },
  { id: "document", label: "Document", Icon: IconDoc },
  { id: "split", label: "Split", Icon: IconColumns },
];

function FileButton({ label, primary }: { label: string; primary?: boolean }) {
  const inferFromFile = useEditorStore((s) => s.inferFromFile);
  const ref = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        type="button"
        onClick={() => ref.current?.click()}
        className={
          primary
            ? "inline-flex items-center gap-1.5 bg-cv-accent text-white rounded-lg px-3 py-1.5 text-sm font-medium shadow-[0_1px_2px_rgba(59,91,217,.45)] hover:brightness-105"
            : "inline-flex items-center gap-1.5 text-cv-ink2 hover:text-cv-accent rounded-lg px-3 py-1.5 text-sm"
        }
      >
        <IconSpark className="w-4 h-4" />
        {label}
      </button>
      <input
        ref={ref}
        type="file"
        accept=".pdf,.docx,.txt,.md"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void inferFromFile(f);
          e.target.value = "";
        }}
      />
    </>
  );
}

function EmptyState() {
  const startBlank = useEditorStore((s) => s.startBlank);
  return (
    <div className="flex items-center justify-center h-full px-6">
      <div className="max-w-[460px] text-center animate-cvfade">
        <div className="mx-auto w-[60px] h-[60px] rounded-2xl bg-cv-subtle border border-cv-border flex items-center justify-center text-cv-ink3">
          <IconDoc className="w-7 h-7" />
        </div>
        <h2 className="mt-4 font-geist font-semibold text-[21px] text-cv-ink">No structure yet</h2>
        <p className="mt-2 text-sm text-cv-ink2 leading-relaxed">
          Infer a structure from an existing CV, or start from a blank document. The JSON is the
          source of truth — every edit here writes straight to it when you click Done.
        </p>
        <div className="mt-5 flex items-center justify-center gap-2">
          <FileButton label="Infer from CV" primary />
          <button
            type="button"
            onClick={startBlank}
            className="text-cv-ink2 hover:text-cv-accent rounded-lg px-3 py-1.5 text-sm border border-cv-border"
          >
            Start blank
          </button>
        </div>
      </div>
    </div>
  );
}

const STEPS = [
  "Reading document",
  "Detecting section breaks",
  "Extracting entries & dates",
  "Structuring JSON",
  "Validating against schema",
];

function InferringState() {
  const { inferStep, inferTotal, inferLabel, inferError, inferFilename } = useEditorStore();
  const reset = useEditorStore((s) => s.reset);
  return (
    <div className="flex items-center justify-center h-full px-6">
      <div className="w-[420px] bg-cv-surface border border-cv-border rounded-2xl p-5 shadow-sm animate-cvfade">
        <div className="flex items-center gap-2.5">
          <span className="inline-flex items-center justify-center w-8 h-8 rounded-lg bg-cv-accent-soft text-cv-accent">
            <IconSpark className="w-4 h-4" />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium text-cv-ink">
              {inferError ? "Inference failed" : "Inferring structure"}
            </div>
            <div className="font-geist-mono text-[11px] text-cv-ink3 truncate">{inferFilename}</div>
          </div>
        </div>
        <div className="mt-4 h-1.5 rounded-full bg-cv-sunk overflow-hidden">
          <div
            className="h-full bg-cv-accent transition-[width] duration-300"
            style={{ width: `${Math.round((inferStep / inferTotal) * 100)}%` }}
          />
        </div>
        <ul className="mt-4 space-y-2">
          {STEPS.map((label, i) => {
            const n = i + 1;
            const done = n < inferStep || (n === inferTotal && inferStep === inferTotal && !inferError);
            const active = n === inferStep && !inferError;
            const errored = inferError && n === inferStep;
            return (
              <li key={label} className="flex items-center gap-2.5">
                <span
                  className={`w-[18px] h-[18px] rounded-full flex items-center justify-center text-white text-[10px] ${
                    errored
                      ? "bg-cv-danger"
                      : done
                      ? "bg-cv-accent"
                      : active
                      ? "border-2 border-cv-accent border-t-transparent animate-cvspin"
                      : "border border-cv-border"
                  }`}
                >
                  {done && !errored ? <IconCheck className="w-3 h-3" /> : null}
                </span>
                <span
                  className={`text-sm ${active ? "font-medium text-cv-ink" : done ? "text-cv-ink2" : "text-cv-ink3"}`}
                >
                  {inferLabel && (active || errored) ? inferLabel : label}
                </span>
              </li>
            );
          })}
        </ul>
        {inferError && (
          <div className="mt-3 text-xs text-cv-danger">
            {inferError}
            <button type="button" onClick={reset} className="ml-2 underline hover:no-underline">
              Try again
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function SegBtn({
  active,
  onClick,
  title,
  children,
}: {
  active?: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1 text-sm transition-colors ${
        active
          ? "bg-white shadow-sm font-semibold text-cv-ink"
          : "text-cv-ink2 hover:text-cv-ink"
      }`}
    >
      {children}
    </button>
  );
}

export function CvEditor() {
  const cv = useEditorStore((s) => s.cv);
  const view = useEditorStore((s) => s.view);
  const jsonOpen = useEditorStore((s) => s.jsonOpen);
  const inferring = useEditorStore((s) => s.inferring);
  const canUndo = useEditorStore((s) => s.canUndo);
  const canRedo = useEditorStore((s) => s.canRedo);
  const saving = useEditorStore((s) => s.saving);
  const saveError = useEditorStore((s) => s.saveError);
  const st = useEditorStore();
  const setEditorOpen = useStore((s) => s.setEditorOpen);
  const [loading, setLoading] = useState(true);

  // Load the saved structure once when the editor opens.
  useEffect(() => {
    let alive = true;
    api
      .getCvStructure()
      .then((saved) => {
        if (!alive) return;
        if (saved) st.load(saved);
        else st.reset(); // 404 → empty state
      })
      .catch(() => alive && st.reset())
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keyboard undo/redo.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;
      if (e.key.toLowerCase() === "z" && !e.shiftKey) {
        e.preventDefault();
        st.undo();
      } else if ((e.key.toLowerCase() === "z" && e.shiftKey) || e.key.toLowerCase() === "y") {
        e.preventDefault();
        st.redo();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function onDone() {
    if (!cv) {
      setEditorOpen(false);
      return;
    }
    const ok = await st.save();
    if (ok) setEditorOpen(false);
  }

  const sectionCount = cv?.sections.length ?? 0;
  const loaded = !!cv && !inferring;

  return (
    <div className="fixed inset-0 z-40 bg-cv-canvas font-geist flex flex-col">
      {/* Top bar */}
      <header
        className="flex items-center justify-between px-[18px] py-[11px] border-b border-cv-border z-20"
        style={{ background: "rgba(250,248,244,.85)", backdropFilter: "blur(10px)" }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <span className="inline-flex items-center justify-center w-[30px] h-[30px] rounded-lg bg-cv-accent text-white shadow-[0_1px_3px_rgba(59,91,217,.4)]">
            <IconBlocks className="w-4 h-4" />
          </span>
          <div className="min-w-0">
            <div className="font-geist font-semibold text-sm text-cv-ink tracking-tight">
              Structure Editor
            </div>
            <div className="font-geist-mono text-[10.5px] text-cv-ink3">CVDocument · live JSON</div>
          </div>
          {loaded && (
            <>
              <span className="h-6 w-px bg-cv-border mx-1" />
              <span className="truncate text-sm text-cv-ink2 max-w-[180px]">
                {cv!.contact.name || "Untitled"}
              </span>
              <span className="font-geist-mono text-[11px] text-cv-ink2 bg-cv-sunk rounded-md px-1.5 py-0.5">
                {sectionCount} section{sectionCount === 1 ? "" : "s"}
              </span>
            </>
          )}
        </div>

        <div className="flex items-center gap-2">
          {loaded && (
            <>
              <div className="flex items-center gap-0.5 bg-cv-sunk border border-cv-border rounded-xl p-[3px]">
                {VIEWS.map((v) => (
                  <SegBtn key={v.id} active={view === v.id} onClick={() => st.setView(v.id)} title={v.label}>
                    <v.Icon className="w-[15px] h-[15px]" />
                    <span className="hidden lg:inline">{v.label}</span>
                  </SegBtn>
                ))}
              </div>
              <div className="flex items-center gap-0.5 bg-cv-sunk border border-cv-border rounded-xl p-[3px]">
                <button
                  type="button"
                  title="Undo"
                  disabled={!canUndo}
                  onClick={st.undo}
                  className="p-1.5 rounded-lg text-cv-ink2 hover:text-cv-ink disabled:opacity-30"
                >
                  <IconUndo className="w-4 h-4" />
                </button>
                <button
                  type="button"
                  title="Redo"
                  disabled={!canRedo}
                  onClick={st.redo}
                  className="p-1.5 rounded-lg text-cv-ink2 hover:text-cv-ink disabled:opacity-30"
                >
                  <IconRedo className="w-4 h-4" />
                </button>
              </div>
              <button
                type="button"
                onClick={st.toggleJson}
                className="inline-flex items-center gap-1.5 text-cv-ink2 hover:text-cv-ink rounded-lg px-2.5 py-1.5 text-sm"
              >
                <IconBraces className="w-4 h-4" />
                <span className="hidden lg:inline">{jsonOpen ? "Hide JSON" : "View JSON"}</span>
              </button>
              <FileButton label="Re-infer" />
            </>
          )}
          <button
            type="button"
            onClick={() => void onDone()}
            disabled={saving}
            className="inline-flex items-center gap-1.5 bg-cv-accent text-white rounded-lg px-3 py-1.5 text-sm font-medium shadow-[0_1px_2px_rgba(59,91,217,.45)] hover:brightness-105 disabled:opacity-60"
          >
            <IconCheck className="w-4 h-4" />
            {saving ? "Saving…" : cv ? "Done" : "Close"}
          </button>
        </div>
      </header>

      {saveError && (
        <div className="px-4 py-2 bg-cv-danger/10 text-cv-danger text-sm border-b border-cv-border">
          Couldn’t save: {saveError}
        </div>
      )}

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        <main className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center h-full text-cv-ink3 text-sm">Loading…</div>
          ) : inferring ? (
            <InferringState />
          ) : !cv ? (
            <EmptyState />
          ) : view === "blocks" ? (
            <BlocksView />
          ) : view === "document" ? (
            <DocumentView />
          ) : (
            <SplitView />
          )}
        </main>
        {loaded && jsonOpen && <JsonDrawer />}
      </div>
    </div>
  );
}
