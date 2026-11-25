"""walk() against a scripted fake CLI: responses per `project <sub> [<root>] [<stage>]` from a JSON table."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from _support import write_fake_cli

from gatedtrain_agent import runs
from gatedtrain_agent.client import Client
from gatedtrain_agent.walker import EXIT_NEEDS_PERSON, walk

FAKE = """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    table = json.load(open(os.path.join(here, "table.json")))
    i = argv.index("project")
    sub = argv[i + 1]
    stage = argv[i + 3] if len(argv) > i + 3 and not argv[i + 3].startswith("--") else None
    rec = {"sub": sub, "stage": stage, "argv": argv}
    if "--decision" in argv:
        rec["decision"] = json.load(open(argv[argv.index("--decision") + 1]))
    open(os.path.join(here, "calls.jsonl"), "a").write(json.dumps(rec) + "\\n")
    r = table.get(sub + ":" + str(stage)) or table.get(sub) or {}
    print(json.dumps({"schema": "gatedtrain.cli_result", "schema_version": 1, "command": "project " + sub,
                      "ok": r.get("exit", 0) == 0, "exit": r.get("exit", 0), "data": r.get("data", {}),
                      "problems": r.get("problems", []), "warnings": []}))
    sys.exit(r.get("exit", 0))
"""
NAMES = ["a", "preregister", "gate", "report"]


def status(done=()):
    return {"data": {"stages": {n: {"status": "done" if n in done else "pending"} for n in NAMES}}}


class WalkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "proj"
        self.root.mkdir()
        self.client = Client(write_fake_cli(self.tmp, FAKE))

    def walk(self, table: dict, *, ask=None, accept=False, keeper=False) -> tuple[int, str]:
        (self.tmp / "table.json").write_text(json.dumps(table))
        return self.walk_raw(ask=ask, accept=accept, keeper=keeper)

    def walk_raw(self, *, ask=None, accept=False, keeper=False) -> tuple[int, str]:
        if keeper:
            (self.root / "trust").mkdir(exist_ok=True)
            (self.root / "trust" / "keeper.json").write_text("{}")
        out = io.StringIO()
        code = walk(self.client, self.root, accept_inconclusive=accept, ask=ask, out=out)
        return code, out.getvalue()

    def calls(self) -> list[dict]:
        path = self.tmp / "calls.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def runs(self) -> list[str]:
        return [c["stage"] for c in self.calls() if c["sub"] == "run"]

    def test_runs_stages_in_status_order_skipping_done(self) -> None:
        code, out = self.walk({"status": status(done=["a"]),
                               "report": {"data": {"report": {"status": "PASS"}, "report_md": "/r/report.md"}}})
        self.assertEqual(code, 0)
        self.assertEqual(self.runs(), ["preregister", "gate", "report"])
        self.assertIn("preregister: done", out)
        self.assertIn("/r/report.md", out)

    def test_pipeline_yaml_gives_the_order(self) -> None:
        (self.root / "pipeline.yaml").write_text("stages:\n- name: report\n- name: a\n- name: gate\n")
        st = status()
        st["data"]["stages"] = {n: st["data"]["stages"][n] for n in ["a", "gate", "preregister", "report"]}
        self.walk({"status": st, "report": {"data": {"report": {"status": "PASS"}}}})
        self.assertEqual(self.runs(), ["report", "a", "gate", "preregister"])

    def test_data_too_small_stops_exit_4(self) -> None:
        code, out = self.walk({"status": status(), "plan:a": {"data": {"needs_user": "data_too_small",
                                                                       "summary": "too few"}}})
        self.assertEqual(code, EXIT_NEEDS_PERSON)
        self.assertIn("--accept-inconclusive", out)
        self.assertEqual(self.runs(), [])

    def test_accept_inconclusive_flag_passes_decision(self) -> None:
        code, _ = self.walk({"status": status(), "plan:a": {"data": {"needs_user": "data_too_small"}},
                             "report": {"data": {"report": {"status": "INCONCLUSIVE"}}, "exit": 1}}, accept=True)
        first = next(c for c in self.calls() if c["sub"] == "run")
        self.assertEqual(first["decision"], {"accept_inconclusive": True})
        self.assertEqual(code, 1)

    def test_confirm_metric_no_tty_exit_4_never_runs_preregister(self) -> None:
        code, out = self.walk({"status": status(done=["a"]),
                               "plan:preregister": {"data": {"needs_user": "confirm_metric", "summary": "S"}}})
        self.assertEqual(code, EXIT_NEEDS_PERSON)
        self.assertNotIn("preregister", self.runs())
        self.assertIn("terminal", out)
        self.assertFalse((self.root / "agent" / "decisions" / "preregister.json").exists())

    def test_confirm_metric_tty_yes_runs_with_flag(self) -> None:
        prompts = []
        code, out = self.walk({"status": status(done=["a"]),
                               "plan:preregister": {"data": {"needs_user": "confirm_metric", "summary": "SENTENCE"}},
                               "report": {"data": {"report": {"status": "PASS"}}}},
                              ask=lambda p: prompts.append(p) or " yes ")
        self.assertEqual(code, 0)
        self.assertIn("SENTENCE", out)
        run = next(c for c in self.calls() if c["sub"] == "run" and c["stage"] == "preregister")
        self.assertEqual(run["decision"], {"metric_confirmed": True})
        self.assertEqual(len(prompts), 1)

    def test_confirm_metric_tty_other_answer_exit_4(self) -> None:
        for answer in ("y", "no", "YES", ""):
            with self.subTest(answer=answer):
                (self.tmp / "calls.jsonl").unlink(missing_ok=True)
                code, _ = self.walk({"status": status(done=["a"]),
                                     "plan:preregister": {"data": {"needs_user": "confirm_metric"}}},
                                    ask=lambda _p, a=answer: a)
                self.assertEqual(code, EXIT_NEEDS_PERSON)
                self.assertEqual(self.runs(), [])

    def test_keeper_project_refusal_prints_keeper_confirm_command(self) -> None:
        code, out = self.walk({"status": status(done=["a"]),
                               "plan:preregister": {"data": {"needs_user": "confirm_metric", "summary": "SENTENCE"}},
                               "run:preregister": {"exit": 3, "problems": [
                                   {"rule": "AN-18", "field": "f", "message": "not confirmed", "fix": "confirm"}]}},
                              keeper=True)
        dec = self.root / "agent" / "decisions" / "preregister.json"
        self.assertEqual(code, EXIT_NEEDS_PERSON)
        self.assertEqual(json.loads(dec.read_text()), {"metric_confirmed": True})
        self.assertIn("SENTENCE", out)
        self.assertIn("not confirmed", out)
        self.assertIn(f"gatedtrain keeper confirm {self.root} --decision {dec}", out)

    def test_keeper_project_other_refusal_is_exit_3_not_confirm(self) -> None:
        for rule in ("AN-17", "BUDGET"):
            with self.subTest(rule=rule):
                code, out = self.walk({"status": status(done=["a"]),
                                       "plan:preregister": {"data": {"needs_user": "confirm_metric"}},
                                       "run:preregister": {"exit": 3, "problems": [
                                           {"rule": rule, "field": "f", "message": "nope", "fix": "x"}]}},
                                      keeper=True)
                self.assertEqual(code, 3)
                self.assertIn(f"{rule} f: nope", out)
                self.assertNotIn("keeper confirm", out)

    def test_unknown_needs_user_stops_exit_4_runs_nothing(self) -> None:
        code, out = self.walk({"status": status(), "plan:a": {"data": {"needs_user": "pack_defined",
                                                                       "summary": "SUM"}}})
        self.assertEqual(code, EXIT_NEEDS_PERSON)
        self.assertIn("SUM", out)
        self.assertIn("needs a person (pack_defined)", out)
        self.assertEqual(self.runs(), [])

    def test_ask_eof_or_interrupt_exit_4(self) -> None:
        for exc in (EOFError, KeyboardInterrupt):
            with self.subTest(exc=exc.__name__):
                def ask(_p, exc=exc):
                    raise exc()
                code, out = self.walk({"status": status(done=["a"]),
                                       "plan:preregister": {"data": {"needs_user": "confirm_metric"}}}, ask=ask)
                self.assertEqual(code, EXIT_NEEDS_PERSON)
                self.assertIn("nothing was confirmed", out)
                self.assertEqual(self.runs(), [])

    def test_crashed_report_is_refused_exit_3(self) -> None:
        crash = {"exit": 2, "problems": [{"rule": "AGENT", "field": "gatedtrain", "message": "boom", "fix": "f"}]}
        (self.tmp / "table.json").write_text(json.dumps({"status": status(done=NAMES), "report": crash}))
        code, out = self.walk_raw()
        self.assertEqual(code, 3)
        self.assertIn("report: refused", out)
        self.assertIn("AGENT gatedtrain: boom", out)

    def test_non_report_envelope_is_refused(self) -> None:
        # exit 1 with an AGENT problem (what Client builds for a non-JSON result) must not read as "not PASS"
        crash = {"exit": 1, "problems": [{"rule": "AGENT", "field": "gatedtrain", "message": "x", "fix": "f"}]}
        code, out = self.walk({"status": status(done=NAMES), "report": crash})
        self.assertEqual(code, 3)
        self.assertIn("report: refused", out)

    def test_held_lock_runs_nothing_exit_3(self) -> None:
        with runs.hold(self.root, "other"):
            code, out = self.walk({"status": status()})
        self.assertEqual(code, 3)
        self.assertIn("other", out)
        self.assertEqual(self.runs(), [])

    def test_busy_lock_writes_no_decision_file(self) -> None:
        with runs.hold(self.root, "other"):
            code, _ = self.walk({"status": status(done=["a"]),
                                 "plan:preregister": {"data": {"needs_user": "confirm_metric", "summary": "s"}}},
                                ask=lambda _p: "yes")
        self.assertEqual(code, 3)
        self.assertFalse((self.root / "agent" / "decisions" / "preregister.json").exists())
        self.assertEqual(self.runs(), [])

    def test_lock_error_is_exit_3_not_traceback(self) -> None:
        (self.root / "agent").write_text("not a directory")
        code, out = self.walk({"status": status()})
        self.assertEqual(code, 3)
        self.assertIn("a: refused", out)
        self.assertEqual(self.runs(), [])

    def test_lock_release_error_after_the_run_reports_the_run(self) -> None:
        real = self.client.call

        def call(args, **kw):
            env = real(args, **kw)
            if args[:2] == ["project", "run"]:
                lock = self.root / "agent" / "run.lock"
                lock.unlink()
                lock.mkdir()  # unlink in hold()'s finally now fails
            return env
        self.client.call = call
        code, out = self.walk({"status": status()})
        self.assertEqual(code, 3)
        self.assertIn("a: ran (exit 0)", out)
        self.assertIn("could not release", out)

    def test_walk_holds_lock_while_running_and_releases(self) -> None:
        seen = []
        table = {"status": status(done=["a", "gate", "report"]), "report": {"data": {"report": {"status": "PASS"}}}}
        real = self.client.call

        def spy(args, **kw):
            if args[:2] == ["project", "run"]:
                try:
                    runs.start(self.client, self.root, "x", None)
                except runs.RunBusy as exc:
                    seen.append(str(exc))
            return real(args, **kw)
        self.client.call = spy
        code, _ = self.walk(table, ask=lambda _p: "yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn("preregister", seen[0])
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_ctrl_c_during_a_run_reports_the_stage_and_releases_the_lock(self) -> None:
        def call(args, **kw):
            if args[:2] == ["project", "run"]:
                raise KeyboardInterrupt
            return real(args, **kw)
        real = self.client.call
        self.client.call = call
        code, out = self.walk({"status": status()})
        self.assertEqual(code, 130)
        self.assertIn("a: interrupted", out)
        self.assertFalse((self.root / "agent" / "run.lock").exists())

    def test_refusal_exit_3_with_problem_lines(self) -> None:
        code, out = self.walk({"status": status(), "run:a": {"exit": 3, "problems": [
            {"rule": "BUDGET", "field": "gpu", "message": "over budget", "fix": "ask"}]}})
        self.assertEqual(code, 3)
        self.assertIn("a: refused", out)
        self.assertIn("BUDGET gpu: over budget", out)
        self.assertEqual(self.runs(), ["a"])

    def test_gate_fail_runs_report_exit_1(self) -> None:
        code, out = self.walk({"status": status(done=["a", "preregister"]),
                               "run:gate": {"exit": 1, "data": {"stage": "gate", "status": "FAIL"}},
                               "report": {"exit": 1, "data": {"report": {"status": "FAIL"}, "report_md": "/r.md"}}})
        self.assertEqual(code, 1)
        self.assertIn("/r.md", out)
        self.assertIn("FAIL", out)
        self.assertEqual([c["sub"] for c in self.calls()][-1], "report")


if __name__ == "__main__":
    unittest.main()
