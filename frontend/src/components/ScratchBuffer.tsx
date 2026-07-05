// Scratch Buffer — a persistent, cross-job floating notes window. Framed as a daemon
// process surface (SCRATCH_BUFFER) consistent with the app's terminal/HUD visual language.
// Mounted once, globally, in App.tsx — visible regardless of which job is selected.
//
// Two independent triggers: the parked orb (bottom-right, always visible; click always
// opens) and Cmd/Ctrl+Space (global, from anywhere; toggles open/closed). Esc and the
// header's minimize button close the window without discarding notes — entries persist via
// scratchStore (localStorage); draft text and window position are session-only local state.
//
// Ported from the design handoff (.claude/designs/Scratch Buffer Design.zip,
// design_handoff_scratch_buffer/, search SCRATCH_BUFFER), reusing the app's real theme
// tokens/icons instead of the reference's hardcoded hex values. See that README for the
// full behavior spec.
import { useEffect, useRef, useState } from "react";
import { SHELL_THEME } from "../theme/tokens";
import { chamferPath } from "../theme/chrome";
import { Icon, Grip } from "../theme/Icon";
import { useScratchStore, sortEntries, timeLabel, type ScratchEntry } from "../scratchStore";

const T = SHELL_THEME;
const WIN_WIDTH = 288;
// Keep at least this much of the window on-screen when dragged toward an edge — extends the
// reference prototype's left/top-only clamp to all four edges (README ask).
const MIN_VISIBLE = 40;

interface Pos {
  x: number | null;
  y: number;
}

function clampPos(x: number, y: number): Pos {
  return {
    x: Math.min(Math.max(8, x), window.innerWidth - MIN_VISIBLE),
    y: Math.min(Math.max(8, y), window.innerHeight - MIN_VISIBLE),
  };
}

