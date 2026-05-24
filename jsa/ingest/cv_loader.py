"""PDF/DOCX to plain-text CV extraction."""

from pathlib import Path


def load_cv(path: Path) -> str:
    """Extract plain text from a CV file.

    Dispatches on file extension (case-insensitive):
      .pdf  — uses pypdf (PdfReader)
      .docx — uses python-docx (Document)

    Raises ValueError for unsupported extensions.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text)
        return "\n".join(pages_text)

    elif suffix == ".docx":
        from docx import Document

        doc = Document(path)
        return "\n".join(paragraph.text for paragraph in doc.paragraphs)

    else:
        raise ValueError(f"Unsupported CV format: {path.suffix}")
