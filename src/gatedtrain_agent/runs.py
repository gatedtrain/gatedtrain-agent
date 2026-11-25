"""Background stage runs: one gatedtrain stage per project at a time, started detached and polled by `wait`."""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from gatedtrain_agent.client import Client, Envelope

POLL_SECONDS = 0.05
RC_GRACE_SECONDS = 2.0
UNREADABLE_LOCK_SECONDS = 10.0


class RunBusy(Exception):
    """A stage of this project is already running."""


def _agent_dir(root: Path) -> Path:
    return Path(root) / "agent"


def _lock_path(root: Path) -> Path:
    return _agent_dir(root) / "run.lock"


def _runs_dir(root: Path) -> Path:
    return _agent_dir(root) / "runs"


STAGE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


def _check_stage(stage: str) -> None:
    if not isinstance(stage, str) or not STAGE_RE.fullmatch(stage) or ".." in stage:
        raise ValueError(f"invalid stage name {stage!r}: use letters, digits, '_', '.', '-' and no '..'")


def _stat_fields(pid: int) -> list[str] | None:
    """Fields after the command name in /proc/<pid>/stat (state first, start time at index 19), or None."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return text[text.rindex(")") + 2:].split()
    except (OSError, ValueError):
        return None


def _start_time(pid: int) -> str | None:
    """Process start time (field 22 of /proc/<pid>/stat), or None when unavailable."""
    fields = _stat_fields(pid)
    return fields[19] if fields is not None and len(fields) > 19 else None


def _pid_alive(pid: int, start: str | None = None) -> bool:
    """Live when the pid exists, is not a zombie and, if a start time is known and /proc is readable, the start
    time matches. A zombie is finished: it only waits for its parent to reap it."""
    if Path("/proc/self/stat").exists():
        fields = _stat_fields(pid)
        if fields is None or (fields and fields[0] == "Z"):
            return False
        if start is not None and (len(fields) <= 19 or fields[19] != start):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _owner_alive(entry: object) -> bool:
    """True when `entry` ({"pid": int, "start": str}) names a live process."""
    if not isinstance(entry, dict):
        return False
    pid, start = entry.get("pid"), entry.get("start")
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and _pid_alive(
        pid, start if isinstance(start, str) else None)


def _lock_state(root: Path) -> tuple[str, dict | None]:
    """('none' | 'live' | 'stale', content)."""
    path = _lock_path(root)
    content = _read_json(path)
    if content is None:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return "none", None
        # A lock being written right now is empty for a moment: treat it as live while young.
        return ("live" if age < UNREADABLE_LOCK_SECONDS else "stale"), None
    if _owner_alive(content) or _owner_alive(content.get("child")):
        return "live", content
    return "stale", content


def running(root: Path) -> dict | None:
    state, content = _lock_state(Path(root))
    return content if state == "live" and content is not None else None


@contextmanager
def _guard(root: Path) -> Iterator[None]:
    """Serialise lock takeover between starters (threads and processes): the verdict 'stale', the removal of the
    old lock and the creation of the new one must not interleave with another starter's."""
    path = _agent_dir(root) / "run.lock.guard"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _acquire(root: Path, stage: str) -> Path:
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _guard(root):
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                state, content = _lock_state(root)
                if state == "live":
                    who = f"stage {content.get('stage')} (pid {content.get('pid')})" if content else "another stage"
                    raise RunBusy(f"{who} is already running for this project") from None
                path.unlink(missing_ok=True)
                continue
            os.close(fd)
            return path
    raise RunBusy("could not take the run lock for this project")


def _write_lock(lock: Path, content: dict) -> None:
    tmp = lock.with_suffix(".lock.tmp")
    tmp.write_text(json.dumps(content), encoding="utf-8")
    os.replace(tmp, lock)


def _supersede(root: Path, stage: str) -> None:
    """Remove the previous run's record and return code of `stage`, so neither is taken for the new run's. Under the
    guard, so the previous run's reaper cannot write its return code after this."""
    with _guard(root):
        for suffix in (".json", ".rc"):
            (_runs_dir(root) / f"{stage}{suffix}").unlink(missing_ok=True)


class Held:
    """The lock held by `hold`. `child(pid)` records the gatedtrain process the foreground run started, so the lock
    stays live while that process runs even if this one is killed without cleanup."""

    def __init__(self, lock: Path, stage: str):
        self._lock = lock
        self._content = {"stage": stage, "pid": os.getpid(), "start": _start_time(os.getpid())}

    def _write(self) -> None:
        _write_lock(self._lock, self._content)

    def child(self, pid: int) -> None:
        self._content = {**self._content, "child": {"pid": pid, "start": _start_time(pid)}}
        self._write()


@contextmanager
def hold(root: Path, stage: str) -> Iterator[Held]:
    """Hold the project's run lock for this process while a stage runs in the foreground (used by `do`).

    Raises RunBusy when another stage is running. The lock is released on exit, also on exceptions."""
    _check_stage(stage)
    root = Path(root)
    lock = _acquire(root, stage)
    try:
        held = Held(lock, stage)
        held._write()
        # A foreground run supersedes this stage's earlier background run: `wait` must not return that
        # older result as if it were this run's.
        _supersede(root, stage)
        yield held
    finally:
        with _guard(root):
            content = _read_json(lock)
            if content is None or content.get("pid") == os.getpid():
                lock.unlink(missing_ok=True)