export function ScratchBuffer() {
  const entries = useScratchStore((s) => s.entries);
  const load = useScratchStore((s) => s.load);
  const addEntry = useScratchStore((s) => s.addEntry);
  const togglePin = useScratchStore((s) => s.togglePin);
  const deleteEntry = useScratchStore((s) => s.deleteEntry);

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [pos, setPos] = useState<Pos>({ x: null, y: 78 });
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Default the window's initial position (top-right, ~32px margin) the first time it opens,
  // regardless of which trigger opened it. After that, position is whatever the user dragged
  // it to (never reset while mounted).
  function placeIfUnset() {
    setPos((p) => (p.x === null ? clampPos(window.innerWidth - 320, p.y) : p));
  }

  // Global triggers, bound once. Cmd/Ctrl+Space toggles; Esc closes. Both use functional
  // state updates (no read of `open` from this closure) so the once-bound listener never
  // goes stale — reading `open` here directly would freeze at its initial `false` forever.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.code === "Space" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setOpen((wasOpen) => {
          const willOpen = !wasOpen;
          if (willOpen) placeIfUnset();
          return willOpen;
        });
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Autofocus the quick-capture input whenever the window opens, from either trigger.
  useEffect(() => {
    if (!open) return;
    const id = setTimeout(() => inputRef.current?.focus(), 30);
    return () => clearTimeout(id);
  }, [open]);

  // Clean up an in-flight drag if the component ever unmounts mid-drag.
  useEffect(() => {
    return () => dragCleanupRef.current?.();
  }, []);

  function openWindow() {
    placeIfUnset();
    setOpen(true);
  }

  function commitDraft() {
    const text = draft.trim();
    if (!text) return;
    addEntry(text);
    setDraft("");
  }

  function startDrag(e: React.MouseEvent<HTMLDivElement>) {
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = pos;
    setDragging(true);
    function onMove(ev: MouseEvent) {
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      setPos(clampPos((orig.x ?? 0) + dx, orig.y + dy));
    }
    function onUp() {
      setDragging(false);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      dragCleanupRef.current = null;
    }
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    dragCleanupRef.current = onUp;
  }

  const sorted = sortEntries(entries);
  const count = entries.length;

  return (
    <>
      <button
        type="button"
        onClick={openWindow}
        title="Scratch buffer (⌘ + Space)"
        style={{
          position: "fixed",
          right: 22,
          bottom: 22,
          zIndex: 70,
          width: 46,
          height: 46,
          borderRadius: T.chamfer ? 10 : 46,
          border: `1px solid ${T.aBorder}`,
          background: T.surface,
          color: T.a,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
          boxShadow: `0 6px 24px rgba(0,0,0,.5), 0 0 18px ${T.a}44`,
          padding: 0,
        }}
      >
        <Icon name="pin" size={18} />
        {count > 0 && (
          <span
            style={{
              position: "absolute",
              top: -4,
              right: -4,
              minWidth: 16,
              height: 16,
              padding: "0 4px",
              borderRadius: 8,
              background: T.a,
              color: "#06080B",
              font: `700 9px ${T.mono}`,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            {count}
          </span>
        )}
      </button>

      {open && (
        <div
          style={{
            position: "fixed",
            left: pos.x ?? window.innerWidth - 320,
            top: pos.y,
            zIndex: 70,
            width: WIN_WIDTH,
            maxHeight: "min(70vh, 460px)",
            display: "flex",
            flexDirection: "column",
            background: `color-mix(in srgb, ${T.surface} 94%, transparent)`,
            backdropFilter: "blur(8px)",
            border: `1px solid ${T.aBorder}`,
            borderRadius: T.chamfer ? 0 : 10,
            clipPath: T.chamfer ? chamferPath(12) : undefined,
            boxShadow: `0 24px 60px rgba(0,0,0,.6), 0 0 0 1px ${T.a}22`,
          }}
        >
          <div
            onMouseDown={startDrag}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px 10px",
              borderBottom: `1px solid ${T.bd}`,
              cursor: dragging ? "grabbing" : "grab",
              flex: "none",
              userSelect: "none",
            }}
          >
            <span style={{ color: T.ink3, display: "flex" }}>
              <Grip color={T.ink3} size={12} />
            </span>
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: 6,
                background: T.a,
                boxShadow: `0 0 6px ${T.a}`,
                animation: "jsblink 2.4s ease-in-out infinite",
                flex: "none",
              }}
            />
            <span
              style={{ font: `600 10.5px ${T.mono}`, letterSpacing: ".08em", color: T.ink, flex: 1 }}
            >
              SCRATCH_BUFFER
            </span>
            <span style={{ font: `400 9px ${T.mono}`, color: T.ink3 }}>{entries.length}</span>
            <button
              type="button"
              className="jbtn"
              onClick={() => setOpen(false)}
              title="Minimize"
              style={{
                border: "none",
                background: "transparent",
                color: T.ink3,
                cursor: "pointer",
                padding: 3,
                display: "flex",
              }}
            >
              <Icon name="minus" size={12} />
            </button>
          </div>

          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              padding: "8px 10px",
              borderBottom: `1px solid ${T.bd}`,
              background: T.sunk,
              flex: "none",
            }}
          >
            <span style={{ color: T.a, display: "flex" }}>
              <Icon name="plus" size={11} />
            </span>
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitDraft();
              }}
              placeholder="quick note… (Enter to log)"
              style={{
                flex: 1,
                border: "none",
                background: "transparent",
                outline: "none",
                font: `400 11.5px ${T.mono}`,
                color: T.ink,
              }}
            />
            <span
              style={{
                font: `500 8px ${T.mono}`,
                color: T.ink3,
                border: `1px solid ${T.bd2}`,
                borderRadius: 3,
                padding: "1px 5px",
                flex: "none",
              }}
            >
              ↵
            </span>
          </div>

          <div style={{ flex: 1, overflow: "auto", minHeight: 0 }}>
            {sorted.length ? (
              sorted.map((entry) => (
                <ScratchRow
                  key={entry.id}
                  entry={entry}
                  onTogglePin={togglePin}
                  onDelete={deleteEntry}
                />
              ))
            ) : (
              <div
                style={{
                  padding: "18px 12px",
                  font: `400 12px ${T.ui}`,
                  color: T.ink3,
                  fontStyle: "italic",
                  textAlign: "center",
                }}
              >
                No notes yet — type above.
              </div>
            )}
          </div>

          <div
            style={{
              padding: "5px 10px",
              borderTop: `1px solid ${T.bd}`,
              font: `400 9px ${T.mono}`,
              color: T.ink3,
              flex: "none",
            }}
          >
            Persists across all jobs · ⌘+Space to toggle
          </div>
        </div>
      )}
    </>
  );
}

function ScratchRow({
  entry,
  onTogglePin,
  onDelete,
}: {
  entry: ScratchEntry;
  onTogglePin: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div
      className="jrow"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "7px 10px",
        borderBottom: `1px solid ${T.bd}`,
      }}
    >
      <span
        style={{ font: `500 8.5px ${T.mono}`, color: T.ink3, flex: "none", width: 26, marginTop: 1 }}
      >
        {timeLabel(entry.ts)}
      </span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <span style={{ font: `500 9px ${T.mono}`, color: T.accent2, marginRight: 6 }}>
          {entry.tag}
        </span>
        <span style={{ font: `400 12px/1.45 ${T.ui}`, color: T.ink, wordBreak: "break-word" }}>
          {entry.text}
        </span>
      </div>
      <button
        type="button"
        className="jbtn"
        onClick={() => onTogglePin(entry.id)}
        title="Pin"
        style={{
          border: "none",
          background: "transparent",
          padding: 2,
          cursor: "pointer",
          color: entry.pinned ? T.a : T.ink3,
          flex: "none",
        }}
      >
        <Icon name="pin" size={12} />
      </button>
      <button
        type="button"
        className="jbtn"
        onClick={() => onDelete(entry.id)}
        title="Delete"
        style={{
          border: "none",
          background: "transparent",
          padding: 2,
          cursor: "pointer",
          color: T.ink3,
          flex: "none",
        }}
      >
        <Icon name="x" size={12} />
      </button>
    </div>
  );
}
