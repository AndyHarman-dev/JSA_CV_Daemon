import { describe, it, expect, beforeEach } from "vitest";
import { useScratchStore, guessTag, timeLabel, sortEntries, type ScratchEntry } from "../scratchStore";

const STORAGE_KEY = "jsa_scratch_entries";

beforeEach(() => {
  localStorage.clear();
  useScratchStore.setState({ entries: [] });
});

describe("guessTag", () => {
  it("uses a leading #word literally", () => {
    expect(guessTag("#custom tag body")).toBe("#custom");
  });
  it("infers #salary from salary/comp keywords", () => {
    expect(guessTag("Salary floor: $190k")).toBe("#salary");
    expect(guessTag("total comp target")).toBe("#salary");
  });
  it("infers #visa from visa/sponsor keywords", () => {
    expect(guessTag("Visa status: not required")).toBe("#visa");
    expect(guessTag("needs sponsorship")).toBe("#visa");
  });
  it("infers #notice from notice keyword", () => {
    expect(guessTag("Notice period: 4 weeks")).toBe("#notice");
  });
  it("falls back to #note", () => {
    expect(guessTag("just a random blurb")).toBe("#note");
  });
});

describe("timeLabel", () => {
  const now = 1_000_000_000;
  it("shows minutes under 1h, minimum 1", () => {
    expect(timeLabel(now - 1000, now)).toBe("1m"); // rounds up from a few seconds
    expect(timeLabel(now - 5 * 60_000, now)).toBe("5m");
  });
  it("shows hours between 1h and 24h", () => {
    expect(timeLabel(now - 2 * 3_600_000, now)).toBe("2h");
  });
  it("shows days at 24h and beyond", () => {
    expect(timeLabel(now - 25 * 3_600_000, now)).toBe("1d");
    expect(timeLabel(now - 3 * 86_400_000, now)).toBe("3d");
  });
});

describe("sortEntries", () => {
  it("sorts pinned first, then most-recent-first within each group", () => {
    const entries: ScratchEntry[] = [
      { id: "a", text: "a", tag: "#note", pinned: false, ts: 100 },
      { id: "b", text: "b", tag: "#note", pinned: true, ts: 50 },
      { id: "c", text: "c", tag: "#note", pinned: false, ts: 200 },
      { id: "d", text: "d", tag: "#note", pinned: true, ts: 150 },
    ];
    expect(sortEntries(entries).map((e) => e.id)).toEqual(["d", "b", "c", "a"]);
  });
  it("does not mutate the input array", () => {
    const entries: ScratchEntry[] = [
      { id: "a", text: "a", tag: "#note", pinned: false, ts: 1 },
      { id: "b", text: "b", tag: "#note", pinned: false, ts: 2 },
    ];
    const copy = [...entries];
    sortEntries(entries);
    expect(entries).toEqual(copy);
  });
});

describe("useScratchStore", () => {
  it("starts empty when localStorage has no entries — no seed data", () => {
    useScratchStore.getState().load();
    expect(useScratchStore.getState().entries).toEqual([]);
  });

  it("load() reads a previously persisted array", () => {
    const stored: ScratchEntry[] = [{ id: "e1", text: "hi", tag: "#note", pinned: false, ts: 1 }];
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
    useScratchStore.getState().load();
    expect(useScratchStore.getState().entries).toEqual(stored);
  });

  it("load() recovers to empty on corrupt JSON", () => {
    localStorage.setItem(STORAGE_KEY, "{not json");
    useScratchStore.getState().load();
    expect(useScratchStore.getState().entries).toEqual([]);
  });

  it("addEntry trims, tags, prepends, and persists", () => {
    useScratchStore.getState().addEntry("  Notice period: 4 weeks  ");
    const entries = useScratchStore.getState().entries;
    expect(entries).toHaveLength(1);
    expect(entries[0]).toMatchObject({ text: "Notice period: 4 weeks", tag: "#notice", pinned: false });
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(entries);

    useScratchStore.getState().addEntry("second note");
    expect(useScratchStore.getState().entries.map((e) => e.text)).toEqual([
      "second note",
      "Notice period: 4 weeks",
    ]);
  });

  it("addEntry ignores empty/whitespace-only text", () => {
    useScratchStore.getState().addEntry("   ");
    expect(useScratchStore.getState().entries).toEqual([]);
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it("togglePin flips the flag and persists", () => {
    useScratchStore.getState().addEntry("pin me");
    const id = useScratchStore.getState().entries[0].id;
    useScratchStore.getState().togglePin(id);
    expect(useScratchStore.getState().entries[0].pinned).toBe(true);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)[0].pinned).toBe(true);

    useScratchStore.getState().togglePin(id);
    expect(useScratchStore.getState().entries[0].pinned).toBe(false);
  });

  it("deleteEntry removes immediately and persists", () => {
    useScratchStore.getState().addEntry("keep");
    useScratchStore.getState().addEntry("delete me");
    const toDelete = useScratchStore.getState().entries.find((e) => e.text === "delete me")!;
    useScratchStore.getState().deleteEntry(toDelete.id);
    const entries = useScratchStore.getState().entries;
    expect(entries.map((e) => e.text)).toEqual(["keep"]);
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!)).toEqual(entries);
  });
});
