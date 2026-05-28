---
name: project-jsa-phase5
description: Phase 5 gotchas — asyncio.TimeoutError/OSError in Python 3.11+, Gemini no native resume, UUID extraction heuristic
metadata:
  type: project
---

## asyncio.TimeoutError is a subclass of OSError in Python 3.11+

In Python 3.11+, `asyncio.TimeoutError` is an alias for `TimeoutError`, which is a subclass of `OSError`. This means a bare `except OSError` will silently swallow timeout exceptions. The fix (already in `jsa/agents/_pty_common.py`):

```python
except asyncio.TimeoutError:   # must come BEFORE OSError
    raise AgentTimeout(...)
except OSError:
    ...
```

**Why:** Phase 5 was bitten by this — timeouts were being caught as OSError and not re-raised as AgentTimeout, causing jobs to silently fail without the correct error state.

**How to apply:** Any code in phases 7, 8, 11 that catches `OSError` in a context where timeouts are possible must explicitly catch `asyncio.TimeoutError` first.

---

## Gemini CLI DOES support native session resume (since BF-5)

**Updated in BF-5**: `GeminiCliBackend` was completely rewritten from pty to subprocess mode. `--resume <uuid>` works. `restore_session` now mirrors `ClaudeCliBackend` exactly — if `external_id` is set, returns handle immediately; if None, raises RuntimeError.

The old history-replay fallback (and the WARNING log) is gone. Session UUIDs are reliably persisted via `-o json` output which includes `"session_id"`.

**Why:** Gemini CLI v0.41.2 has `--session-id <uuid>` (create) and `--resume <uuid>` (continue) flags. Sessions stored on disk. Also has `-p` non-interactive mode and `-o json` for structured output.

---

## `jsa/agents/_pty_common.py` is DELETED (since BF-5)

Deleted in BF-5 — it was only used by the old pty-based `GeminiCliBackend`. `ClaudeCliBackend` was already subprocess-based. `ptyprocess` dependency removed from `pyproject.toml`.

Do not reference `_pty_common` in any new code.
