// Client-side, backend-free CV export: a static HTML rendering of the CV, a "download .html"
// action, and a "download PDF" action that prints that HTML via an off-screen same-origin
// iframe. Ported from the reference standalone editor (~/Desktop/cv-editor.html —
// buildExportBody/exportEntryHtml/buildExportHtml/downloadHtml/downloadPdf).
//
// Renders from the live `EditorCV` (the store's in-memory state), NOT from `exportJson()`'s
// trimmed wire-schema output — exportJson omits empty array fields entirely (e.g.
// `contact.links`/`section.items`/`entry.bullets` are absent, not `[]`, when empty), matching
// jsa/schema/cv.py's optional fields, whereas EditorSection/EditorEntry always carry real
// arrays (populated by toEditor/toEditorEntry) and already know `kind` — exactly what the
// reference itself does (its buildExportBody reads `this.state.cv`, not its own exportJson()).
import type { EditorCV, EditorEntry, EditorSection } from "../../types";

const FONT_LINK =
  'https://fonts.googleapis.com/css2?family=Newsreader:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Sans:wght@400;500;600;700&family=Share+Tech+Mono&display=swap';
const SERIF = "'Newsreader', Georgia, serif";
const UI_FONT = "'IBM Plex Sans', system-ui, sans-serif";
const MONO = "'Share Tech Mono', ui-monospace, monospace";

