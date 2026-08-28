"""Tests for the deterministic schema -> canonical Markdown serializer + tolerant schema.

The schema is intentionally tolerant (see jsa/schema/cv.py): the model emits a CV section
as "a named block with content" using whatever key it likes. These tests pin both the
canonical render and the normalization that absorbs real-world shape variance — the exact
gap that broke a live run (sections with no ``type``, ``title`` vs ``name``, ``content``/
``items``/``subsections`` content keys).
"""

from __future__ import annotations

from jsa.render.serialize import cover_letter_to_markdown, cv_to_markdown
from jsa.schema import CoverLetter, CVDocument, cv_has_summary

_CV = {
    "contact": {
        "name": "Jane Doe",
        "email": "jane@x.com",
        "phone": "+1-555-867-5309",
        "location": "NYC",
        "links": ["linkedin.com/in/jane"],
    },
    "sections": [
        {"name": "Summary", "text": "Backend engineer with six years of experience."},
        {"name": "Experience", "entries": [
            {"role": "Senior Engineer", "company": "Acme", "dates": "2020–Present",
             "location": "Remote", "bullets": ["Built X serving 1M users", "Cut latency 40%"]},
            {"role": "Engineer", "company": "Beta", "dates": "2018–2020", "bullets": ["Did Z"]},
        ]},
        {"name": "Skills", "items": ["Python", "Go", "Docker", "K8s"]},
        {"name": "Education", "entries": [
            {"degree": "B.S. Computer Science", "institution": "MIT", "dates": "2014–2018"},
        ]},
        {"name": "Projects", "entries": [
            {"heading": "Foo", "dates": "2021", "text": "A tool.", "bullets": ["bar", "baz"]},
        ]},
    ],
}

_CL = {
    "salutation": "Dear Hiring Team,",
    "paragraphs": [
        "I'm excited to apply because your mission to democratize X resonates with me.",
        "Over six years I scaled backend systems serving millions, matching this role.",
    ],
    "signoff": "Sincerely,\nJane Doe",
}


class TestCvSerializer:
    def test_deterministic(self):
        cv = CVDocument.model_validate(_CV)
        assert cv_to_markdown(cv) == cv_to_markdown(cv)
        # Re-validating an identical dict yields identical Markdown.
        assert cv_to_markdown(CVDocument.model_validate(_CV)) == cv_to_markdown(cv)

    def test_header_and_contact(self):
        md = cv_to_markdown(CVDocument.model_validate(_CV))
        lines = md.splitlines()
        assert lines[0] == "# Jane Doe"
        # contact line directly under the name, pipe-joined, no blank between; links are
        # rendered as clickable Markdown links with an https:// href.
        assert lines[1] == (
            "jane@x.com | +1-555-867-5309 | "
            "[linkedin.com/in/jane](https://linkedin.com/in/jane) | NYC"
        )

    def test_section_rules_and_headings(self):
        md = cv_to_markdown(CVDocument.model_validate(_CV))
        # Every section heading is preceded by a `---` rule.
        for name in ("## Summary", "## Experience", "## Skills", "## Education", "## Projects"):
            assert f"---\n{name}" in md
        # bullets render as Markdown list items
        assert "- Built X serving 1M users" in md
        # flat skill items render as a comma-joined keyword line
        assert "Python, Go, Docker, K8s" in md
        # entry header combines heading and subheading (role — company, degree — institution)
        assert "**Senior Engineer** — Acme" in md
        assert "**B.S. Computer Science** — MIT" in md

    def test_no_raw_html(self):
        md = cv_to_markdown(CVDocument.model_validate(_CV))
        assert "<div" not in md and "<span" not in md and "<p>" not in md


