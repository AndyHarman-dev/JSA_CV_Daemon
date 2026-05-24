// Typed fetch wrappers — full implementation in Phase 9
export const api = {
  getJobs: () => fetch('/api/jobs').then(r => r.json()),
}
