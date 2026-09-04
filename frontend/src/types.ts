export type JobState =
  | "queued" | "pending" | "running" | "awaiting_input" | "fit_done" | "unfit" | "cv_review"
  | "cv_done" | "cl_done" | "review" | "approved" | "failed" | "dismissed";

export type Stage =
  | "fit_assessment" | "cv_adjust" | "cover_letter" | "revising_cv" | "revising_cl";

// One row of the base-CV deck index (GET /api/cv-decks). `is_default` is server-computed
// against the index's `default_id`; the editor store also tracks `defaultDeckId` from the
// same response, and that store field — not this flag — is the deck rail's source of truth,
// so a local set-default reflects before the index round-trips.
export interface CvDeckDTO {
  id: string;
  name: string | null;
  auto_title: string | null;
  has_cv: boolean;
  is_default: boolean;
}

export interface JobDTO {
  id: string;
  company: string;
  role: string;
  link: string;
  tier: "A" | "B" | "C";
  jd: string;
  state: JobState;
  current_stage: Stage | null;
  backend_name: string | null;
  // Set only after the job has hopped at least once (model-first fallback ladder).
  model_name: string | null;
  // `model_name` if set, else the backend's currently-configured model. Display-only —
  // never fed back into the header's runtime-selection dropdown. See CLAUDE.md's
  // "Model ladder" section.
  effective_model: string | null;
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
  suggested_replies: string[] | null;
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
  // Invalidation hint only — no content. Mirrors jsa/events/schema.py::TranscriptChangedEvent.
  | { type: "transcript_changed"; job_id: string }
  // Model-first fallback ladder (Phase 4): a job hopped to the next model rung on the
  // SAME backend. Mirrors jsa/events/schema.py::ModelSwitchedEvent exactly.
  | { type: "model_switched"; job_id: string; backend: string; from_model: string; to_model: string }
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
    }
  // Phase 8 — streaming. A batched slice of in-progress model output, already coalesced
  // server-side (jsa/pipeline/streaming.py::ChunkAccumulator). Mirrors
  // jsa/events/schema.py::AgentChunkEvent exactly.
  | { type: "agent_chunk"; job_id: string; stage: string; kind: "content" | "reasoning"; text: string }
  // Marks the end of one streamed turn. superseded=true means discard the buffer outright
  // (a retry/nudge/self-heal path replayed the whole turn). Mirrors
  // jsa/events/schema.py::AgentTurnEndEvent exactly.
  | { type: "agent_turn_end"; job_id: string; stage: string; superseded: boolean };

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

// Mirrors jsa/api/transcript.py::build_transcript's per-turn dict shape exactly.
// One ordered, display-ready turn from GET /api/jobs/{id}/transcript.
export interface TranscriptTurn {
  seq: number;
  kind: "question" | "answer" | "delivery" | "verdict" | "plumbing";
  role: "user" | "assistant";
  stage: Stage | null;
  text: string;
  created_at: string | null;
  follow_up_id: number | null;
  suggested_replies: string[] | null;
  reasoning: string | null;
}
