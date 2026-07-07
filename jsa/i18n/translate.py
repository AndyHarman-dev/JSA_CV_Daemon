"""Incremental UI-string translation generator.

Build-time script (NOT imported by the server) that keeps the frontend's per-locale
translation catalogs in sync with the hand-maintained English source,
``frontend/src/i18n/strings.en.json``. Run it via ``scripts/translate-ui.sh``.

For every locale in ``jsa.i18n.languages.LANGUAGES`` (except ``en``), translates only the
keys that are missing from ``strings.<code>.json`` or whose English source value changed
since the last run (tracked by a sha256 hash in ``strings.meta.json``) — so re-running after
adding a handful of new strings costs one small API call per locale, not a full retranslation.

Two backends, mirroring the two ways ``jsa/agents/`` talks to Claude
(``jsa/agents/anthropic_api.py`` vs ``jsa/agents/claude_cli.py``):

- ``api`` (default) — ``anthropic.Anthropic()`` sync client. Needs ``ANTHROPIC_API_KEY``.
- ``cli`` — one-shot, non-interactive ``claude -p`` subprocess (no session/resume needed,
  unlike the pipeline's ``ClaudeCliBackend`` — there's nothing to resume for a single batch
  translation call). Needs the Claude Code CLI installed and logged in; no API key required.

Usage:
    python -m jsa.i18n.translate                    # incremental update, all locales, API backend
    python -m jsa.i18n.translate --backend cli       # same, but via the Claude Code CLI
    python -m jsa.i18n.translate --force             # retranslate every key, all locales
    python -m jsa.i18n.translate --only es           # single locale
    python -m jsa.i18n.translate --check             # exit 1 if any locale is missing keys (CI)
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from jsa.i18n.languages import LANGUAGES, language_name

_I18N_DIR = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n"
_SOURCE_PATH = _I18N_DIR / "strings.en.json"
_META_PATH = _I18N_DIR / "strings.meta.json"
_MAX_WORKERS = 8  # locales are independent; each is one subprocess/API call


def _locale_path(code: str) -> Path:
    return _I18N_DIR / f"strings.{code}.json"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _keys_needing_translation(
    source: dict[str, str], existing: dict[str, str], meta_for_locale: dict[str, str], force: bool
) -> dict[str, str]:
    """Return {key: english_value} for keys that are new, changed, or force-retranslated."""
    pending: dict[str, str] = {}
    for key, english_value in source.items():
        if force:
            pending[key] = english_value
            continue
        last_hash = meta_for_locale.get(key)
        if key not in existing or last_hash != _hash(english_value):
            pending[key] = english_value
    return pending


def _build_prompt(pending: dict[str, str], code: str) -> str:
    name = language_name(code)
    return (
        f"Translate the string VALUES in this JSON object from English into {name} ({code}).\n"
        "Keep every JSON key exactly as-is — only translate the values.\n"
        "Preserve placeholders like {n}, {count}, {name} exactly, verbatim, in the translated "
        "text.\n"
        "Preserve punctuation/casing conventions natural to the target language for UI labels "
        "(short, terse, often uppercase for buttons — mirror the English style).\n"
        "Return ONLY a JSON object with the same keys and translated values — no commentary, "
        "no markdown fences.\n\n"
        f"{json.dumps(pending, ensure_ascii=False, indent=2)}"
    )


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return raw


def _translate_batch_api(pending: dict[str, str], code: str) -> dict[str, str]:
    """Call the Anthropic API once to translate a batch of English strings into `code`.

    Mirrors ``jsa/agents/anthropic_api.py``'s ``_call_api`` — a per-call sync client.
    """
    import anthropic

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8192,
            messages=[{"role": "user", "content": _build_prompt(pending, code)}],
        )
    finally:
        client.close()
    return json.loads(_strip_fences(response.content[0].text))


def _translate_batch_cli(pending: dict[str, str], code: str) -> dict[str, str]:
    """Translate a batch via a one-shot, non-interactive `claude -p` subprocess.

    Mirrors ``jsa/agents/claude_cli.py``'s non-interactive print-mode invocation
    (``claude --output-format text -p <msg>``). Unlike the pipeline's
    ``ClaudeCliBackend``, this is a single stateless call — no ``--session-id``/
    ``--resume`` and no sentinel-block protocol, since there is no multi-turn
    conversation or `<<<FINAL>>>` parser involved, just one batch of strings in
    and one JSON object out.
    """
    cmd = ["claude", "--output-format", "text", "--model", "haiku", "-p", _build_prompt(pending, code)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '(no stderr)'}"
        )
    return json.loads(_strip_fences(result.stdout))


def _translate_batch(pending: dict[str, str], code: str, backend: str) -> dict[str, str]:
    if backend == "cli":
        return _translate_batch_cli(pending, code)
    return _translate_batch_api(pending, code)


def run(
    force: bool = False, only: str | None = None, check: bool = False, backend: str = "api"
) -> int:
    source = _load_json(_SOURCE_PATH)
    if not source:
        print(f"No source strings at {_SOURCE_PATH}", file=sys.stderr)
        return 1

    meta = _load_json(_META_PATH)
    codes = [code for code, _, _ in LANGUAGES if code != "en"]
    if only:
        codes = [c for c in codes if c == only]

    pending_by_code = {
        code: pending
        for code in codes
        for pending in [_keys_needing_translation(source, _load_json(_locale_path(code)), meta.get(code, {}), force)]
        if pending
    }

    if check:
        for code, pending in pending_by_code.items():
            print(f"[check] {code}: {len(pending)} key(s) need translation", file=sys.stderr)
        return 1 if pending_by_code else 0

    if not pending_by_code:
        return 0

    # Locales are independent (own subprocess/API call, own output file) — translate
    # them concurrently instead of one at a time. File writes happen back on the
    # main thread as results complete, so strings.meta.json is never written from
    # two threads at once.
    had_error = False
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(_MAX_WORKERS, len(pending_by_code))
    ) as pool:
        future_to_code = {
            pool.submit(_translate_batch, pending, code, backend): code
            for code, pending in pending_by_code.items()
        }
        print(
            f"Translating {len(future_to_code)} locale(s) via {backend} "
            f"({min(_MAX_WORKERS, len(future_to_code))} at a time)..."
        )
        for future in concurrent.futures.as_completed(future_to_code):
            code = future_to_code[future]
            pending = pending_by_code[code]
            try:
                translated = future.result()
            except Exception as exc:
                had_error = True
                print(f"[{code}] translation failed: {exc}", file=sys.stderr)
                continue

            existing = _load_json(_locale_path(code))
            existing.update(translated)
            _locale_path(code).write_text(
                json.dumps(dict(sorted(existing.items())), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            meta_for_locale = meta.get(code, {})
            meta_for_locale.update({key: _hash(value) for key, value in pending.items()})
            meta[code] = meta_for_locale
            _META_PATH.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
            print(f"Done: {len(pending)} key(s) -> {language_name(code)} ({code})")

    return 1 if had_error else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="retranslate every key")
    parser.add_argument("--only", metavar="CODE", help="only update this locale")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if any locale is missing translations"
    )
    parser.add_argument(
        "--backend",
        choices=["api", "cli"],
        default="api",
        help="'api' (default) uses the Anthropic API (needs ANTHROPIC_API_KEY); "
        "'cli' shells out to the Claude Code CLI (`claude -p`), no API key needed",
    )
    args = parser.parse_args()
    sys.exit(run(force=args.force, only=args.only, check=args.check, backend=args.backend))


if __name__ == "__main__":
    main()
