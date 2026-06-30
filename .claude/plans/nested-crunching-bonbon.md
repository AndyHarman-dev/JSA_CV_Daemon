---
status: Pending
---

# Fix: opencode-cli backend review issues

## Context

The `/code-review high` pass on `feat/opencode-cli-backend` surfaced 7 findings (6 CONFIRMED, 1 PLAUSIBLE) in `jsa/agents/opencode_cli.py`, `jsa/pipeline/stages.py`, and the test file. This plan fixes all of them before merging.

Branch off from `feat/opencode-cli-backend` → new branch `fix/opencode-cli-review`.

---

## Files to modify

| File | Findings addressed |
|------|--------------------|
| `jsa/agents/opencode_cli.py` | F1, F2, F3, F4, F5 |
| `jsa/pipeline/stages.py` | F6 |
| `tests/backend/test_opencode_cli.py` | F7 |

---

## Finding F1 — nonzero exit with non-empty stdout silently returned

**Location:** `opencode_cli.py`, `_run` method, condition `if result.returncode != 0 and not stdout:`

**Problem:** If opencode exits non-zero but writes any text to stdout (partial output, error preamble), `_run` returns that text as if it were a valid reply. Downstream `parse_reply` then raises `ProtocolError` instead of `OpenCodeCliError`, masking the root cause.

**Fix:** Remove the `and not stdout` guard — raise `OpenCodeCliError` (or `OpenCodeSessionExpiredError`) on _any_ nonzero exit, regardless of stdout content.

```python
# Before
if result.returncode != 0 and not stdout:
    ...

# After
if result.returncode != 0:
    ...
```

---

## Finding F2 + F3 — AgentLimitReached bypassed in _parse_with_nudge

**Location:** `opencode_cli.py`, `_parse_with_nudge`

**Problem (F2):** The limit-keyword check runs only inside the `"no sentinel block"` branch. If the model hits a quota mid-reply and produces an unterminated block (`<<<FINAL>>>` without `<<<END>>>`), `parse_reply` raises `ProtocolError("unterminated block")`, the `"no sentinel block" not in str(exc)` guard fires `raise` immediately, and `AgentLimitReached` is never raised.

**Problem (F3):** The nudge reply `raw2` is passed directly to `parse_reply` with no limit-keyword check. If the nudge itself hits a rate limit, the caller gets `ProtocolError` instead of `AgentLimitReached`.

**Fix:** Move the limit-keyword check _before_ the `"no sentinel block"` guard (so it applies to all `ProtocolError` variants), promote `_LIMIT_KEYWORDS` to module level, and add a symmetric limit check on `raw2`.

```python
_LIMIT_KEYWORDS = ("usage limit", "rate limit", "limit reached", "quota")  # module-level

async def _parse_with_nudge(self, session_id: str, raw: str) -> AgentReply:
    try:
        return parse_reply(raw)
    except ProtocolError as exc:
        # Check limit keywords for ALL ProtocolError variants, including "unterminated block"
        raw_lower = raw.lower()
        if any(kw in raw_lower for kw in _LIMIT_KEYWORDS):
            raise AgentLimitReached(raw[:500])
        if "no sentinel block" not in str(exc):
            raise  # re-raise "unterminated block" and other non-nudgeable variants
        logger.warning(...)
        # ... build nudge_cmd ...
        raw2 = await asyncio.to_thread(self._run, nudge_cmd, session_id)
        try:
            return parse_reply(raw2)
        except ProtocolError:
            raw2_lower = raw2.lower()
            if any(kw in raw2_lower for kw in _LIMIT_KEYWORDS):
                raise AgentLimitReached(raw2[:500])
            raise
```

While here, also move `_PROMPT_MAP` (in `run_research`) to module level — it was a local dict recreated on every call.

---

## Finding F4 — empty-string external_id bypasses None guard

**Location:** `opencode_cli.py`, `restore_session` and `send_message`

**Problem:** Both guards use `is not None` / `is None`, so `external_id=""` passes the guard and causes `opencode --session "" -p ...` to run, silently creating an anonymous session.

**Fix:** Switch to truthiness checks.

