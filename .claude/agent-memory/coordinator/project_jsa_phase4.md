---
name: project-jsa-phase4
description: Phase 4 gotchas: SessionHandle dataclass decision and registry factory pattern
metadata:
  type: project
---

## SessionHandle is a concrete @dataclass, not a Protocol

ARCH.md describes `SessionHandle` as `Protocol` but Phase 4 implemented it as a concrete `@dataclass` base class. This was codified in ARCH.md. All backends (Phases 5, 11) must **subclass** `SessionHandle` and add their process/connection fields.

**Why:** Ergonomics — Protocol-style doesn't let backend-specific handle types carry extra state cleanly when you also want runtime isinstance checks.
**How to apply:** When implementing ClaudeCliBackend, GeminiCliBackend, AnthropicAPIBackend: define `@dataclass class ClaudeSessionHandle(SessionHandle): ...` with the extra fields (e.g., `pty_process`, `session_id`).

## backend_for() calls cls() with no arguments

`jsa/agents/registry.py` → `backend_for(name)` calls `_REGISTRY[name]()` with zero arguments. Backends that need constructor config (model name, timeout, etc.) must either:
- Provide a no-arg `__init__` with defaults, or
- Be registered via a factory lambda: `register("claude-cli", lambda: ClaudeCliBackend(model="claude-opus-4-5"))`.

**Why:** The simple `()` call was the Phase 4 choice. Phase 5 (CLI backends) should address this before registering real backends.
**How to apply:** In Phase 5, use `register("claude-cli", lambda: ClaudeCliBackend())` or ensure constructors have defaults.
