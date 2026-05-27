# Memory Index

- [Advisor usage guidance](feedback_advisor_usage.md) — call advisor before architectural decisions and when uncertain on design; project CLAUDE.md mandates this
- [TDD methodology](feedback_tdd.md) — each phase uses TDD: failing tests first (or alongside) then implement; doctests in headers where possible
- [JSA project context](project_jsa_context.md) — stack, key design decisions, and non-obvious conventions for the JSA job application assistant project
- [Phase 2–3 gotchas](project_jsa_phase2_3.md) — transition() requires new_stage for running/awaiting_input; checkpoint calls transition first; csv_loader blank-row behavior
- [Phase 4 gotchas](project_jsa_phase4.md) — SessionHandle is @dataclass (not Protocol); backend_for() needs no-arg constructors or lambda factories
- [Phase 5 gotchas](project_jsa_phase5.md) — asyncio.TimeoutError is OSError subclass in Python 3.11+; Gemini CLI no native resume; UUID extraction heuristic in _pty_common
- [Phase 6 gotchas](project_jsa_phase6.md) — cover_letter→review must be one checkpoint; orchestrator task GC; fresh/resume discriminated by Message row existence
- [Phase 8 pre-flight](project_jsa_phase8_prep.md) — settings/cli.py duplication to fix; startup sequence order; static bundle path; CORS policy; Phase 8 is the full-system unlock
- [Phase 8 gotchas](project_jsa_phase8.md) — set_current_stage() for revision flow; running→pending in ALLOWED; CORS regex; ASGITransport lifespan pattern; CLI startup sequence
- [BF-1/2/3 bugfix phases](project_jsa_bugfixes_bf.md) — dismiss state, JD in API, agent_timeout, prompt fixes (cover letter sentinel, CV markdown + format)
