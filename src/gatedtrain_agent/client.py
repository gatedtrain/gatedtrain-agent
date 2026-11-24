"""Client for the gatedtrain CLI: runs it with --json and parses the one-line result envelope."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

PREFIX_ENV = "GATEDTRAIN_AGENT_CLI"
RESULT_SCHEMA = "gatedtrain.cli_result"
TIMEOUT_EXIT = 124


@dataclass(frozen=True)
class Envelope:
    command: str
    ok: bool
    exit_code: int
    data: dict = field(default_factory=dict)
    problems: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def problem_lines(self) -> list[str]:
        return [f"{p.get('rule', '')} {p.get('field', '')}: {p.get('message', '')} (fix: {p.get('fix', '')})"
                for p in self.problems]


def cli_prefix() -> list[str]:
    """The command that starts gatedtrain: `GATEDTRAIN_AGENT_CLI` (a JSON array of strings) or python -m gatedtrain."""
    raw = os.environ.get(PREFIX_ENV)
    if raw is None:
        return [sys.executable, "-m", "gatedtrain"]
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{PREFIX_ENV} must be a JSON array of strings: {exc}") from exc
    if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{PREFIX_ENV} must be a non-empty JSON array of strings")
    return value


def _error(args: list[str], returncode: int, what: str, stdout: str, stderr: str = "") -> Envelope:
    tail = f" stderr tail: {stderr[-200:]}" if stderr.strip() else ""
    problem = {
        "rule": "AGENT",
        "field": "gatedtrain",
        "message": f"{what}; exit {returncode}; stdout tail: {stdout[-200:]}{tail}",
        "fix": "run the same command without --json to see its output",
    }
    return Envelope(" ".join(args[:2]), False, returncode, {}, [problem], [])


class Client:
    def __init__(self, prefix: list[str] | None = None, timeout: float | None = None):
        self.prefix = list(prefix) if prefix is not None else cli_prefix()
        self.timeout = timeout

    def argv(self, args: list[str]) -> list[str]:
        return [*self.prefix, "--json", *args]

    def call(self, args: list[str]) -> Envelope:
        try:
            proc = subprocess.run(self.argv(args), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  errors="replace", timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return _error(args, TIMEOUT_EXIT, f"gatedtrain did not finish within {self.timeout} seconds", out)
        except OSError as exc:
            return _error(args, 127, f"cannot start gatedtrain: {exc}", "")
        return self.parse(proc.stdout, proc.returncode, args, proc.stderr or "")

    @staticmethod
    def parse(stdout: str, returncode: int, args: list[str], stderr: str = "") -> Envelope:
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        if not lines:
            return _error(args, returncode, "gatedtrain printed no result", stdout, stderr)
        try:
            raw: Any = json.loads(lines[-1])
        except ValueError:
            return _error(args, returncode, "gatedtrain printed a non-JSON result", stdout, stderr)
        if not isinstance(raw, dict) or raw.get("schema") != RESULT_SCHEMA:
            return _error(args, returncode, "gatedtrain printed JSON with an unexpected schema", stdout, stderr)
        try:
            return Envelope(
                command=str(raw["command"]), ok=bool(raw["ok"]), exit_code=int(raw["exit"]),
                data=dict(raw.get("data") or {}), problems=[dict(p) for p in raw.get("problems") or []],
                warnings=[str(w) for w in raw.get("warnings") or []],
            )
        except (KeyError, TypeError, ValueError):
            return _error(args, returncode, "gatedtrain printed a malformed result", stdout, stderr)
