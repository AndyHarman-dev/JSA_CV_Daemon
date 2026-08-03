---
description: Wipe local sqlite DB and output/ dir for a clean test run from scratch
---

Remove the JSA sqlite database and the generated `output/` directory so the app starts from a clean state for testing.

Run exactly these commands, then confirm both were removed (or report if they didn't exist):

```bash
rm -rf output
rm -f ~/.jsa/jsa.sqlite
```
