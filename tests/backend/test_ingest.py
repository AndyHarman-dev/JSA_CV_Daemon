"""Tests for jsa.ingest.csv_loader, jsa.ingest.cv_loader, and jsa.prompts.loader."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jsa.ingest.csv_loader import load_csv
from jsa.ingest.cv_loader import load_cv
from jsa.prompts import loader as prompt_loader
from jsa.prompts.loader import read_prompt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha1_16(raw: bytes) -> str:
    return hashlib.sha1(raw).hexdigest()[:16]


def _expected_job_id(company: str, role: str, link: str) -> str:
    return _sha1_16(f"{company}|{role}|{link}".encode())


def _expected_jd_hash(jd: str) -> str:
    return _sha1_16(jd.encode())


def _write_csv(path: Path, lines: list[str]) -> Path:
    """Write lines to a file and return the path."""
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# csv_loader tests
# ---------------------------------------------------------------------------

class TestCsvLoaderHappyPath:
    def test_valid_csv_returns_correct_fields(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,We need an engineer.",
                "Beta,Designer,https://beta.com/2,B,Looking for a designer.",
                "Gamma,PM,https://gamma.com/3,C,Seeking a product manager.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert errors == []
        assert len(jobs) == 3

        expected_fields = {"id", "company", "role", "link", "tier", "jd", "jd_hash"}
        for job in jobs:
            assert set(job.keys()) == expected_fields

    def test_valid_csv_all_three_tiers_returned(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,JD text A.",
                "Beta,Designer,https://beta.com/2,B,JD text B.",
                "Gamma,PM,https://gamma.com/3,C,JD text C.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        tiers = {j["tier"] for j in jobs}
        assert tiers == {"A", "B", "C"}
        assert errors == []

    def test_valid_csv_fields_stored_correctly(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme Inc,Software Engineer,https://acme.com/jobs/42,A,Great job description.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 1
        job = jobs[0]
        assert job["company"] == "Acme Inc"
        assert job["role"] == "Software Engineer"
        assert job["link"] == "https://acme.com/jobs/42"
        assert job["tier"] == "A"
        assert job["jd"] == "Great job description."
        assert errors == []


class TestCsvLoaderJobIdStability:
    def test_job_id_is_deterministic(self, tmp_path):
        """Same company/role/link always produces the same id."""
        row = "Acme,Engineer,https://acme.com/1,A,Some JD."
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            ["company,role,link,tier,JD", row],
        )
        jobs1, _ = load_csv(csv_file)
        jobs2, _ = load_csv(csv_file)

        assert jobs1[0]["id"] == jobs2[0]["id"]

    def test_job_id_computed_correctly(self, tmp_path):
        """Job ID = sha1(company|role|link)[:16]."""
        company, role, link = "Acme", "Engineer", "https://acme.com/1"
        expected_id = _expected_job_id(company, role, link)

        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                f"{company},{role},{link},A,A JD.",
            ],
        )
        jobs, _ = load_csv(csv_file)

        assert jobs[0]["id"] == expected_id

    def test_different_company_produces_different_id(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,JD.",
                "Beta,Engineer,https://acme.com/1,A,JD.",
            ],
        )
        jobs, _ = load_csv(csv_file)

        assert jobs[0]["id"] != jobs[1]["id"]


class TestCsvLoaderJdHash:
    def test_jd_hash_computed_correctly(self, tmp_path):
        """jd_hash = sha1(jd)[:16]."""
        jd = "We are looking for a backend engineer with Python experience."
        expected_hash = _expected_jd_hash(jd)

        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                f"Acme,Engineer,https://acme.com/1,A,{jd}",
            ],
        )
        jobs, _ = load_csv(csv_file)

        assert jobs[0]["jd_hash"] == expected_hash

    def test_jd_hash_is_deterministic(self, tmp_path):
        jd = "Consistent JD text for hashing."
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            ["company,role,link,tier,JD", f"Acme,Engineer,https://acme.com/1,A,{jd}"],
        )
        jobs1, _ = load_csv(csv_file)
        jobs2, _ = load_csv(csv_file)

        assert jobs1[0]["jd_hash"] == jobs2[0]["jd_hash"]

    def test_different_jd_produces_different_hash(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,First JD text.",
                "Beta,Engineer,https://beta.com/1,A,Second JD text.",
            ],
        )
        jobs, _ = load_csv(csv_file)

        assert jobs[0]["jd_hash"] != jobs[1]["jd_hash"]


class TestCsvLoaderHeaderValidation:
    def test_missing_required_header_raises_value_error(self, tmp_path):
        # Missing 'JD' column
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier",
                "Acme,Engineer,https://acme.com/1,A",
            ],
        )
        with pytest.raises(ValueError, match="missing columns"):
            load_csv(csv_file)

    def test_missing_company_header_raises_value_error(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "role,link,tier,JD",
                "Engineer,https://acme.com/1,A,Some JD.",
            ],
        )
        with pytest.raises(ValueError):
            load_csv(csv_file)

    def test_extra_header_raises_value_error(self, tmp_path):
        # Has all required + an extra 'notes' column
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD,notes",
                "Acme,Engineer,https://acme.com/1,A,Some JD.,extra",
            ],
        )
        with pytest.raises(ValueError, match="unexpected columns"):
            load_csv(csv_file)

    def test_renamed_header_jd_lowercase_raises_value_error(self, tmp_path):
        """Header 'jd' (lowercase) is not the same as the required 'JD'."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,jd",
                "Acme,Engineer,https://acme.com/1,A,Some JD.",
            ],
        )
        with pytest.raises(ValueError):
            load_csv(csv_file)

    def test_completely_wrong_headers_raises_value_error(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "col1,col2,col3",
                "val1,val2,val3",
            ],
        )
        with pytest.raises(ValueError):
            load_csv(csv_file)


