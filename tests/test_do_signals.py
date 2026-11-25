"""`gatedtrain-agent do` as a real process: Ctrl-C during a stage and a SIGKILLed `do` (fake CLI, no gatedtrain)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import write_fake_cli
from gatedtrain_agent import runs

# status: one stage "a" pending; plan: nothing needed; run: sleep until killed; report: not reached.
FAKE = """
    i = argv.index("project")
    sub = argv[i + 1]
    if sub == "run":
        open(argv[i + 2] + "/child.pid", "w").write(str(__import__("os").getpid()))
        time.sleep(120)
    data = {"stages": {"a": {"status": "pending"}}} if sub == "status" else {}
    print(json.dumps({"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "project " + sub,
                      "ok": True, "exit": 0, "data": data, "problems": [], "warnings": []}))
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"


class DoProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "proj"
        self.root.mkdir()
        self.prefix = write_fake_cli(self.dir, FAKE)

    def start_do(self) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["GATEDTRAIN_AGENT_CLI"] = json.dumps(self.prefix)
        proc = subprocess.Popen([sys.executable, "-m", "gatedtrain_agent.cli", "do", str(self.root)], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)  # its own process group, like a terminal's foreground job
        self.addCleanup(self._kill_group, proc)
        return proc

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()

    def wait_for_child(self) -> int:
        lock = self.root / "agent" / "run.lock"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                child = json.loads(lock.read_text()).get("child")
                if child and (self.root / "child.pid").exists():
                    return child["pid"]
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        self.fail("the stage never started")

    def test_ctrl_c_is_a_short_message_not_a_traceback(self) -> None:
        proc = self.start_do()
        child = self.wait_for_child()
        os.killpg(proc.pid, signal.SIGINT)  # what the terminal sends the whole foreground group
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130, out + err)
        self.assertNotIn("Traceback", err)
        self.assertIn("a: interrupted", out)
        self.assertFalse((self.root / "agent" / "run.lock").exists())
        self.assertFalse(_alive(child))

    def test_sigkilled_do_leaves_a_lock_that_stays_live_while_its_child_runs(self) -> None:
        proc = self.start_do()
        child = self.wait_for_child()
        os.kill(proc.pid, signal.SIGKILL)  # only `do`: the child lives on
        proc.wait()
        self.assertTrue(_alive(child))
        live = runs.running(self.root)
        self.assertIsNotNone(live)
        self.assertEqual(live["stage"], "a")
        with self.assertRaises(runs.RunBusy):
            runs.start(runs.Client(self.prefix), self.root, "b", None)
        os.kill(child, signal.SIGKILL)
        deadline = time.monotonic() + 10
        while runs.running(self.root) is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNone(runs.running(self.root))


if __name__ == "__main__":
    unittest.main()
