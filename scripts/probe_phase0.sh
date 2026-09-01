#!/usr/bin/env bash
# Phase 0 live probes — run in a shell with exported API keys.
# Usage: source ~/PycharmProjects/Assesser/.env  # or export keys manually
#        bash scripts/probe_phase0.sh
#
# The agent's shell has none of these keys. This script MUST be run by the user
# so it inherits their exported environment. A silent skip is not a pass.

set -u  # undefined vars error out (we check each key before use)

BOLD='\033[1m'; GREEN='\033[32m'; RED='\033[31m'; YEL='\033[33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; }
skip() { echo -e "${YEL}SKIP${NC} $*"; }
hdr()  { echo -e "\n${BOLD}══ $* ══${NC}"; }

# ----------------------------------------------------------------------------
# Probe 1: OpenCode Zen + GO /models listing
# ----------------------------------------------------------------------------
hdr "Probe 1a: GET https://opencode.ai/zen/v1/models"
if key="$(printenv OPENCODE_API_KEY)" && [[ -n "$key" ]]; then
  resp=$(curl -sS -m 15 -H "Authorization: Bearer $key" \
    "https://opencode.ai/zen/v1/models" 2>&1)
  rc=$?
  if [[ $rc -ne 0 ]]; then
    fail "curl failed (rc=$rc): $resp"
  else
    # Show shape + extract model IDs
    echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print('  NOT JSON:', e)
    print('  Raw (first 500 chars):', sys.stdin.read()[:500] if hasattr(sys.stdin, 'read') else '')
    sys.exit(0)
print('  Top-level keys:', list(d.keys()))
data = d.get('data', [])
if isinstance(data, list):
    print('  data[] count:', len(data))
    ids = [m.get('id','?') for m in data if isinstance(m, dict)]
    for i in ids:
        print('   -', i)
else:
    print('  data is not a list — raw shape:', type(d.get('data')).__name__)
    print('  First 400 chars:', json.dumps(d)[:400])
"
    pass "zen /models fetched"
  fi
else
  skip "OPENCODE_API_KEY not set — cannot probe zen /models"
fi

hdr "Probe 1b: GET https://opencode.ai/zen/go/v1/models"
go_key="$(printenv OPENCODE_GO_API_KEY)"
if [[ -z "$go_key" ]]; then go_key="$(printenv OPENCODE_API_KEY)"; fi
if [[ -n "$go_key" ]]; then
  resp=$(curl -sS -m 15 -H "Authorization: Bearer $go_key" \
    "https://opencode.ai/zen/go/v1/models" 2>&1)
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
    sys.exit(0)
print('  Top-level keys:', list(d.keys()))
data = d.get('data', [])
if isinstance(data, list):
    print('  data[] count:', len(data))
    ids = [m.get('id','?') for m in data if isinstance(m, dict)]
    for i in ids:
        print('   -', i)
else:
    print('  data is not a list — raw shape:', type(d.get('data')).__name__)
    print('  First 400 chars:', json.dumps(d)[:400])
"
    pass "go /models fetched"
  fi
else
  skip "Neither OPENCODE_GO_API_KEY nor OPENCODE_API_KEY set — cannot probe go /models"
fi

# ----------------------------------------------------------------------------
# Probe 1c: OpenRouter /models
# ----------------------------------------------------------------------------
hdr "Probe 1c: GET https://openrouter.ai/api/v1/models"
if key="$(printenv OPENROUTER_API_KEY)" && [[ -n "$key" ]]; then
  resp=$(curl -sS -m 15 -H "Authorization: Bearer $key" \
    "https://openrouter.ai/api/v1/models" 2>&1)
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
    sys.exit(0)
print('  Top-level keys:', list(d.keys()))
data = d.get('data', [])
if isinstance(data, list):
    print('  data[] count:', len(data))
    print('  total_count:', d.get('total_count'))
    # Show first 15 models + whether they support response_format / structured_outputs
    for m in data[:15]:
        sid = m.get('id','?')
        name = m.get('name','?')
        params = m.get('supported_parameters', [])
        flags = []
        if 'response_format' in params: flags.append('response_format')
        if 'structured_outputs' in params: flags.append('structured_outputs')
        flags_str = '  [' + ', '.join(flags) + ']' if flags else ''
        print('   -', sid, '(', name, ')', flags_str)
    print('  ... (showing first', min(15, len(data)), 'of', len(data), ')')
else:
    print('  data is not a list — first 400 chars:', json.dumps(d)[:400])
"
    pass "openrouter /models fetched"
  fi
