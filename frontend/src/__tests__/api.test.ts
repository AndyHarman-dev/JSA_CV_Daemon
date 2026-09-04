import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { api } from "../api";

function makeResponse(
  data: unknown,
  { ok = true, status = 200 }: { ok?: boolean; status?: number } = {}
): Response {
  return {
    ok,
    status,
    json: async () => data,
    text: async () => (typeof data === "string" ? data : JSON.stringify(data)),
  } as unknown as Response;
}

beforeEach(() => {
  global.fetch = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getJobs", () => {
  it("fetches /api/jobs and returns parsed JSON", async () => {
    const jobs = [{ id: "abc", state: "pending" }];
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(jobs));

    const result = await api.getJobs();

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs", undefined);
    expect(result).toEqual(jobs);
  });

  it("fetches /api/jobs?state=pending when state filter is provided", async () => {
    vi.mocked(global.fetch).mockResolvedValue(makeResponse([]));

    await api.getJobs("pending");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs?state=pending", undefined);
  });

  it("URL-encodes special characters in the state filter", async () => {
    vi.mocked(global.fetch).mockResolvedValue(makeResponse([]));

    await api.getJobs("awaiting_input");

    // encodeURIComponent("awaiting_input") = "awaiting_input" (underscores are safe)
    expect(global.fetch).toHaveBeenCalledWith("/api/jobs?state=awaiting_input", undefined);
  });
});

describe("getJob", () => {
  it("fetches /api/jobs/{id} and returns parsed JSON", async () => {
    const job = { id: "abc123", state: "running" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(job));

    const result = await api.getJob("abc123");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc123", undefined);
    expect(result).toEqual(job);
  });

  it("URL-encodes the job id", async () => {
    vi.mocked(global.fetch).mockResolvedValue(makeResponse({}));

    await api.getJob("id with spaces");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/id%20with%20spaces", undefined);
  });
});

describe("answerFollowUp", () => {
  it("POSTs to /api/jobs/{id}/answer with correct body", async () => {
    const updatedJob = { id: "abc", state: "running" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(updatedJob));

    const result = await api.answerFollowUp("abc", 7, "my answer text");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ follow_up_id: 7, text: "my answer text" }),
    });
    expect(result).toEqual(updatedJob);
  });
});

describe("approve", () => {
  it("POSTs to /api/jobs/{id}/approve", async () => {
    const response = { cv_pdf_path: "/out/cv.pdf", cl_pdf_path: "/out/cl.pdf" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(response));

    const result = await api.approve("abc");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc/approve", {
      method: "POST",
    });
    expect(result).toEqual(response);
  });
});

describe("revise", () => {
  it("POSTs to /api/jobs/{id}/revise with target and text", async () => {
    const updatedJob = { id: "abc", state: "review" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(updatedJob));

    const result = await api.revise("abc", "cv", "make it shorter");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc/revise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "cv", text: "make it shorter" }),
    });
    expect(result).toEqual(updatedJob);
  });

  it("POSTs to /api/jobs/{id}/revise with target cl", async () => {
    const updatedJob = { id: "abc", state: "review" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(updatedJob));

    await api.revise("abc", "cl", "add more detail");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc/revise", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "cl", text: "add more detail" }),
    });
  });
});

describe("reset", () => {
  it("POSTs to /api/jobs/{id}/reset", async () => {
    const updatedJob = { id: "abc", state: "pending" };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(updatedJob));

    const result = await api.reset("abc");

    expect(global.fetch).toHaveBeenCalledWith("/api/jobs/abc/reset", {
      method: "POST",
    });
    expect(result).toEqual(updatedJob);
  });
});

describe("getDocument", () => {
  it("fetches /api/jobs/{id}/document/{stage} without version", async () => {
    const doc = { markdown: "# CV\n", version: 1 };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(doc));

    const result = await api.getDocument("abc", "cv_adjust");

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/jobs/abc/document/cv_adjust",
      undefined
    );
    expect(result).toEqual(doc);
  });

  it("appends ?version= when version is provided", async () => {
    const doc = { markdown: "# CV v2\n", version: 2 };
    vi.mocked(global.fetch).mockResolvedValue(makeResponse(doc));

    await api.getDocument("abc", "cover_letter", 2);

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/jobs/abc/document/cover_letter?version=2",
      undefined
    );
  });
});

describe("error handling", () => {
  it("throws an Error with status in message when response is not ok", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      makeResponse("Internal Server Error", { ok: false, status: 500 })
    );

    await expect(api.getJobs()).rejects.toThrow(/HTTP 500/);
  });

  it("throws an Error with 404 in message for not found responses", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      makeResponse("Not Found", { ok: false, status: 404 })
    );

    await expect(api.getJob("nonexistent")).rejects.toThrow(/HTTP 404/);
  });

  it("includes response body text in the error message", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      makeResponse("job not found", { ok: false, status: 404 })
    );

    await expect(api.getJob("xyz")).rejects.toThrow("job not found");
  });
});

describe("deleteCvDeck — 204 No Content", () => {
  // Regression: apiFetch used to call response.json() unconditionally. DELETE
  // /api/cv-decks/{id} answers 204 with an empty body, so a *successful* delete rejected
  // with a SyntaxError and every step after the await in editorStore.deleteDeck was
  // skipped — the rail kept a row for the deleted deck, activeDeckId was never re-homed,
  // and the user saw "Could not delete this base CV" for an operation that had worked.
  function empty204(): Response {
    return {
      ok: true,
      status: 204,
      json: async () => {
        throw new SyntaxError("Unexpected end of JSON input");
      },
      text: async () => "",
    } as unknown as Response;
  }

  it("resolves instead of throwing when the server returns 204 with no body", async () => {
    vi.mocked(global.fetch).mockResolvedValue(empty204());

    await expect(api.deleteCvDeck("a".repeat(32))).resolves.toBeUndefined();
    expect(global.fetch).toHaveBeenCalledWith(
      `/api/cv-decks/${"a".repeat(32)}`,
      { method: "DELETE" }
    );
  });

  it("still surfaces a real failure as a thrown HTTP error", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      makeResponse("nope", { ok: false, status: 400 })
    );

    await expect(api.deleteCvDeck("bad")).rejects.toThrow(/HTTP 400/);
  });
});