```python
# restore_session
if external_id:                        # was: if external_id is not None:
    return OpenCodeSessionHandle(...)
raise RuntimeError(...)

# send_message
if not handle.external_id:             # was: if handle.external_id is None:
    raise RuntimeError(...)
```

---

## Finding F5 — synchronous Path.read_text blocks event loop

**Location:** `opencode_cli.py`, `run_research`, line reading the prompt file

**Problem:** `Path.read_text()` is synchronous blocking I/O called directly in an `async` method. CLAUDE.md Rule 2: "Any synchronous … blocking I/O call must be wrapped: `await asyncio.to_thread(sync_function, *args)`."

**Fix:**

```python
prompt_path = prompts_dir / _PROMPT_MAP[agent_name]
system_prompt = await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")
```

---

## Finding F6 — silent research fallback with no observability

**Location:** `jsa/pipeline/stages.py`, `_gather_research`, line `return text if open_tag in text else _research_placeholder(stage)`

**Problem:** When `run_research` returns text that doesn't contain the expected XML tag (`[INTEL_BRIEF]` / `[COMPANY_BRIEF]`), the function silently falls back to the placeholder. The job proceeds without any signal in the logs that research was effectively discarded.

**Fix:** Add a `logger.warning` before returning the placeholder in the tag-absent branch.

```python
if open_tag in text:
    return text
logger.warning(
    "run_research for job %s (%s) returned output missing expected tag %r; "
    "falling back to research placeholder",
    job.id, agent_name, open_tag,
)
return _research_placeholder(stage)
```

---

## Finding F7 — tests use MagicMock/patch (CLAUDE.md Rule 4)

**Location:** `tests/backend/test_opencode_cli.py`

**Problem:** The entire test file patches `subprocess.run` via `unittest.mock.patch` and constructs `MagicMock` objects. CLAUDE.md Rule 4 says "Do not `patch` or `MagicMock` internal functions." (`subprocess.run` is stdlib, but the rule has no explicit qualifier.)

**Fix — injectable subprocess runner:** Add an optional `_runner` parameter to `OpenCodeCliBackend.__init__` (defaults to `subprocess.run`). Tests pass a `FakeRunner` callable instead of patching the module. This eliminates all `patch(...)` context managers while keeping test intent identical.

```python
# opencode_cli.py
def __init__(self, model=..., timeout=120.0, _runner=None) -> None:
    self._model = model
    self._timeout = timeout
    self._subprocess_run = _runner if _runner is not None else subprocess.run

# _run method uses self._subprocess_run instead of subprocess.run
```

```python
# test file — replace patch pattern with:
class FakeRunner:
    def __init__(self, response=_FINAL_TEXT, returncode=0, stderr=b""):
        self.stdout = response.encode()
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []
    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self

backend = OpenCodeCliBackend(timeout=5.0, _runner=FakeRunner())
```

All `_make_mock_proc` helpers and `with patch(...)` blocks are replaced by `FakeRunner` instances passed to the constructor. The `MagicMock` import is removed.

---

## Execution order

1. Create branch: `git checkout -b fix/opencode-cli-review` (from `feat/opencode-cli-backend`)
2. Edit `jsa/agents/opencode_cli.py`: F1 → F2+F3 → F4 → F5 (in order, single pass)
3. Edit `jsa/pipeline/stages.py`: F6
4. Edit `jsa/agents/opencode_cli.py` + `tests/backend/test_opencode_cli.py`: F7
5. Run tests: `pytest tests/backend/test_opencode_cli.py -v`
6. Run full suite: `pytest -v -m "not integration"`

---

## Verification

```bash
# All unit tests pass
pytest tests/backend/test_opencode_cli.py -v

# Full backend suite (skip integration)
pytest -v -m "not integration"

# Confirm no patch/MagicMock imports remain in test file
grep -n "MagicMock\|from unittest.mock import" tests/backend/test_opencode_cli.py
# should return nothing

# Confirm _LIMIT_KEYWORDS is module-level
grep -n "_LIMIT_KEYWORDS" jsa/agents/opencode_cli.py
# should show it at the top of the file, not inside a def

# Confirm asyncio.to_thread wraps read_text
grep -n "read_text" jsa/agents/opencode_cli.py
# should show it inside asyncio.to_thread(...)
```