else
  skip "OPENROUTER_API_KEY not set — cannot probe openrouter /models"
fi

# ----------------------------------------------------------------------------
# Probe 2: Mistral /models
# ----------------------------------------------------------------------------
hdr "Probe 2: GET https://api.mistral.ai/v1/models"
if key="$(printenv MISTRAL_API_KEY)" && [[ -n "$key" ]]; then
  resp=$(curl -sS -m 15 -H "Authorization: Bearer $key" \
    "https://api.mistral.ai/v1/models" 2>&1)
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
    sys.exit(0)
print('  Top-level keys:', list(d.keys()))
data = d.get('data', [])
if isinstance(data, list):
    print('  data[] count:', len(data))
    ids = [m.get('id','?') for m in data if isinstance(m, dict)]
    for i in ids:
        print('   -', i)
else:
    print('  data is not a list — raw shape:', type(d.get('data')).__name__)
    print('  First 400 chars:', json.dumps(d)[:400])
"
    pass "mistral /models fetched"
  fi
else
  skip "MISTRAL_API_KEY not set — cannot probe mistral /models"
fi

# ----------------------------------------------------------------------------
# Probe 3: Gemini /models
# ----------------------------------------------------------------------------
hdr "Probe 3: GET https://generativelanguage.googleapis.com/v1beta/models"
gemini_key="$(printenv GEMINI_API_KEY)"
if [[ -z "$gemini_key" ]]; then gemini_key="$(printenv GOOGLE_API_KEY)"; fi
if [[ -n "$gemini_key" ]]; then
  resp=$(curl -sS -m 15 \
    "https://generativelanguage.googleapis.com/v1beta/models?key=$gemini_key" 2>&1)
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
    sys.exit(0)
print('  Top-level keys:', list(d.keys()))
models = d.get('models', [])
if isinstance(models, list):
    print('  models[] count:', len(models))
    for m in models:
        name = m.get('name','?')
        methods = m.get('supportedGenerationMethods', [])
        # Only show models supporting generateContent
        if 'generateContent' in methods:
            print('   -', name, '  [generateContent]')
else:
    print('  models is not a list — first 400 chars:', json.dumps(d)[:400])
"
    pass "gemini /models fetched"
  fi
else
  skip "Neither GEMINI_API_KEY nor GOOGLE_API_KEY set — cannot probe gemini /models"
fi

# ----------------------------------------------------------------------------
# Probe 4: Gemini structured-output schema probe
# ----------------------------------------------------------------------------
hdr "Probe 4: Gemini structured-output schema (responseSchema vs responseJsonSchema)"
if [[ -z "$gemini_key" ]]; then
  skip "No Gemini key — cannot probe structured output"
else
  schema_file="/tmp/gemini_probe_schema.json"
  if [[ ! -f "$schema_file" ]]; then
    # Generate it from JSA itself
    python3 -c "
from jsa.schema.turn_models import json_schema_for
from jsa.db.models import Stage
import json
with open('$schema_file','w') as f:
    json.dump(json_schema_for(Stage.cv_adjust), f)
print('  schema generated')
"
  fi
  # --- 4a: responseSchema ---
  echo -e "\n  ${BOLD}4a: generationConfig.responseSchema${NC}"
  # Build the payload in Python, writing to a temp file — avoids shell-quoting
  # breakage from newlines inside the schema's description strings.
  python3 -c "
import json
with open('$schema_file') as f:
    schema = json.load(f)
payload = {
    'contents': [{'role':'user','parts':[{'text':'Reply with kind=final, payload={\"contact\":{\"name\":\"Test\"},\"sections\":[]}'}]}],
    'generationConfig': {
        'responseMimeType': 'application/json',
        'responseSchema': schema,
    }
}
with open('/tmp/gemini_payload_a.json','w') as f:
    json.dump(payload, f)
