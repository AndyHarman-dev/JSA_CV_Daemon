---
name: phase-sa2-gemini-research
description: Phase SA-2 gotchas — Gemini backend research via inline prompt + google_web_search
metadata:
  type: project
---

## Core approach
Gemini CLI has no `--agent` flag (no `.gemini/agents/` discovery). Instead, `GeminiCliBackend.run_research` inlines the research system prompt directly into `-p`, relying on Gemini's built-in `google_web_search` and `web_fetch` tools (auto-execute in headless mode — no `--yolo` or approval flags needed).

## Key decisions
- Research prompt files live at `jsa/prompts/GEMINI_CV_RESEARCH.md` and `GEMINI_CL_RESEARCH.md` — no YAML frontmatter (Gemini doesn't read it), plain system prompt body only.
- `_gather_research` dispatch changed from `isinstance(backend, ClaudeCliBackend)` to `hasattr(backend, "run_research")` duck-typing. Any backend can opt in to research by implementing `run_research(agent_name, query) -> str`.
- `run_research` is still NOT on the `AgentBackend` ABC — it's an optional extension method.
- `GeminiCliBackend._run` updated to accept optional `timeout` parameter (mirrors `ClaudeCliBackend._run`) — needed so `run_research` can pass `RESEARCH_TIMEOUT = 300.0`.
- Research command: `gemini --skip-trust -p "<system_prompt>\n\n<query>" -o json` (no `--session-id`, stateless one-shot).
- Response extracted from `data["response"]` — no sentinel parsing (same as Claude research).

## Test pattern
Monkeypatch `_run` at the instance level (not class level) for `TestGeminiRunResearch` — avoids touching other tests. For `TestGatherResearchGeminiBackend`, patch `run_research` at the class level with `(self, agent_name, query)` signature (bound method needs self absorbed).

**Why:** `FakeAgentBackend` has no `run_research` → `hasattr` returns False → placeholder immediately. The existing `TestGatherResearchFakeBackend` tests still pass unchanged.
