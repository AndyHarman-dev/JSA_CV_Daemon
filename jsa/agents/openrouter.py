"""OpenRouterBackend: OpenAI-compatible chat-completions HTTP backend for OpenRouter.

Talks to https://openrouter.ai/api/v1/chat/completions. OpenRouter is itself a
multi-provider aggregator, so it widens the BF-19 chain's provider diversity more
per line of code than any other backend here. Most of the request/parse/retry/
downgrade machinery is inherited unchanged from
``jsa.agents._openai_compat.OpenAICompatBackend``; this module supplies OpenRouter's
endpoint, auth env var, default model, and its mandatory routing guard.

The API key is read from the ``OPENROUTER_API_KEY`` environment variable (never
hardcoded, never logged) at call time.
"""

from __future__ import annotations

from typing import Any

from jsa.agents._openai_compat import OpenAICompatBackend


class OpenRouterBackend(OpenAICompatBackend):
    """AgentBackend implementation that calls the OpenRouter chat-completions API."""

    name = "openrouter"
    endpoint_url = "https://openrouter.ai/api/v1/chat/completions"
    env_vars = ("OPENROUTER_API_KEY",)
    # Cheapest tier per Assesser's providers/tiers.py (Aug 2026 catalog check).
    # OpenRouter model IDs are namespaced ("vendor/model") — a bare model name is a
    # 4xx. Confirmed present in the live Phase-0 /models probe.
    default_model = "nvidia/nemotron-3-nano-30b-a3b"

    def _extra_payload(self, system_prompt: str) -> dict[str, Any]:
        """Mandatory routing guard: without it, OpenRouter may route a request to
        an upstream endpoint that silently ignores ``response_format`` instead of
        honoring it or failing loudly — which would make the per-session
        structured→sentinel downgrade fire on every single turn instead of failing
        loudly once. Confirmed by the reference bake-off project
        (openrouter_provider.py). Do not remove this as a simplification."""
        return {"provider": {"require_parameters": True}}

    def _reasoning_payload(self) -> dict[str, Any]:
        """Ask OpenRouter to emit reasoning tokens. Without this the aggregator does
        not surface a thinking stream at all, so the chat UI's REASONING card never
        gets anything to show (a non-reasoning routed model still sends nothing —
        that is a model capability, not a wiring gap).

        This is the one field in this payload that interacts badly with the
        ``require_parameters`` guard above: an upstream that cannot honor
        ``reasoning`` gets filtered out, and if that empties the eligible-provider
        set OpenRouter answers 4xx. That is a reasoning-only rejection, not a dead
        backend — the shared base's ``_ReasoningRejected`` degrade catches it,
        drops this field for the rest of the instance's life, and retries once
        clean. See ``_openai_compat.py``'s ``_reasoning_payload`` hook."""
        if not self._reasoning:
            return {}
        return {"reasoning": {"enabled": True}}

    def _system_content(self, system_prompt: str) -> str | list[dict]:
        """An explicit ``cache_control: {"type": "ephemeral"}`` breakpoint on the
        system text block — required for OpenRouter's Anthropic/Qwen/Gemini
        upstreams to cache at all; harmless for upstreams (OpenAI/DeepSeek/Grok/
        Groq/Z.AI/Gemini-2.5) that cache automatically. See CLAUDE.md -> "Prompt
        caching" -> OpenRouter's degrade-on-4xx for why a rejection of this field
        must not cost this backend its BF-19 slot — that degrade path
        (``_CacheRejected``, in ``_openai_compat.py``) is keyed off this method
        returning a list rather than a plain string."""
        if not self._prompt_caching:
            return system_prompt
        return [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
