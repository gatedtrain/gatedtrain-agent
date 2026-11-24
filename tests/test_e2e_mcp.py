"""The MCP server over stdio on the synthetic pack (needs GATEDTRAIN_SRC)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from mcp.client import Client as McpClient
from mcp.client.stdio import StdioServerParameters

from _support import e2e_prefix, gatedtrain_src

from gatedtrain_agent.client import Client

SRC = gatedtrain_src()


@unittest.skipUnless(SRC, "set GATEDTRAIN_SRC to a gatedtrain v0.5.0 checkout")
class McpE2ETest(unittest.TestCase):
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

    def test_drive_a_project_to_pass(self) -> None:
        asyncio.run(asyncio.wait_for(self._drive(), 900))

    async def _drive(self) -> None:
        root = self.tmp / "proj"
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["GATEDTRAIN_AGENT_CLI"] = json.dumps(self.prefix)
        params = StdioServerParameters(command=sys.executable, args=["-m", "gatedtrain_agent.cli", "mcp"],
                                       env=env, cwd=str(self.tmp))
        async with McpClient(params) as mcp:
            listed = {t.name for t in (await mcp.list_tools()).tools}
            self.assertEqual(len(listed), 9)

            async def call(name: str, **args) -> dict:
                res = await mcp.call_tool(name, args)
                self.assertFalse(res.is_error, res)
                return res.structured_content

            out = await call("project_create", root=str(root), goal="lookup task",
                             data=str(SRC / "tests" / "fixtures" / "ainative" / "toy.jsonl"),
                             pipeline=str(self.pipeline(0.0)))
            self.assertTrue(out["ok"], out)
            out = await call("project_status", root="relative")
            self.assertFalse(out["ok"])
            stages = [s["name"] for s in yaml.safe_load((root / "pipeline.yaml").read_text())["stages"]]
            for stage in stages:
                plan = await call("stage_plan", root=str(root), stage=stage)
                if stage == "preregister":
                    self.assertEqual(plan["data"].get("needs_user"), "confirm_metric", plan)
                    refused = await call("stage_run", root=str(root), stage=stage,
                                         decision={"metric_confirmed": True})
                    self.assertFalse(refused["ok"])
                    self.assertIn("person's", refused["error"])
                    # The person confirms at their terminal.
                    dec = self.tmp / "dec.json"
                    dec.write_text(json.dumps({"metric_confirmed": True}))
                    done = Client(self.prefix).call(["project", "run", str(root), stage, "--decision", str(dec)])
                    self.assertTrue(done.ok, done)
                    continue
                out = await call("stage_run", root=str(root), stage=stage, wait_seconds=5)
                for _ in range(200):
                    if not out.get("running"):
                        break
                    out = await call("stage_wait", root=str(root), stage=stage, wait_seconds=5)
                self.assertFalse(out.get("running"), out)
                self.assertTrue(out["ok"], out)
            rep = await call("project_report", root=str(root))
            self.assertTrue(rep["ok"], rep)
        st = Client(self.prefix).call(["project", "status", str(root)])
        self.assertEqual(st.data["gate"], "PASS")
        self.assertTrue((root / "stages" / "report" / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()
