"""Shared test helpers for CLI-backend unit tests that patch run_killable.

Builds (returncode, stdout, stderr) tuples shaped like
jsa.agents._subprocess.run_killable's return value (KillableResult), so
tests can do:

    with patch("jsa.agents.claude_cli.run_killable", new=AsyncMock(return_value=ok("hi"))):
        ...
"""

from __future__ import annotations


def ok(stdout: bytes | str = b"", returncode: int = 0, stderr: bytes = b"") -> tuple[int, bytes, bytes]:
    """A successful run_killable() result. `stdout` may be str or bytes."""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    return returncode, stdout, stderr


def empty(returncode: int = 1, stderr: bytes = b"error") -> tuple[int, bytes, bytes]:
    """A failed run_killable() result with empty stdout (returncode != 0 by default)."""
    return returncode, b"", stderr
