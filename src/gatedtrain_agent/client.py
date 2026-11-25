"""Client for the gatedtrain CLI: runs it with --json and parses the one-line result envelope."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

PREFIX_ENV = "GATEDTRAIN_AGENT_CLI"
RESULT_SCHEMA = "gatedtrain.cli_result"
TIMEOUT_EXIT = 124
INTERRUPT_GRACE_SECONDS = 5.0


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


def _stop(proc: subprocess.Popen, grace: float) -> None:
    """Wait `grace` seconds for the child to exit (draining its pipes so it cannot block on a full one), then
    terminate it, then kill it; always reap it."""
    try:
        for action in (None, proc.terminate):
            if action is not None:
                action()
            try:
                proc.communicate(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue
        proc.kill()
        proc.wait()
    except BaseException:  # a second Ctrl-C: do not leave the child behind
        proc.kill()
        proc.wait()
        raise


class Client:
    def __init__(self, prefix: list[str] | None = None, timeout: float | None = None):
        self.prefix = list(prefix) if prefix is not None else cli_prefix()
        self.timeout = timeout

    def argv(self, args: list[str]) -> list[str]:
        return [*self.prefix, "--json", *args]

    def call(self, args: list[str], on_start: Callable[[int], None] | None = None) -> Envelope:
        """Run one gatedtrain command and parse its result. `on_start` gets the child's pid right after it starts.

        On Ctrl-C the child (which the terminal also signalled) gets a grace period to exit, then is terminated
        and killed; it is always reaped before the interrupt propagates."""
        argv = self.argv(args)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, errors="replace")
        except OSError as exc:
            return _error(args, 127, f"cannot start gatedtrain: {exc}", "")
        with proc:
            try:
                if on_start is not None:
                    on_start(proc.pid)
                stdout, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                # A grandchild may still hold the pipes: read what is there, but never wait for it.
                try:
                    out, _ = proc.communicate(timeout=1.0)
                except subprocess.TimeoutExpired as exc:
                    out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                return _error(args, TIMEOUT_EXIT, f"gatedtrain did not finish within {self.timeout} seconds", out)
            except KeyboardInterrupt:
                _stop(proc, INTERRUPT_GRACE_SECONDS)
                raise
            except BaseException:
                _stop(proc, 0)
                raise
        return self.parse(stdout, proc.returncode, args, stderr or "")

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
