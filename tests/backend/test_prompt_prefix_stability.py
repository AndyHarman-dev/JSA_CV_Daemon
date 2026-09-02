"""Byte-stability gate for the prompt-caching plan (see the plan's Phase 0).

Prompt caching is a pure prefix match: a caching provider only reports a cache hit
when the sent prefix is byte-identical to a previously-sent one. The assembled system
prompt (``assemble_system_prompt``) embeds ``json.dumps(schema, indent=2)`` where
``schema`` comes from Pydantic's ``model_json_schema()`` — nothing today asserts that
this rendering is deterministic *across process restarts*, not just within one process.
If any part of that path iterates a ``set`` (e.g. an enum/``Literal`` union materialised
through one), key/item order can vary with ``PYTHONHASHSEED``, so the same logical
schema would render as different bytes on different server restarts — silently turning
prompt caching into a pure cache-write cost with zero reads, while every other unit
test in this plan would still pass. This test is the single gate that would catch that
failure mode; run it before any other phase of the prompt-caching plan.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys

import pytest

from jsa.db.models import Stage
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.prompts.loader import read_prompt
from jsa.schema.turn_models import json_schema_for

_SUBPROCESS_SNIPPET = """
import hashlib
from jsa.db.models import Stage
from jsa.pipeline.prompt_assembly import assemble_system_prompt
from jsa.prompts.loader import read_prompt
from jsa.schema.turn_models import json_schema_for

prompt = assemble_system_prompt(
    read_prompt("cv_adjust"),
    language="en",
    structured_model=json_schema_for(Stage.cv_adjust),
)
print(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
"""


def _assembled_prompt_hash() -> str:
    prompt = assemble_system_prompt(
        read_prompt("cv_adjust"),
        language="en",
        structured_model=json_schema_for(Stage.cv_adjust),
    )
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _subprocess_hash(pythonhashseed: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SNIPPET],
        capture_output=True,
        text=True,
        env={"PYTHONHASHSEED": pythonhashseed, **_inherited_env()},
        check=True,
        cwd=str(_repo_root()),
    )
    return result.stdout.strip()


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _inherited_env() -> dict[str, str]:
    import os

    # Preserve PATH/PYTHONPATH/venv-related vars so the subprocess can import jsa;
    # PYTHONHASHSEED itself is set explicitly by the caller and must win.
    return {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}


def test_assembled_system_prompt_is_byte_stable_within_process():
    first = _assembled_prompt_hash()
    second = _assembled_prompt_hash()
    assert first == second


@pytest.mark.parametrize("pythonhashseed", ["0", "1"])
def test_assembled_system_prompt_is_byte_stable_across_processes(pythonhashseed):
    in_process = _assembled_prompt_hash()
    out_of_process = _subprocess_hash(pythonhashseed)
    assert out_of_process == in_process, (
        f"assembled system prompt hash differs under PYTHONHASHSEED={pythonhashseed} "
        "— prompt caching would silently never hit across server restarts; sort the "
        "non-deterministic collection at the point it is built (see plan Phase 0)"
    )