class TestSchemaTolerance:
    """Normalization absorbs the shape variance that broke the live run."""

    def test_missing_type_discriminator(self):
        # No `type` on any section (the universal live failure) → still valid.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Skills", "items": ["C++", "Unreal"]}],
        })
        assert "C++, Unreal" in cv_to_markdown(cv)

    def test_title_alias_and_content_key(self):
        # `title` instead of `name`; `content` string instead of `text`.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "phone": "+1-555"},
            "sections": [{"title": "Summary", "content": "Seasoned engineer."}],
        })
        md = cv_to_markdown(cv)
        assert "## Summary" in md and "Seasoned engineer." in md

    def test_entries_under_arbitrary_key_and_role_company(self):
        # Experience entries arrive under `subsections`; entry uses role/company aliases.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Experience", "subsections": [
                {"role": "Dev", "company": "Rive", "bullets": ["systems thinking"]},
            ]}],
        })
        md = cv_to_markdown(cv)
        assert "**Dev** — Rive" in md and "- systems thinking" in md

    def test_content_under_novel_keys_not_dropped(self):
        # The denylist normalizer absorbs content under keys we never anticipated, so a
        # model that invents `positions`/`accomplishments`/`noteworthy` does not silently
        # render a thinned CV (the invisible failure mode of extra="ignore").
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [
                {"name": "Experience", "positions": [
                    {"role": "Dev", "company": "Rive",
                     "accomplishments": ["Shipped SDK v2", "Cut build time 30%"]}]},
                {"name": "Achievements", "noteworthy": ["Patent on X", "Speaker at GDC"]},
            ],
        })
        md = cv_to_markdown(cv)
        assert "Shipped SDK v2" in md and "Cut build time 30%" in md
        assert "Patent on X" in md and "Speaker at GDC" in md

    def test_unknown_keys_ignored_not_fatal(self):
        # A leaked change-log style key is dropped, not rejected.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com", "change_log": "edited summary"},
            "sections": [{"name": "Summary", "text": "Engineer.", "metadata": {"x": 1}}],
        })
        md = cv_to_markdown(cv)
        assert "change_log" not in md and "metadata" not in md

    def test_type_discriminator_used_as_name_fallback(self):
        # A model that emits {"type": "summary", ...} instead of a name/title key must not
        # lose the section's heading (see jsa/schema/cv.py `_NAME_FALLBACK_KEYS`).
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"type": "summary", "text": "Seasoned engineer."}],
        })
        assert cv.sections[0].name == "Summary"
        assert cv_has_summary(cv) is True
        md = cv_to_markdown(cv)
        assert "## Summary" in md and "Seasoned engineer." in md

    def test_explicit_name_wins_over_type_fallback(self):
        # An explicit name/title always takes precedence over the type/kind fallback.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Profile", "type": "summary", "text": "Engineer."}],
        })
        assert cv.sections[0].name == "Profile"
        assert "## Profile" in cv_to_markdown(cv)


class TestLayoutPolicies:
    """Program-owned layout decisions that don't depend on what the model emits."""

    def test_entry_url_key_becomes_clickable_link(self):
        # The exact live bug: a project entry with a `url` key (no text/bullets) was
        # silently dropped. It must now survive and render as a clickable link.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Projects", "entries": [
                {"name": "DI Container", "url": "github.com/me/DI-Container"},
                {"heading": "ASP.NET", "bullets": ["ASP.NET", "EF Core"]},
            ]}],
        })
        md = cv_to_markdown(cv)
        assert "[github.com/me/DI-Container](https://github.com/me/DI-Container)" in md
        assert "**DI Container**" in md

    def test_tld_like_skill_names_not_misclassified_as_links(self):
        # Regression: "ASP.NET" matched the URL regex (word + .net TLD) and was
        # reclassified from bullets to links on backend validation. Real URLs have
        # a path (slash + content); bare domain-like skill names must stay in bullets.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Skills", "entries": [
                {"heading": "Frameworks", "bullets": ["ASP.NET", "Django.DEV", "EF Core"]},
            ]}],
        })
        entry = cv.sections[0].entries[0]
        assert entry.bullets == ["ASP.NET", "Django.DEV", "EF Core"]
        assert entry.links == []

    def test_bare_domain_with_no_path_not_a_link(self):
        # A string that looks like a domain but has no path/slash is ambiguous — keep
        # it as a bullet, not a link. (Only protocol://, www., or domain/path qualify.)
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Skills", "entries": [
                {"heading": "Tools", "bullets": ["github.com", "linkedin.com"]},
            ]}],
        })
        entry = cv.sections[0].entries[0]
        assert entry.bullets == ["github.com", "linkedin.com"]
        assert entry.links == []

    def test_real_urls_with_path_still_classified_as_links(self):
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com", "links": ["github.com/janedoe"]},
            "sections": [{"name": "Projects", "entries": [
                {"heading": "Foo", "links": ["github.com/me/foo", "linkedin.com/in/jane"]},
            ]}],
        })
        entry = cv.sections[0].entries[0]
        assert entry.links == ["github.com/me/foo", "linkedin.com/in/jane"]

    def test_skills_rendered_compactly(self):
        # Skills emitted as entries (category heading + comma-list) collapse to one line
        # per group, not two — `**Group:** a, b, c`.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Skills", "entries": [
                {"heading": "Languages", "text": "C++, Python, Go"},
                {"heading": "Tools", "bullets": ["Docker", "K8s"]},
            ]}],
        })
        md = cv_to_markdown(cv)
        assert "**Languages:** C++, Python, Go" in md
        assert "**Tools:** Docker, K8s" in md

    def test_section_order_preserved_summary_last(self):
        # Section order is the user's call — the serializer never reorders, even Summary.
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [
                {"name": "Skills", "items": ["C++"]},
                {"name": "Profile", "text": "Seasoned engineer."},
            ],
        })
        md = cv_to_markdown(cv)
        assert md.index("## Skills") < md.index("## Profile")

    def test_section_order_preserved_summary_in_middle(self):
        # Regression: a curated structure that places Summary mid-list must render that way
        # (this is the exact shape the LLM emitted for a real job before the fix).
        cv = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [
                {"name": "Skills", "items": ["C++"]},
                {"name": "Experience", "text": "Did stuff."},
                {"name": "Summary", "text": "Seasoned engineer."},
                {"name": "Projects", "text": "Built things."},
            ],
        })
        md = cv_to_markdown(cv)
        assert (
            md.index("## Skills")
            < md.index("## Experience")
            < md.index("## Summary")
            < md.index("## Projects")
        )


