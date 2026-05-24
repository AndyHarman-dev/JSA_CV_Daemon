# JSA — Job Search Assistant

A CLI-launched local web application that automates tailored job-application document generation.

## Requirements

- Python 3.11+
- Node.js 18+
- System libraries for WeasyPrint: `cairo`, `pango`, `gdk-pixbuf`
  - macOS: `brew install cairo pango gdk-pixbuf libffi`
  - Linux: `apt install libcairo2 libpango-1.0-0 libgdk-pixbuf2.0-0`

## Install

```bash
pip install -e .
```

## Usage

```bash
jsa --csv jobs.csv --cv resume.pdf --out ./output --backend claude-cli
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--csv` | required | CSV file with columns: company,role,link,tier,JD |
| `--cv` | required | CV file (.pdf or .docx) |
| `--out` | `output/` | Output directory for generated PDFs |
| `--backend` | `claude-cli` | AI backend: claude-cli \| gemini-cli \| anthropic |
| `--db` | `~/.jsa/jsa.sqlite` | SQLite database path |
| `--port` | `8765` | Port for the local web server |
| `--no-browser` | false | Do not open browser on start |

## CSV format

```csv
company,role,link,tier,JD
Acme Corp,Software Engineer,https://acme.com/jobs/1,A,"Full job description here..."
```

Tier values: `A` (high priority), `B` (medium), `C` (low).

## Prompt files

Edit `jsa/prompts/PROMPT_CDADJUST.md` and `jsa/prompts/CVL_PROMPT.md` to customise the AI prompts.
