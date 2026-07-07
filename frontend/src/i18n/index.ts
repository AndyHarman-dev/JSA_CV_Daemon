// Loads every `strings.<code>.json` catalog (the hand-maintained `en` source plus any
// LLM-generated locale files dropped in by `scripts/translate-ui.sh`) via Vite's glob import —
// new locale files are picked up automatically, no manual registration required.

export type Catalog = Record<string, string>;

const modules = import.meta.glob<{ default: Catalog }>("./strings.*.json", { eager: true });

const catalogs: Record<string, Catalog> = {};
for (const path in modules) {
  const match = /strings\.([a-z]{2})\.json$/.exec(path);
  if (match) {
    catalogs[match[1]] = modules[path].default;
  }
}

export function getCatalog(code: string): Catalog | undefined {
  return catalogs[code];
}

export const englishCatalog: Catalog = catalogs["en"] ?? {};
