---
name: feedback-tdd
description: each PLAN.md phase uses TDD — failing tests first, then implement
metadata:
  type: feedback
---

Each implementation phase follows TDD where possible: write failing tests for the target behaviour first, then implement until they pass. For phases where the compile-time machinery must exist before anything can be instantiated (e.g. Phase 0 NTTP probe, Phase 1 primitives), write tests alongside the types rather than after, and favour small units that can be tested in isolation before wiring into the full pipeline.

**Why:** Project CLAUDE.md mandates this as the development approach.

**How to apply:** In phase execution (step 4c), always invoke Tester to set up / write tests before or alongside Coder's implementation work. Do not skip tests.
