"""MistralBackend: OpenAI-compatible chat-completions HTTP backend for Mistral AI.

Talks to https://api.mistral.ai/v1/chat/completions — an OpenAI-compatible endpoint,
same shape as OpenCode Zen's. All the request/parse/retry/downgrade machinery is
inherited unchanged from ``jsa.agents._openai_compat.OpenAICompatBackend``; this
module only supplies Mistral's endpoint, auth env var, default model, and a
``prompt_cache_key`` derived from the system prompt (see CLAUDE.md → "Prompt
caching").

The API key is read from the ``MISTRAL_API_KEY`` environment variable (never
hardcoded, never logged) at call time.
"""

from __future__ import annotations

import hashlib
from typing import Any

from jsa.agents._openai_compat import OpenAICompatBackend


class MistralBackend(OpenAICompatBackend):
    """AgentBackend implementation that calls the Mistral AI chat-completions API."""

    name = "mistral"
    endpoint_url = "https://api.mistral.ai/v1/chat/completions"
    env_vars = ("MISTRAL_API_KEY",)
    # Cheapest tier per Assesser's providers/tiers.py (verified against Mistral's own
    # pricing page, Aug 2026) and confirmed present in the live Phase-0 /models probe.
    default_model = "mistral-small-2603"

    def _extra_payload(self, system_prompt: str) -> dict[str, Any]:
        """A stable ``prompt_cache_key`` derived from the system prompt, so every
        request sharing the same byte-identical system prefix (see CLAUDE.md ->
        "Prompt caching" -> the cross-job system-prefix invariant) routes to a
        server that already holds it. A wrong/unknown key just degrades to a cache
        miss, never an error, so no kill-switch-off fallback beyond the plain
        omission below is needed."""
        if not self._prompt_caching:
            return {}
        digest = hashlib.sha1(system_prompt.encode("utf-8")).hexdigest()[:16]
        return {"prompt_cache_key": f"jsa-{digest}"}

    def _reasoning_payload(self) -> dict[str, Any]:
        """Mistral only produces a thinking trace when ``reasoning_effort`` is set,
        and the parameter is two-valued — ``"high"`` (full thinking chunk before the
        answer) or ``"none"`` (omitted entirely). There is no middle setting, so
        asking for reasoning at all means ``"high"``.

        The trace does NOT arrive as ``reasoning_content``: with this set,
        ``content`` becomes a chunk LIST mixing ``{"type": "thinking"}`` and
        ``{"type": "text"}`` entries. ``_openai_compat.py``'s ``_split_content_delta``
        is what handles that shape (and is a required type guard, not just a
        feature — a list appended into the content accumulator would ``TypeError``
        on join). A model that doesn't support the parameter rejects it with a 4xx,
        which the shared ``_ReasoningRejected`` degrade turns into "no thinking on
        this instance" rather than a lost BF-19 slot."""
        if not self._reasoning:
            return {}
        return {"reasoning_effort": "high"}
