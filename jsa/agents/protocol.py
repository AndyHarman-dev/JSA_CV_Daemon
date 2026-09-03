"""Sentinel grammar parser: NEED_INPUT / FINAL / TOOL_CALLS block detection and
ProtocolError."""

import json
import logging
import re
from typing import Literal

from jsa.agents.base import AgentReply, ToolCall

logger = logging.getLogger(__name__)

# Matches complete sentinel blocks (non-greedy so multiple blocks are found separately).
# TOOL_CALLS is the revision-tool-use plan's prompt-rung (rung 2) transport — a model
# whose backend has no native tool-calling channel signals a batch of tool calls as a
# JSON array inside this block instead. Only revising_cv/revising_cl sessions ever
# instruct a model to emit it (jsa/pipeline/prompt_assembly.py's tool contract, Phase 4)
# — see run_stage's guard (jsa/pipeline/stages.py) for what happens if one shows up
# spontaneously in a non-tool session.
_BLOCK_RE = re.compile(
    r"<<<(NEED_INPUT|FINAL|TOOL_CALLS)>>>(.*?)<<<END>>>",
    re.DOTALL,
)

# Matches any open sentinel marker (to detect unterminated blocks)
_OPEN_MARKER_RE = re.compile(r"<<<(?:NEED_INPUT|FINAL|TOOL_CALLS)>>>")

# Optional suggestions block inside a NEED_INPUT body: everything from the marker to
# the end of the (already-extracted) content is the suggestion list, one per line.
# The marker must start its own line (start-of-string or immediately after a newline)
# — otherwise a question that legitimately quotes the literal substring
# "<<<SUGGESTIONS>>>" (e.g. pasted JD text) would have everything after it silently
# truncated and misread as a suggestion list.
_SUGGESTIONS_RE = re.compile(r"(?:^|\n)<<<SUGGESTIONS>>>(.*)", re.DOTALL)

# Defense-in-depth: strip Change Log content if a model places it inside a FINAL block.
# Matches <change_log>...</change_log> (XML-wrapped, case-insensitive).
# Consumes at most one newline on each side to preserve surrounding paragraph structure.
_CHANGE_LOG_XML_RE = re.compile(
    r"\n?[ \t]*<change_log>.*?</change_log>[ \t]*\n?",
    re.DOTALL | re.IGNORECASE,
)
# Matches a ## or ### Change Log heading and its section body (lines until the next heading).
# Uses [^\n]* instead of .* so re.DOTALL is not needed; stops at the next Markdown heading.
_CHANGE_LOG_HEADING_RE = re.compile(
    r"^#{2,3}\s+Change\s+Log\b[^\n]*(?:\n(?!#{1,3}\s)[^\n]*)*",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_change_log(content: str) -> str:
    """Remove any Change Log content from a FINAL block payload.

    Strips both:
    - XML-wrapped form: <change_log>...</change_log> (case-insensitive)
    - Markdown heading form: ## Change Log or ### Change Log plus its section body
      (lines up to the next Markdown heading or end of string, case-insensitive)
    """
    content = _CHANGE_LOG_XML_RE.sub("", content)
    content = _CHANGE_LOG_HEADING_RE.sub("", content)
    return content.strip()


class ProtocolError(Exception):
    pass


def _parse_tool_calls_block(raw: str, content: str) -> AgentReply:
    """Parse a TOOL_CALLS block body as a JSON array of ``{"name", "arguments"}``
    objects, synthesizing ``call_0``, ``call_1``, ... ids (the prompt rung has no
    provider-issued call id — see ``jsa.agents.base.ToolCall``'s docstring).

    Raises ``ProtocolError`` on invalid JSON, a non-array/empty body, or any item
    that isn't ``{"name": str, "arguments": dict}`` — mirrors the strictness of the
    NEED_INPUT/FINAL branches: a malformed tool-call batch fails loudly rather than
    silently executing a partial or misread set of calls.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"malformed TOOL_CALLS block: invalid JSON ({exc})") from exc
    if not isinstance(data, list) or not data:
        raise ProtocolError("malformed TOOL_CALLS block: expected a non-empty JSON array")
    calls: list[ToolCall] = []
    for i, item in enumerate(data):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not item["name"]
            or not isinstance(item.get("arguments"), dict)
        ):
            raise ProtocolError(
                f"malformed TOOL_CALLS block: item {i} must be an object with a "
                "non-empty string 'name' and an object 'arguments'"
            )
        calls.append(ToolCall(id=f"call_{i}", name=item["name"], arguments=item["arguments"]))
    return AgentReply(raw=raw, content=content, kind="tool_calls", tool_calls=calls)


def parse_reply(raw: str) -> AgentReply:
    """Parse a raw agent reply and return an AgentReply.

    Resolution rules (per ARCH.md):
    1. Scan for complete <<<NEED_INPUT>>>...<<<END>>> and <<<FINAL>>>...<<<END>>> blocks.
    2. Exactly one block → valid; classify by kind, content is the inside text.
    3. Zero complete blocks → check for unterminated markers; raise accordingly.
    4. Multiple complete blocks → take the last; log a warning.
    5. Unclosed sentinel (open marker without <<<END>>>) → raise ProtocolError("unterminated block").
    6. Nested sentinels not supported; <<<END>>> is always a literal terminator.
    """
    matches = _BLOCK_RE.findall(raw)

    if len(matches) == 0:
        # No complete blocks found — check for unclosed/unterminated markers
        if _OPEN_MARKER_RE.search(raw):
            raise ProtocolError("unterminated block")
        raise ProtocolError("no sentinel block")

    if len(matches) > 1:
        logger.warning(
            "parse_reply: found %d sentinel blocks in reply; taking the last one. "
            "Agent may have emitted multiple sentinels.",
            len(matches),
        )

    # Take the last complete block
    marker, inner = matches[-1]
    content = inner.strip()

    if marker == "FINAL":
        content = _strip_change_log(content)
        return AgentReply(raw=raw, content=content, kind="final", question=None)
    elif marker == "TOOL_CALLS":
        return _parse_tool_calls_block(raw, content)
    else:  # NEED_INPUT
        suggested_replies: list[str] | None = None
        suggestions_match = _SUGGESTIONS_RE.search(content)
        if suggestions_match:
            question_text = content[: suggestions_match.start()].strip()
            lines = [
                line.strip()
                for line in suggestions_match.group(1).splitlines()
                if line.strip()
            ]
            if lines:
                suggested_replies = lines
            content = question_text
        return AgentReply(
            raw=raw,
            content=content,
            kind="needs_input",
            question=content,
            suggested_replies=suggested_replies,
        )
