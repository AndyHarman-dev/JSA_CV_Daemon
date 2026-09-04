import { useState } from "react";
import { useStore } from "../store";
import { StatusBadge } from "./StatusBadge";
import { LaunchButton } from "./LaunchButton";
import { InjectTrigger } from "./PromptInjector";
import type { JobDTO, JobState } from "../types";
import { useT } from "../i18n/useT";
import { SHELL_THEME } from "../theme/tokens";
import { panelBase } from "../theme/chrome";
import { Icon } from "../theme/Icon";

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
          // Span, not div — this whole row is a <button>, whose content model is phrasing.
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
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

export function JobList() {
  const jobs = useStore((s) => Object.values(s.jobs));
  const selectedId = useStore((s) => s.selectedId);
  const selectJob = useStore((s) => s.selectJob);
  const launchAll = useStore((s) => s.launchAll);
  const cvStructureExists = useStore((s) => s.cvStructureExists);
  const t = useT();
  const [search, setSearch] = useState("");
  const [activeFilters, setActiveFilters] = useState<string[]>([]);

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
              <span style={{ font: `400 9.5px ${T.mono}`, color: T.ink3, textTransform: "none" }}>
                {groupJobs.length}
              </span>
            </h2>
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
