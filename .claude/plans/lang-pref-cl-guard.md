---
status: Done
---

# Sub-plan: per-language cover-letter guard (`_not_a_cover_letter`)

Part of the language-preference feature (see `.claude/plans/streamed-meandering-deer.md`
Workstream C). Parent context: once the pipeline can emit non-English CV/cover-letter output
(Workstream B), the English-only `_not_a_cover_letter` guard in `jsa/schema/cv.py` goes blind —
a non-English cover letter mis-emitted as the CV would sail through undetected. Decision: fix
now with **per-language formula lists**, not a ship-with-caveat.

## What exists today (read this first)

- `jsa/schema/cv.py:94-111` — `_LETTER_FORMULA_RE`: a single English regex alternation of
  letter idioms ("writing to express", "dear hiring", "sincerely,", "yours sincerely/faithfully",
  "thank you for consider...", "look forward to hearing/discussing", "i am writing/applying to/for",
  "i'm applying for", "would welcome the opportunity/discussing/the chance", "ready to
  contribute immediately"). `_LETTER_MATCH_THRESHOLD = 2` (`cv.py:111`).
- `jsa/schema/cv.py:355-372` — `_not_a_cover_letter`, a `@model_validator(mode="after")` on
  `CVDocument` (`cv.py:339`). Calls `_LETTER_FORMULA_RE.findall(_cv_text_blob(self.sections))`
  (`_cv_text_blob` at `cv.py:311-325`); `len(hits) >= _LETTER_MATCH_THRESHOLD` → raises
  `ValueError` with a message quoting the matched phrases.
- Pydantic v2 model validators can accept a `ValidationInfo` param
  (`def _not_a_cover_letter(self, info: ValidationInfo) -> "CVDocument"`) whose `.context`
  is whatever dict was passed to `model_validate(data, context={...})`. This is the mechanism
  to make the guard language-aware without adding a constructor field to the schema itself.
- Three call sites construct a `CVDocument` from raw dict data — all three need the ability to
  pass a `context={"language": code}` (or none, defaulting to English formulas):
  - `jsa/api/routes_cv_structure.py:53` — `CVDocument.model_validate(body.structured)` (editor
    PUT; no pipeline language concept here — the CV Structure Editor's saved base structure is
    not itself a pipeline output. **Leave this call site without a language context** — the
    editor's structure isn't LLM output being gated for cover-letter-ness in a *different*
    language than English was assumed; if there's ambiguity, default (no context) → English
    formulas only, which is today's behavior, so this call site is a no-op change.)
  - `jsa/pipeline/infer_structure.py:111` — `CVDocument.model_validate(data)` (structure
    inference from an uploaded CV). Same reasoning — leave as-is / English default, unless
    Workstream B decided to thread language into infer_structure too, in which case mirror the
    cv_adjust site below. Check `jsa/pipeline/stages.py` at the time you do this work for
    whether `infer_structure.py` gained a language parameter; if so, thread it through here the
    same way.
  - `jsa/pipeline/stages.py:176` — inside `_parse_structured(content, model, label, job_id=None)`,
    `return model.model_validate(data)`. **This is the one that matters**: it's called from
    `_validate_final_content(stage, content, job)` (`stages.py:191-205`), which is called from
    two places (`stages.py:294` and `stages.py:773`) inside the cv_adjust/cover_letter self-heal
    and final-handling paths — i.e. actual LLM output for a job that may be running in any
    configured language.

## Required change

1. **`jsa/schema/cv.py`**:
   - Add a `dict[str, tuple[str, ...]]`-shaped per-language letter-formula catalog,
     e.g. `_LETTER_FORMULAS_BY_LANG: dict[str, tuple[str, ...]]` keyed by the same ISO codes as
     `jsa.i18n.languages.LANGUAGES` (import `from jsa.i18n.languages import CODES` only to
     validate, not to require every language — start with a **practical subset**: `en` plus at
     minimum `es`, `fr`, `de`, `pt`, `it` hand-translated idiom lists mirroring the existing
     12 English phrases each — e.g. Spanish: `"le escribo para expresar"`, `"estimado/a
     encargado"`, `"atentamente,"`, `"agradezco su consideración"`, `"espero (?:tener la
     oportunidad|conversar)"`, `"escribo para postular"`, etc. French/German/Portuguese/Italian
     analogues in the same spirit (translate the 12 English idioms, don't invent new ones).
   - Compile each language's tuple into its own `re.Pattern` (mirror `_LETTER_FORMULA_RE`'s
     `re.I` flag), e.g. `_LETTER_FORMULA_RE_BY_LANG: dict[str, re.Pattern]`.
   - Add a lookup helper `_letter_formula_re(language: str) -> re.Pattern` that returns the
     language's compiled pattern, **falling back to the English pattern** for any language not
     yet in the catalog (so an unauthored language degrades to today's behavior rather than
     silently disabling the guard — a partial guard beats no guard).
   - Change `_not_a_cover_letter` to accept `info: ValidationInfo` (pydantic v2 signature:
     `from pydantic import ValidationInfo`) and read `language = (info.context or {}).get("language", "en")`,
     then use `_letter_formula_re(language)` instead of the module-level `_LETTER_FORMULA_RE`.
     Keep `_LETTER_FORMULA_RE` itself intact as the `"en"` entry (or alias it) — do not delete
     the existing English-only regex, other code/tests may reference it.
   - Keep `_LETTER_MATCH_THRESHOLD = 2` shared across all languages (no evidence it should vary).

2. **`jsa/pipeline/stages.py`**:
   - `_parse_structured(content, model, label, job_id=None, language: str = "en")` — add a
     `language` param, pass `context={"language": language}` into `model.model_validate(data,
     context=...)` at `stages.py:176`. (Only `CVDocument` actually reads the context; `CoverLetter`
     validation ignoring an unused context key is harmless — do not special-case by model type.)
   - `_validate_final_content(stage, content, job, language: str = "en")` — thread `language`
     through to `_parse_structured`.
   - Update both call sites (`stages.py:294`, `stages.py:773`) to pass the job's resolved
     language. **Coordinate with Workstream B** (pipeline language directive injection, done by
     the main session in parallel): B is threading a resolved `language` string through
     `run_stage` already (from `preferences_path`). If B's changes have landed by the time you
     implement this, reuse that same resolved value at these two call sites rather than
     re-reading preferences yourself. If B has NOT landed yet, add the `language: str = "en"`
     parameter defaulting to `"en"` so existing callers keep working unchanged, and leave a
     one-line note at each call site (`# TODO(lang-pref): pass resolved job language once
     threaded — see Workstream B`) for the main session to fill in the real value. Do not block
     on B — ship the guard fix functional with the default, threading-ready signature.

## Tests (`tests/backend/test_schema_cv.py` or new `tests/backend/test_cv_lang_guard.py`)

- English cover-letter-as-CV (existing `_COVER_LETTER_AS_CV` fixture in
  `tests/backend/test_cv_structure.py`) still rejects with no context (default English).
- A Spanish cover-letter-shaped payload (≥2 Spanish letter idioms) passed as
  `CVDocument.model_validate(data, context={"language": "es"})` → raises `ValueError` /
  `ValidationError` mentioning cover-letter.
- The same Spanish payload validated **without** a context (defaults to English formulas) does
  **NOT** trip the guard — demonstrates the exact regression this fix closes (this is the "before"
  behavior you're proving is fixed when the context IS passed).
- A legitimate non-English (Spanish/French) CV — real résumé content, no letter idioms — passes
  validation with `context={"language": "<code>"}` (no false positive).
- An unauthored language code (e.g. `"th"`) falls back to the English pattern (no crash, still
  gates against literal English letter idioms if present).

## Verification
`pytest tests/backend/test_cv_lang_guard.py tests/backend/test_cv_structure.py -v` — all pass;
no change to existing English-only test behavior (backward compatible default).
