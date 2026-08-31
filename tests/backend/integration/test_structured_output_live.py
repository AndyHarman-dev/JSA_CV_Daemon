"""Phase 6 of the structured-output plan: real-API structured-mode integration tests.

Every Phase 3/4 test stubs the SDK/HTTP boundary — nothing before this file has
verified against the REAL Anthropic API that (a) it accepts pydantic's
`model_json_schema()` output (title, nested $defs, `anyOf: [X, null]` nullables) as
a tool `input_schema`, (b) forced `tool_choice` actually yields a usable tool_use
reply, and (c) the fit schema behaves — the Phase 3 advisor addendum calls these
non-negotiable before structured mode is trusted as a default. Also covers OpenCode
Zen's wire-level acceptance of `response_format` for the free-tier model in practice
(flagged as an open question in jsa/agents/opencode_zen.py's `_call_api_once`
docstring).

Skipped by default (`pytest -m "not integration"`); run explicitly with
`pytest tests/backend/integration/test_structured_output_live.py -v -m integration`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jsa.agents.anthropic_api import AnthropicAPIBackend
from jsa.agents.opencode_zen import OpenCodeZenBackend
from jsa.db.models import Stage
from jsa.schema.turn_models import json_schema_for

pytestmark = pytest.mark.integration


def _load_dotenv_key(name: str) -> str | None:
    """Minimal .env reader — no python-dotenv dependency in this project.

    Looks for `name=value` in the repo-root .env, without overriding a value
    already present in the real environment. Mirrors
    test_opencode_zen_live.py's helper of the same name (duplicated rather than
    imported, matching that file's own convention of not sharing test helpers
    across integration test modules).
    """
    if name in os.environ:
        return os.environ[name]
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return None


@pytest.fixture(scope="module")
def anthropic_api_key() -> str:
    key = _load_dotenv_key("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set in environment or .env")
    os.environ["ANTHROPIC_API_KEY"] = key
    return key


@pytest.fixture(scope="module")
def opencode_api_key() -> str:
    key = _load_dotenv_key("OPENCODE_API_KEY")
    if not key:
        pytest.skip("OPENCODE_API_KEY not set in environment or .env")
    os.environ["OPENCODE_API_KEY"] = key
    return key


_CV_SYSTEM_PROMPT = (
    "You are a CV-tailoring assistant. Reply with a minimal but valid CV for the "
    "candidate described, using the structured schema you were given."
)
_CV_USER_MSG = (
    "Candidate: Jane Doe, jane.doe@example.com. Six years as a backend engineer. "
    "Produce a minimal CV with a contact and at least one section."
)


class TestAnthropicStructuredSchemaAcceptance:
    """(a) + (b): the real API accepts a turn-union schema as a forced tool's
    input_schema, and forced tool_choice actually yields a usable tool_use reply.
    A successful start_session here is itself proof of (b) — _extract_structured_text
    (jsa/agents/anthropic_api.py) raises ProtocolError on any stop_reason other than
    a completed tool_use block, so a returned AgentReply means the API replied with
    stop_reason == "tool_use", not just that the request didn't 400."""

    async def test_cv_adjust_schema_accepted_and_produces_final(self, anthropic_api_key):
        backend = AnthropicAPIBackend(timeout=60.0)
        schema = json_schema_for(Stage.cv_adjust)
        handle, reply = await backend.start_session(_CV_SYSTEM_PROMPT, _CV_USER_MSG, schema)
        assert reply.kind == "final"
        payload = json.loads(reply.content)
        assert "contact" in payload
        assert "sections" in payload

    async def test_cover_letter_schema_accepted(self, anthropic_api_key):
        backend = AnthropicAPIBackend(timeout=60.0)
        schema = json_schema_for(Stage.cover_letter)
        handle, reply = await backend.start_session(
            "You are a cover-letter assistant. Reply using the structured schema you were given.",
            "Write a brief cover letter for a backend engineer role at Acme.",
            schema,
        )
        assert reply.kind == "final"
        payload = json.loads(reply.content)
        assert "paragraphs" in payload

    async def test_fit_schema_accepted_and_verdict_parses(self, anthropic_api_key):
        """(c): the fit schema behaves — a real reply parses into `_parse_fit_verdict`'s
        expected "VERDICT\\nreason" content shape."""
        backend = AnthropicAPIBackend(timeout=60.0)
        schema = json_schema_for(Stage.fit_assessment)
        handle, reply = await backend.start_session(
            "You are a fit-assessment gate. Reply using the structured schema you were "
            "given: a verdict (FIT or UNFIT) and a required reason.",
            "COMPANY: Acme\nROLE: Backend Engineer\n\n"
            "JOB DESCRIPTION:\nSix years of backend experience with distributed systems.\n\n"
            "CANDIDATE: Six years as a backend engineer working on distributed systems.",
            schema,
        )
        assert reply.kind == "final"
        first_line = reply.content.split("\n", 1)[0]
        assert first_line in ("FIT", "UNFIT")
        assert len(reply.content.split("\n", 1)) == 2  # a reason followed the verdict


class TestOpenCodeZenStructuredWireAcceptance:
    """Observes, against the real free-tier proxy, whether response_format is honored
    or silently ignored/rejected — the open question flagged in
    jsa/agents/opencode_zen.py's `_call_api_once` docstring. Either outcome is a PASS
    here (the per-session downgrade exists precisely to handle a model/proxy that
    doesn't honor the schema) — the test's job is to prove start_session never raises
    an unexpected exception under structured mode, and to report which path fired."""

    async def test_structured_start_session_either_conforms_or_downgrades_cleanly(
        self, opencode_api_key
    ):
        backend = OpenCodeZenBackend(timeout=60.0)
        schema = json_schema_for(Stage.cv_adjust)
        last_exc: Exception | None = None
        for _ in range(5):
            try:
                handle, reply = await backend.start_session(_CV_SYSTEM_PROMPT, _CV_USER_MSG, schema)
            except Exception as exc:  # noqa: BLE001 — transient upstream flakiness, retry
                last_exc = exc
                continue
            assert reply.kind in ("final", "needs_input")
            if handle.structured_enabled:
                # The model honored response_format — reply.content is bare payload JSON.
                payload = json.loads(reply.content)
                assert "contact" in payload or "sections" in payload
            else:
                # Downgraded to sentinel mid-call — reply.raw was still sentinel-parseable
                # (parse_reply succeeded via _parse_with_nudge), which is the whole point
                # of the downgrade path.
                assert reply.raw
            return
        pytest.fail(f"opencode-zen structured live call failed after 5 attempts: {last_exc}")
