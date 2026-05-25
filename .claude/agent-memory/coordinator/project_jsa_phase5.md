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

## Gemini CLI has no native session resume

`GeminiCliBackend.restore_session()` falls back to history-replay (re-sends prior user turns in the pty session). This is a documented limitation — a WARNING is logged. The assumption is that the system prompt makes follow-up generation deterministic given the same user turns.

**Why:** Gemini CLI did not expose a `--resume` flag equivalent at implementation time.

**How to apply:** Phase 12 integration tests with GeminiCliBackend must account for this — the resumed assistant content may differ from the original. Don't assert exact assistant text equality across park/resume with Gemini.

---

## _extract_session_id uses a UUID heuristic on raw pty output

`jsa/agents/_pty_common.py` → `_extract_session_id` scans the pty output for the first UUID-shaped string to find the Claude CLI session ID. This is a heuristic — if the JD or CV text contains a UUID-like string, it could extract the wrong one.

**Why:** Claude CLI doesn't provide a structured output channel for the session ID.

**How to apply:** There's a TODO comment in the code. If resume failures are observed in testing (Phase 12), check whether input data contains UUID-like strings.

---

## Shared pty helpers are in `jsa/agents/_pty_common.py`

Both `ClaudeCliBackend` and `GeminiCliBackend` use shared pty read/write helpers extracted in Phase 5. Any new pty-based backend (Phase 11 is Anthropic API so not pty) should reuse these rather than duplicate.
