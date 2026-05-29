"""Sentinel grammar parser: NEED_INPUT / FINAL block detection and ProtocolError."""

import logging
import re
from typing import Literal

from jsa.agents.base import AgentReply

logger = logging.getLogger(__name__)

# Matches complete sentinel blocks (non-greedy so multiple blocks are found separately)
_BLOCK_RE = re.compile(
    r"<<<(NEED_INPUT|FINAL)>>>(.*?)<<<END>>>",
    re.DOTALL,
)

# Matches any open sentinel marker (to detect unterminated blocks)
_OPEN_MARKER_RE = re.compile(r"<<<(?:NEED_INPUT|FINAL)>>>")

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
    - Markdown heading form: ## Change Log or ### Change Log and all text following it
      to end of string (case-insensitive)
    """
    content = _CHANGE_LOG_XML_RE.sub("", content)
    content = _CHANGE_LOG_HEADING_RE.sub("", content)
    return content.strip()


class ProtocolError(Exception):
    pass


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
    else:  # NEED_INPUT
        return AgentReply(raw=raw, content=content, kind="needs_input", question=content)
