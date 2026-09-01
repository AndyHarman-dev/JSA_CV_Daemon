#!/usr/bin/env bash
# Focused Phase 0 probe: ONLY Gemini structured-output schema testing.
# The full /models probes (1a-3) and the go /messages auth probe (5) are settled.
#
# Goal: find which Gemini generationConfig field + schema form actually accepts
# JSA's cv_adjust turn model (which has nested $defs: CVDocument/Contact/Section/Entry).
#
# History:
#   4a responseSchema (raw $defs)  -> FAIL: $defs/$ref/additionalProperties rejected
#   4b responseJsonSchema (raw)    -> TIMEOUT on gemini-3.6-flash
#   4c responseSchema (inlined)    -> FAIL: inliner missed $ref inside anyOf arrays
#   4d responseJsonSchema (inlined)-> FAIL: same incomplete inliner
#
# This script fixes the inliner (truly recursive, handles $ref inside anyOf/items)
# and tries multiple current models with a longer timeout.

set -u
BOLD='\033[1m'; GREEN='\033[32m'; RED='\033[31m'; YEL='\033[33m'; NC='\033[0m'
pass() { echo -e "${GREEN}PASS${NC} $*"; }
fail() { echo -e "${RED}FAIL${NC} $*"; }
skip() { echo -e "${YEL}SKIP${NC} $*"; }
hdr()  { echo -e "\n${BOLD}══ $* ══${NC}"; }

gemini_key="$(printenv GEMINI_API_KEY)"
if [[ -z "$gemini_key" ]]; then gemini_key="$(printenv GOOGLE_API_KEY)"; fi
if [[ -z "$gemini_key" ]]; then
  echo "Set GEMINI_API_KEY or GOOGLE_API_KEY first."; exit 1
fi

# Generate the schema from JSA
schema_file="/tmp/gemini_probe_schema.json"
python3 -c "
from jsa.schema.turn_models import json_schema_for
from jsa.db.models import Stage
import json
with open('$schema_file','w') as f:
    json.dump(json_schema_for(Stage.cv_adjust), f)
print('schema generated -> $schema_file')
"

# ---------------------------------------------------------------------------
# Correct recursive inliner: resolves $ref anywhere, including inside
# anyOf/items arrays. Also strips additionalProperties and $defs.
# ---------------------------------------------------------------------------
inlined_file="/tmp/gemini_schema_inlined.json"
python3 << 'PYEOF'
import json

with open("/tmp/gemini_probe_schema.json") as f:
    schema = json.load(f)

defs = schema.pop("$defs", {})

def inline(obj, defs, _seen=None):
    """Recursively resolve every $ref, including inside anyOf/items arrays."""
    if _seen is None:
        _seen = set()
    if isinstance(obj, dict):
        if "$ref" in obj:
            ref = obj["$ref"]
            key = ref.rsplit("/", 1)[-1]
            if key in _seen:
                return {}  # cycle guard
            return inline(dict(defs[key]), defs, _seen | {key})
        return {k: inline(v, defs, _seen) for k, v in obj.items()}
    if isinstance(obj, list):
        return [inline(v, defs, _seen) for v in obj]
    return obj

def strip_fields(obj, remove):
    """Strip disallowed keys (additionalProperties, title, default, etc.) recursively."""
    if isinstance(obj, dict):
        for k in list(remove & obj.keys()):
            del obj[k]
        for v in obj.values():
            strip_fields(v, remove)
    elif isinstance(obj, list):
        for v in obj:
            strip_fields(v, remove)

inlined = inline(schema, defs)
# Gemini's responseSchema is a restricted OpenAPI subset — strip everything
# it does not recognize. Keep only: type, properties, required, description,
# enum, items, anyOf.
strip_fields(inlined, {"additionalProperties", "title", "default"})

with open("/tmp/gemini_schema_inlined.json", "w") as f:
    json.dump(inlined, f)

# Sanity check: no unresolved $refs should remain
def has_ref(obj):
    if isinstance(obj, dict):
        if "$ref" in obj:
            return True
        return any(has_ref(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_ref(v) for v in obj)
    return False

print("  inlined schema written ->", "/tmp/gemini_schema_inlined.json")
print("  unresolved $refs remaining:", has_ref(inlined))
print("  top-level keys:", list(inlined.keys()))
PYEOF

# Models from Assesser's tiers.py — verified working (Aug 2026):
#   CHEAP=gemini-3.1-flash-lite, MEDIUM=gemini-3-flash-preview, PREMIUM=gemini-3.6-flash
# Order: cheapest/fastest first.
MODELS=(gemini-3.1-flash-lite gemini-3-flash-preview gemini-3.6-flash)
TIMEOUT=45

run_probe() {
    local label="$1" field="$2" model="$3" schema_file="$4"
    echo -e "\n  ${BOLD}$label${NC}"
    python3 -c "
import json
with open('$schema_file') as f:
    schema = json.load(f)
payload = {
    'contents': [{'role':'user','parts':[{'text':'Reply with kind=final, payload={\"contact\":{\"name\":\"Test\"},\"sections\":[]}'}]}],
    'generationConfig': {
        'responseMimeType': 'application/json',
        '$field': schema,
    }
}
with open('/tmp/gemini_run_payload.json','w') as f:
    json.dump(payload, f)
print('  payload written')
"
    resp=$(curl -sS -m "$TIMEOUT" -X POST \
        -H "Content-Type: application/json" \
        "https://generativelanguage.googleapis.com/v1beta/models/$model:generateContent?key=$gemini_key" \
        -d @/tmp/gemini_run_payload.json 2>&1)
    echo "$resp" | python3 -c "
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
except Exception as e:
    print('    NOT JSON:', e)
    print('    Raw (first 400):', raw[:400])
    sys.exit(0)
if 'error' in d:
    print('    ERROR:', d['error'].get('message','')[:400])
else:
    cand = d.get('candidates', [])
    if cand:
        parts = cand[0].get('content', {}).get('parts', [])
        txt = ''.join(p.get('text','') for p in parts)
        print('    OK — reply (first 200):', txt[:200])
        print('    finishReason:', cand[0].get('finishReason'))
    else:
        print('    No candidates:', json.dumps(d)[:300])
"
}

hdr "Probe 4: Gemini structured-output — responseSchema vs responseJsonSchema"

for model in "${MODELS[@]}"; do
    hdr "Trying model: $model"

    # A: responseSchema with inlined schema
    run_probe "A: responseSchema (inlined)" "responseSchema" "$model" "$inlined_file"

    # B: responseJsonSchema with inlined schema
    run_probe "B: responseJsonSchema (inlined)" "responseJsonSchema" "$model" "$inlined_file"

    # Also test responseSchema with RAW schema (to confirm it still fails — baseline)
    run_probe "C: responseSchema (RAW \$defs, baseline)" "responseSchema" "$model" "$schema_file"
done

hdr "Done"
echo "Look for the first 'OK' above — that tells us which (field, model) works."
