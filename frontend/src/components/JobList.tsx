import { useState } from "react";
import { useStore } from "../store";
import { api } from "../api";
import { StatusBadge } from "./StatusBadge";
import { LaunchButton } from "./LaunchButton";
import { InjectTrigger } from "./PromptInjector";
import type { JobDTO, JobState } from "../types";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase } from "../theme/chrome";
import { Icon } from "../theme/Icon";
import { toFileUrl, triggerDownload } from "../lib/downloadFile";

const T = SHELL_THEME;

const TIER_COLOR: Record<string, string> = { A: T.green, B: T.accent2, C: T.ink3 };

interface Group {
  code: string;
  labelKey: string;
  chipKey: string;
  states: JobState[];
}

// `labelKey` is a translation key, resolved by the component below via `t()`.
// "jobList.queued" is deliberately its own top-most group (not folded into "running")
// so fresh, never-launched jobs are visually distinct from ones already dispatching.
// `code` is a stable identifier for the filter-chip toggle state (queueFilters); `chipKey`
// is a separate, terser translation key for the filter chip itself, since the section
// header text (e.g. "Queued — Not Started") is too long to render as a chip.
const GROUPS: Group[] = [
  { code: "queued", labelKey: "jobList.queued", chipKey: "jobList.filterQueued", states: ["queued"] },
  { code: "inbox", labelKey: "jobList.inbox", chipKey: "jobList.filterInbox", states: ["awaiting_input"] },
  { code: "needsReview", labelKey: "jobList.needsReview", chipKey: "jobList.filterNeedsReview", states: ["unfit"] },
  {
    code: "running",
    labelKey: "jobList.running",
    chipKey: "jobList.filterRunning",
    states: ["running", "pending", "fit_done", "cv_done", "cl_done"],
  },
  { code: "review", labelKey: "jobList.review", chipKey: "jobList.filterReview", states: ["cv_review", "review"] },
  { code: "done", labelKey: "jobList.done", chipKey: "jobList.filterDone", states: ["approved"] },
  { code: "failed", labelKey: "jobList.failed", chipKey: "jobList.filterFailed", states: ["failed"] },
  { code: "dismissed", labelKey: "jobList.dismissed", chipKey: "jobList.filterDismissed", states: ["dismissed"] },
];

