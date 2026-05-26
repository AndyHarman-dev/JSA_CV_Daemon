import type { JobDTO, Stage } from "./types";

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

  getJob(id: string): Promise<JobDTO> {
    return apiFetch<JobDTO>(`/api/jobs/${encodeURIComponent(id)}`);
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

  getDocument(
    id: string,
    stage: Stage,
    version?: number
  ): Promise<{ markdown: string; version: number }> {
    const base = `/api/jobs/${encodeURIComponent(id)}/document/${encodeURIComponent(stage)}`;
    const url = version !== undefined ? `${base}?version=${version}` : base;
    return apiFetch<{ markdown: string; version: number }>(url);
  },
};
