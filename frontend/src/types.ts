export type JobState =
  | "pending" | "running" | "awaiting_input" | "fit_done" | "unfit" | "cv_done"
  | "cl_done" | "review" | "approved" | "failed" | "dismissed";

export type Stage =
  | "fit_assessment" | "cv_adjust" | "cover_letter" | "revising_cv" | "revising_cl";

export interface JobDTO {
  id: string;
  company: string;
  role: string;
  link: string;
  tier: "A" | "B" | "C";
  jd: string;
  state: JobState;
  current_stage: Stage | null;
  fit_reason: string | null;
  error: string | null;
  retry_count: number;
  updated_at: string;
  created_at: string;
}

export interface FollowUpDTO {
  id: number;
  job_id: string;
  stage: Stage;
  question: string;
  answer: string | null;
  asked_at: string;
  answered_at: string | null;
}

export interface DocumentDTO {
  id: number;
  job_id: string;
  stage: Stage;
  version: number;
  markdown: string;
  pdf_path: string | null;
  docx_path: string | null;
}

export type WSEvent =
  | { type: "status_changed"; job_id: string; from_state: string; to_state: string }
  | { type: "stage_complete"; job_id: string; stage: string }
  | { type: "follow_up_needed"; job_id: string; follow_up_id: number; question: string; stage: string }
  | { type: "log"; job_id: string; level: "info" | "warn" | "error"; text: string }
  | { type: "error"; job_id: string; message: string }
  | { type: "approved"; job_id: string; cv_pdf_path: string; cl_pdf_path: string }
  | { type: "job_removed"; job_id: string }
  | { type: "backend_switched"; job_id: string; from_backend: string; to_backend: string };

export interface FullJobDTO extends JobDTO {
  follow_ups: FollowUpDTO[];
  documents: DocumentDTO[];
}
