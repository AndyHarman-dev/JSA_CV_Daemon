export type JobState =
  | "pending" | "running" | "awaiting_input" | "cv_done"
  | "cl_done" | "review" | "approved" | "failed";

export type Stage =
  | "cv_adjust" | "cover_letter" | "revising_cv" | "revising_cl";

export interface JobDTO {
  id: string;
  company: string;
  role: string;
  link: string;
  tier: "A" | "B" | "C";
  state: JobState;
  current_stage: Stage | null;
  error: string | null;
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
}

export type WSEventType =
  | "status_changed" | "stage_complete" | "follow_up_needed"
  | "log" | "error" | "approved";

export interface WSEvent {
  type: WSEventType;
  job_id: string;
  payload: Record<string, unknown>;
}

export interface LogEntry {
  job_id: string;
  level: "info" | "warn" | "error";
  text: string;
  ts: number; // Date.now() when received
}

export interface FullJobDTO extends JobDTO {
  follow_ups: FollowUpDTO[];
  documents: DocumentDTO[];
}
