"""jsa/i18n/languages.py — the single language catalog used by /api/config and validation."""

from __future__ import annotations

from jsa.i18n.languages import LANGUAGES, is_valid, language_name


class TestCatalog:
    def test_english_is_present_and_first(self):
        assert LANGUAGES[0] == ("en", "English", "English")

    def test_all_entries_are_code_english_native_triples(self):
        for entry in LANGUAGES:
            assert len(entry) == 3
            code, english, native = entry
            assert code and english and native
            assert code.islower()

    def test_no_duplicate_codes(self):
        codes = [code for code, _, _ in LANGUAGES]
        assert len(codes) == len(set(codes))


class TestValidation:
    def test_is_valid_known_code(self):
        assert is_valid("es") is True

    def test_is_valid_unknown_code(self):
        assert is_valid("xx") is False

    def test_language_name_known(self):
        assert language_name("ja") == "Japanese"

    def test_language_name_unknown_falls_back_to_code(self):
        assert language_name("xx") == "xx"