function JobRow({
  job,
  isSelected,
  onSelect,
}: {
  job: JobDTO;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const t = useT();
  return (
    <button
      type="button"
      className="jrow"
      onClick={onSelect}
      style={{
        position: "relative",
        display: "flex",
        flexDirection: "column",
        gap: 6,
        width: "100%",
        textAlign: "left",
        padding: "9px 11px",
        border: `1px solid ${isSelected ? T.aBorder : "transparent"}`,
        borderRadius: T.btnRadius,
        background: isSelected ? T.surface : "transparent",
        cursor: "pointer",
      }}
    >
      {isSelected && (
        <span
          style={{
            position: "absolute",
            left: 0,
            top: 7,
            bottom: 7,
            width: 2,
            background: T.a,
            boxShadow: `0 0 8px ${T.a}`,
          }}
        />
      )}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <span
          style={{
            font: `600 12.5px ${T.ui}`,
            color: T.ink,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {job.company}
        </span>
        <span style={{ font: `600 9.5px ${T.mono}`, color: TIER_COLOR[job.tier] ?? T.ink3, flex: "none" }}>
          T{job.tier}
        </span>
      </div>
      <div
        style={{
          font: `400 11.5px ${T.ui}`,
          color: T.ink2,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {job.role}
      </div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        {job.state === "queued" ? (
          // Span, not div — this whole row is a <button>, whose content model is
          // phrasing, so a <div> here is invalid HTML (React logs validateDOMNesting).
          // Order is the CV-decks design hand-off's own (cvTriggerBtn, injectTriggerBtn,
          // launchSlot); the syringe slots between the deck picker and LAUNCH.
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <BaseCvTrigger job={job} />
            <InjectTrigger job={job} />
            <LaunchButton jobId={job.id} />
          </span>
        ) : (
          <StatusBadge state={job.state} />
        )}
        {job.effective_model && (
          <span
            title={`${t("jobList.modelTitle")}: ${job.effective_model}`}
            style={{
              font: `400 9.5px ${T.mono}`,
              color: T.ink3,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
              maxWidth: "50%",
            }}
          >
            {job.effective_model}
          </span>
        )}
      </div>
    </button>
  );
}

// Per-row base-CV picker trigger (design_handoff_cv_decks_feature's "JSA App Shell"). Local
// to JobList since it's row chrome, not a shared widget — the popover itself (BaseCvPicker)
// is store-driven and mounted once in App.tsx. Only rendered for `queued` rows (Phase 6 —
// assignment is pre-launch only, matching PUT /api/jobs/{id}/base-cv's 409 outside `queued`).
function BaseCvTrigger({ job }: { job: JobDTO }) {
  const t = useT();
  const openCvPicker = useStore((s) => s.openCvPicker);
  const has = job.base_cv_id !== null;
  const title = has ? t("baseCvPicker.change") : t("baseCvPicker.assign");

  // Anchors the popover off the trigger's own position (works for both a mouse click and a
  // keyboard activation, which has no clientX/clientY of its own).
  const open = (target: HTMLElement) => {
    const rect = target.getBoundingClientRect();
    openCvPicker(job, { clientX: rect.left, clientY: rect.bottom });
  };

  // A `role="button"` <span>, not a real <button> — same reasoning as LaunchButton: this
  // sits inside JobList's outer row <button>, and nesting a real button there is invalid
  // HTML (and triggers React's validateDOMNesting warning).
  return (
    <span
      role="button"
      tabIndex={0}
      title={title}
      aria-label={title}
      onClick={(e) => {
        e.stopPropagation(); // don't also trigger the row's onSelect
        open(e.currentTarget);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          open(e.currentTarget);
        }
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        justifyContent: "center",
        width: 22,
        height: 22,
        flex: "none",
        border: `1px solid ${has ? T.aBorder : T.bd}`,
        borderRadius: T.btnRadius,
        background: has ? T.aSoft : "transparent",
        color: has ? T.a : T.ink3,
        cursor: "pointer",
      }}
    >
      <Icon name="doc" size={12} />
    </span>
  );
}

function QueueSearch({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const t = useT();
  return (
    <div
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        margin: "0 4px 8px",
        background: T.sunk,
        border: `1px solid ${T.bd2}`,
        borderRadius: T.btnRadius,
        padding: "0 9px",
      }}
    >
      <span style={{ color: T.ink3, display: "flex", flex: "none" }}>
        <Icon name="search" size={12} />
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("jobList.searchPlaceholder")}
        style={{
          flex: 1,
          minWidth: 0,
          border: "none",
          background: "transparent",
          outline: "none",
          padding: "8px 8px",
          font: `400 12px ${T.ui}`,
          color: T.ink,
        }}
      />
      {value && (
        <button
          type="button"
          className="jbtn"
          onClick={() => onChange("")}
          title={t("jobList.searchClear")}
          style={{
            border: "none",
            background: "transparent",
            color: T.ink3,
            cursor: "pointer",
            padding: 3,
            display: "flex",
            flex: "none",
            borderRadius: T.btnRadius,
          }}
        >
          <Icon name="x" size={11} />
        </button>
      )}
    </div>
  );
}

function QueueFilters({
  counts,
  active,
  onToggle,
  onClear,
}: {
  counts: Record<string, number>;
  active: string[];
  onToggle: (code: string) => void;
  onClear: () => void;
}) {
  const t = useT();
  const chips = GROUPS.map((g) => {
    const n = counts[g.code] || 0;
    const on = active.includes(g.code);
    if (!n && !on) return null;
    return (
      <button
        key={g.code}
        type="button"
        className="jbtn"
        onClick={() => onToggle(g.code)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          padding: "4px 9px",
          border: `1px solid ${on ? T.a : T.bd}`,
          borderRadius: T.btnRadius,
          background: on ? T.aSoft : T.sunk,
          color: on ? T.a : T.ink2,
          font: `500 9.5px ${T.mono}`,
          letterSpacing: ".05em",
          cursor: "pointer",
        }}
      >
        {t(g.chipKey)}
        <span style={{ color: on ? T.a : T.ink3, opacity: 0.8 }}>{n}</span>
      </button>
    );
  });
  if (!chips.some(Boolean)) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 5, margin: "0 4px 14px" }}>
      {chips}
      {active.length > 0 && (
        <button
          type="button"
          className="jbtn"
          onClick={onClear}
          style={{
            display: "inline-flex",
            alignItems: "center",
            padding: "4px 9px",
            border: `1px solid ${T.bd}`,
            borderRadius: T.btnRadius,
            background: "transparent",
            color: T.ink3,
            font: `500 9.5px ${T.mono}`,
            letterSpacing: ".05em",
            cursor: "pointer",
          }}
        >
          {t("jobList.filterClear")}
        </button>
      )}
    </div>
  );
}

