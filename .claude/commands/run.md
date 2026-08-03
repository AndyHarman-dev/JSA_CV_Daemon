---
description: Run the JSA CLI against a jobs CSV using the claude-cli backend
argument-hint: [csv-path]
---

Run the JSA pipeline CLI with the Claude CLI backend.

CSV path resolution:
- If the user supplied an argument (`$ARGUMENTS`), use that as the CSV path.
- Otherwise, default to `~/test.csv`.

Run exactly this command, substituting the resolved CSV path:

```bash
jsa --csv <resolved-csv-path> --backends claude-cli
```
