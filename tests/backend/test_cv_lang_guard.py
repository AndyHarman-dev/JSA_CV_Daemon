"""Per-language cover-letter content-kind guard (`CVDocument._not_a_cover_letter`).

Once the pipeline can emit non-English CV/cover-letter output, an English-only guard goes
blind to a non-English cover letter mis-emitted as a CV. Covers the pydantic v2
`ValidationInfo.context`-driven per-language formula lookup added to `jsa/schema/cv.py`:
default (no context) stays English-only (backward compatible), an explicit
`context={"language": <code>}` selects that language's formula list, a legitimate non-English
CV does not false-positive, and an unauthored language code falls back to the English pattern.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from jsa.schema import CVDocument

# --- fixtures ----------------------------------------------------------------------------

# A cover letter wearing CV shape (English) — mirrors _COVER_LETTER_AS_CV in
# tests/backend/test_cv_structure.py — must trip the guard with no context (default English).
_ENGLISH_COVER_LETTER_AS_CV = {
    "contact": {"name": "Jane Doe", "email": "jane@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the role.",
        "I would welcome discussing it further. Sincerely, Jane",
    ]}],
}

# A cover letter wearing CV shape, in Spanish — ≥2 Spanish letter idioms.
_SPANISH_COVER_LETTER_AS_CV = {
    "contact": {"name": "Juana Perez", "email": "juana@x.com"},
    "sections": [{"name": "", "items": [
        "Le escribo para expresar mi sincero interés en el puesto.",
        "Espero tener noticias suyas pronto. Atentamente, Juana",
    ]}],
}

# A legitimate Spanish CV — real résumé content, no letter idioms.
_SPANISH_CV = {
    "contact": {"name": "Juana Perez", "email": "juana@x.com", "location": "Madrid"},
    "sections": [
        {"name": "Resumen", "text": "Ingeniera de software con seis años de experiencia."},
        {"name": "Experiencia", "entries": [
            {"heading": "Desarrolladora Senior", "subheading": "Acme", "dates": "2020-Presente",
             "bullets": ["Construyó sistemas para un millón de usuarios",
                         "Redujo la latencia en un 40%"]},
        ]},
        {"name": "Habilidades", "items": ["Python", "Go", "Docker"]},
    ],
}

# A legitimate French CV — real résumé content, no letter idioms.
_FRENCH_CV = {
    "contact": {"name": "Jean Dupont", "email": "jean@x.com", "location": "Paris"},
    "sections": [
        {"name": "Résumé", "text": "Ingénieur logiciel avec six ans d'expérience."},
        {"name": "Expérience", "entries": [
            {"heading": "Développeur Senior", "subheading": "Acme", "dates": "2020-Présent",
             "bullets": ["A conçu des systèmes pour un million d'utilisateurs",
                         "A réduit la latence de 40 %"]},
        ]},
        {"name": "Compétences", "items": ["Python", "Go", "Docker"]},
    ],
}

# A cover letter shaped payload written in a language with no authored formula catalog
# but containing literal English letter idioms — must still be caught via the English
# fallback (_letter_formula_re falls back to "en" for any code not in the dict).
_UNAUTHORED_LANG_COVER_LETTER_WITH_ENGLISH_TELLS = {
    "contact": {"name": "Somchai", "email": "somchai@x.com"},
    "sections": [{"name": "", "items": [
        "I am writing to express my strong interest in the role.",
        "I would welcome discussing it further. Sincerely, Somchai",
    ]}],
}

# A cover letter shaped payload written in Thai, using Thai's own authored formula tuple
# (jsa/schema/cv.py's _LETTER_FORMULAS_BY_LANG["th"]) — all 20 catalog languages in
# jsa/i18n/languages.py now have their own tuple, so Thai is no longer a fallback case.
_THAI_COVER_LETTER_AS_CV = {
    "contact": {"name": "Somchai", "email": "somchai@x.com"},
    "sections": [{"name": "", "items": [
        "เขียนจดหมายฉบับนี้เพื่อแสดงความสนใจในตำแหน่งนี้",
        "หวังว่าจะได้รับการติดต่อกลับเร็วๆ นี้ ขอแสดงความนับถือ Somchai",
    ]}],
}

# A legitimate Thai CV — real résumé content, no letter idioms.
_THAI_CV = {
    "contact": {"name": "Somchai Jaidee", "email": "somchai@x.com", "location": "กรุงเทพฯ"},
    "sections": [
        {"name": "สรุป", "text": "วิศวกรซอฟต์แวร์ที่มีประสบการณ์หกปี"},
        {"name": "ประสบการณ์", "entries": [
            {"heading": "นักพัฒนาอาวุโส", "subheading": "Acme", "dates": "2020-ปัจจุบัน",
             "bullets": ["สร้างระบบสำหรับผู้ใช้หนึ่งล้านคน", "ลดเวลาแฝงลง 40%"]},
        ]},
        {"name": "ทักษะ", "items": ["Python", "Go", "Docker"]},
    ],
}


# --- tests -------------------------------------------------------------------------------


class TestEnglishDefault:
    def test_english_cover_letter_rejected_with_no_context(self):
        with pytest.raises(ValidationError, match="cover letter"):
            CVDocument.model_validate(_ENGLISH_COVER_LETTER_AS_CV)


class TestSpanishGuard:
    def test_spanish_cover_letter_rejected_with_spanish_context(self):
        with pytest.raises(ValidationError, match="cover letter"):
            CVDocument.model_validate(
                _SPANISH_COVER_LETTER_AS_CV, context={"language": "es"}
            )

    def test_spanish_cover_letter_not_caught_without_context(self):
        # Demonstrates the exact regression this fix closes: without a language context the
        # guard falls back to English-only formulas, so a Spanish letter-as-CV sails through.
        cv = CVDocument.model_validate(_SPANISH_COVER_LETTER_AS_CV)
        assert cv.contact.name == "Juana Perez"

    def test_legitimate_spanish_cv_passes(self):
        cv = CVDocument.model_validate(_SPANISH_CV, context={"language": "es"})
        assert cv.contact.name == "Juana Perez"


class TestFrenchGuard:
    def test_legitimate_french_cv_passes(self):
        cv = CVDocument.model_validate(_FRENCH_CV, context={"language": "fr"})
        assert cv.contact.name == "Jean Dupont"


class TestUnauthoredLanguageFallback:
    def test_unauthored_language_code_falls_back_to_english_pattern(self):
        # "xx" is not a real catalog code — exercises _letter_formula_re's fallback path
        # directly, independent of which real languages currently have an authored tuple.
        with pytest.raises(ValidationError, match="cover letter"):
            CVDocument.model_validate(
                _UNAUTHORED_LANG_COVER_LETTER_WITH_ENGLISH_TELLS,
                context={"language": "xx"},
            )


class TestThaiGuard:
    def test_thai_cover_letter_rejected_with_thai_context(self):
        with pytest.raises(ValidationError, match="cover letter"):
            CVDocument.model_validate(
                _THAI_COVER_LETTER_AS_CV, context={"language": "th"}
            )

    def test_legitimate_thai_cv_passes(self):
        cv = CVDocument.model_validate(_THAI_CV, context={"language": "th"})
        assert cv.contact.name == "Somchai Jaidee"