class TestCsvLoaderSoftSkipTier:
    def test_invalid_tier_d_adds_error_skips_row(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,D,Some JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert len(errors) == 1
        assert "D" in errors[0]

    def test_invalid_tier_z_adds_error_skips_row(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,Z,Some JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert len(errors) == 1

    def test_invalid_tier_numeric_adds_error_skips_row(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,1,Some JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert len(errors) == 1

    def test_error_message_includes_row_context(self, tmp_path):
        """Error message should identify the company/role for the bad row."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "BadCo,SomeRole,https://bad.co/1,X,JD text.",
            ],
        )
        _, errors = load_csv(csv_file)

        assert len(errors) == 1
        # Error should mention the invalid tier value
        assert "X" in errors[0]

    def test_invalid_tier_lowercase_a_skipped(self, tmp_path):
        """Lowercase 'a' is not a valid tier — only 'A', 'B', 'C' are."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,a,Some JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert len(errors) == 1


class TestCsvLoaderBlankRowSkip:
    def test_completely_blank_row_silently_skipped(self, tmp_path):
        """A row with all empty cells is not in jobs or errors."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                ",,,,",
                "Acme,Engineer,https://acme.com/1,A,Some JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 1
        assert errors == []
        assert jobs[0]["company"] == "Acme"

    def test_whitespace_only_row_silently_skipped(self, tmp_path):
        """A row with only whitespace in all cells is silently skipped."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "   ,  ,  ,  ,  ",
                "Acme,Engineer,https://acme.com/1,A,JD text.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 1
        assert errors == []

    def test_multiple_blank_rows_all_skipped(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                ",,,,",
                ",,,,",
                "Acme,Engineer,https://acme.com/1,A,Some JD.",
                ",,,,",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 1
        assert errors == []


class TestCsvLoaderMixed:
    def test_mixed_valid_invalid_tier_blank(self, tmp_path):
        """CSV with good rows, one bad tier, and one blank row."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,Good JD.",           # valid
                ",,,,",                                                    # blank — skip silently
                "Beta,Designer,https://beta.com/2,D,Another JD.",         # invalid tier → error
                "Gamma,PM,https://gamma.com/3,B,PM JD.",                  # valid
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 2
        assert len(errors) == 1
        companies = {j["company"] for j in jobs}
        assert companies == {"Acme", "Gamma"}

    def test_all_invalid_tiers_returns_empty_jobs_with_errors(self, tmp_path):
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,X,JD.",
                "Beta,Designer,https://beta.com/2,Y,JD.",
            ],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert len(errors) == 2

    def test_empty_csv_body_returns_empty_results(self, tmp_path):
        """CSV with only a header row returns empty lists."""
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            ["company,role,link,tier,JD"],
        )
        jobs, errors = load_csv(csv_file)

        assert jobs == []
        assert errors == []


class TestCsvLoaderOversizedField:
    """U3 — CSV oversized-field resilience.

    Python's csv module defaults field_size_limit() to 128 KiB per field, and
    that limit was never raised anywhere in this repo, so a job description
    (JD) >=128 KB used to raise `_csv.Error: field larger than field limit`
    from *within* DictReader iteration and abort ingest of the whole file —
    even rows that were perfectly fine. These tests cover the fix.
    """

    def test_oversized_jd_no_longer_raises_and_is_ingested(self, tmp_path):
        """A JD well past the old 128 KiB default limit must parse normally now."""
        big_jd = "A" * (200 * 1024)  # 200 KB, no commas/quotes/newlines
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,Normal JD.",
                f"BigCo,Analyst,https://bigco.com/2,B,{big_jd}",
            ],
        )

        jobs, errors = load_csv(csv_file)  # must not raise csv.Error

        assert errors == []
        assert len(jobs) == 2
        big_job = next(j for j in jobs if j["company"] == "BigCo")
        assert len(big_job["jd"]) == len(big_jd)

    def test_oversized_jd_and_malformed_row_alongside_valid_rows(self, tmp_path):
        """Oversized JD ingests fine; a genuinely malformed row still soft-skips;
        valid rows are unaffected either way."""
        big_jd = "B" * (200 * 1024)
        csv_file = _write_csv(
            tmp_path / "jobs.csv",
            [
                "company,role,link,tier,JD",
                "Acme,Engineer,https://acme.com/1,A,Good JD.",          # valid
                f"BigCo,Analyst,https://bigco.com/2,B,{big_jd}",         # oversized JD, now valid
                "Beta,Designer,https://beta.com/3,Q,Bad tier JD.",       # malformed → soft-skip
                "Gamma,PM,https://gamma.com/4,C,Another good JD.",       # valid
            ],
        )

        jobs, errors = load_csv(csv_file)

        assert len(jobs) == 3
        companies = {j["company"] for j in jobs}
        assert companies == {"Acme", "BigCo", "Gamma"}
        assert len(errors) == 1
        assert "Q" in errors[0]

    def test_field_exceeding_configured_cap_is_soft_skipped_not_fatal(self, tmp_path):
        """Defense-in-depth: even if a field exceeds whatever cap is configured
        (simulated here via a lowered limit), the manual next()-in-a-loop
        iteration must soft-skip the offending row into `errors` rather than
        letting the csv.Error propagate and abort the whole load — subsequent
        valid rows must still be returned."""
        import csv as csv_module

        original_limit = csv_module.field_size_limit()
        csv_module.field_size_limit(1024)  # artificially tiny, to force a real csv.Error
        try:
            too_big = "C" * 2048  # exceeds the 1024 cap set above
            csv_file = _write_csv(
                tmp_path / "jobs.csv",
                [
                    "company,role,link,tier,JD",
                    "Acme,Engineer,https://acme.com/1,A,Good JD.",
                    f"BigCo,Analyst,https://bigco.com/2,B,{too_big}",   # blows the 1024 cap
                    "Gamma,PM,https://gamma.com/3,C,After the bad row.",  # proves recovery
                ],
            )

            jobs, errors = load_csv(csv_file)  # must not raise

            assert len(errors) >= 1
            # Both the row before AND the row after the offending one must come
            # through — proving the manual next()-in-a-loop iteration recovers
            # and keeps reading the rest of the file, not just that it doesn't
            # crash before reaching the bad row.
            assert {j["company"] for j in jobs} == {"Acme", "Gamma"}
        finally:
            csv_module.field_size_limit(original_limit)


# ---------------------------------------------------------------------------
# cv_loader tests
# ---------------------------------------------------------------------------

class TestCvLoaderPdf:
    def test_pdf_extension_calls_pypdf_and_returns_text(self, tmp_path):
        pdf_file = tmp_path / "resume.pdf"
        pdf_file.write_bytes(b"dummy PDF bytes")

        mock_page1 = MagicMock()
        mock_page1.extract_text.return_value = "Page one content"
        mock_page2 = MagicMock()
        mock_page2.extract_text.return_value = "Page two content"

        mock_reader_instance = MagicMock()
        mock_reader_instance.pages = [mock_page1, mock_page2]

        with patch("pypdf.PdfReader", return_value=mock_reader_instance) as mock_reader_cls:
            result = load_cv(pdf_file)

        mock_reader_cls.assert_called_once_with(pdf_file)
        assert result == "Page one content\nPage two content"

    def test_pdf_extension_uppercase_dispatches_correctly(self, tmp_path):
        """`.PDF` (uppercase) should use the pypdf path."""
        pdf_file = tmp_path / "resume.PDF"
        pdf_file.write_bytes(b"dummy PDF bytes")

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Uppercase PDF content"
        mock_reader_instance = MagicMock()
        mock_reader_instance.pages = [mock_page]

        with patch("pypdf.PdfReader", return_value=mock_reader_instance):
            result = load_cv(pdf_file)

        assert result == "Uppercase PDF content"

    def test_pdf_pages_with_none_text_filtered_out(self, tmp_path):
        """Pages where extract_text() returns None/empty are excluded from output."""
        pdf_file = tmp_path / "resume.pdf"
        pdf_file.write_bytes(b"dummy bytes")

        mock_page_with_text = MagicMock()
        mock_page_with_text.extract_text.return_value = "Real content"
        mock_page_empty = MagicMock()
        mock_page_empty.extract_text.return_value = None
        mock_page_blank = MagicMock()
        mock_page_blank.extract_text.return_value = ""

        mock_reader_instance = MagicMock()
        mock_reader_instance.pages = [mock_page_with_text, mock_page_empty, mock_page_blank]

        with patch("pypdf.PdfReader", return_value=mock_reader_instance):
            result = load_cv(pdf_file)

        assert result == "Real content"

    def test_pdf_single_page_returns_plain_string(self, tmp_path):
        pdf_file = tmp_path / "resume.pdf"
        pdf_file.write_bytes(b"dummy bytes")

        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Single page text"
        mock_reader_instance = MagicMock()
        mock_reader_instance.pages = [mock_page]

        with patch("pypdf.PdfReader", return_value=mock_reader_instance):
            result = load_cv(pdf_file)

        assert result == "Single page text"


class TestCvLoaderDocx:
    def test_docx_extension_calls_python_docx_and_returns_text(self, tmp_path):
        docx_file = tmp_path / "resume.docx"
        docx_file.write_bytes(b"dummy DOCX bytes")

        mock_para1 = MagicMock()
        mock_para1.text = "First paragraph"
        mock_para2 = MagicMock()
        mock_para2.text = "Second paragraph"

        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [mock_para1, mock_para2]

        with patch("docx.Document", return_value=mock_doc_instance) as mock_doc_cls:
            result = load_cv(docx_file)

        mock_doc_cls.assert_called_once_with(docx_file)
        assert result == "First paragraph\nSecond paragraph"

    def test_docx_extension_uppercase_dispatches_correctly(self, tmp_path):
        """.DOCX (uppercase) should use the python-docx path."""
        docx_file = tmp_path / "resume.DOCX"
        docx_file.write_bytes(b"dummy DOCX bytes")

        mock_para = MagicMock()
        mock_para.text = "Uppercase DOCX content"
        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [mock_para]

        with patch("docx.Document", return_value=mock_doc_instance):
            result = load_cv(docx_file)

        assert result == "Uppercase DOCX content"

    def test_docx_empty_paragraphs_returns_joined_empty_strings(self, tmp_path):
        docx_file = tmp_path / "resume.docx"
        docx_file.write_bytes(b"dummy bytes")

        mock_para1 = MagicMock()
        mock_para1.text = "Content"
        mock_para2 = MagicMock()
        mock_para2.text = ""  # empty paragraph (e.g. blank line in docx)

        mock_doc_instance = MagicMock()
        mock_doc_instance.paragraphs = [mock_para1, mock_para2]

        with patch("docx.Document", return_value=mock_doc_instance):
            result = load_cv(docx_file)

        # The implementation joins all paragraphs including empty ones
        assert result == "Content\n"


class TestCvLoaderUnsupportedExtension:
    def test_txt_extension_raises_value_error(self, tmp_path):
        txt_file = tmp_path / "resume.txt"
        txt_file.write_text("plain text resume", encoding="utf-8")

        with pytest.raises(ValueError, match="Unsupported"):
            load_cv(txt_file)

    def test_rtf_extension_raises_value_error(self, tmp_path):
        rtf_file = tmp_path / "resume.rtf"
        rtf_file.write_bytes(b"rtf content")

        with pytest.raises(ValueError):
            load_cv(rtf_file)

    def test_no_extension_raises_value_error(self, tmp_path):
        no_ext_file = tmp_path / "resume"
        no_ext_file.write_bytes(b"some bytes")

        with pytest.raises(ValueError):
            load_cv(no_ext_file)

    def test_error_message_includes_extension(self, tmp_path):
        bad_file = tmp_path / "resume.odt"
        bad_file.write_bytes(b"odt bytes")

        with pytest.raises(ValueError, match=r"\.odt"):
            load_cv(bad_file)


# ---------------------------------------------------------------------------
# prompts/loader tests
# ---------------------------------------------------------------------------

class TestReadPromptHappyPath:
    def test_cv_adjust_returns_non_empty_string(self):
        result = read_prompt("cv_adjust")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cover_letter_returns_non_empty_string(self):
        result = read_prompt("cover_letter")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cv_adjust_contains_sentinel_instructions(self):
        """The stub prompt must include sentinel grammar instructions."""
        result = read_prompt("cv_adjust")
        assert "<<<NEED_INPUT>>>" in result or "NEED_INPUT" in result

    def test_cover_letter_contains_sentinel_instructions(self):
        result = read_prompt("cover_letter")
        assert "<<<FINAL>>>" in result or "FINAL" in result

    def test_cv_adjust_stub_marker_present(self):
        """The cv_adjust stub file contains the word STUB or similar indicator."""
        result = read_prompt("cv_adjust")
        # Stub files contain "STUB" per the prompt stubs
        assert "STUB" in result

    def test_cover_letter_stub_marker_present(self):
        result = read_prompt("cover_letter")
        assert "STUB" in result


class TestReadPromptInvalidName:
    def test_invalid_name_raises_key_error(self):
        """read_prompt raises KeyError for any name not in the registry."""
        with pytest.raises(KeyError):
            read_prompt("bad_name")  # type: ignore[arg-type]

    def test_empty_string_raises_key_error(self):
        with pytest.raises(KeyError):
            read_prompt("")  # type: ignore[arg-type]

    def test_cv_adjust_misspelled_raises_key_error(self):
        with pytest.raises(KeyError):
            read_prompt("cv-adjust")  # type: ignore[arg-type]

    def test_cover_letter_misspelled_raises_key_error(self):
        with pytest.raises(KeyError):
            read_prompt("coverletter")  # type: ignore[arg-type]


class TestReadPromptNoCaching:
    def test_two_calls_return_identical_content(self):
        """Without caching, two calls to the same prompt return the same string."""
        result1 = read_prompt("cv_adjust")
        result2 = read_prompt("cv_adjust")
        assert result1 == result2

    def test_reads_fresh_from_disk(self, tmp_path, monkeypatch):
        """When _PROMPTS_DIR is patched to a temp dir, file edits are visible immediately."""
        # Set up a fake prompts directory with a stub file
        fake_prompt = tmp_path / "PROMPT_CDADJUST.md"
        fake_prompt.write_text("Original content", encoding="utf-8")

        monkeypatch.setattr(prompt_loader, "_PROMPTS_DIR", tmp_path)

        first = read_prompt("cv_adjust")
        assert first == "Original content"

        # Now edit the file — a caching impl would still return "Original content"
        fake_prompt.write_text("Updated content", encoding="utf-8")

        second = read_prompt("cv_adjust")
        assert second == "Updated content"
        assert first != second
