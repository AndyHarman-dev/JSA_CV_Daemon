"""Dev auto-answer rules: load from JSON and match questions by substring."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict


class Rule(TypedDict):
    match: str
    answer: str


class Rules(TypedDict):
    default: str
    rules: list[Rule]


def load_rules(path: Path) -> Rules:
    """Read rules file fresh from disk on every call (no caching).

    No caching mirrors jsa/prompts/loader.py so edits take effect without restart.
    """
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    return Rules(default=data["default"], rules=data.get("rules", []))


def match_answer(question: str, rules: Rules) -> str:
    """Return the first rule whose 'match' is a case-insensitive substring of question.

    Falls back to rules['default'] when no rule matches.
    """
    q_lower = question.lower()
    for rule in rules["rules"]:
        if rule["match"].lower() in q_lower:
            return rule["answer"]
    return rules["default"]
