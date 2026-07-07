// Shared primitives for the CV Structure Editor: kind metadata (icon/label/code) and
// auto-growing text inputs/areas (measure scrollHeight — not CSS field-sizing — for
// cross-browser support, per the design handoff). Icons themselves live in the shared
// theme icon set (../../theme/Icon) — this module no longer owns its own SVG factory.
import {
  forwardRef,
  useEffect,
  useLayoutEffect,
  useRef,
  type TextareaHTMLAttributes,
} from "react";
import type { IconName } from "../../theme/Icon";
import type { SectionKind } from "../../types";

export const KIND_ICON: Record<SectionKind, IconName> = {
  summary: "text",
  bullets: "list",
  skills: "tag",
  experience: "work",
  projects: "spark",
  education: "cap",
};

// Values are translation keys (not literal English), resolved by callers via `t(KIND_LABEL[k])`
// — this module is a plain constants/helpers file, not a component, and can't call `useT()`.
export const KIND_LABEL: Record<SectionKind, string> = {
  summary: "kindLabel.summary",
  bullets: "kindLabel.bullets",
  skills: "kindLabel.skills",
  experience: "kindLabel.experience",
  projects: "kindLabel.projects",
  education: "kindLabel.education",
};

// Short telemetry-style codes shown next to kind labels (e.g. in the inject-module menu).
export const KIND_CODE: Record<SectionKind, string> = {
  summary: "SUM",
  bullets: "LST",
  skills: "SKL",
  experience: "EXP",
  projects: "PRJ",
  education: "EDU",
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
