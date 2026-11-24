"""`gatedtrain-agent do` end to end on the synthetic pack (needs GATEDTRAIN_SRC)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from _support import e2e_prefix, gatedtrain_src

from gatedtrain_agent.client import Client
from gatedtrain_agent.walker import EXIT_NEEDS_PERSON, walk

SRC = gatedtrain_src()


@unittest.skipUnless(SRC, "set GATEDTRAIN_SRC to a gatedtrain v0.5.0 checkout")
class DoE2ETest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.prefix = e2e_prefix()

    def pipeline(self, noise: float) -> Path:
        from gatedtrain.testing import synthetic_pack as pack
        raw = yaml.safe_load(pack.SCENARIO.pipeline.read_text())
        next(s for s in raw["stages"] if s["name"] == "write_recipe")["params"]["noise"] = noise
        path = self.tmp / "pipe.yaml"
        path.write_text(yaml.safe_dump(raw))
        return path

    def do(self, root: Path, noise: float) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["GATEDTRAIN_AGENT_CLI"] = json.dumps(self.prefix)
        return subprocess.run(
            [sys.executable, "-m", "gatedtrain_agent.cli", "do", str(root), "--goal", "lookup task",
             "--data", str(SRC / "tests" / "fixtures" / "ainative" / "toy.jsonl"),
             "--pipeline", str(self.pipeline(noise))],
            cwd=self.tmp, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)

    def test_no_tty_stops_at_preregister_then_yes_passes(self) -> None:
        root = self.tmp / "proj"
        proc = self.do(root, 0.0)
        self.assertEqual(proc.returncode, EXIT_NEEDS_PERSON, proc.stdout + proc.stderr)
        self.assertIn("preregister: needs a person", proc.stdout)
        client = Client(self.prefix)
        st = client.call(["project", "status", str(root)])
        self.assertNotEqual(st.data["stages"]["preregister"]["status"], "done")
        out = io.StringIO()
        self.assertEqual(walk(client, root, ask=lambda _p: "yes", out=out), 0, out.getvalue())
        self.assertTrue((root / "stages" / "report" / "report.md").is_file())
        st = client.call(["project", "status", str(root)])
        self.assertEqual({v["status"] for v in st.data["stages"].values()}, {"done"})
        self.assertEqual(st.data["gate"], "PASS")

    def test_gate_not_pass_exits_1_with_a_report(self) -> None:
        root = self.tmp / "proj"
        self.assertEqual(self.do(root, 0.6).returncode, EXIT_NEEDS_PERSON)
        out = io.StringIO()
        self.assertEqual(walk(Client(self.prefix), root, ask=lambda _p: "yes", out=out), 1, out.getvalue())
        self.assertTrue((root / "stages" / "report" / "report.md").is_file())
        self.assertIn("report.md", out.getvalue())
        self.assertNotIn("gate: PASS", out.getvalue())

    def test_degenerate_recipe_is_refused_exit_3(self) -> None:
        # noise 1.0 never reaches the gate: gatedtrain refuses at smoke (MM-38)
        root = self.tmp / "proj"
        self.assertEqual(self.do(root, 1.0).returncode, EXIT_NEEDS_PERSON)
        out = io.StringIO()
        self.assertEqual(walk(Client(self.prefix), root, ask=lambda _p: "yes", out=out), 3, out.getvalue())
        self.assertIn("smoke: refused", out.getvalue())


if __name__ == "__main__":
    unittest.main()
