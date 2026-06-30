// Shared primitives for the CV Structure Editor: inline-SVG icons (≈1.55 stroke, rounded)
// and auto-growing text inputs/areas (measure scrollHeight — not CSS field-sizing — for
// cross-browser support, per the design handoff).
import {
  forwardRef,
  useEffect,
  useLayoutEffect,
  useRef,
  type TextareaHTMLAttributes,
} from "react";
import type { SectionKind } from "../../types";

type IconProps = { className?: string };

function svg(path: React.ReactNode, extra?: { fill?: boolean }) {
  return function Icon({ className }: IconProps) {
    return (
      <svg
        className={className}
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill={extra?.fill ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.55"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        {path}
      </svg>
    );
  };
}

export const IconBlocks = svg(
  <>
    <rect x="3" y="3" width="7" height="7" rx="1.5" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" />
    <rect x="14" y="14" width="7" height="7" rx="1.5" />
  </>
);
export const IconDoc = svg(
  <>
    <path d="M6 2h8l4 4v16H6z" />
    <path d="M14 2v4h4" />
    <path d="M9 12h6M9 16h6" />
  </>
);
export const IconColumns = svg(
  <>
    <rect x="3" y="4" width="7" height="16" rx="1.5" />
    <rect x="14" y="4" width="7" height="16" rx="1.5" />
  </>
);
export const IconUndo = svg(
  <>
    <path d="M9 7L4 12l5 5" />
    <path d="M4 12h11a5 5 0 0 1 0 10h-3" />
  </>
);
export const IconRedo = svg(
  <>
    <path d="M15 7l5 5-5 5" />
    <path d="M20 12H9a5 5 0 0 0 0 10h3" />
  </>
);
export const IconBraces = svg(
  <>
    <path d="M8 3c-2 0-2 2-2 4s0 3-2 5c2 2 2 3 2 5s0 4 2 4" />
    <path d="M16 3c2 0 2 2 2 4s0 3 2 5c-2 2-2 3-2 5s0 4-2 4" />
  </>
);
export const IconCheck = svg(<path d="M4 12l5 5L20 6" />);
export const IconPlus = svg(
  <>
    <path d="M12 5v14M5 12h14" />
  </>
);
export const IconX = svg(<path d="M6 6l12 12M18 6L6 18" />);
export const IconTrash = svg(
  <>
    <path d="M4 7h16" />
    <path d="M9 7V4h6v3" />
    <path d="M6 7l1 13h10l1-13" />
  </>
);
export const IconChevUp = svg(<path d="M6 15l6-6 6 6" />);
export const IconChevDown = svg(<path d="M6 9l6 6 6-6" />);
export const IconGrip = svg(
  <>
    <circle cx="9" cy="6" r="1.3" fill="currentColor" stroke="none" />
    <circle cx="15" cy="6" r="1.3" fill="currentColor" stroke="none" />
    <circle cx="9" cy="12" r="1.3" fill="currentColor" stroke="none" />
    <circle cx="15" cy="12" r="1.3" fill="currentColor" stroke="none" />
    <circle cx="9" cy="18" r="1.3" fill="currentColor" stroke="none" />
    <circle cx="15" cy="18" r="1.3" fill="currentColor" stroke="none" />
  </>
);
export const IconSpark = svg(
  <>
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z" />
  </>
);
export const IconLink = svg(
  <>
    <path d="M10 14a4 4 0 0 0 6 0l2-2a4 4 0 0 0-6-6l-1 1" />
    <path d="M14 10a4 4 0 0 0-6 0l-2 2a4 4 0 0 0 6 6l1-1" />
  </>
);
export const IconCopy = svg(
  <>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h8" />
  </>
);

// Kind glyphs.
const IconText = svg(<path d="M5 6h14M5 12h14M5 18h9" />);
const IconList = svg(
  <>
    <path d="M9 6h11M9 12h11M9 18h11" />
    <circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none" />
    <circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none" />
  </>
);
const IconTag = svg(
  <>
    <path d="M3 12l9-9 9 9-9 9z" />
    <circle cx="9" cy="9" r="1.3" fill="currentColor" stroke="none" />
  </>
);
const IconWork = svg(
  <>
    <rect x="3" y="7" width="18" height="13" rx="2" />
    <path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
  </>
);
const IconCap = svg(
  <>
    <path d="M2 9l10-4 10 4-10 4z" />
    <path d="M6 11v4c0 1 3 2 6 2s6-1 6-2v-4" />
  </>
);

export const KIND_ICON: Record<SectionKind, (p: IconProps) => JSX.Element> = {
  summary: IconText,
  bullets: IconList,
  skills: IconTag,
  experience: IconWork,
  projects: IconBlocks,
  education: IconCap,
};

export const KIND_LABEL: Record<SectionKind, string> = {
  summary: "Summary",
  bullets: "Bullets",
  skills: "Skills",
  experience: "Experience",
  projects: "Projects",
  education: "Education",
};

// --- auto-growing textarea --------------------------------------------------------------

type AutoTextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

export const AutoTextarea = forwardRef<HTMLTextAreaElement, AutoTextareaProps>(
  function AutoTextarea({ value, className, ...rest }, _ref) {
    const ref = useRef<HTMLTextAreaElement>(null);
    useLayoutEffect(() => {
      const el = ref.current;
      if (!el) return;
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    }, [value]);
    return (
      <textarea
        ref={ref}
        value={value}
        rows={1}
        className={`resize-none overflow-hidden ${className ?? ""}`}
        {...rest}
      />
    );
  }
);

// An <input> whose width tracks its content (for the Document/paper inline fields).
export function ContentInput({
  value,
  className,
  minCh = 4,
  ...rest
}: React.InputHTMLAttributes<HTMLInputElement> & { minCh?: number }) {
  const v = String(value ?? "");
  return (
    <input
      value={value}
      style={{ width: `${Math.max(minCh, v.length + 1)}ch` }}
      className={className}
      {...rest}
    />
  );
}

// Re-export a tiny hook so callers can refit a textarea on mount if needed.
export function useRefit(ref: React.RefObject<HTMLTextAreaElement>, dep: unknown) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [ref, dep]);
}