class TestContentKindGuards:
    """Hard letter guard (schema) vs. soft summary predicate (used by the self-heal nudge)."""

    def test_cover_letter_prose_rejected(self):
        # ≥2 letter formulas in the CV text → the document is rejected as not-a-CV.
        import pytest
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="cover letter"):
            CVDocument.model_validate({
                "contact": {"name": "Jane", "email": "j@x.com"},
                "sections": [{"name": "", "items": [
                    "I am writing to express my strong interest in the role.",
                    "I would welcome discussing it. Sincerely, Jane",
                ]}],
            })

    def test_single_letter_phrase_below_threshold_accepted(self):
        # One letter-ish phrase is tolerated (< 2-match threshold): no false reject.
        cv = CVDocument.model_validate({
            "contact": {"name": "Jane", "email": "j@x.com"},
            "sections": [{"name": "Summary",
                          "text": "Engineer; I look forward to hearing about hard problems."}],
        })
        assert cv.contact.name == "Jane"

    def test_cv_has_summary_predicate(self):
        with_summary = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Professional Profile", "text": "Seasoned engineer."}],
        })
        without_summary = CVDocument.model_validate({
            "contact": {"name": "A", "email": "a@x.com"},
            "sections": [{"name": "Skills", "items": ["C++", "Python"]}],
        })
        assert cv_has_summary(with_summary) is True
        assert cv_has_summary(without_summary) is False


class TestCoverLetterSerializer:
    def test_deterministic(self):
        cl = CoverLetter.model_validate(_CL)
        assert cover_letter_to_markdown(cl) == cover_letter_to_markdown(cl)

    def test_structure(self):
        md = cover_letter_to_markdown(CoverLetter.model_validate(_CL))
        blocks = md.strip().split("\n\n")
        assert blocks[0] == "Dear Hiring Team,"
        assert blocks[-1] == "Sincerely,\nJane Doe"
        # body paragraphs preserved verbatim, in order
        assert _CL["paragraphs"][0] in md
        assert _CL["paragraphs"][1] in md

    def test_paragraphs_from_string_blob(self):
        # `body` as a single newline-separated string → split into paragraphs.
        cl = CoverLetter.model_validate({
            "greeting": "Dear Team,",
            "body": "First paragraph that is reasonably long and meaningful, easily clearing "
                    "the minimum length floor on its own merit.\n\n"
                    "Second paragraph that also carries real substance and a good deal of length.",
            "closing": "Best,\nJane",
        })
        md = cover_letter_to_markdown(cl)
        assert "First paragraph" in md and "Second paragraph" in md
        assert md.strip().startswith("Dear Team,")
