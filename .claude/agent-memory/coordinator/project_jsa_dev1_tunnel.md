---
name: project-jsa-dev1-tunnel
description: DEV-1 --dev-tunnel flag: cloudflared quick tunnel + relaxed CORS for phone/remote dev access
metadata:
  type: project
---

## DEV-1: `--dev-tunnel` flag (phase complete, 2026-05-30)

**Why:** User wants to access the JSA web UI from their phone while not on the local network. The server normally binds to 127.0.0.1 and CORS only allows localhost:*.

**What was built:**
- `jsa/cli.py`: `_start_tunnel(port)` helper — checks for cloudflared binary (shutil.which), spawns `cloudflared tunnel --url http://localhost:<port>` with merged stdout/stderr, daemon thread drains the pipe continuously (no `break` after URL found — critical for preventing OS pipe-buffer deadlock), prints URL once when found, warns if cloudflared exits without emitting a URL.
- `jsa/server.py`: `create_app(settings, dev_tunnel=False)` — when `dev_tunnel=True`, uses `allow_origins=["*"]`; otherwise keeps the existing `allow_origin_regex=r"http://localhost:\d+"`.
- `--dev-tunnel` flag added to `main()` in cli.py. Help text warns: "exposes the unauthenticated API publicly — dev use only."

**How to use:**
```
jsa --csv jobs.csv --cv resume.pdf --dev-tunnel
# Prints: [JSA] ✓ Tunnel URL: https://xxx.trycloudflare.com
```
cloudflared must be installed: `brew install cloudflared` (free, no account needed for quick tunnels).

**Critical gotcha (caught in review):** The `_watch` thread MUST NOT `break` after finding the URL. cloudflared keeps writing to the merged pipe indefinitely; a `break` leaves the pipe unread, fills the 64 KB OS buffer, and stalls cloudflared. Use a `found` flag and continue draining until EOF.

**Test file:** `tests/backend/test_dev_tunnel.py` (20 tests). CORS tests inspect `app.user_middleware` stack directly (implementation-coupled but appropriate). Subprocess tests use `unittest.mock.patch` on `shutil.which` and `subprocess.Popen` with `iter([...])` mock stdout.
