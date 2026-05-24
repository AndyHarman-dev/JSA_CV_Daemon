---
name: feedback-advisor-usage
description: call advisor before architectural decisions and when uncertain on design
metadata:
  type: feedback
---

Call the `advisor` tool before proceeding whenever uncertain about a design or implementation decision. For any discussion involving architecture at a significant scale — message structure, field registry design, parsing pipeline, extensibility — calling `advisor` is preferred to get deeper insight before committing to an approach.

**Why:** Project CLAUDE.md explicitly mandates this; the advisor model has provided high-quality guidance throughout the project.

**How to apply:** Before invoking Architect on any non-trivial decision, call advisor first. Also call advisor when an agent returns unexpected results more than once.
