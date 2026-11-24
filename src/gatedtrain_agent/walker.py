"""`do`: walk a project's stages in order and stop, in plain words, wherever a person is needed."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import yaml

from gatedtrain_agent.client import Client, Envelope
from gatedtrain_agent.runs import RunBusy, hold

EXIT_NEEDS_PERSON = 4
_EXIT_NOT_PASS = 1
_EXIT_REFUSED = 3


def _decision_file(root: Path, stage: str, decision: dict) -> Path:
    path = root / "agent" / "decisions" / f"{stage}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decision), encoding="utf-8")
    return path


def _stage_order(root: Path, stages: dict) -> list[str]:
    """Pipeline order. The `--json` envelope sorts keys, so `project status` loses it; the project's own
    `pipeline.yaml` has it. Without that file, fall back to the status keys as given."""
    names: list[str] = []
    try:
        raw = yaml.safe_load((root / "pipeline.yaml").read_text(encoding="utf-8"))
        names = [s["name"] for s in raw["stages"] if isinstance(s, dict) and s.get("name") in stages]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        names = []
    return names + [n for n in stages if n not in names]


def _print_problems(env: Envelope, out: TextIO) -> None:
    for line in env.problem_lines():
        print(f"  {line}", file=out)


def _crashed(env: Envelope, command: str) -> bool:
    """True for an envelope the client built from a crash or garbage, or one of another command."""
    return not env.command.startswith(command) or any(p.get("rule") == "AGENT" for p in env.problems)


def _finish(client: Client, root: Path, out: TextIO, gate_failed: bool) -> int:
    """Run `project report`, print where it is and the gate status; exit 0 on PASS, else 1."""
    rep = client.call(["project", "report", str(root)])
    data = rep.data or {}
    report = data.get("report") or {}
    status = report.get("status") if isinstance(report, dict) else None
    if _crashed(rep, "project report") or rep.exit_code not in (0, _EXIT_NOT_PASS):
        print("report: refused", file=out)
        _print_problems(rep, out)
        return _EXIT_REFUSED
    if data.get("report_md"):
        print(f"report: {data['report_md']}", file=out)
    print(f"gate: {status or ('PASS' if rep.exit_code == 0 else 'not PASS')}", file=out)
    return 0 if rep.exit_code == 0 and not gate_failed else _EXIT_NOT_PASS


def _needs_person(out: TextIO, name: str, summary: str, *lines: str, value: str | None = None) -> int:
    print(f"{name}: needs a person" + (f" ({value})" if value else ""), file=out)
    if summary:
        print(f"  {summary}", file=out)
    for line in lines:
        print(f"  {line}", file=out)
    return EXIT_NEEDS_PERSON


def _busy(out: TextIO, exc: RunBusy) -> int:
    print(f"busy: {exc}", file=out)
    return _EXIT_REFUSED


def walk(client: Client, root: Path, *, accept_inconclusive: bool = False,
         ask: Callable[[str], str] | None, out: TextIO) -> int:
    """Run every stage that is not done, in pipeline order. Exit 0 PASS, 1 not PASS, 3 refused, 4 person needed.

    Each `project run` holds the project's run lock, so no background run can start beside it."""
    root = Path(root)
    status = client.call(["project", "status", str(root)])
    if not status.ok:
        print("status: refused", file=out)
        _print_problems(status, out)
        return _EXIT_REFUSED
    stages = status.data.get("stages") or {}
    gate_failed = False
    for name in _stage_order(root, stages):
        info = stages[name]
        if isinstance(info, dict) and info.get("status") == "done":
            continue
        plan = client.call(["project", "plan", str(root), name])
        if not plan.ok:
            print(f"{name}: refused", file=out)
            _print_problems(plan, out)
            return _EXIT_REFUSED
        needs = plan.data.get("needs_user")
        summary = plan.data.get("summary", "")
        decision: dict | None = None
        keeper = False
        if needs == "data_too_small":
            if not accept_inconclusive:
                return _needs_person(out, name, summary,
                                     "The data is too small for a conclusive result. Re-run with "
                                     "--accept-inconclusive to accept an INCONCLUSIVE outcome, or add data.")
            decision = {"accept_inconclusive": True}
        elif needs == "confirm_metric":
            if (root / "trust" / "keeper.json").exists():
                keeper = True
                decision = {"metric_confirmed": True}
            elif ask is None:
                return _needs_person(out, name, summary,
                                     "Run `gatedtrain-agent do` at a terminal to confirm the metric.")
            else:
                print(summary, file=out)
                try:
                    answer = ask("Type yes to confirm this metric: ")
                except (EOFError, KeyboardInterrupt):
                    print("", file=out)
                    return _needs_person(out, name, "", "No answer: nothing was confirmed.")
                if answer.strip() != "yes":
                    return _needs_person(out, name, "", "The metric was not confirmed.")
                decision = {"metric_confirmed": True}
        elif needs:
            return _needs_person(out, name, summary, value=str(needs))
        args = ["project", "run", str(root), name]
        path: Path | None = None
        env: Envelope | None = None
        try:
            with hold(root, name):
                if decision is not None:
                    path = _decision_file(root, name, decision)
                    args += ["--decision", str(path)]
                env = client.call(args)
        except RunBusy as exc:
            return _busy(out, exc)
        except (OSError, ValueError) as exc:
            if env is not None:
                # The stage ran; only releasing the lock failed. Say what the stage did, then stop.
                print(f"{name}: ran (exit {env.exit_code})", file=out)
                _print_problems(env, out)
                print(f"  AGENT run lock: could not release it: {exc}", file=out)
            else:
                print(f"{name}: refused", file=out)
                print(f"  AGENT run lock or decision file: {exc}", file=out)
            return _EXIT_REFUSED
        if env.exit_code == _EXIT_NOT_PASS and "stage" in env.data:
            gate_failed = True
            print(f"{name}: done", file=out)
            continue
        if not env.ok:
            if keeper and any(p.get("rule") == "AN-18" for p in env.problems):
                return _needs_person(out, name, summary, *env.problem_lines(),
                                     "The keeper must confirm this metric. Run:",
                                     f"gatedtrain keeper confirm {root} --decision {path}")
            print(f"{name}: refused", file=out)
            _print_problems(env, out)
            return _EXIT_REFUSED
        print(f"{name}: {'reused' if env.data.get('reused') else 'done'}", file=out)
    return _finish(client, root, out, gate_failed)
