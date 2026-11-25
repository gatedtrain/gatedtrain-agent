import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import write_fake_cli
from gatedtrain_agent import runs
from gatedtrain_agent.client import Client

BODY = (
    'time.sleep({sleep})\n'
    'print(json.dumps({{"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "project run", '
    '"ok": True, "exit": 0, "data": {{"argv": argv}}, "problems": [], "warnings": []}}))\n'
)


class RunsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "project"
        self.root.mkdir()
        self.addCleanup(self._drain)

    def _drain(self):
        """Kill any child still holding the lock and join the reaper threads."""
        live = runs.running(self.root)
        if live:
            try:
                os.killpg(live["pid"], signal.SIGKILL)
            except OSError:
                pass
        for t in threading.enumerate():
            if t.name.startswith("reap-"):
                t.join(timeout=10)

    def client(self, sleep: float) -> Client:
        return Client(write_fake_cli(self.dir, BODY.format(sleep=sleep)))

    def test_hold_blocks_start_and_releases(self):
        client = self.client(0.1)
        lock = self.root / "agent" / "run.lock"
        with runs.hold(self.root, "train"):
            self.assertEqual(runs.running(self.root)["pid"], os.getpid())
            with self.assertRaises(runs.RunBusy):
                runs.start(client, self.root, "other", None)
            with self.assertRaises(runs.RunBusy):
                with runs.hold(self.root, "again"):
                    pass
            self.assertTrue(lock.exists())
        self.assertFalse(lock.exists())

    def test_hold_releases_on_exception(self):
        with self.assertRaises(KeyError):
            with runs.hold(self.root, "train"):
                raise KeyError("x")
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_wait_reports_running_while_do_holds_the_stage(self):
        client = self.client(0)
        runs.start(client, self.root, "train", None)
        self.assertFalse(runs.wait(client, self.root, "train", 10)["running"])
        for t in threading.enumerate():
            if t.name.startswith("reap-"):
                t.join(timeout=10)
        with runs.hold(self.root, "train"):
            result = runs.wait(client, self.root, "train", 0.2)
            self.assertTrue(result["running"])
            self.assertEqual(result["pid"], os.getpid())
        # The older background result is superseded by the foreground run, never returned as fresh.
        with self.assertRaisesRegex(FileNotFoundError, "project_status"):
            runs.wait(client, self.root, "train", 0.1)

    def test_wait_treats_an_unwritten_lock_as_running(self):
        client = self.client(0)
        runs.start(client, self.root, "train", None)
        self.assertFalse(runs.wait(client, self.root, "train", 10)["running"])
        for t in threading.enumerate():
            if t.name.startswith("reap-"):
                t.join(timeout=10)
        (self.root / "agent" / "run.lock").write_text("")
        self.assertTrue(runs.wait(client, self.root, "train", 0.1)["running"])

    def test_start_and_wait_finishes(self):
        client = self.client(0.3)
        info = runs.start(client, self.root, "train", None)
        self.assertEqual(info["stage"], "train")
        self.assertTrue(info["started_utc"].endswith("Z"))
        self.assertEqual(runs.running(self.root)["stage"], "train")
        result = runs.wait(client, self.root, "train", 10)
        self.assertFalse(result["running"])
        env = result["envelope"]
        self.assertTrue(env.ok)
        self.assertEqual(env.data["argv"][:4], ["--json", "project", "run", str(self.root)])
        record = json.loads((self.root / "agent" / "runs" / "train.json").read_text())
        self.assertEqual(record["pid"], info["pid"])
        self.assertEqual((self.root / "agent" / "runs" / "train.rc").read_text(), "0")
        for _ in range(100):
            if runs.running(self.root) is None:
                break
            time.sleep(0.05)
        self.assertIsNone(runs.running(self.root))
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_wait_returns_running_within_seconds(self):
        client = self.client(3)
        info = runs.start(client, self.root, "train", None)
        t0 = time.monotonic()
        result = runs.wait(client, self.root, "train", 0.5)
        self.assertLess(time.monotonic() - t0, 1.5)
        self.assertEqual(result, {"running": True, "stage": "train", "pid": info["pid"]})
        later = runs.wait(client, self.root, "train", 15)
        self.assertFalse(later["running"])
        self.assertTrue(later["envelope"].ok)

    def test_second_start_is_busy(self):
        client = self.client(2)
        info = runs.start(client, self.root, "train", None)
        with self.assertRaises(runs.RunBusy) as ctx:
            runs.start(client, self.root, "evaluate_sealed", None)
        self.assertIn("train", str(ctx.exception))
        self.assertIn(str(info["pid"]), str(ctx.exception))
        runs.wait(client, self.root, "train", 15)

    def test_stale_lock_replaced(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        agent = self.root / "agent"
        agent.mkdir()
        (agent / "run.lock").write_text(json.dumps({"stage": "old", "pid": dead.pid}))
        self.assertIsNone(runs.running(self.root))
        client = self.client(0.1)
        info = runs.start(client, self.root, "train", None)
        self.assertEqual(json.loads((agent / "run.lock").read_text())["pid"], info["pid"])
        self.assertTrue(runs.wait(client, self.root, "train", 10)["envelope"].ok)

    def test_concurrent_takeover_of_a_stale_lock_has_one_winner(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        agent = self.root / "agent"
        agent.mkdir()
        (agent / "run.lock").write_text(json.dumps({"stage": "old", "pid": dead.pid}))
        # The interleaving that breaks an unserialised takeover: both starters judge the lock stale, then s0 removes
        # it and creates its own, then s1 acts on its old verdict (removing s0's new lock) and creates too. Hold
        # s1 back after its verdict until s0 is done; when takeover is serialised the waits just time out.
        barrier = threading.Barrier(2)
        s0_done = threading.Event()
        real = runs._lock_state

        def state(root):
            verdict = real(root)
            if verdict[0] == "stale":
                try:
                    barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError:
                    pass
                if threading.current_thread().name == "s1":
                    s0_done.wait(timeout=2.0)
            return verdict

        results = []

        def acquire(stage):
            try:
                runs._acquire(self.root, stage)
                results.append("won")
            except runs.RunBusy:
                results.append("busy")
            finally:
                if stage == "s0":
                    s0_done.set()

        with mock.patch.object(runs, "_lock_state", state):
            threads = [threading.Thread(target=acquire, args=(f"s{i}",), name=f"s{i}") for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        self.assertEqual(sorted(results), ["busy", "won"])

    def test_start_kills_and_reaps_the_child_when_a_write_fails(self):
        client = self.client(30)
        # The lock write right after Popen fails.
        self.enterContext(mock.patch.object(runs, "_write_lock", side_effect=OSError("disk full")))
        spawned = []
        real = subprocess.Popen

        def spy(*args, **kwargs):
            proc = real(*args, **kwargs)
            spawned.append(proc)
            return proc

        with mock.patch.object(runs.subprocess, "Popen", spy):
            with self.assertRaises(OSError):
                runs.start(client, self.root, "train", None)
        self.assertEqual(len(spawned), 1)
        # wait() already ran: the child is reaped (a return code is known), not a zombie or still running.
        self.assertIsNotNone(spawned[0].returncode)
        self.assertFalse(runs._pid_alive(spawned[0].pid))
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_a_live_child_keeps_the_lock_live_after_its_owner_died(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with runs.hold(self.root, "train") as held:
            held.child(child.pid)
            content = json.loads((self.root / "agent" / "run.lock").read_text())
            self.assertEqual(content["child"]["pid"], child.pid)
            self.assertEqual(content["child"]["start"], runs._start_time(child.pid))
            # The owner dies without cleanup: its pid is gone but the child still runs.
            content["pid"] = dead.pid
            (self.root / "agent" / "run.lock").write_text(json.dumps(content))
            self.assertEqual(runs.running(self.root)["stage"], "train")
            with self.assertRaises(runs.RunBusy):
                runs.start(self.client(0), self.root, "other", None)
            child.kill()
            child.wait()
            self.assertIsNone(runs.running(self.root))
            # Restore ownership so hold's own cleanup removes the lock.
            content["pid"] = os.getpid()
            (self.root / "agent" / "run.lock").write_text(json.dumps(content))

    def test_start_and_wait_handle_large_output(self):
        body = 'for _ in range(60000):\n    print("n" * 99)\n' + BODY.format(sleep=0)
        client = Client(write_fake_cli(self.dir, body))
        runs.start(client, self.root, "train", None)
        result = runs.wait(client, self.root, "train", 30)
        self.assertTrue(result["envelope"].ok)
        self.assertGreater((self.root / "agent" / "runs" / "train.out").stat().st_size, 5_000_000)

    def test_a_zombie_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        deadline = time.monotonic() + 10
        while runs._stat_fields(child.pid)[0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(runs._pid_alive(child.pid))
        self.assertFalse(runs._pid_alive(child.pid, runs._start_time(child.pid)))

    def test_a_superseded_run_leaves_no_return_code(self):
        client = self.client(0.5)
        info = runs.start(client, self.root, "train", None)
        record = self.root / "agent" / "runs" / "train.json"
        record.write_text(json.dumps({"pid": info["pid"] + 1}))  # a newer run replaced this one's record
        for t in threading.enumerate():
            if t.name.startswith("reap-"):
                t.join(timeout=10)
        self.assertFalse((self.root / "agent" / "runs" / "train.rc").exists())
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_a_failed_start_does_not_remove_a_lock_another_starter_took_over(self):
        client = self.client(30)
        other = {"stage": "other", "pid": os.getpid(), "start": runs._start_time(os.getpid())}

        def write(lock, content):
            # Our child is now dead, so a concurrent starter judged the lock stale and took it over...
            lock.write_text(json.dumps(other))
            raise OSError("then our own write failed")

        self.enterContext(mock.patch.object(runs, "_write_lock", write))
        with self.assertRaises(OSError):
            runs.start(client, self.root, "train", None)
        # ... and our failure path must leave that lock alone.
        self.assertEqual(json.loads((self.root / "agent" / "run.lock").read_text()), other)

    def test_hold_clears_the_previous_run_under_the_guard(self):
        runs_dir = self.root / "agent" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "train.json").write_text("{}")
        (runs_dir / "train.rc").write_text("0")
        with runs.hold(self.root, "train"):
            self.assertFalse((runs_dir / "train.json").exists())
            self.assertFalse((runs_dir / "train.rc").exists())

    def test_decision_file_written_and_passed(self):
        client = self.client(0.1)
        runs.start(client, self.root, "preregister", {"metric_confirmed": True})
        path = self.root / "agent" / "decisions" / "preregister.json"
        self.assertEqual(json.loads(path.read_text()), {"metric_confirmed": True})
        env = runs.wait(client, self.root, "preregister", 10)["envelope"]
        argv = env.data["argv"]
        self.assertEqual(argv[argv.index("--decision") + 1], str(path))

    def test_wait_unknown_stage(self):
        with self.assertRaises(FileNotFoundError):
            runs.wait(self.client(0), self.root, "nope", 0.1)

    def test_dead_pid_without_rc_gives_exit_minus_one(self):
        dead = subprocess.Popen([sys.executable, "-c", "print('boom')"], stdout=subprocess.DEVNULL)
        dead.wait()
        runs_dir = self.root / "agent" / "runs"
        runs_dir.mkdir(parents=True)
        out = runs_dir / "train.out"
        out.write_text("Traceback...\n")
        (runs_dir / "train.json").write_text(json.dumps({"pid": dead.pid, "argv": ["x", "--json", "project", "run"],
                                                         "out": str(out)}))
        old = runs.RC_GRACE_SECONDS
        runs.RC_GRACE_SECONDS = 0.1
        self.addCleanup(setattr, runs, "RC_GRACE_SECONDS", old)
        env = runs.wait(self.client(0), self.root, "train", 5)["envelope"]
        self.assertFalse(env.ok)
        self.assertEqual(env.exit_code, -1)

    def _write_record(self, pid, out_text):
        runs_dir = self.root / "agent" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        out = runs_dir / "train.out"
        out.write_text(out_text)
        (runs_dir / "train.json").write_text(json.dumps({"pid": pid, "argv": ["x", "--json", "project", "run"],
                                                         "out": str(out)}))

    def test_no_rc_but_valid_envelope_keeps_its_exit(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        envelope = json.dumps({"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "project run",
                               "ok": True, "exit": 0, "data": {}, "problems": [], "warnings": []})
        self._write_record(dead.pid, "noise\n" + envelope + "\n")
        old = runs.RC_GRACE_SECONDS
        runs.RC_GRACE_SECONDS = 0.1
        self.addCleanup(setattr, runs, "RC_GRACE_SECONDS", old)
        env = runs.wait(self.client(0), self.root, "train", 5)["envelope"]
        self.assertTrue(env.ok)
        self.assertEqual(env.exit_code, 0)

    def test_pid_reuse_with_wrong_start_time_is_stale(self):
        agent = self.root / "agent"
        agent.mkdir()
        real = runs._start_time(os.getpid())
        if real is None:
            self.skipTest("no /proc")
        (agent / "run.lock").write_text(json.dumps({"stage": "old", "pid": os.getpid(), "start": real + "1"}))
        self.assertIsNone(runs.running(self.root))
        (agent / "run.lock").write_text(json.dumps({"stage": "old", "pid": os.getpid(), "start": real}))
        self.assertEqual(runs.running(self.root)["stage"], "old")
        (agent / "run.lock").write_text(json.dumps({"stage": "old", "pid": os.getpid(), "start": real + "1"}))
        client = self.client(0.1)
        runs.start(client, self.root, "train", None)
        self.assertTrue(runs.wait(client, self.root, "train", 10)["envelope"].ok)

    def test_invalid_stage_names(self):
        client = self.client(0)
        for bad in ["", "..", "../x", "a/b", ".hidden", "a..b", "-x"]:
            with self.assertRaises(ValueError):
                runs.start(client, self.root, bad, None)
            with self.assertRaises(ValueError):
                runs.wait(client, self.root, bad, 0)
        self.assertFalse((self.root / "agent").exists())

    def test_corrupt_record_is_a_clear_error(self):
        runs_dir = self.root / "agent" / "runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "train.json").write_text(json.dumps({"started_utc": "x"}))
        with self.assertRaises(ValueError) as ctx:
            runs.wait(self.client(0), self.root, "train", 0)
        self.assertIn("corrupt", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
