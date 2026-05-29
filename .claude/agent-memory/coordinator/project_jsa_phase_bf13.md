---
name: project-jsa-phase-bf13
description: BF-13 PDF/browser rendering sync — html:True in markdown-it; h1 centering in browser; BF-11 test updates
metadata:
  type: project
---

## BF-13: PDF/browser rendering synchronization

**Why:** Exported PDF styling didn't match browser preview. Two root causes:

1. Model used HTML tags (`<div align="center">`, `<strong>`) for styling. Browser's `marked` renders HTML → styling visible. BF-11 tried to fix escaped HTML by *stripping* tags — but that destroyed both the HTML tags AND the content styling, losing centering and bold in the PDF.

2. Even with pure `# Name` markdown (which the prompt requires), browser h1 had no `text-center` Tailwind class, but `styles.css` already had `h1 { text-align: center; }`. Mismatched in opposite direction.

**Fix applied:**
- `weasy.py`: `html: False` → `html: True` in MarkdownIt; removed `re.sub` strip entirely
- `styles.css`: Added `[align="center"] { text-align: center; }` and `center { text-align: center; display: block; }`
- `MarkdownPreview.tsx`: Added `[&_h1]:text-center` to h1 class string
- `test_bf11_html_strip.py`: Renamed class to `TestHtmlPassThroughRendering`; flipped assertions from "stripped" to "passed through"; updated all stale docstrings referencing "regex" and "strip"

**Why BF-11 was the wrong fix:** BF-11 fixed "HTML escaped as visible `&lt;div&gt;`" by stripping. The real fix was `html: True` which passes HTML through rather than escaping it. No pre-render strip needed.

**How to apply:** If a future bug involves HTML rendering in PDF, check `weasy.py` first — it now uses `html: True` which means model HTML passes through to WeasyPrint. Prompt still says "no HTML tags" but renderer is tolerant as defense-in-depth.

**Test pattern:** BF-11 tests are now in `test_bf11_html_strip.py` but renamed to `TestHtmlPassThroughRendering`. Key invariants: `<div` in html_string (passes through), `&lt;div` NOT in html_string (not escaped).
