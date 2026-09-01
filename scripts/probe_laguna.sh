#!/usr/bin/env bash
# Probe laguna-s-2.1-free's live status and real protocol compatibility.
#
# Context: it showed up in the live `zen/v1/models` listing during the Phase 0 probe
# but has never appeared in the docs table at https://opencode.ai/docs/zen/ (re-checked
# 2026-08-31 — still absent; two other free-tier models have shifted since Phase 0:
# "Big Pickle" is new, "deepseek-v4-flash-free" is gone from the docs snapshot). Docs
# absence alone doesn't prove much either way — the question that actually matters is
# whether it accepts a request on the ONE endpoint jsa/agents/opencode_zen.py hardcodes,
# https://opencode.ai/zen/v1/chat/completions (there is no per-model dispatch there,
# unlike opencode-go's _PROTOCOL table — zen always calls this single endpoint).
#
# Usage:
#   export OPENCODE_API_KEY=...      # or put it in a .env file in this directory / repo root
#   bash scripts/probe_laguna.sh
#
# This script MUST be run by the user so it inherits their exported environment / .env.

set -u

BOLD='\033[1m'; GREEN='\033[32m'; RED='\033[31m'; YEL='\033[33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; }
skip() { echo -e "${YEL}SKIP${NC} $*"; }
hdr()  { echo -e "\n${BOLD}══ $* ══${NC}"; }

# Load a local .env if present and OPENCODE_API_KEY isn't already exported.
if [[ -z "$(printenv OPENCODE_API_KEY)" ]]; then
  for envfile in ./.env "$(dirname "$0")/../.env"; do
    if [[ -f "$envfile" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$envfile"
      set +a
      break
    fi
  done
fi

key="$(printenv OPENCODE_API_KEY)"
if [[ -z "$key" ]]; then
  fail "OPENCODE_API_KEY not set and no .env found with it — export it or add it to a .env file first"
  exit 1
fi

MODEL="laguna-s-2.1-free"

# ----------------------------------------------------------------------------
# Step 1: is it still listed live at all?
# ----------------------------------------------------------------------------
hdr "Step 1: GET https://opencode.ai/zen/v1/models — is $MODEL still listed?"
resp=$(curl -sS -m 15 -H "Authorization: Bearer $key" "https://opencode.ai/zen/v1/models" 2>&1)
rc=$?
if [[ $rc -ne 0 ]]; then
  fail "curl failed (rc=$rc): $resp"
else
  echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('  NOT JSON:', e)
    sys.exit(1)
data = d.get('data', [])
ids = [m.get('id', '?') for m in data if isinstance(m, dict)]
if '$MODEL' in ids:
    print('  FOUND: $MODEL is present in the live listing (', len(ids), 'total models )')
else:
    print('  NOT FOUND: $MODEL is absent from the live listing (', len(ids), 'total models )')
    print('  Free-tier ids present:', [i for i in ids if i.endswith('-free')])
"
fi

post_once() {
  # $1 = endpoint URL. Echoes "STATUS<TAB>BODY" to stdout.
  local url="$1"
  local body
  body=$(python3 -c "
import json
print(json.dumps({
    'model': '$MODEL',
    'messages': [
        {'role': 'system', 'content': 'Reply with exactly one word: OK'},
        {'role': 'user', 'content': 'ping'},
    ],
    'max_tokens': 16,
}))
")
  local resp
  resp=$(curl -sS -m 30 -w '\n---HTTP_STATUS:%{http_code}---' \
    -H "Authorization: Bearer $key" \
    -H "Content-Type: application/json" \
    -d "$body" \
    "$url" 2>&1)
  local status payload
  status=$(echo "$resp" | grep -o 'HTTP_STATUS:[0-9]*' | cut -d: -f2)
  payload=$(echo "$resp" | sed 's/---HTTP_STATUS:[0-9]*---//')
  printf '%s\t%s\n' "$status" "$payload"
}

# ----------------------------------------------------------------------------
# Step 2: does it actually answer on the endpoint we hardcode? Retry with the
# SAME backoff production uses (jsa/agents/opencode_zen.py's _call_api: up to
# _MAX_ATTEMPTS=3, 1.0s then 3.0s between attempts) so a genuinely transient
# outage (matches this backend's documented free-tier flakiness) is told apart
# from a persistent "this model isn't actually served on this endpoint".
# ----------------------------------------------------------------------------
hdr "Step 2: POST https://opencode.ai/zen/v1/chat/completions with model=$MODEL (up to 3 attempts)"
chat_ok=0
for attempt in 1 2 3; do
  echo "  attempt $attempt/3..."
  IFS=$'\t' read -r status payload < <(post_once "https://opencode.ai/zen/v1/chat/completions")
  if [[ "$status" == "200" ]]; then
    echo "$payload" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('    HTTP 200 but not JSON:', e)
    sys.exit(0)
if 'error' in d:
    print('    HTTP 200 with an error envelope (the known zen quirk):', json.dumps(d)[:400])
else:
    content = d.get('choices', [{}])[0].get('message', {}).get('content')
    print('    Reply content:', content)
"
    chat_ok=1
    break
  else
    echo "    HTTP $status: ${payload:0:200}"
    [[ $attempt -lt 3 ]] && { sleep_for=1.0; [[ $attempt -eq 2 ]] && sleep_for=3.0; sleep "$sleep_for"; }
  fi
done
if [[ $chat_ok -eq 1 ]]; then
  pass "/chat/completions succeeded (attempt $attempt) — check above for an error envelope in a 200 body"
else
  fail "/chat/completions failed all 3 attempts — not a one-off blip"
fi

# ----------------------------------------------------------------------------
# Step 3 (diagnostic only): try /responses instead — this is the endpoint
# muse-spark-1.2-contributor-free actually needs instead of /chat/completions.
# opencode_zen.py does NOT support switching endpoints per model today — this
# is purely to learn whether that's laguna's situation too, before deciding
# whether it's worth building that support.
# ----------------------------------------------------------------------------
hdr "Step 3 (diagnostic): POST https://opencode.ai/zen/v1/responses with model=$MODEL"
IFS=$'\t' read -r status payload < <(post_once "https://opencode.ai/zen/v1/responses")
if [[ "$status" == "200" ]]; then
  pass "HTTP 200 from /responses — body: ${payload:0:300}"
else
  echo "  HTTP $status: ${payload:0:300}"
fi

hdr "Verdict"
echo "chat_ok=$chat_ok (1 = /chat/completions worked at least once across 3 tries)"
echo ""
echo "If chat_ok=1 — laguna-s-2.1-free IS /chat/completions-compatible (the earlier 503 was"
echo "a real transient blip, consistent with this backend's known free-tier flakiness). Add it:"
echo "  - DEFAULT_CATALOG[\"opencode-zen\"] in jsa/agents/model_catalog.py"
echo "  - remove it from _ZEN_FREE_EXCLUDE in the same file"
echo "If chat_ok=0 but Step 3 returned a clean 200 — laguna needs /responses, same situation as"
echo "muse-spark. Keep it excluded from the catalog (opencode_zen.py has no per-model endpoint"
echo "dispatch to route it there) unless we're asked to build that."
echo "If chat_ok=0 and Step 3 also failed — genuinely unavailable on this account/tier right"
echo "now. Keep it excluded; worth re-probing later."