function escapeHtml(s: string | undefined | null): string {
  return (s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// Displays the raw text the user typed (e.g. "github.com/foo", no scheme) while making it a
// real, clickable link in the exported document — the reference renders these as plain
// colored text, which looks like a link but isn't one once opened/printed.
function linkHref(raw: string): string {
  return /^([a-z][a-z0-9+.-]*:|\/\/)/i.test(raw) ? raw : `https://${raw}`;
}

function linkHtml(raw: string, style: string): string {
  return `<a href="${escapeHtml(linkHref(raw))}" style="${style}">${escapeHtml(raw)}</a>`;
}

function bulletsHtml(bullets: string[]): string {
  const items = bullets.filter((b) => b && b.trim());
  if (!items.length) return "";
  return `<div class="cv-bullets" style="margin:3px 0 0">${items
    .map(
      (b) =>
        `<div class="cv-bullet" style="font-size:13px;color:#211C16;padding-left:14px;text-indent:-14px;margin-bottom:1px">&bull;&nbsp; ${escapeHtml(b)}</div>`
    )
    .join("")}</div>`;
}

// Field-presence-driven, not kind-specific — mirrors the reference's exportEntryHtml,
// which never branches on section kind at the entry level.
function entryHtml(e: EditorEntry): string {
  const head = `<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap">
    <span style="font:600 14px ${SERIF};color:#211C16;flex:1 1 auto;min-width:0">${escapeHtml(e.heading)}</span>
    ${e.dates ? `<span style="font:400 11.5px ${MONO};color:#6B6358;flex:none;margin-left:auto">${escapeHtml(e.dates)}</span>` : ""}
  </div>`;
  const sub =
    e.subheading || e.location
      ? `<div style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-style:italic;font-size:12.5px;font-family:${SERIF};color:#6B6358">
    <span>${escapeHtml(e.subheading)}</span><span>${escapeHtml(e.location)}</span></div>`
      : "";
  const text = e.text ? `<p style="margin:2px 0 0;font-size:13px;color:#211C16">${escapeHtml(e.text)}</p>` : "";
  const bullets = bulletsHtml(e.bullets);
  const links = e.links.filter(Boolean).length
    ? `<div style="font-size:12px;color:#2563EB;margin-top:2px">${e.links
        .filter(Boolean)
        .map((l) => linkHtml(l, "color:#2563EB"))
        .join(" &nbsp;&middot;&nbsp; ")}</div>`
    : "";
  return `<div class="cv-entry"><div class="cv-entry-head">${head}${sub}</div>${text}${bullets}${links}</div>`;
}

function sectionBodyHtml(section: EditorSection): string {
  const kind = section.kind;
  if (kind === "summary") {
    return `<p style="margin:4px 0 0;font-size:13.5px;line-height:1.6;color:#211C16">${escapeHtml(section.text)}</p>`;
  }
  if (kind === "bullets") {
    const items = section.items.filter(Boolean);
    return `<div class="cv-bullets" style="margin:4px 0 0">${items
      .map(
        (b) =>
          `<div class="cv-bullet" style="font-size:13px;color:#211C16;padding-left:14px;text-indent:-14px;margin-bottom:2px">&bull;&nbsp; ${escapeHtml(b)}</div>`
      )
      .join("")}</div>`;
  }
  if (kind === "skills") {
    return `<div style="margin-top:3px">${section.entries
      .map(
        (e) =>
          `<div style="font-size:13.5px;margin-bottom:3px;color:#211C16;font-family:${UI_FONT}"><strong>${escapeHtml(e.heading)}:</strong> ${escapeHtml(e.bullets.join(", "))}</div>`
      )
      .join("")}</div>`;
  }
  return `<div class="cv-entries" style="margin-top:4px">${section.entries.map(entryHtml).join("")}</div>`;
}

export function buildExportBody(cv: EditorCV): string {
  const bits: string[] = [];
  if (cv.contact.email) bits.push(escapeHtml(cv.contact.email));
  if (cv.contact.phone) bits.push(escapeHtml(cv.contact.phone));
  cv.contact.links.filter(Boolean).forEach((l) => bits.push(linkHtml(l, "color:#2563EB")));
  if (cv.contact.location) bits.push(escapeHtml(cv.contact.location));

  const sectionsHtml = cv.sections
    .map(
      (s, idx) => `<div class="cv-section" style="margin-top:${idx === 0 ? 4 : 18}px;padding-top:14px;border-top:1px solid #E3DFD6">
        <div class="cv-section-title" style="font:600 11.5px ${UI_FONT};letter-spacing:.13em;text-transform:uppercase;color:#211C16;margin-bottom:7px">${escapeHtml(s.name)}</div>
        ${sectionBodyHtml(s)}
      </div>`
    )
    .join("");

  return `<div style="max-width:680px;margin:0 auto;font-family:${SERIF}">
      <div style="text-align:center;font-size:28px;font-weight:500;color:#211C16;margin-bottom:6px">${escapeHtml(cv.contact.name) || "Untitled"}</div>
      <div style="text-align:center;font-size:12.5px;color:#6B6358;margin-bottom:22px;font-family:${UI_FONT}">${bits.join(" &nbsp;&middot;&nbsp; ")}</div>
      ${sectionsHtml}
    </div>`;
}

export function buildExportHtml(cv: EditorCV, opts: { bottomMargin: number }): string {
  const name = cv.contact.name || "CV";
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><title>${escapeHtml(name)}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com"><link href="${FONT_LINK}" rel="stylesheet">
    <style>
    @page{margin-bottom:${opts.bottomMargin}px}
    @media print{body{margin:0}}
    body{margin:0;padding:48px 24px;background:#FCFBF8}
    .cv-section{break-inside:auto}
    .cv-section-title{page-break-after:avoid;page-break-inside:avoid;break-after:avoid-page;break-inside:avoid-page}
    .cv-entries{display:block}
    .cv-entry{margin-top:12px}
    .cv-entry:first-child{margin-top:0}
    .cv-entry-head{page-break-inside:avoid;page-break-after:avoid;break-inside:avoid-page;break-after:avoid-page}
    .cv-bullets{break-inside:auto}
    .cv-bullet{page-break-inside:avoid;break-inside:avoid-page}
    </style>
    </head><body>${buildExportBody(cv)}</body></html>`;
}

function slugName(cv: EditorCV): string {
  return (cv.contact.name || "cv").trim().replace(/\s+/g, "_").toLowerCase();
}

export function downloadHtml(cv: EditorCV, bottomMargin: number): void {
  const blob = new Blob([buildExportHtml(cv, { bottomMargin })], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${slugName(cv)}.html`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function downloadPdf(cv: EditorCV, bottomMargin: number): void {
  // Print via an off-screen same-origin iframe instead of window.open — avoids popup blockers.
  const iframe = document.createElement("iframe");
  iframe.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden";
  document.body.appendChild(iframe);
  const cleanup = () => {
    setTimeout(() => {
      try {
        document.body.removeChild(iframe);
      } catch {
        // already removed
      }
    }, 500);
  };
  const win = iframe.contentWindow;
  if (!win) {
    cleanup();
    return;
  }
  const doc = win.document;
  doc.open();
  doc.write(buildExportHtml(cv, { bottomMargin }));
  doc.close();
  // `iframe.onload` and the setTimeout fallback below can both fire (a doc.write()'d iframe's
  // load timing is inconsistent across browsers) — this flag makes `go` idempotent so print()
  // is never called twice, which would otherwise pop two native print dialogs back to back.
  let printed = false;
  const go = () => {
    if (printed) return;
    printed = true;
    try {
      win.focus();
      win.print();
    } catch {
      // print() can throw if the iframe never finished loading; the timeout fallback below covers it
    }
  };
  win.onafterprint = cleanup;
  iframe.onload = go;
  setTimeout(go, 350);
  setTimeout(cleanup, 15000);
}
