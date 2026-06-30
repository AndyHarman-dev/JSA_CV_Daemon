---
status: Done
---

# Fix: CV Structure Editor section order ignored (Summary forced to top)

## Context

The user reshaped the canonical base CV (`~/.jsa/cv_structure.json`) in the Structure
Editor, deliberately moving the **Summary** section to the *middle* to test whether the
pipeline honors curated section order. The rendered CVs still showed Summary at the top —
appearing to ignore the curated structure.

**Root cause (confirmed against the live DB, not inferred):** this is a **single-layer**
bug in the deterministic serializer, not an LLM-adherence problem.

Evidence from the latest `cv_adjust` document in `~/.jsa/jsa.sqlite` (doc id 7):
- **Stored LLM JSON** (`documents.structured`): `Skills → Experience → Summary → Shipped
  Games → Reference Projects`. The model honored the curated mid-Summary order exactly.
- **Rendered Markdown** (`documents.markdown`, what becomes the PDF/DOCX the user sees):
  `Summary → Skills → Experience → …`. Summary was floated from index 2 to index 0.

The reorder happens in `_ordered_sections()` (`jsa/render/serialize.py:133-137`), which runs
*after* the LLM and floats any Summary/Profile section to the top. Because "the pipeline
(not the LLM) owns 100% of layout", this silently overrides the curated order.

The runtime wiring is correct and not at fault: `server.py:78` passes
`settings.cv_structure_path` → `Orchestrator` → `run_stage(..., cv_structure_path=...)` →
`_build_initial_user_msg`, which injects the skeleton with a "preserve order" instruction.

The user chose to fix the serializer **and** harmonize the prompt/nudge wording, which today
still say "Summary must be first" — a latent conflict with the curated-order goal that could
bite under a different model or in the no-structure fallback.

## Changes

### 1. `jsa/render/serialize.py` — stop floating Summary (the actual fix)
- Rewrite `_ordered_sections()` (L133-137) to **preserve `cv.sections` order verbatim**
  (return `list(cv.sections)`). Keep the function (or inline it) — the Skills *compaction*
  logic is unrelated and stays untouched.
- Update the module docstring (L10-12) and `cv_to_markdown` usage: remove the
  "Summary/Profile is floated to the top" layout policy line; the program no longer reorders.
- `_SUMMARY_RE` is still used by `cv_has_summary` / Skills logic elsewhere — keep it.

> Why this alone fixes it without a fallback regression: when **no** curated structure
> exists, the prompt already instructs the model to lead with a Summary, so the JSON arrives
> Summary-first and an order-preserving serializer renders it Summary-first. Removing the
> float only changes behavior when the JSON intentionally places Summary elsewhere — exactly
> the desired fix.

### 2. `jsa/prompts/PROMPT_CDADJUST.md` — remove the "Summary first" mandate
*(CLAUDE.md marks this file user-maintained; editing it is intentional per the user's choice.)*
- L99-101: change "The **first** section must be a `"Summary"`…" to make order **defer to
  the BASE CV STRUCTURE when present**, and only "lead with a Summary" when no skeleton was
  provided. Keep the "if the base CV has no summary, write one" guidance.
- Confirm L91-93 ("mirror its sections, order, and per-section shape exactly") stays — it's
  the correct instruction; the L99-101 mandate was the contradiction.

### 3. `jsa/pipeline/stages.py` — relax the missing-summary nudge
- `_CV_SUMMARY_NUDGE` (L217-224): drop "as the FIRST entry in `sections`"; ask the model to
  add a `"Summary"` section without forcing its position. It remains a soft nudge that fires
  only when no Summary exists at all (unchanged trigger/semantics).

### 4. Tests
- `tests/backend/test_serialize.py`: replace `test_summary_floated_to_top` (L174-184, which
  asserts the now-removed float) with `test_section_order_preserved` — assert a CV whose
  `sections` list Summary in the middle renders with Summary in the middle (and a Summary-first
  input still renders Summary-first).
- `tests/backend/test_stages.py` `TestCvAdjustSummaryNudge` (L856-895): assertions check for
  presence of `## Summary`, not position — verify they still pass after the nudge reword;
  adjust only the "missing a Summary" substring check if the nudge text changes that phrase.
- Add a regression test (in `test_serialize.py` or `test_stages.py`) mirroring the live-DB
  case: input order `Skills, Experience, Summary, …` → rendered headings preserve that order.

## Verification

1. `pip install -e .` (no new deps; skip if env unchanged).
2. `pytest tests/backend/test_serialize.py tests/backend/test_stages.py -v` — all pass,
   including the new order-preservation tests.
3. `pytest -v -m "not integration"` — no regressions beyond the known pre-existing failures
   (see memory: preexisting-test-debt).
4. Manual end-to-end: with the user's current `~/.jsa/cv_structure.json` (Summary in the
   middle), re-run a job through `cv_adjust` and confirm the rendered PDF/Markdown shows
   Summary in its curated mid position. Re-rendering an existing approved job via
   `POST /api/jobs/{id}/export` is the quickest check.
5. Sanity: a base structure with **no** Summary still yields a Summary section (nudge) and a
   structure with Summary first still renders Summary first.

## Out of scope
- No changes to the structure-editor frontend, the schema, the state machine, or rendering
  backends. The JSON contract and storage path are already correct.
