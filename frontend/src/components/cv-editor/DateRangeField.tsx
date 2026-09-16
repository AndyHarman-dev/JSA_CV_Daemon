// Month/year date-range picker for an entry's `dates` field in the paper preview, replacing a
// free-text input with two month+year <select> pairs and a PRESENT toggle — same mechanism as
// the reference's monthYearSelect/dateRangeField (~/Desktop/cv-editor.html:193-223), paper
// variant only (this repo has no dark-chrome "blocks" date field to match, see the plan's scope
// note on why BlocksView.tsx keeps its free-text input).
import type { CSSProperties } from "react";
import { useT } from "../../i18n/useT";
import { Icon } from "../../theme/Icon";
import { paperT } from "../../theme/tokens";
import { formatDateRange, MONTHS, parseDateStr, yearOptions } from "./dateRange";

const PA = paperT;

const selectBase: CSSProperties = {
  font: `400 11px ${PA.mono}`,
  color: PA.ink2,
  border: "none",
  borderBottom: `1px solid ${PA.bd}`,
  outline: "none",
  background: "transparent",
  appearance: "none",
  WebkitAppearance: "none",
  cursor: "pointer",
  padding: "2px 12px 2px 1px",
  borderRadius: 0,
};

function MonthYearSelect({ ym, onChange }: { ym: string; onChange: (mm: string, yy: string) => void }) {
  const [y, m] = (ym || "").split("-");
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
      <span style={{ position: "relative", display: "inline-flex" }}>
        <select value={m || ""} onChange={(e) => onChange(e.target.value, y || "")} style={{ ...selectBase, width: 50 }}>
          <option value="">—</option>
          {MONTHS.map((mn, i) => (
            <option key={mn} value={String(i + 1).padStart(2, "0")}>
              {mn}
            </option>
          ))}
        </select>
        <span style={{ position: "absolute", right: 1, top: "50%", transform: "translateY(-50%)", pointerEvents: "none", color: PA.ink3, display: "flex" }}>
          <Icon name="down" size={8} />
        </span>
      </span>
      <span style={{ position: "relative", display: "inline-flex" }}>
        <select value={y || ""} onChange={(e) => onChange(m || "", e.target.value)} style={{ ...selectBase, width: 62 }}>
          <option value="">––––</option>
          {yearOptions().map((yr) => (
            <option key={yr} value={String(yr)}>
              {yr}
            </option>
          ))}
        </select>
        <span style={{ position: "absolute", right: 1, top: "50%", transform: "translateY(-50%)", pointerEvents: "none", color: PA.ink3, display: "flex" }}>
          <Icon name="down" size={8} />
        </span>
      </span>
    </span>
  );
}

export function DateRangeField({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const t = useT();
  const parsed = parseDateStr(value);
  const set = (patch: Partial<{ start: string; end: string; present: boolean }>) => {
    const next = { ...parsed, ...patch };
    onChange(formatDateRange(next.start, next.end, next.present));
  };
  const combine = (mm: string, yy: string) => (!mm && !yy ? "" : `${yy || new Date().getFullYear()}-${mm || "01"}`);

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
      <MonthYearSelect ym={parsed.start} onChange={(mm, yy) => set({ start: combine(mm, yy) })} />
      <span style={{ color: PA.ink3, font: `500 11px ${PA.mono}`, flex: "none" }}>—</span>
      {parsed.present ? (
        <button
          type="button"
          title={t("paperSheet.dateEditPresentTitle")}
          onClick={() => set({ present: false })}
          style={{
            font: `500 11.5px ${PA.mono}`,
            color: PA.a,
            letterSpacing: ".04em",
            padding: "2px 4px",
            border: "none",
            borderBottom: `1px dashed ${PA.bd}`,
            background: "transparent",
            borderRadius: 0,
            cursor: "pointer",
          }}
        >
          {t("paperSheet.datePresentLabel")}
        </button>
      ) : (
        <MonthYearSelect ym={parsed.end} onChange={(mm, yy) => set({ end: combine(mm, yy) })} />
      )}
      <button
        type="button"
        onClick={() => set({ present: !parsed.present, end: "" })}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          border: `1px solid ${parsed.present ? PA.a : PA.bd}`,
          background: parsed.present ? `color-mix(in srgb, ${PA.a} 12%, transparent)` : "transparent",
          color: parsed.present ? PA.a : PA.ink3,
          borderRadius: 4,
          padding: "3px 8px",
          font: `600 10px ${PA.mono}`,
          letterSpacing: ".06em",
          cursor: "pointer",
          flex: "none",
        }}
      >
        {t("paperSheet.datePresentLabel")}
      </button>
    </div>
  );
}
