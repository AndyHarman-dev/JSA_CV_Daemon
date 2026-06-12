## Not Nested Yet

**Concurrency without blocking.** A parked job frees its worker slot; the next CSV row starts immediately.

Scenario: 10 jobs at the table. The app picks up 5 jobs and runs in paraller while other 5 are asleep. Next, if any of the 5 job is stopped for follow-up or any other reason, the app picks up another one, while the stopped being resolved.