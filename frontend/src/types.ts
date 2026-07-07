export type JobState =
  | "queued" | "pending" | "running" | "awaiting_input" | "fit_done" | "unfit" | "cv_done"
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
  language: string | null;
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
  | { type: "backend_switched"; job_id: string; from_backend: string; to_backend: string }
  // CV Structure Editor — one-shot infer progress (job-less; keyed by a transient task_id).
  // Mirrors jsa/events/schema.py::InferProgressEvent exactly.
  | {
      type: "infer_progress";
      task_id: string;
      step: number;
      total: number;
      label: string;
      status: "active" | "done" | "error";
      message: string;
    };

// --- CV Structure Editor — the CVDocument schema (mirrors jsa/schema/cv.py) -------------
// The editor reads/writes exactly this shape. `kind`/`id` are UI-only and stripped on export.

export interface CVContact {
  name: string; // required, non-empty
  email?: string;
  phone?: string;
  location?: string;
  links: string[]; // linkedin / github / portfolio URLs
}

export interface CVEntry {
  heading?: string; // role / project / degree / award title
  subheading?: string; // company / institution / issuer
  dates?: string;
  location?: string;
  text?: string; // prose description for the entry
  bullets: string[];
  links: string[]; // repo / demo / portfolio URLs
}

export interface CVSection {
  name: string; // "Summary", "Experience", "Skills", …
  text?: string; // free prose (Summary)
  items: string[]; // flat keyword/bullet list
  entries: CVEntry[]; // structured sub-entries
}

export interface CVDocument {
  contact: CVContact;
  sections: CVSection[]; // min length 1, ordered, INTERCHANGEABLE
}

// Editor "kind" — a UI convenience that drives which editing surface to show. NOT a schema
// field (the backend serializer infers layout from section name + populated fields).
export type SectionKind =
  | "summary"
  | "bullets"
  | "skills"
  | "experience"
  | "projects"
  | "education";

// Editor-local shapes: the schema types plus a transient `id` (React keys / drag / selection)
// and, on a section, the inferred `kind`. Both are stripped by exportJson().
export interface EditorEntry extends CVEntry {
  id: string;
}

export interface EditorSection extends Omit<CVSection, "entries"> {
  id: string;
  kind: SectionKind;
  entries: EditorEntry[];
}

export interface EditorCV {
  contact: CVContact;
  sections: EditorSection[];
}

export interface FullJobDTO extends JobDTO {
  follow_ups: FollowUpDTO[];
  documents: DocumentDTO[];
}
