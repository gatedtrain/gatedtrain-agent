"""MCP tool functions against a fake CLI."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
import threading
import unittest
from pathlib import Path

from _support import write_fake_cli

from gatedtrain_agent import runs
from gatedtrain_agent.client import Client
from gatedtrain_agent.mcp_server import TOOL_NAMES, Tools, build_server

FAKE = """
    sub = argv[argv.index("project") + 1]
    if sub == "run":
        time.sleep(float(__import__("os").environ.get("FAKE_SLEEP", "0")))
    print(json.dumps({"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "project " + sub,
                      "ok": True, "exit": 0, "data": {"argv": argv}, "problems": [], "warnings": []}))
"""


class McpToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.root = self.dir / "proj"
        self.root.mkdir()
        (self.root / "project.yaml").write_text("goal: x\n")
        self.client = Client(write_fake_cli(self.dir, FAKE))
        self.tools = Tools(self.client)
        self.addCleanup(self._drain)

    def _drain(self) -> None:
        live = runs.running(self.root)
        if live:
            try:
                os.killpg(live["pid"], signal.SIGKILL)
            except OSError:
                pass
        for t in threading.enumerate():
            if t.name.startswith("reap-"):
                t.join(timeout=10)

    def test_relative_root_refused(self) -> None:
        out = self.tools.project_status("relative/proj")
        self.assertFalse(out["ok"])
        self.assertIn("absolute", out["error"])
        out = self.tools.job_list("proj")
        self.assertFalse(out["ok"])

    def test_missing_root_refused_but_create_allowed(self) -> None:
        missing = str(self.dir / "nope")
        out = self.tools.project_status(missing)
        self.assertFalse(out["ok"])
        self.assertIn("does not exist", out["error"])
        out = self.tools.project_create(missing, "g", str(self.dir / "d.jsonl"), str(self.dir / "p.yaml"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["command"], "project create")
        self.assertEqual(set(out), {"ok", "exit", "command", "data", "problems", "warnings"})
        out = self.tools.project_create("rel", "g", str(self.dir / "d"), str(self.dir / "p"))
        self.assertFalse(out["ok"])
        out = self.tools.project_create(missing, "g", "d.jsonl", str(self.dir / "p"))
        self.assertIn("absolute", out["error"])

    def test_not_a_project_refused_and_untouched(self) -> None:
        plain = self.dir / "plain"
        plain.mkdir()
        out = self.tools.stage_run(str(plain), "train", None)
        self.assertFalse(out["ok"])
        self.assertIn("not a gatedtrain project", out["error"])
        self.assertEqual(list(plain.iterdir()), [])

    def test_stage_and_job_id_checked(self) -> None:
        r = str(self.root)
        self.assertFalse(self.tools.stage_plan(r, "--help")["ok"])
        self.assertFalse(self.tools.stage_plan(r, "../x")["ok"])
        self.assertFalse(self.tools.job_logs(r, "--tail")["ok"])
        self.assertFalse(self.tools.job_cancel(r, "")["ok"])
        self.assertTrue(self.tools.job_cancel(r, "j1")["ok"])

    def test_start_oserror_reported(self) -> None:
        tools = Tools(Client([str(self.dir / "no-such-cli")]))
        out = tools.stage_run(str(self.root), "train", None, wait_seconds=0)
        self.assertFalse(out["ok"])
        self.assertIn("cannot start stage", out["error"])
        self.assertIsNone(runs.running(self.root))

    def test_create_passes_optional_args(self) -> None:
        out = self.tools.project_create(str(self.dir / "n"), "g", str(self.dir / "d"), str(self.dir / "p"),
                                        keeper="/run/k.sock", gpu_hours=3)
        argv = out["data"]["argv"]
        self.assertEqual(argv[argv.index("--keeper") + 1], "/run/k.sock")
        self.assertEqual(argv[argv.index("--gpu-hours") + 1], "3")

    def test_read_tools(self) -> None:
        r = str(self.root)
        self.assertEqual(self.tools.project_status(r)["command"], "project status")
        self.assertEqual(self.tools.stage_plan(r, "train")["data"]["argv"][-1], "train")
        self.assertEqual(self.tools.project_report(r)["command"], "project report")
        self.assertEqual(self.tools.job_list(r)["command"], "project job")
        argv = self.tools.job_logs(r, "j1", tail=5)["data"]["argv"]
        self.assertEqual(argv[-4:], [r, "j1", "--tail", "5"][-4:])
        self.assertEqual(self.tools.job_cancel(r, "j1")["data"]["argv"][-3:], ["cancel", r, "j1"])

    def test_metric_confirmed_refused_without_keeper(self) -> None:
        out = self.tools.stage_run(str(self.root), "preregister", {"metric_confirmed": True})
        self.assertFalse(out["ok"])
        self.assertIn("metric confirmation without a keeper is the person's", out["error"])
        self.assertIn("gatedtrain-agent do", out["error"])
        self.assertFalse((self.root / "agent").exists())

    def test_metric_confirmed_allowed_with_keeper(self) -> None:
        (self.root / "trust").mkdir()
        (self.root / "trust" / "keeper.json").write_text("{}")
        out = self.tools.stage_run(str(self.root), "preregister", {"metric_confirmed": True}, wait_seconds=10)
        self.assertFalse(out["running"])
        self.assertTrue(out["ok"])
        dec = json.loads((self.root / "agent" / "decisions" / "preregister.json").read_text())
        self.assertEqual(dec, {"metric_confirmed": True})

    def test_stage_run_returns_running_then_wait_finishes(self) -> None:
        os.environ["FAKE_SLEEP"] = "1.5"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        out = self.tools.stage_run(str(self.root), "train", None, wait_seconds=0)
        self.assertEqual(out["running"], True)
        self.assertEqual(out["stage"], "train")
        out = self.tools.stage_wait(str(self.root), "train", wait_seconds=0)
        self.assertTrue(out["running"])
        out = self.tools.stage_wait(str(self.root), "train", wait_seconds=30)
        self.assertFalse(out["running"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["command"], "project run")

    def test_wait_seconds_clamped(self) -> None:
        os.environ["FAKE_SLEEP"] = "1.5"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        out = self.tools.stage_run(str(self.root), "train", None, wait_seconds=-5)
        self.assertTrue(out["running"])
        self.assertEqual(self.tools._clamp(10_000), 600)
        self.assertEqual(self.tools._clamp(-1), 0)

    def test_busy_project_refused(self) -> None:
        os.environ["FAKE_SLEEP"] = "1.5"
        self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        self.tools.stage_run(str(self.root), "train", None, wait_seconds=0)
        out = self.tools.stage_run(str(self.root), "gate", None, wait_seconds=0)
        self.assertFalse(out["ok"])
        self.assertIn("train", out["error"])
        self.tools.stage_wait(str(self.root), "train", wait_seconds=30)

    def test_wait_never_started_and_bad_stage(self) -> None:
        out = self.tools.stage_wait(str(self.root), "train", wait_seconds=0)
        self.assertFalse(out["ok"])
        self.assertIn("never started", out["error"])
        out = self.tools.stage_run(str(self.root), "../x", None)
        self.assertFalse(out["ok"])

    def test_decision_must_be_object(self) -> None:
        out = self.tools.stage_run(str(self.root), "train", ["x"])  # type: ignore[arg-type]
        self.assertFalse(out["ok"])

    def test_tools_list_exact(self) -> None:
        server = build_server(self.client)
        names = {t.name for t in asyncio.run(server.list_tools())}
        self.assertEqual(names, set(TOOL_NAMES))
        self.assertEqual(len(names), 9)
        self.assertEqual(names, {"project_create", "project_status", "stage_plan", "stage_run", "stage_wait",
                                 "project_report", "job_list", "job_logs", "job_cancel"})

    def test_skill_path_exists_and_names_tools(self) -> None:
        from gatedtrain_agent import cli
        path = Path(cli.skill_path())
        self.assertTrue(path.is_absolute() and path.is_file())
        text = path.read_text()
        self.assertTrue(text.startswith("---\nname: gatedtrain-operator\ndescription: "))
        self.assertLessEqual(len(text.splitlines()), 120)
        for name in TOOL_NAMES:
            self.assertIn(name, text)
        for line in text.splitlines():
            if "keeper confirm" in line:
                self.assertRegex(line.lower(), r"user|person|themselves|never")
        self.assertIn("claude mcp add gatedtrain -- gatedtrain-agent mcp", text)
        self.assertIn("~/.claude/skills/gatedtrain-operator/", text)
        self.assertIn("agent/decisions/<stage>.json", text)
        self.assertNotIn("preregister.json", text)
        self.assertNotIn("keeper.json", text)
        self.assertIn("AN-18", text)
        self.assertIn("keeper serve", text)
        self.assertIsNone(re.search(r"run `?gatedtrain keeper confirm", text))


if __name__ == "__main__":
    unittest.main()
