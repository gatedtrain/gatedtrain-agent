import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from _support import write_fake_cli
from gatedtrain_agent import client as client_module
from gatedtrain_agent.client import Client, Envelope, cli_prefix

ENVELOPE = (
    'print(json.dumps({"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "run stage", '
    '"ok": False, "exit": 3, "data": {"argv": argv}, "problems": [{"rule": "R1", "field": "f", '
    '"message": "bad", "fix": "do x"}], "warnings": ["w"]}))\nsys.exit(3)\n'
)


class ClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_parses_envelope(self):
        client = Client(write_fake_cli(self.dir, ENVELOPE))
        env = client.call(["run", "stage"])
        self.assertIsInstance(env, Envelope)
        self.assertFalse(env.ok)
        self.assertEqual(env.exit_code, 3)
        self.assertEqual(env.command, "run stage")
        self.assertEqual(env.problems[0]["rule"], "R1")
        self.assertEqual(env.warnings, ["w"])
        self.assertEqual(env.problem_lines(), ["R1 f: bad (fix: do x)"])

    def test_adds_json_flag_first(self):
        client = Client(write_fake_cli(self.dir, ENVELOPE))
        env = client.call(["run", "stage"])
        self.assertEqual(env.data["argv"], ["--json", "run", "stage"])
        self.assertEqual(client.argv(["a"])[-2:], ["--json", "a"])

    def test_stderr_tail_in_error_message(self):
        client = Client(write_fake_cli(self.dir, 'sys.stderr.write("x" * 500 + "TRACEBACK-END")\nsys.exit(1)\n'))
        env = client.call(["run", "stage"])
        self.assertFalse(env.ok)
        message = env.problems[0]["message"]
        self.assertIn(" stderr tail: ", message)
        self.assertTrue(message.endswith("TRACEBACK-END"))
        self.assertLessEqual(len(message.split(" stderr tail: ")[1]), 200)

    def test_no_stderr_no_tail(self):
        client = Client(write_fake_cli(self.dir, "sys.exit(1)\n"))
        self.assertNotIn("stderr tail", client.call(["run"]).problems[0]["message"])

    def test_non_json_stdout(self):
        client = Client(write_fake_cli(self.dir, 'print("Traceback (most recent call last):")\nsys.exit(1)\n'))
        env = client.call(["run", "stage"])
        self.assertFalse(env.ok)
        self.assertEqual(env.exit_code, 1)
        self.assertEqual(env.command, "run stage")
        self.assertEqual(env.problems[0]["rule"], "AGENT")
        self.assertEqual(env.problems[0]["field"], "gatedtrain")
        self.assertIn("exit 1", env.problems[0]["message"])
        self.assertIn("Traceback", env.problems[0]["message"])

    def test_wrong_schema(self):
        client = Client(write_fake_cli(self.dir, 'print(json.dumps({"schema": "other"}))\n'))
        env = client.call(["x", "y"])
        self.assertFalse(env.ok)
        self.assertEqual(env.problems[0]["rule"], "AGENT")

    def test_timeout(self):
        client = Client(write_fake_cli(self.dir, "time.sleep(5)\n"), timeout=0.5)
        env = client.call(["x", "y"])
        self.assertFalse(env.ok)
        self.assertEqual(env.exit_code, 124)
        self.assertEqual(env.problems[0]["rule"], "AGENT")

    def test_large_stdout_and_stderr_do_not_block_or_get_lost(self):
        body = ('for _ in range(80000):\n    print("n" * 99)\n'
                'sys.stderr.write(("e" * 99 + "\\n") * 80000)\n' + ENVELOPE)
        env = Client(write_fake_cli(self.dir, body)).call(["run", "stage"])
        self.assertEqual(env.exit_code, 3)
        self.assertEqual(env.problems[0]["rule"], "R1")

    def test_large_output_without_an_envelope_keeps_only_a_tail(self):
        body = 'for _ in range(80000):\n    print("n" * 99)\nprint("END-MARK")\nsys.exit(1)\n'
        env = Client(write_fake_cli(self.dir, body)).call(["run", "stage"])
        self.assertEqual(env.problems[0]["rule"], "AGENT")
        self.assertLess(len(env.problems[0]["message"]), 1000)
        self.assertIn("END-MARK", env.problems[0]["message"])

    def test_on_start_receives_the_child_pid_before_it_finishes(self):
        seen = []
        client = Client(write_fake_cli(self.dir, "time.sleep(0.3)\n" + ENVELOPE))
        env = client.call(["run", "stage"], on_start=seen.append)
        self.assertEqual(env.exit_code, 3)
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], 0)

    def test_interrupt_stops_and_reaps_the_child(self):
        old = client_module.INTERRUPT_GRACE_SECONDS
        client_module.INTERRUPT_GRACE_SECONDS = 0.2
        self.addCleanup(setattr, client_module, "INTERRUPT_GRACE_SECONDS", old)
        pids = []
        client = Client(write_fake_cli(self.dir, "time.sleep(60)\n"))
        timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(KeyboardInterrupt):
            client.call(["run", "stage"], on_start=pids.append)
        self.assertEqual(len(pids), 1)
        # Reaped: no zombie left behind (a zombie still answers kill(pid, 0)).
        with self.assertRaises(ChildProcessError):
            os.waitpid(pids[0], os.WNOHANG)
        self.assertFalse(os.path.exists(f"/proc/{pids[0]}"))

    def test_timeout_does_not_wait_for_a_grandchild_holding_the_pipes(self):
        body = ('import subprocess\n'
                'subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])\n'
                'time.sleep(60)\n')
        t0 = time.monotonic()
        env = Client(write_fake_cli(self.dir, body), timeout=0.5).call(["x", "y"])
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(env.exit_code, 124)

    def test_a_failing_on_start_stops_and_reaps_the_child(self):
        pids = []

        def fail(pid):
            pids.append(pid)
            raise OSError("lock write failed")

        client = Client(write_fake_cli(self.dir, "time.sleep(60)\n"))
        with self.assertRaises(OSError):
            client.call(["run", "stage"], on_start=fail)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pids[0], os.WNOHANG)
        self.assertFalse(os.path.exists(f"/proc/{pids[0]}"))

    def test_prefix_env(self):
        with mock.patch.dict(os.environ, {"GATEDTRAIN_AGENT_CLI": '["a", "b"]'}):
            self.assertEqual(cli_prefix(), ["a", "b"])
        for bad in ("not json", "[1]"):
            with mock.patch.dict(os.environ, {"GATEDTRAIN_AGENT_CLI": bad}):
                with self.assertRaises(ValueError) as ctx:
                    cli_prefix()
                self.assertIn("GATEDTRAIN_AGENT_CLI", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
