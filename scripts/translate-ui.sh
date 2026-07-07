#!/usr/bin/env bash
# Regenerate frontend/src/i18n/strings.<code>.json from strings.en.json.
# Incremental by default (only new/changed keys hit the model) — see jsa/i18n/translate.py.
#
# Two backends (--backend api|cli), mirroring jsa/agents/anthropic_api.py vs
# jsa/agents/claude_cli.py:
#   api (default) — Anthropic API via the SDK. Needs ANTHROPIC_API_KEY.
#   cli           — one-shot `claude -p` subprocess. Needs the Claude Code CLI
#                    installed and logged in; no API key required.
#
# Usage:
#   ./scripts/translate-ui.sh                    # incremental update, all locales, API backend
#   ./scripts/translate-ui.sh --backend cli      # same, but via the Claude Code CLI
#   ./scripts/translate-ui.sh --force            # retranslate every key
#   ./scripts/translate-ui.sh --only es          # single locale
#   ./scripts/translate-ui.sh --check            # CI: exit 1 if any locale is behind
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

backend="api"
prev=""
for arg in "$@"; do
  if [ "$prev" = "--backend" ]; then
    backend="$arg"
  fi
  prev="$arg"
done

if [ "$backend" = "api" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ANTHROPIC_API_KEY is not set — the Anthropic SDK needs it to translate." >&2
  echo "Pass --backend cli to translate via the Claude Code CLI instead (no API key needed)." >&2
  exit 1
fi

python -m jsa.i18n.translate "$@"