function CvGateBanner() {
  const setEditorOpen = useStore((s) => s.setEditorOpen);
  const t = useT();
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: "10px 11px",
        marginBottom: 16,
        ...panelBase(T, { bg: T.surface, border: T.aBorder, radius: T.btnRadius }),
      }}
    >
      <div style={{ font: `600 10px ${T.mono}`, letterSpacing: ".1em", color: T.a, textTransform: "uppercase" }}>
        {t("cvGate.title")}
      </div>
      <div style={{ font: `400 11.5px ${T.ui}`, color: T.ink2 }}>{t("cvGate.body")}</div>
      <button
        type="button"
        onClick={() => setEditorOpen(true)}
        style={{
          alignSelf: "flex-start",
          font: `600 9px ${T.mono}`,
          letterSpacing: ".06em",
          color: T.a,
          background: "transparent",
          border: `1px solid ${T.aBorder}`,
          borderRadius: T.btnRadius,
          padding: "4px 9px",
          cursor: "pointer",
          textTransform: "uppercase",
        }}
      >
        {t("cvGate.cta")}
      </button>
    </div>
  );
}

/** Filesystem-safe filename fragment from free-text job metadata (company/role). */
function slugFragment(s: string): string {
  return s.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "job";
}

/** Bulk-download every ready CV/cover-letter PDF for jobs parked at `cv_review`/`review`
 * ("Reap Material" — see JobList's review-group header). A `cv_review` job only ever has
 * a CV to reap; a `review` job may have both, and either half is skipped if its Document
 * has no `pdf_path` yet (mirrors export_job's own per-artifact skip-if-absent rule).
 * Downloads are staggered — firing many `a.click()` calls in the same tick is what trips
 * a browser's "this site is trying to download multiple files" block. */
async function reapMaterial(
  reviewJobs: JobDTO[]
): Promise<{ failedCompanies: string[] }> {
  const downloads: { url: string; filename: string }[] = [];
  const failedCompanies: string[] = [];

  await Promise.all(
    reviewJobs.map(async (job) => {
      try {
        const full = await api.getJob(job.id);
        const base = `${slugFragment(job.company)}-${slugFragment(job.role)}`;
        const latestCv = full.documents
          .filter((d) => d.stage === "cv_adjust")
          .sort((a, b) => b.version - a.version)[0];
        const latestCl = full.documents
          .filter((d) => d.stage === "cover_letter")
          .sort((a, b) => b.version - a.version)[0];
        const cvUrl = toFileUrl(latestCv?.pdf_path);
        if (cvUrl) downloads.push({ url: cvUrl, filename: `${base}-cv.pdf` });
        const clUrl = toFileUrl(latestCl?.pdf_path);
        if (clUrl) downloads.push({ url: clUrl, filename: `${base}-cover-letter.pdf` });
      } catch {
        failedCompanies.push(job.company);
      }
    })
  );

  downloads.forEach((d, i) => {
    setTimeout(() => triggerDownload(d.url, d.filename), i * 400);
  });

  return { failedCompanies };
}

