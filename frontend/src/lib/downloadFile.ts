// Shared file-download helpers used by both the single-job download menu (ReviewPane) and
// the bulk "Reap Material" action (JobList) — kept here so the two stay byte-identical
// rather than drifting into two slightly different anchor-click implementations.

/** Convert an absolute filesystem path stored in the DB to a /api/files/<relpath> URL. */
export function toFileUrl(absPath: string | null | undefined): string | null {
  if (!absPath) return null;
  // Extract last two path segments: slug/filename
  const rel = absPath.replace(/^.*?([^/\\]+[/\\][^/\\]+)$/, "$1").replace(/\\/g, "/");
  return `/api/files/${rel}`;
}

/** Trigger a browser download of the file at `url`. An empty `filename` (the default)
 * lets the browser fall back to the URL's own name — the file is served
 * `Content-Disposition: inline`, so this attribute is what forces a save dialog/download
 * instead of navigating the tab to the file. */
export function triggerDownload(url: string, filename = ""): void {
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
