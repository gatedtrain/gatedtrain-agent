"""Shared test helpers: the gatedtrain checkout for end-to-end tests and a fake-CLI script writer."""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path


def gatedtrain_src() -> Path | None:
    """The gatedtrain v0.5.0 checkout named by GATEDTRAIN_SRC, or None when unset or missing."""
    raw = os.environ.get("GATEDTRAIN_SRC")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def e2e_prefix() -> list[str]:
    """CLI prefix for end-to-end tests: the fixture runner of the gatedtrain checkout."""
    src = gatedtrain_src()
    if src is None:
        raise RuntimeError("GATEDTRAIN_SRC is not set")
    return [sys.executable, str(src / "tests" / "fixtures" / "ainative" / "run_cli.py")]


def write_fake_cli(directory: Path, body: str, name: str = "fake_cli.py") -> list[str]:
    """Write a Python script and return the CLI prefix that runs it. `body` may use `argv` (sys.argv[1:])."""
    script = directory / name
    script.write_text("import json, sys, time\nargv = sys.argv[1:]\n" + textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(script)]
