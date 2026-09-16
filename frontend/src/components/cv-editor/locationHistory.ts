// Recently-used location suggestions for the paper preview's contact/entry location fields,
// offered through a shared <datalist>. Ported from the reference's loadLocations/
// recordLocation/allLocations (~/Desktop/cv-editor.html:278-298): a plain localStorage list of
// freeform strings the user has typed before, merged with every location already present in
// the loaded CV so suggestions include the CV's own data even on a fresh browser profile.
import { useState } from "react";
import type { EditorCV } from "../../types";

const STORAGE_KEY = "jsa.cvEditor.recentLocations";

function loadRecent(): string[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function saveRecent(list: string[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(list));
  } catch {
    // localStorage unavailable (private mode, quota) — suggestions just don't persist
  }
}

export function useRecentLocations(): { recent: string[]; record: (v: string) => void } {
  const [recent, setRecent] = useState<string[]>(loadRecent);
  const record = (v: string) => {
    const trimmed = v.trim();
    if (!trimmed || recent.includes(trimmed)) return;
    const next = [...recent, trimmed].slice(-200);
    setRecent(next);
    saveRecent(next);
  };
  return { recent, record };
}

export function allLocations(cv: EditorCV, recent: string[]): string[] {
  const set = new Set(recent);
  if (cv.contact.location) set.add(cv.contact.location);
  cv.sections.forEach((s) =>
    s.entries.forEach((e) => {
      if (e.location) set.add(e.location);
    })
  );
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}
