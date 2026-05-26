"""CSV parsing and job-ID hashing for ingest."""

import csv
import hashlib
from pathlib import Path

_REQUIRED_HEADERS = {"company", "role", "link", "tier", "JD"}
_VALID_TIERS = {"A", "B", "C"}


def _job_id(company: str, role: str, link: str) -> str:
    raw = f"{company}|{role}|{link}".encode()
    return hashlib.sha1(raw).hexdigest()[:16]


def _jd_hash(jd: str) -> str:
    return hashlib.sha1(jd.encode()).hexdigest()[:16]


def load_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Parse a job CSV file and return (job_dicts, errors).

    Raises ValueError if required headers are missing or unexpected.
    Soft-skips rows with invalid tier or completely blank rows (appends to errors).

    Auto-detects the delimiter (comma, semicolon, tab, pipe) using csv.Sniffer
    so that semicolon-separated exports (common from European-locale spreadsheets)
    are accepted without any manual conversion.
    """
    with open(path, newline="", encoding="utf-8") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel  # fallback: standard comma-separated
        reader = csv.DictReader(fh, dialect=dialect)

        # Validate headers
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty or has no headers")
        actual_headers = set(reader.fieldnames)
        if actual_headers != _REQUIRED_HEADERS:
            missing = _REQUIRED_HEADERS - actual_headers
            extra = actual_headers - _REQUIRED_HEADERS
            parts = []
            if missing:
                parts.append(f"missing columns: {sorted(missing)}")
            if extra:
                parts.append(f"unexpected columns: {sorted(extra)}")
            raise ValueError(f"CSV header validation failed: {'; '.join(parts)}")

        jobs: list[dict] = []
        errors: list[str] = []

        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            company = row["company"].strip()
            role = row["role"].strip()
            link = row["link"].strip()
            tier = row["tier"].strip()
            jd = row["JD"].strip()

            # Skip completely blank rows
            if not company and not role and not link and not tier and not jd:
                continue

            # Validate tier
            if tier not in _VALID_TIERS:
                errors.append(
                    f"Row {row_num}: invalid tier {tier!r} (must be A, B, or C) — "
                    f"company={company!r}, role={role!r}; row skipped"
                )
                continue

            jobs.append(
                {
                    "id": _job_id(company, role, link),
                    "company": company,
                    "role": role,
                    "link": link,
                    "tier": tier,
                    "jd": jd,
                    "jd_hash": _jd_hash(jd),
                }
            )

    return jobs, errors
