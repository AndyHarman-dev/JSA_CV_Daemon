// Pure date-range parse/format helpers for the paper entry-date picker. The CV schema keeps
// `dates` as one free-text string (e.g. "Jun 2018 — Present"), so these functions convert
// between that string and the {start, end, present} shape the month/year <select> pair edits.
// Ported from the reference (~/Desktop/cv-editor.html:154-192).
export const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function yearOptions(): number[] {
  const now = new Date().getFullYear();
  const out: number[] = [];
  for (let y = now + 3; y >= now - 55; y--) out.push(y);
  return out;
}

// Accepts "YYYY-MM", "YYYY", "Mon YYYY"/"Month YYYY", "MM/YYYY", "YYYY/MM" and normalizes to
// "YYYY-MM" (or "" if unparseable). Hand-rolled rather than `new Date(freeformString)`, which
// is implementation-defined and known to disagree between Chromium and WebKit on these shapes.
export function toMonthInput(raw: string | undefined | null): string {
  const s = (raw || "").trim();
  if (!s) return "";
  if (/^\d{4}-\d{2}$/.test(s)) return s;
  if (/^\d{4}$/.test(s)) return `${s}-01`;
  let m = s.match(/^([A-Za-z]{3,9})\.?,?\s+(\d{4})$/);
  if (m) {
    const idx = MONTHS.findIndex((mn) => mn.toLowerCase() === m![1].slice(0, 3).toLowerCase());
    if (idx >= 0) return `${m[2]}-${String(idx + 1).padStart(2, "0")}`;
  }
  m = s.match(/^(\d{1,2})[/.](\d{4})$/);
  if (m && Number(m[1]) >= 1 && Number(m[1]) <= 12) return `${m[2]}-${String(Number(m[1])).padStart(2, "0")}`;
  m = s.match(/^(\d{4})[/.](\d{1,2})$/);
  if (m && Number(m[2]) >= 1 && Number(m[2]) <= 12) return `${m[1]}-${String(Number(m[2])).padStart(2, "0")}`;
  return "";
}

export interface ParsedDateRange {
  start: string;
  end: string;
  present: boolean;
}

export function parseDateStr(str: string | undefined | null): ParsedDateRange {
  const s = (str || "").trim();
  if (!s) return { start: "", end: "", present: false };
  const parts = s.split(/\s*[—–-]\s*/);
  const endRaw = parts[1] || "";
  const present = /present/i.test(endRaw) || (parts.length === 1 && /present/i.test(parts[0]));
  return { start: toMonthInput(parts[0]), end: present ? "" : toMonthInput(endRaw), present };
}

export function monthName(ym: string): string {
  if (!ym || !/^\d{4}-\d{2}$/.test(ym)) return "";
  const [y, m] = ym.split("-");
  return new Date(Number(y), Number(m) - 1, 1).toLocaleString("en-US", { month: "short", year: "numeric" });
}

export function formatDateRange(start: string, end: string, present: boolean): string {
  const s = monthName(start);
  const e = present ? "Present" : monthName(end);
  if (!s && !e) return "";
  return [s, e].filter(Boolean).join(" — ");
}
