# Copyright 2026 Willian Duque
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for run_eval: the stream judge, process handling and aggregation.

Run from the skill-creator directory:  python3 -m unittest scripts.test_run_eval -v
No test calls the real `claude`; process tests put a fake one first on PATH.
"""
import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from scripts import run_eval as re_mod
from scripts.run_eval import ERROR, FIRED, NOT_FIRED, StreamJudge

TARGET = "evalprobe:demo"


def init(*skills):
    return {"type": "system", "subtype": "init", "skills": list(skills), "slash_commands": []}


def start(name):
    return {"type": "stream_event", "event": {"type": "content_block_start",
            "content_block": {"type": "tool_use", "name": name, "input": {}}}}


def text_start():
    return {"type": "stream_event", "event": {"type": "content_block_start",
            "content_block": {"type": "text", "text": ""}}}


def delta(partial):
    return {"type": "stream_event", "event": {"type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": partial}}}


def stop():
    return {"type": "stream_event", "event": {"type": "content_block_stop"}}


def tool(name, inp):
    return [start(name), delta(json.dumps(inp)), stop()]


def judge(**kw):
    args = dict(target=TARGET, bare_name="demo", target_paths={"/probe/skills/demo/SKILL.md"},
                check_listing=True, max_read_only=8)
    args.update(kw)
    return StreamJudge(**args)


def run(events, j=None):
    j = j or judge()
    for e in events:
        d = j.feed(e)
        if d:
            return d
    return None


class JudgeTests(unittest.TestCase):
    def test_toolsearch_then_skill_fires(self):
        self.assertEqual(run([init(TARGET), *tool("ToolSearch", {"query": "x"}),
                              *tool("Skill", {"skill": TARGET})]), (FIRED, "skill"))

    def test_read_other_file_then_skill_fires(self):
        self.assertEqual(run([init(TARGET), *tool("Read", {"file_path": "/etc/hosts"}),
                              *tool("Skill", {"skill": TARGET})]), (FIRED, "skill"))

    def test_text_block_first_then_skill_fires(self):
        self.assertEqual(run([init(TARGET), text_start(), stop(),
                              *tool("Skill", {"skill": TARGET})]), (FIRED, "skill"))

    def test_competitor_skill_is_not_fired_with_its_name(self):
        self.assertEqual(run([init(TARGET, "other"), *tool("Skill", {"skill": "other"})]),
                         (NOT_FIRED, "competitor:other"))

    def test_side_effecting_tool_is_preempted_at_block_start(self):
        # Decided on the start event alone: no delta is needed, so nothing has executed yet.
        self.assertEqual(run([init(TARGET), start("Bash")]), (NOT_FIRED, "preempted:Bash"))
        self.assertEqual(run([init(TARGET), start("mcp__atlassian__createJiraIssue")]),
                         (NOT_FIRED, "preempted:mcp__atlassian__createJiraIssue"))

    def test_read_of_target_skill_file_fires(self):
        self.assertEqual(run([init(TARGET),
                              *tool("Read", {"file_path": "/probe/skills/demo/SKILL.md"})]),
                         (FIRED, "read"))

    def test_skill_decided_from_partial_json_only_once_value_is_closed(self):
        j = judge()
        self.assertIsNone(j.feed(init(TARGET)))
        self.assertIsNone(j.feed(start("Skill")))
        self.assertIsNone(j.feed(delta('{"skill": "evalprobe:de')))
        self.assertEqual(j.feed(delta('mo"}')), (FIRED, "skill"))

    def test_prefix_of_target_is_not_a_hit(self):
        self.assertEqual(run([init(TARGET, "evalprobe:demo-extra"),
                              *tool("Skill", {"skill": "evalprobe:demo-extra"})]),
                         (NOT_FIRED, "competitor:evalprobe:demo-extra"))

    def test_result_success_without_skill_is_not_fired(self):
        self.assertEqual(run([init(TARGET), {"type": "result", "subtype": "success",
                                             "is_error": False}]), (NOT_FIRED, "no-skill"))

    def test_error_result_is_error_not_a_miss(self):
        self.assertEqual(run([init(TARGET), {"type": "result", "subtype": "error_during_execution",
                                             "is_error": True}]),
                         (ERROR, "result:error_during_execution"))

    def test_target_missing_from_listing_is_error(self):
        self.assertEqual(run([init("something-else")]), (ERROR, f"target-not-listed:{TARGET}"))

    def test_probe_shadowed_by_same_name_skill_is_error(self):
        self.assertEqual(run([init(TARGET, "demo", "anthropic-skills:demo")]),
                         (ERROR, "shadowed-by:anthropic-skills:demo,demo"))

    def test_installed_target_tolerates_plugin_copy(self):
        j = judge(target="demo")
        self.assertIsNone(j.feed(init("demo", "anthropic-skills:demo")))

    def test_read_only_budget_ends_as_not_fired(self):
        events = [init(TARGET)] + [e for _ in range(3) for e in tool("Grep", {"pattern": "x"})]
        self.assertEqual(run(events, judge(max_read_only=3)),
                         (NOT_FIRED, "no-skill-after-3-read-only-calls"))

    def test_assistant_fallback_used_only_without_partial_events(self):
        msg = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Skill", "input": {"skill": TARGET}}]}}
        self.assertEqual(run([init(TARGET), msg]), (FIRED, "skill"))
        # With partial events, the complete message must not count the same call twice.
        j = judge(max_read_only=2)
        grep_msg = {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Grep", "input": {"pattern": "x"}}]}}
        self.assertIsNone(run([init(TARGET), *tool("Grep", {"pattern": "x"}), grep_msg], j))


FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import os, sys, time, json
    query = sys.stdin.read()
    open(os.environ["FAKE_LOG"], "a").write(json.dumps({"argv": sys.argv[1:], "stdin": query, "pid": os.getpid()}) + "\\n")
    script = open(os.environ["FAKE_SCRIPT"]).read()
    if script.startswith("SLEEP"):
        time.sleep(30)
    sys.stdout.write(script)          # written verbatim: the last line may lack a newline
    sys.stdout.flush()
    """)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fake = self.tmp / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "log.jsonl"
        self.script = self.tmp / "script.txt"
        self.env = mock.patch.dict(os.environ, {
            "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
            "FAKE_LOG": str(self.log), "FAKE_SCRIPT": str(self.script)})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def go(self, lines, query="hello", timeout=10):
        self.script.write_text(lines)
        return re_mod.run_single_query(query, "demo", "desc", timeout, str(self.tmp),
                                       mode="probe", body="body")

    def test_tail_without_trailing_newline_is_parsed(self):
        out = "\n".join(json.dumps(e) for e in
                        [init(TARGET), *tool("Skill", {"skill": TARGET})])   # no final "\n"
        self.assertEqual(self.go(out), (FIRED, "skill"))

    def test_eof_before_result_is_error(self):
        self.assertEqual(self.go(json.dumps(init(TARGET)) + "\n")[0], ERROR)

    def test_timeout_is_error(self):
        self.assertEqual(self.go("SLEEP", timeout=2), (ERROR, "timeout"))

    def test_query_goes_to_stdin_not_argv(self):
        self.go(json.dumps(init(TARGET)) + "\n", query="--dangerous-looking flag text")
        rec = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual(rec["stdin"], "--dangerous-looking flag text")
        self.assertNotIn("--dangerous-looking flag text", rec["argv"])
        self.assertIn("--plugin-dir", rec["argv"])

    def test_probe_plugin_is_removed_afterwards(self):
        before = set(Path(tempfile.gettempdir()).glob("trigger-eval-*"))
        self.go(json.dumps(init(TARGET)) + "\n")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("trigger-eval-*")), before)


