import { useStore } from "../store";
import { getCatalog, englishCatalog } from "./index";

// Every component rendering translatable chrome reads `language` from the global store via
// this hook, so a language change re-renders everywhere reactively — the same cross-cutting-
// signal shape as `lastBackendSwitch` in store.ts.
export function useT() {
  const language = useStore((s) => s.language);
  const catalog = getCatalog(language);

  return function t(key: string, vars?: Record<string, string | number>): string {
    let value = catalog?.[key] ?? englishCatalog[key] ?? key;
    if (vars) {
      for (const [name, replacement] of Object.entries(vars)) {
        value = value.split(`{${name}}`).join(String(replacement));
      }
    }
    return value;
  };
}
