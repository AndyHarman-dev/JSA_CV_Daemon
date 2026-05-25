"""Checkpoint write/read helpers for single-transaction job state persistence.

This module is a thin re-export of repo.checkpoint that makes the single-
transaction rule explicit at the call site. All stages should import and call
`checkpoint(...)` from here rather than calling repo directly, so the
single-transaction contract is expressed in one place.
"""

from jsa.db import repo


# Re-export repo.checkpoint as the canonical checkpoint function for stages.
# Every write that changes job state must go through this path so that
# Messages, Document, FollowUp, and Job.state are committed atomically.
checkpoint = repo.checkpoint

__all__ = ["checkpoint"]