class InterruptTests(unittest.TestCase):
    """SIGTERM to the runner must kill every probe and launch no new ones."""

    def test_sigterm_kills_running_probes_and_stops_the_queue(self):
        import signal, subprocess, sys, time
        tmp = Path(tempfile.mkdtemp())
        fake = tmp / "bin" / "claude"
        fake.parent.mkdir()
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        (tmp / "script.txt").write_text("SLEEP")
        skill = tmp / "demo"
        skill.mkdir()
        (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\n\nbody\n")
        evals = [{"query": f"q{i}", "should_trigger": i % 2 == 0} for i in range(6)]
        (tmp / "evals.json").write_text(json.dumps(evals))
        log = tmp / "log.jsonl"
        env = dict(os.environ, PATH=f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
                   FAKE_LOG=str(log), FAKE_SCRIPT=str(tmp / "script.txt"))
        runner = subprocess.Popen(
            [sys.executable, "-m", "scripts.run_eval", "--eval-set", str(tmp / "evals.json"),
             "--skill-path", str(skill), "--project-root", str(tmp), "--num-workers", "2",
             "--runs-per-query", "2", "--timeout", "60"],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline and (not log.exists() or len(log.read_text().splitlines()) < 2):
            time.sleep(0.2)
        self.assertTrue(log.exists() and len(log.read_text().splitlines()) >= 2, "probes never started")
        runner.send_signal(signal.SIGTERM)
        runner.wait(timeout=15)
        time.sleep(2)
        launched = [json.loads(line)["pid"] for line in log.read_text().splitlines()]
        alive = []
        for pid in launched:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        for pid in alive:                       # never leave the fakes behind, even on failure
            os.kill(pid, signal.SIGKILL)
        self.assertEqual(alive, [], "probes survived the interrupted runner")
        self.assertLess(len(launched), len(evals) * 2, "the queue kept launching after SIGTERM")


class AggregateTests(unittest.TestCase):
    def evals(self):
        return [{"query": "a", "should_trigger": True}, {"query": "b", "should_trigger": False}]

    def test_errors_are_excluded_and_make_a_query_inconclusive(self):
        outcomes = {"a": [(FIRED, "skill"), (ERROR, "timeout"), (FIRED, "skill")],
                    "b": [(ERROR, "timeout"), (ERROR, "timeout"), (NOT_FIRED, "no-skill")]}
        calls = {"a": 0, "b": 0}

        def fake(query, *a, **k):
            o = outcomes[query][calls[query]]
            calls[query] += 1
            return o

        with mock.patch.object(re_mod, "run_single_query", side_effect=fake):
            out = re_mod.run_eval(self.evals(), "demo", "d", 1, 10, Path("."), runs_per_query=3)
        a, b = out["results"]
        self.assertEqual((a["triggers"], a["runs"], a["errors"], a["pass"]), (2, 2, 1, True))
        self.assertEqual((b["runs"], b["errors"], b["pass"]), (1, 2, None))
        self.assertEqual(out["summary"]["inconclusive"], 1)
        self.assertEqual(out["summary"]["error_reasons"], {"timeout": 3})

    def test_bad_inputs_are_rejected_before_any_run(self):
        bad = [
            ("", self.evals()),
            ("../x", self.evals()),
            ("demo", []),
            ("demo", [{"query": "a", "should_trigger": "false"}]),
            ("demo", [{"query": "a", "should_trigger": True}, {"query": "a", "should_trigger": False}]),
        ]
        with mock.patch.object(re_mod, "run_single_query") as rsq:
            for name, evals in bad:
                with self.assertRaises(re_mod.EvalSetupError, msg=(name, evals)):
                    re_mod.run_eval(evals, name, "d", 1, 10, Path("."))
            rsq.assert_not_called()


class ContextTests(unittest.TestCase):
    def test_project_skill_runs_from_its_project(self):
        with tempfile.TemporaryDirectory() as d:
            sp = Path(d) / "repo" / ".claude" / "skills" / "demo"
            sp.mkdir(parents=True)
            self.assertEqual(re_mod.installed_scope(sp), "project")
            self.assertEqual(re_mod.resolve_project_root(sp, None), (Path(d) / "repo").resolve())

    def test_home_is_refused_unless_explicit(self):
        with mock.patch.object(Path, "cwd", return_value=Path.home()):
            with self.assertRaises(re_mod.EvalSetupError):
                re_mod.resolve_project_root(Path("/nowhere/demo"), None)
        self.assertEqual(re_mod.resolve_project_root(Path("/nowhere/demo"), str(Path.home())),
                         Path.home().resolve())


if __name__ == "__main__":
    unittest.main()