def _reap(proc: subprocess.Popen, rc_path: Path, lock: Path) -> None:
    code = proc.wait()
    root = lock.parent.parent
    # Under the guard no starter can take the stale lock or clear this stage's files in between: a run that was
    # superseded (its record replaced or removed) leaves no return code behind, and only our own lock is removed.
    with _guard(root):
        record = _read_json(rc_path.with_suffix(".json"))
        if record is not None and record.get("pid") == proc.pid:
            tmp = rc_path.with_suffix(".rc.tmp")
            tmp.write_text(str(code), encoding="utf-8")
            os.replace(tmp, rc_path)
        content = _read_json(lock)
        if content is not None and content.get("pid") == proc.pid:
            lock.unlink(missing_ok=True)


def start(client: Client, root: Path, stage: str, decision: dict | None) -> dict:
    _check_stage(stage)
    root = Path(root)
    lock = _acquire(root, stage)
    proc: subprocess.Popen | None = None
    try:
        runs = _runs_dir(root)
        runs.mkdir(parents=True, exist_ok=True)
        args = ["project", "run", str(root), stage]
        if decision is not None:
            decisions = _agent_dir(root) / "decisions"
            decisions.mkdir(parents=True, exist_ok=True)
            decision_path = decisions / f"{stage}.json"
            decision_path.write_text(json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            args += ["--decision", str(decision_path)]
        argv = client.argv(args)
        out_path = runs / f"{stage}.out"
        rc_path = runs / f"{stage}.rc"
        _supersede(root, stage)
        with open(out_path, "wb") as out:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start_time = _start_time(proc.pid)
        _write_lock(lock, {"stage": stage, "pid": proc.pid, "start": start_time})
        (runs / f"{stage}.json").write_text(
            json.dumps({"pid": proc.pid, "start": start_time, "started_utc": started, "argv": argv,
                        "out": str(out_path)}, indent=2),
            encoding="utf-8")
    except BaseException:
        if proc is not None:
            # Nobody would reap or track this child: stop it before giving up the lock.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
        with _guard(root):
            # Only our own lock: once the child is dead the lock reads stale and another starter may have it.
            content = _read_json(lock)
            if content is None or content.get("pid") == (proc.pid if proc is not None else None):
                lock.unlink(missing_ok=True)
        raise
    threading.Thread(target=_reap, args=(proc, rc_path, lock), name=f"reap-{stage}", daemon=True).start()
    return {"stage": stage, "pid": proc.pid, "started_utc": started}


def _finished(root: Path, stage: str, rc_path: Path, record: dict) -> dict:
    try:
        code = int(rc_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        code = -1
    try:
        stdout = Path(record.get("out") or _runs_dir(root) / f"{stage}.out").read_text(encoding="utf-8",
                                                                                      errors="replace")
    except OSError:
        stdout = ""
    argv = record.get("argv") or ["project", "run", stage]
    return {"running": False, "envelope": Client.parse(stdout, code, list(argv[-4:]))}


def wait(client: Client, root: Path, stage: str, seconds: float) -> dict:
    _check_stage(stage)
    root = Path(root)
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        # `do` runs a stage in the foreground under the lock, with no background record.
        state, live = _lock_state(root)
        if state == "live" and live is None:
            # A lock taken a moment ago and not yet written: wait for its content.
            if time.monotonic() >= deadline:
                return {"running": True, "stage": stage, "pid": None}
            time.sleep(POLL_SECONDS)
            continue
        if state != "live" or live.get("stage") != stage:
            break
        record = _read_json(_runs_dir(root) / f"{stage}.json")
        if record is not None and record.get("pid") == live.get("pid"):
            break
        if time.monotonic() >= deadline:
            return {"running": True, "stage": stage, "pid": live.get("pid")}
        time.sleep(POLL_SECONDS)
    record = _read_json(_runs_dir(root) / f"{stage}.json")
    if record is None:
        raise FileNotFoundError(f"stage {stage} has no background run for {root} (never started here, or last run "
                                "in the foreground by `gatedtrain-agent do`); use project_status")
    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError(f"run record of stage {stage} is corrupt (missing or invalid pid); start the stage again")
    start = record.get("start") if isinstance(record.get("start"), str) else None
    rc_path = _runs_dir(root) / f"{stage}.rc"
    dead_since: float | None = None
    while True:
        if rc_path.exists():
            return _finished(root, stage, rc_path, record)
        if _pid_alive(pid, start):
            dead_since = None
            if time.monotonic() >= deadline:
                return {"running": True, "stage": stage, "pid": pid}
        else:
            # Gone without a return code (killed, host reboot) unless the reaper is about to write it.
            dead_since = dead_since if dead_since is not None else time.monotonic()
            if time.monotonic() - dead_since >= RC_GRACE_SECONDS:
                return _finished(root, stage, rc_path, record)
        time.sleep(POLL_SECONDS)
