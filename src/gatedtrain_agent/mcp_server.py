"""MCP server: nine tools that drive a gatedtrain AI-native project through its --json CLI.

The server holds no trust logic. It refuses relative or missing roots, refuses a metric confirmation for a project
without a keeper (that confirmation is the person's), and allows one running stage per project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from gatedtrain_agent import runs
from gatedtrain_agent.client import Client, Envelope

TOOL_NAMES = (
    "project_create", "project_status", "stage_plan", "stage_run", "stage_wait",
    "project_report", "job_list", "job_logs", "job_cancel",
)
MAX_WAIT_SECONDS = 600


def _error(message: str) -> dict[str, Any]:
    return {"ok": False, "error": message}


def _envelope(env: Envelope) -> dict[str, Any]:
    return {"ok": env.ok, "exit": env.exit_code, "command": env.command, "data": env.data,
            "problems": env.problems, "warnings": env.warnings}


class Tools:
    """The tool implementations; every method returns a JSON-serialisable dict and never raises on bad input."""

    def __init__(self, client: Client | None = None):
        self.client = client if client is not None else Client()

    @staticmethod
    def _clamp(seconds: float) -> float:
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(float(MAX_WAIT_SECONDS), value))

    @staticmethod
    def _abs(label: str, value: Any) -> str | None:
        """An error message when `value` is not an absolute path string, else None."""
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return f"{label} must be an absolute path; got {value!r}"
        return None

    def _root(self, root: Any) -> tuple[Path | None, dict | None]:
        message = self._abs("root", root)
        if message:
            return None, _error(message)
        path = Path(root)
        if not path.is_dir():
            return None, _error(f"root {root} does not exist (use project_create to make a project)")
        if not (path / "project.yaml").is_file():
            return None, _error(f"root {root} is not a gatedtrain project (no project.yaml)")
        return path, None

    @staticmethod
    def _stage(stage: Any) -> str | None:
        try:
            runs._check_stage(stage)
        except ValueError as exc:
            return str(exc)
        return None

    @staticmethod
    def _job(job_id: Any) -> str | None:
        if not isinstance(job_id, str) or not job_id or job_id.startswith("-"):
            return f"invalid job_id {job_id!r}: it must be non-empty and not start with '-'"
        return None

    def _call(self, root: Any, command: list[str], *rest: str) -> dict[str, Any]:
        path, err = self._root(root)
        if err:
            return err
        return _envelope(self.client.call([*command, str(path), *rest]))

    def project_create(self, root: str, goal: str, data: str, pipeline: str, keeper: str | None = None,
                       gpu_hours: float | None = None) -> dict[str, Any]:
        """Create a gatedtrain project at an absolute, not yet existing root from a goal, a data file and a pipeline."""
        for label, value in (("root", root), ("data", data), ("pipeline", pipeline)):
            message = self._abs(label, value)
            if message:
                return _error(message)
        if keeper is not None:
            message = self._abs("keeper", keeper)
            if message:
                return _error(message)
        args = ["project", "create", root, "--goal", goal, "--data", data, "--pipeline", pipeline]
        if keeper is not None:
            args += ["--keeper", keeper]
        if gpu_hours is not None:
            args += ["--gpu-hours", str(gpu_hours)]
        return _envelope(self.client.call(args))

    def project_status(self, root: str) -> dict[str, Any]:
        """Status of every stage and the gate. Stages are listed alphabetically; the order is in pipeline.yaml."""
        return self._call(root, ["project", "status"])

    def stage_plan(self, root: str, stage: str) -> dict[str, Any]:
        """The plan of one stage: what it will do, and `needs_user` when a person must decide first."""
        message = self._stage(stage)
        if message:
            return _error(message)
        return self._call(root, ["project", "plan"], stage)

    def project_report(self, root: str) -> dict[str, Any]:
        """The report of a finished project; the only source for result numbers."""
        return self._call(root, ["project", "report"])

    def job_list(self, root: str) -> dict[str, Any]:
        """List the project's jobs."""
        return self._call(root, ["project", "job", "list"])

    def job_logs(self, root: str, job_id: str, tail: int | None = None) -> dict[str, Any]:
        """Logs of one job, optionally only the last `tail` lines."""
        message = self._job(job_id)
        if message:
            return _error(message)
        extra = ["--tail", str(int(tail))] if tail is not None else []
        return self._call(root, ["project", "job", "logs"], job_id, *extra)

    def job_cancel(self, root: str, job_id: str) -> dict[str, Any]:
        """Cancel one job."""
        message = self._job(job_id)
        if message:
            return _error(message)
        return self._call(root, ["project", "job", "cancel"], job_id)

    def stage_run(self, root: str, stage: str, decision: dict | None = None,
                  wait_seconds: float = 60) -> dict[str, Any]:
        """Start a stage in the background and wait up to wait_seconds (0 to 600).

        Returns {"running": true, ...} when it is still running (call stage_wait), else the finished envelope.
        """
        path, err = self._root(root)
        if err:
            return err
        if decision is not None:
            if not isinstance(decision, dict):
                return _error("decision must be a JSON object")
            if "metric_confirmed" in decision and not (path / "trust" / "keeper.json").is_file():
                return _error("metric confirmation without a keeper is the person's: run `gatedtrain-agent do` "
                              "at a terminal")
        try:
            runs.start(self.client, path, stage, decision)
        except runs.RunBusy as exc:
            return _error(f"busy: {exc}")
        except ValueError as exc:
            return _error(str(exc))
        except OSError as exc:
            return _error(f"cannot start stage: {exc}")
        return self._waited(path, stage, wait_seconds)

    def stage_wait(self, root: str, stage: str, wait_seconds: float = 60) -> dict[str, Any]:
        """Wait up to wait_seconds (0 to 600) for a started stage; {"running": true} if it is still going.

        A stage run in the foreground by `gatedtrain-agent do` also reads as running; once it ends there is no
        background result to return, and the error says to read project_status instead."""
        path, err = self._root(root)
        if err:
            return err
        return self._waited(path, stage, wait_seconds)

    def _waited(self, path: Path, stage: str, wait_seconds: float) -> dict[str, Any]:
        try:
            result = runs.wait(self.client, path, stage, self._clamp(wait_seconds))
        except (FileNotFoundError, ValueError) as exc:
            return _error(str(exc))
        if result["running"]:
            return {"ok": True, "running": True, "stage": result["stage"], "pid": result["pid"]}
        return {"running": False, **_envelope(result["envelope"])}


def build_server(client: Client | None = None) -> MCPServer:
    tools = Tools(client)
    server = MCPServer("gatedtrain")
    for name in TOOL_NAMES:
        server.add_tool(getattr(tools, name), name=name)
    return server


def serve() -> None:
    build_server().run("stdio")
