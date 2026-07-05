import "@testing-library/jest-dom/vitest";

// vitest-environment-jsdom copies jsdom's `window.localStorage` accessor onto `globalThis`
// by value at environment setup time; on this Node/jsdom combination that copy resolves to
// `undefined` (a realm/brand-check mismatch between Node's own experimental global
// `localStorage` and jsdom's Storage getter — confirmed jsdom's Storage works fine in
// isolation). Rather than depend on that interaction, give tests a minimal in-memory
// Storage polyfill so code that reads the ambient `localStorage` global (e.g.
// scratchStore.ts) has something real to talk to. Production code is unaffected — real
// browsers provide `localStorage` natively.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

Object.defineProperty(globalThis, "localStorage", {
  value: new MemoryStorage(),
  configurable: true,
  writable: true,
});
