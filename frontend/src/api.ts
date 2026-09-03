import type { JobDTO, FullJobDTO, Stage, CVDocument, TranscriptTurn } from "./types";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`HTTP ${response.status}: ${body}`);
  }
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
};
