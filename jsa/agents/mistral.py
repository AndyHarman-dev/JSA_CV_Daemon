"""MistralBackend: OpenAI-compatible chat-completions HTTP backend for Mistral AI.

Talks to https://api.mistral.ai/v1/chat/completions — an OpenAI-compatible endpoint,
same shape as OpenCode Zen's. All the request/parse/retry/downgrade machinery is
inherited unchanged from ``jsa.agents._openai_compat.OpenAICompatBackend``; this
module only supplies Mistral's endpoint, auth env var, and default model.

The API key is read from the ``MISTRAL_API_KEY`` environment variable (never
hardcoded, never logged) at call time.
"""

from __future__ import annotations

from jsa.agents._openai_compat import OpenAICompatBackend


class MistralBackend(OpenAICompatBackend):
    """AgentBackend implementation that calls the Mistral AI chat-completions API."""

    name = "mistral"
    endpoint_url = "https://api.mistral.ai/v1/chat/completions"
    env_vars = ("MISTRAL_API_KEY",)
    # Cheapest tier per Assesser's providers/tiers.py (verified against Mistral's own
    # pricing page, Aug 2026) and confirmed present in the live Phase-0 /models probe.
    default_model = "mistral-small-2603"
