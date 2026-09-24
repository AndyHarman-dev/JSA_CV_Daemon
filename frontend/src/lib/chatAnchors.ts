// Module-level registry of CV-editor-chat corner-trigger anchor DOM nodes, keyed by scope.
// Plain Map, not React state: DOM node identity is exactly the kind of thing React state
// should NOT hold (it never triggers a re-render on its own, and CvChatPanel only ever
// needs to read the current node imperatively, inside a rAF-throttled remeasure -- see the
// plan's Phase 5 "5c"). Populated by each editable unit's wrapper ref in BlocksView.tsx.
import type { ChatScope } from "../cvChatStore";

const anchors = new Map<string, HTMLElement>();

export function scopeKeyOf(scope: ChatScope | null | undefined): string | null {
  if (!scope) return null;
  switch (scope.type) {
    case "cv":
      return "cv";
    case "contact":
      return "contact";
    case "section":
      return scope.sectionId ? `section:${scope.sectionId}` : null;
    case "entry":
      return scope.sectionId && scope.entryId ? `entry:${scope.sectionId}:${scope.entryId}` : null;
  }
}

// Ref-callback factory: `ref={chatAnchorRef(scope)}` on a unit's `.cvunit` wrapper.
// React calls the returned callback with `null` on unmount, which is what makes an
// entry's own reorder/removal (a new element, or none) never leave a stale node behind.
export function chatAnchorRef(scope: ChatScope) {
  const key = scopeKeyOf(scope);
  return (el: HTMLElement | null) => {
    if (!key) return;
    if (el) anchors.set(key, el);
    else anchors.delete(key);
  };
}

export function getAnchor(scope: ChatScope | null): HTMLElement | null {
  const key = scopeKeyOf(scope);
  return key ? anchors.get(key) ?? null : null;
}
