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
  // How many jobs currently hold this deck (repo.DECK_LOCK_STATES). Non-zero => DELETE
  // answers 409 and the rail's trash icon is disabled.
  in_use_by: number;
}


// Per-job prompt overrides (jsa/schema/injection.py::PromptInjection). Server-normalized:
// the API returns either null or an object whose fields are already stripped, so "has an
// injection" is plain truthiness here — never a re-check for all-blank.
export interface PromptInjectionDTO {
  prefix: string;
  postfix: string;
  first_msg: string;
}

// One saved "dose" from the global preset library (jsa/store/injection_presets.py).
// `saved_at` is client-supplied ISO-8601, display only — the server never generates it.
export interface InjectionPresetDTO {
  id: string;
  name: string;
  prefix: string;
  postfix: string;
  first_msg: string;
  saved_at: string;
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
  // Null unless the user attached pre-launch prompt overrides (PUT .../injection).
  injection: PromptInjectionDTO | null;
  error: string | null;
  retry_count: number;
  updated_at: string;
  created_at: string;
  // Assigned base-CV deck id (GET /api/cv-decks), or null for "use the default deck".
  // Writable only pre-launch — PUT /api/jobs/{id}/base-cv 409s once state != "queued".
  base_cv_id: string | null;
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
  // One tool call's execution outcome, part of the revision-patching tool loop.
  // Published AFTER execution, so `status` is already known. Mirrors
  // jsa/events/schema.py::AgentToolEvent exactly.
  | {
      type: "agent_tool";
      job_id: string;
      stage: string;
      seq: number;
      call_id: string;
      name: string;
      summary: string;
      status: "ok" | "error" | "not_executed" | "budget_exhausted";
      detail: string;
    }
  // Marks the end of one streamed turn. superseded=true means discard the buffer outright
  // (a retry/nudge/self-heal path replayed the whole turn). Mirrors
  // jsa/events/schema.py::AgentTurnEndEvent exactly.
  | { type: "agent_turn_end"; job_id: string; stage: string; superseded: boolean }
  // CV-editor AI chat — job-less, keyed by a transient task_id (same single-flight
  // gating convention as infer_progress: the POST's own resolution — not task_id
  // correlation — is what ends the "busy" state client-side; see cvChatStore.ts).
  // Mirrors jsa/events/schema.py::ChatChunkEvent exactly.
  | { type: "chat_chunk"; task_id: string; kind: "content" | "reasoning"; text: string }
  // Mirrors jsa/events/schema.py::ChatTurnEndEvent exactly.
  | { type: "chat_turn_end"; task_id: string; superseded: boolean };

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

// --- CV-editor AI chat -------------------------------------------------------------------
// Mirrors jsa/store/deck_chats.py::ChatTurn exactly (the wire shape of GET/POST
// .../chat). `scope` here is the SERVER's index-based scope dict ({type, section_index?,
// entry_index?}) as persisted — not cvChatStore's client-id-based ChatScope, which exists
// only for UI addressing (highlight/anchor) and is converted to indices at send time.
export interface ChatDiffItemDTO {
  label: string;
  before: string;
  after: string;
}

export interface ChatTurnDTO {
  id: string;
  role: "user" | "agent" | "scope";
  scope: Record<string, unknown>;
  text: string;
  question: string | null;
  reasoning: string;
  items: ChatDiffItemDTO[];
  document: CVDocument | null;
  status: "pending" | "applied" | "auto" | "discarded" | "none" | "error";
  files: { name: string; size: number }[];
  base_hash: string;
  created_at: string;
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
  // Persisted tool-call marks for this turn, same shape as the live `agent_tool` WS
  // event once accumulated (see store.ts's streamBuffers). Populated by
  // jsa/api/transcript.py, which folds a revision turn's role="tool" Message rows into
  // the FOLLOWING assistant turn (always a `plumbing` turn — its text is the raw JSON
  // envelope). Null on every other turn, and the REASONING card degrades gracefully
  // (no tool rows) when it's absent.
  //
  // `at` is the mark's INDEX within its turn here, not a reasoning-buffer offset:
  // tool mode never streams, so a settled tool turn has no buffer to anchor against
  // and mergeToolSteps' trailing-append preserves exactly the persisted call order.
  tools?: { name: string; detail: string; ok: boolean; at: number }[] | null;
}