export function JobList() {
  const jobs = useStore((s) => Object.values(s.jobs));
  const selectedId = useStore((s) => s.selectedId);
  const selectJob = useStore((s) => s.selectJob);
  const launchAll = useStore((s) => s.launchAll);
  const cvStructureExists = useStore((s) => s.cvStructureExists);
  const t = useT();
  const [search, setSearch] = useState("");
  const [activeFilters, setActiveFilters] = useState<string[]>([]);
  const [reaping, setReaping] = useState(false);
  const [reapError, setReapError] = useState<string | null>(null);

  async function handleReapMaterial(reviewJobs: JobDTO[]) {
    setReaping(true);
    setReapError(null);
    const { failedCompanies } = await reapMaterial(reviewJobs);
    setReaping(false);
    if (failedCompanies.length > 0) {
      setReapError(t("jobList.reapMaterialError", { companies: failedCompanies.join(", ") }));
    }
  }

  const query = search.trim().toLowerCase();
  const bySearch = query
    ? jobs.filter((j) => j.company.toLowerCase().includes(query) || j.role.toLowerCase().includes(query))
    : jobs;
  const counts: Record<string, number> = {};
  GROUPS.forEach((g) => {
    counts[g.code] = bySearch.filter((j) => (g.states as string[]).includes(j.state)).length;
  });
  const visibleGroups = GROUPS.filter((g) => !activeFilters.length || activeFilters.includes(g.code));

  const toggleFilter = (code: string) =>
    setActiveFilters((prev) => (prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]));

  const anyRendered = visibleGroups.some((g) => (counts[g.code] || 0) > 0);

  return (
    <nav
      style={{
        width: "100%",
        height: "100%",
        background: T.subtle,
        borderRight: `1px solid ${T.bd}`,
        overflow: "auto",
        padding: "16px 12px",
      }}
    >
      <div
        style={{
          font: `600 10px ${T.mono}`,
          letterSpacing: ".16em",
          color: T.ink3,
          textTransform: "uppercase",
          padding: "0 4px 12px",
        }}
      >
        PROCESS_QUEUE
      </div>
      {cvStructureExists === false && <CvGateBanner />}
      <QueueSearch value={search} onChange={setSearch} />
      <QueueFilters
        counts={counts}
        active={activeFilters}
        onToggle={toggleFilter}
        onClear={() => setActiveFilters([])}
      />
      {visibleGroups.map((group) => {
        const groupJobs = bySearch.filter((j) => (group.states as string[]).includes(j.state));
        if (groupJobs.length === 0) return null;
        return (
          <section key={group.labelKey} style={{ marginBottom: 16 }}>
            <h2
              style={{
                display: "flex",
                alignItems: "center",
                gap: 7,
                padding: "0 4px 7px",
                margin: 0,
                font: `600 10px ${T.mono}`,
                letterSpacing: ".14em",
                color: T.ink3,
                textTransform: "uppercase",
              }}
            >
              <span>{t(group.labelKey)}</span>
              <span style={{ flex: 1, height: 1, background: T.bd }} />
              {group.labelKey === "jobList.queued" && groupJobs.length > 1 && (
                <button
                  type="button"
                  onClick={() => void launchAll()}
                  style={{
                    font: `600 9px ${T.mono}`,
                    letterSpacing: ".06em",
                    color: T.a,
                    background: "transparent",
                    border: `1px solid ${T.aBorder}`,
                    borderRadius: T.btnRadius,
                    padding: "2px 7px",
                    cursor: "pointer",
                    textTransform: "uppercase",
                  }}
                >
                  {t("launch.launchAll")}
                </button>
              )}
              {group.code === "review" && groupJobs.length > 1 && (
                <button
                  type="button"
                  disabled={reaping}
                  onClick={() => void handleReapMaterial(groupJobs)}
                  style={{
                    font: `600 9px ${T.mono}`,
                    letterSpacing: ".06em",
                    color: T.violet,
                    background: "transparent",
                    border: `1px solid color-mix(in srgb, ${T.violet} 55%, ${T.surface})`,
                    borderRadius: T.btnRadius,
                    padding: "2px 7px",
                    cursor: reaping ? "default" : "pointer",
                    opacity: reaping ? 0.6 : 1,
                    textTransform: "uppercase",
                  }}
                >
                  {reaping ? t("jobList.reapMaterialWorking") : t("jobList.reapMaterial")}
                </button>
              )}
              <span style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, textTransform: "none" }}>
                {groupJobs.length}
              </span>
            </h2>
            {group.code === "review" && reapError && (
              <p
                role="alert"
                style={{
                  margin: "0 4px 8px",
                  font: `400 10.5px ${T.ui}`,
                  color: T.danger,
                }}
              >
                {reapError}
              </p>
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {groupJobs.map((job) => (
                <JobRow
                  key={job.id}
                  job={job}
                  isSelected={job.id === selectedId}
                  onSelect={() => selectJob(job.id)}
                />
              ))}
            </div>
          </section>
        );
      })}
      {jobs.length === 0 && (
        <p style={{ font: `400 13px ${T.ui}`, color: T.ink3, fontStyle: "italic", padding: "0 4px" }}>
          {t("jobList.noJobsLoaded")}
        </p>
      )}
      {jobs.length > 0 && !anyRendered && (
        <p style={{ font: `400 13px ${T.ui}`, color: T.ink3, fontStyle: "italic", padding: "0 4px" }}>
          {t("jobList.noSearchMatches")}
        </p>
      )}
    </nav>
  );
}
