import type {
  JobDTO,
  FullJobDTO,
  Stage,
  CVDocument,
  TranscriptTurn,
  CvDeckDTO,
  PromptInjectionDTO,
  InjectionPresetDTO,
} from "./types";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`HTTP ${response.status}: ${body}`);
  }
  // 204 No Content (DELETE /api/cv-decks/{id}) has an empty body, and `Response.json()`
  // rejects on that — which would turn a *successful* delete into a thrown SyntaxError and
  // skip everything the caller does afterwards (re-listing the decks, re-homing the active
  // deck). Any other empty 2xx body would fail the same way, so key off the absence of a
  // body rather than off the one status code we currently emit.
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const api = {
  getJobs(state?: string): Promise<JobDTO[]> {
    const url = state ? `/api/jobs?state=${encodeURIComponent(state)}` : "/api/jobs";
    return apiFetch<JobDTO[]>(url);
  },

  getJob(id: string): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}`);
  },

  getTranscript(id: string): Promise<TranscriptTurn[]> {
    return apiFetch<TranscriptTurn[]>(`/api/jobs/${encodeURIComponent(id)}/transcript`);
  },

  answerFollowUp(id: string, follow_up_id: number, text: string): Promise<JobDTO> {
    return apiFetch<JobDTO>(`/api/jobs/${encodeURIComponent(id)}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ follow_up_id, text }),
    });
  },

  approve(id: string): Promise<{ cv_pdf_path: string; cl_pdf_path: string }> {
    return apiFetch<{ cv_pdf_path: string; cl_pdf_path: string }>(
      `/api/jobs/${encodeURIComponent(id)}/approve`,
      { method: "POST" }
    );
  },

  approveCv(id: string): Promise<{ pdf_path: string; docx_path: string }> {
    return apiFetch<{ pdf_path: string; docx_path: string }>(
      `/api/jobs/${encodeURIComponent(id)}/approve-cv`,
      { method: "POST" }
    );
  },

  // Assign/clear a job's base-CV deck (pre-launch only — the server 409s otherwise).
  putJobBaseCv(id: string, deckId: string | null): Promise<JobDTO> {
    return apiFetch<JobDTO>(`/api/jobs/${encodeURIComponent(id)}/base-cv`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ deck_id: deckId }),
    });
  },

  revise(id: string, target: "cv" | "cl", text: string): Promise<JobDTO> {
    return apiFetch<JobDTO>(`/api/jobs/${encodeURIComponent(id)}/revise`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, text }),
    });
  },

  reset(id: string): Promise<JobDTO> {
    return apiFetch<JobDTO>(`/api/jobs/${encodeURIComponent(id)}/reset`, {
      method: "POST",
    });
  },

  dismiss(id: string): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}/dismiss`, {
      method: "POST",
    });
  },

  ignoreFit(id: string): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}/ignore-fit`, {
      method: "POST",
    });
  },

  cancel(id: string): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    });
  },

  launch(id: string): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}/launch`, {
      method: "POST",
    });
  },

  // Attach (or clear) a job's pre-launch prompt overrides. Server-gated on `queued` (400
  // otherwise) and returns the full job row, so the caller upserts the response rather than
  // writing optimistically. The body model is extra="forbid" — send exactly these three
  // fields, never a spread preset (its id/name/saved_at would 422).
  putJobInjection(id: string, body: PromptInjectionDTO): Promise<FullJobDTO> {
    return apiFetch<FullJobDTO>(`/api/jobs/${encodeURIComponent(id)}/injection`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  launchAll(): Promise<{ launched: string[]; count: number }> {
    return apiFetch<{ launched: string[]; count: number }>(`/api/jobs/launch-all`, {
      method: "POST",
    });
  },

  deleteJob(id: string): Promise<{ ok: boolean }> {
    return apiFetch<{ ok: boolean }>(`/api/jobs/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  getDocument(
    id: string,
    stage: Stage,
    version?: number
  ): Promise<{ markdown: string; version: number }> {
    const base = `/api/jobs/${encodeURIComponent(id)}/document/${encodeURIComponent(stage)}`;
    const url = version !== undefined ? `${base}?version=${version}` : base;
    return apiFetch<{ markdown: string; version: number }>(url);
  },

  exportJob(
    id: string,
    format: "pdf" | "docx"
  ): Promise<{ cv_path?: string; cl_path?: string }> {
    // Backend (jsa/api/routes_jobs.py::export_job) only sets each key when that
    // stage's Document exists — a cv_review-only job's export omits cl_path
    // entirely. Both keys are optional here to match; do not widen back to
    // required without also changing the backend to always emit both.
    return apiFetch<{ cv_path?: string; cl_path?: string }>(
      `/api/jobs/${encodeURIComponent(id)}/export`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ format }),
      }
    );
  },

  // --- CV Structure Editor (standalone base CV; job-less) ---

  // GET the saved base CV, or null if none has been saved yet (server 404 → empty state).
  async getCvStructure(): Promise<CVDocument | null> {
    const response = await fetch("/api/cv-structure");
    if (response.status === 404) return null;
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }
    const body = (await response.json()) as { structured: CVDocument };
    return body.structured;
  },

  // PUT (validate + persist) the edited base CV. Surfaces the server's 422 reason on failure.
  async saveCvStructure(cv: CVDocument): Promise<CVDocument> {
    const body = await apiFetch<{ structured: CVDocument }>("/api/cv-structure", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ structured: cv }),
    });
    return body.structured;
  },

  // --- Base-CV decks (many base CVs; the CV Structure Editor's rail) ---
  //
  // The /api/cv-structure pair above stays as a default-deck alias for pre-decks callers;
  // everything below addresses a deck explicitly by id.

  // The whole index in one read — deck metadata only, never any deck's CV content.
  async listCvDecks(): Promise<{ decks: CvDeckDTO[]; default_id: string | null }> {
    return apiFetch<{ decks: CvDeckDTO[]; default_id: string | null }>("/api/cv-decks");
  },

  // GET one deck's CV, or null when the slot exists but has no CV saved yet (server 404 →
  // the editor's empty state), mirroring getCvStructure.
  async getCvDeck(id: string): Promise<CVDocument | null> {
    const response = await fetch(`/api/cv-decks/${encodeURIComponent(id)}`);
    if (response.status === 404) return null;
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }
    const body = (await response.json()) as { structured: CVDocument };
    return body.structured;
  },

  // Register an empty slot. `name` null means "no custom name" — the rail then falls back
  // to the server's auto_title (the CV's contact name) for the label.
  async createCvDeck(name: string | null): Promise<CvDeckDTO> {
    const body = await apiFetch<{ deck: CvDeckDTO }>("/api/cv-decks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    return body.deck;
  },

  // PUT (validate + persist) a CV into one deck. Surfaces the server's 422 reason.
  async saveCvDeck(id: string, cv: CVDocument): Promise<CVDocument> {
    const body = await apiFetch<{ structured: CVDocument }>(
      `/api/cv-decks/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ structured: cv }),
      }
    );
    return body.structured;
  },

  // Metadata-only patch: rename (`name`) and/or promote to default (`is_default`).
  async patchCvDeck(
    id: string,
    patch: { name?: string | null; is_default?: boolean }
  ): Promise<CvDeckDTO> {
    const body = await apiFetch<{ deck: CvDeckDTO }>(
      `/api/cv-decks/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }
    );
    return body.deck;
  },

  // Server-side copy of the deck *file*. The caller must have flushed any live edits to
  // that deck first — see editorStore.duplicateDeck.
  async duplicateCvDeck(id: string, name: string | null): Promise<CvDeckDTO> {
    const body = await apiFetch<{ deck: CvDeckDTO }>(
      `/api/cv-decks/${encodeURIComponent(id)}/duplicate`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }
    );
    return body.deck;
  },

  async deleteCvDeck(id: string): Promise<void> {
    await apiFetch<void>(`/api/cv-decks/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // POST a CV file to infer a structure. Runs synchronously server-side (streams
  // `infer_progress` WS events meanwhile) and resolves with the inferred-but-unsaved CV.
  async inferCvStructure(file: File): Promise<{ task_id: string; structured: CVDocument }> {
    const form = new FormData();
    form.append("file", file);
    return apiFetch<{ task_id: string; structured: CVDocument }>(
      "/api/cv-structure/infer",
      { method: "POST", body: form }
    );
  },

  health(): Promise<{ ok: boolean }> {
    return apiFetch<{ ok: boolean }>("/api/health");
  },

  config(): Promise<Record<string, unknown>> {
    return apiFetch<Record<string, unknown>>("/api/config");
  },

  // --- Language preference (global, not per-job) ---

  getPreferences(): Promise<{ language: string }> {
    return apiFetch<{ language: string }>("/api/preferences");
  },

  putPreferences(language: string): Promise<{ language: string }> {
    return apiFetch<{ language: string }>("/api/preferences", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language }),
    });
  },

  // --- Per-backend runtime model selection (global, persisted; see jsa/api/routes_backend_models.py) ---

  getBackendModels(): Promise<{
    selected: Record<string, string>;
    supports_model_selection: Record<string, boolean>;
  }> {
    return apiFetch("/api/backend-models");
  },

  getBackendModelsFor(
    backend: string
  ): Promise<{ backend: string; models: string[]; selected: string | null; source: "live" | "catalog" }> {
    return apiFetch(`/api/backend-models/${encodeURIComponent(backend)}`);
  },

  putBackendModel(backend: string, model: string): Promise<{ backend: string; model: string }> {
    return apiFetch("/api/backend-models", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backend, model }),
    });
  },

  // --- Prompt-injection presets, a.k.a. "doses" (global, not per-job) ---
  // Both directions are enveloped as {presets: [...]} and the PUT is a whole-list replace
  // (add/delete/reorder are all just a new array) — a bare array is a 422.

  getInjectionPresets(): Promise<{ presets: InjectionPresetDTO[] }> {
    return apiFetch<{ presets: InjectionPresetDTO[] }>("/api/injection-presets");
  },

  putInjectionPresets(presets: InjectionPresetDTO[]): Promise<{ presets: InjectionPresetDTO[] }> {
    return apiFetch<{ presets: InjectionPresetDTO[] }>("/api/injection-presets", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ presets }),
    });
  },
};
