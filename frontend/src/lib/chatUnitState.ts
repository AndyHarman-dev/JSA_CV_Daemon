// Dim/ring visual state for one editable unit while the CV chat panel is open and scoped
// (design hand-off's "5f. Dimming, ring, connector"). Every other unit dims to opacity .3;
// the scoped unit gets an accent ring -- with one carve-out: an `entry` scope does NOT dim
// its own parent `section` (the section is still fully visible context, only its OTHER
// entries/fields dim).
import { useCvChatStore, type ChatScope } from "../cvChatStore";

function sameScope(a: ChatScope, b: ChatScope): boolean {
  if (a.type !== b.type) return false;
  if (a.type === "section") return a.sectionId === b.sectionId;
  if (a.type === "entry") return a.sectionId === b.sectionId && a.entryId === b.entryId;
  return true; // "cv" | "contact"
}

export interface UnitChatVisualState {
  active: boolean; // this exact unit is the open scope -- gets the accent ring
  dimmed: boolean; // some OTHER unit is scoped -- this one recedes
}

export function useUnitChatVisualState(unitScope: ChatScope): UnitChatVisualState {
  const open = useCvChatStore((s) => s.open);
  const scope = useCvChatStore((s) => s.scope);

  if (!open || !scope) return { active: false, dimmed: false };
  if (sameScope(scope, unitScope)) return { active: true, dimmed: false };
  if (scope.type === "cv") return { active: false, dimmed: true };
  if (scope.type === "entry" && unitScope.type === "section" && unitScope.sectionId === scope.sectionId) {
    // The scoped entry's own parent section stays undimmed -- it's still the visible
    // context the entry lives in, not a competing unit.
    return { active: false, dimmed: false };
  }
  return { active: false, dimmed: true };
}