print('  payload written')
"
  resp_a=$(curl -sS -m 30 -X POST \
    -H "Content-Type: application/json" \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$gemini_key" \
    -d @/tmp/gemini_payload_a.json 2>&1)
  echo "$resp_a" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('    NOT JSON:', e)
    print('    Raw (first 600):', raw[:600])
    sys.exit(0)
if 'error' in d:
    print('    ERROR response:', json.dumps(d['error'], indent=2)[:500])
else:
    cand = d.get('candidates', [])
    if cand:
        parts = cand[0].get('content', {}).get('parts', [])
        txt = ''.join(p.get('text','') for p in parts)
        print('    OK — reply text (first 300):', txt[:300])
        print('    finishReason:', cand[0].get('finishReason'))
    else:
        print('    No candidates — full body:', json.dumps(d)[:400])
"

  # --- 4b: responseJsonSchema ---
  echo -e "\n  ${BOLD}4b: generationConfig.responseJsonSchema${NC}"
  python3 -c "
import json
with open('$schema_file') as f:
    schema = json.load(f)
payload = {
    'contents': [{'role':'user','parts':[{'text':'Reply with kind=final, payload={\"contact\":{\"name\":\"Test\"},\"sections\":[]}'}]}],
    'generationConfig': {
        'responseMimeType': 'application/json',
        'responseJsonSchema': schema,
    }
}
with open('/tmp/gemini_payload_b.json','w') as f:
    json.dump(payload, f)
print('  payload written')
"
  resp_b=$(curl -sS -m 30 -X POST \
    -H "Content-Type: application/json" \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$gemini_key" \
    -d @/tmp/gemini_payload_b.json 2>&1)
  echo "$resp_b" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('    NOT JSON:', e)
    print('    Raw (first 600):', raw[:600])
    sys.exit(0)
if 'error' in d:
    print('    ERROR response:', json.dumps(d['error'], indent=2)[:500])
else:
    cand = d.get('candidates', [])
    if cand:
        parts = cand[0].get('content', {}).get('parts', [])
        txt = ''.join(p.get('text','') for p in parts)
        print('    OK — reply text (first 300):', txt[:300])
        print('    finishReason:', cand[0].get('finishReason'))
    else:
        print('    No candidates — full body:', json.dumps(d)[:400])
"
  # --- 4c: responseSchema with inlined $defs (fallback per plan Phase 3) ---
  echo -e "\n  ${BOLD}4c: generationConfig.responseSchema (inlined \$defs)${NC}"
  python3 -c "
import json
with open('$schema_file') as f:
    schema = json.load(f)

# Inline \$ref/\$defs — Gemini's responseSchema rejects \$ref and \$defs.
def inline(obj, defs):
    if not isinstance(obj, dict):
        return obj
    if '\$ref' in obj:
        ref = obj['\$ref']
        key = ref.split('/')[-1]
        return inline(dict(defs[key]), defs)
    return {k: inline(v, defs) for k, v in obj.items()}

defs = schema.get('\$defs', {})
inlined = inline({k: v for k, v in schema.items() if k != '\$defs'}, defs)
# Gemini rejects additionalProperties at nested levels too — strip it
def strip_ap(obj):
    if not isinstance(obj, dict):
        return obj
    obj.pop('additionalProperties', None)
    for v in obj.values():
        strip_ap(v)
    return obj
strip_ap(inlined)
# Keep only what Gemini accepts: type, properties, required, description, enum
for keep in ('type','properties','required','description','enum','items'):
    pass

payload = {
    'contents': [{'role':'user','parts':[{'text':'Reply with kind=final, payload={\"contact\":{\"name\":\"Test\"},\"sections\":[]}'}]}],
    'generationConfig': {
        'responseMimeType': 'application/json',
        'responseSchema': inlined,
    }
}
with open('/tmp/gemini_payload_c.json','w') as f:
    json.dump(payload, f)
print('  inlined payload written')
"
  resp_c=$(curl -sS -m 30 -X POST \
    -H "Content-Type: application/json" \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$gemini_key" \
    -d @/tmp/gemini_payload_c.json 2>&1)
  echo "$resp_c" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('    NOT JSON:', e)
    print('    Raw (first 600):', raw[:600])
    sys.exit(0)
if 'error' in d:
    print('    ERROR response:', json.dumps(d['error'], indent=2)[:500])
else:
    cand = d.get('candidates', [])
    if cand:
        parts = cand[0].get('content', {}).get('parts', [])
        txt = ''.join(p.get('text','') for p in parts)
        print('    OK — reply text (first 300):', txt[:300])
        print('    finishReason:', cand[0].get('finishReason'))
    else:
        print('    No candidates — full body:', json.dumps(d)[:400])
"

  # --- 4d: responseJsonSchema with inlined $defs ---
  echo -e "\n  ${BOLD}4d: generationConfig.responseJsonSchema (inlined \$defs)${NC}"
  python3 -c "
import json
with open('$schema_file') as f:
    schema = json.load(f)
def inline(obj, defs):
    if not isinstance(obj, dict):
        return obj
    if '\$ref' in obj:
        ref = obj['\$ref']
        key = ref.split('/')[-1]
        return inline(dict(defs[key]), defs)
    return {k: inline(v, defs) for k, v in obj.items()}
defs = schema.get('\$defs', {})
inlined = inline({k: v for k, v in schema.items() if k != '\$defs'}, defs)
def strip_ap(obj):
    if not isinstance(obj, dict):
        return obj
    obj.pop('additionalProperties', None)
    for v in obj.values():
        strip_ap(v)
    return obj
strip_ap(inlined)
payload = {
    'contents': [{'role':'user','parts':[{'text':'Reply with kind=final, payload={\"contact\":{\"name\":\"Test\"},\"sections\":[]}'}]}],
    'generationConfig': {
        'responseMimeType': 'application/json',
        'responseJsonSchema': inlined,
    }
}
with open('/tmp/gemini_payload_d.json','w') as f:
    json.dump(payload, f)
print('  inlined payload written')
"
  resp_d=$(curl -sS -m 30 -X POST \
    -H "Content-Type: application/json" \
    "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key=$gemini_key" \
    -d @/tmp/gemini_payload_d.json 2>&1)
  echo "$resp_d" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('    NOT JSON:', e)
    print('    Raw (first 600):', raw[:600])
    sys.exit(0)
if 'error' in d:
    print('    ERROR response:', json.dumps(d['error'], indent=2)[:500])
else:
    cand = d.get('candidates', [])
    if cand:
        parts = cand[0].get('content', {}).get('parts', [])
        txt = ''.join(p.get('text','') for p in parts)
        print('    OK — reply text (first 300):', txt[:300])
        print('    finishReason:', cand[0].get('finishReason'))
    else:
        print('    No candidates — full body:', json.dumps(d)[:400])
"

  pass "gemini structured-output probe done (compare 4a/4b raw vs 4c/4d inlined)"
fi

# ----------------------------------------------------------------------------
# Probe 5: OpenCode-GO /messages auth header shape
# ----------------------------------------------------------------------------
hdr "Probe 5: OpenCode-GO /messages (Anthropic-shape) auth header"
go_key="$(printenv OPENCODE_GO_API_KEY)"
if [[ -z "$go_key" ]]; then go_key="$(printenv OPENCODE_API_KEY)"; fi
if [[ -z "$go_key" ]]; then
  skip "Neither OPENCODE_GO_API_KEY nor OPENCODE_API_KEY set — cannot probe /messages"
else
  # Try BOTH auth shapes so we can see which one the gateway actually wants.
  # NOTE: the correct URL is /zen/go/v1/messages. Assesser's opencode_provider.py
  # strips /v1 because the Anthropic SDK appends /v1/messages to base_url itself;
  # here we call the raw endpoint directly, so we include /v1.
  # Shape A: x-api-key header (Anthropic SDK convention, per Assesser)
  echo -e "\n  ${BOLD}5a: x-api-key header${NC}"
  resp_xapi=$(curl -sS -m 20 -w "\nHTTP_STATUS:%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -H "x-api-key: $go_key" \
    -H "anthropic-version: 2023-06-01" \
    "https://opencode.ai/zen/go/v1/messages" \
    -d '{"model":"qwen3.8-max","max_tokens":50,"messages":[{"role":"user","content":"Say hello in one word."}]}' 2>&1)
  echo "$resp_xapi" | tail -1 | grep -q "HTTP_STATUS:200" && pass "x-api-key → 200" || echo "  x-api-key response: $(echo "$resp_xapi" | tail -1)"

  # Shape B: Authorization: Bearer (OpenAI convention)
  echo -e "\n  ${BOLD}5b: Authorization: Bearer header${NC}"
  resp_bearer=$(curl -sS -m 20 -w "\nHTTP_STATUS:%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $go_key" \
    "https://opencode.ai/zen/go/v1/messages" \
    -d '{"model":"qwen3.8-max","max_tokens":50,"messages":[{"role":"user","content":"Say hello in one word."}]}' 2>&1)
  echo "$resp_bearer" | tail -1 | grep -q "HTTP_STATUS:200" && pass "Bearer → 200" || echo "  Bearer response: $(echo "$resp_bearer" | tail -1)"

  # Show the body of whichever succeeded (or both if both failed)
  echo -e "\n  ${BOLD}Response bodies (first 400 chars each):${NC}"
  echo "  5a body:"; echo "$resp_xapi" | head -n -1 | head -c 400; echo
  echo "  5b body:"; echo "$resp_bearer" | head -n -1 | head -c 400; echo
  pass "openCode-GO /messages probe done"
fi

hdr "All probes complete"
echo "Copy this output back so findings can be recorded in the plan's Change Log."
