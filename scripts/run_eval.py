#!/usr/bin/env python3
# Modified by Willian Duque, 2026, from Anthropic's skill-creator (anthropics/skills, Apache-2.0).
"""Measure whether a skill description makes Claude invoke the skill.

Two modes, chosen from the skill path and the description under test:

- installed: the description is the one already installed at `<P>/.claude/skills/<name>` (project)
  or `~/.claude/skills/<name>` (user). Nothing is written; a run counts an invocation of the real
  skill by its exact name, from the project root, so the real CLAUDE.md and sibling skills compete.
- probe: any other description (a draft, or a run_loop candidate). Each `claude -p` gets its own
  temporary plugin via `--plugin-dir`, invoked as `evalprobe:<name>`, so parallel runs never see
  each other's copies. A run is an ERROR if another skill with the same name is visible, because
  it would compete with the probe.

Every run ends FIRED, NOT_FIRED or ERROR, and an ERROR never counts as "did not fire". Tools with
side effects are never executed: the child is killed when such a tool's block starts streaming,
which is before the tool runs.
"""

import argparse
import atexit
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.utils import parse_skill_md

PROBE_PLUGIN = "evalprobe"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Tools a run may use before deciding; none of them changes anything.
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "ToolSearch"}
SKILL_FIELD = re.compile(r'"skill"\s*:\s*"((?:[^"\\]|\\.)*)"')

FIRED, NOT_FIRED, ERROR = "fired", "not_fired", "error"

# Children run in their own session, so the terminal's Ctrl-C/SIGHUP never reaches them, and a
# child nobody reads from is no longer stopped before a tool executes. Track them and kill them all
# when the batch is interrupted.
_LIVE: set = set()
_LIVE_LOCK = threading.Lock()
_STOP = threading.Event()


def stop_all_children() -> None:
    _STOP.set()
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def install_signal_handlers() -> None:
    """Kill every running probe on Ctrl-C, SIGTERM or SIGHUP, and at interpreter exit."""
    def handler(signum, _frame):
        stop_all_children()
        raise SystemExit(128 + signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)
    atexit.register(stop_all_children)


# --------------------------------------------------------------------------- context

class EvalSetupError(Exception):
    """A problem with the inputs; reported once, before any run starts."""


def validate_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise EvalSetupError(
            f"skill name {name!r} is not a valid skill name (lowercase letters, digits and hyphens, "
            f"max 64). Fix the `name:` line in SKILL.md.")


def validate_eval_set(eval_set) -> None:
    if not isinstance(eval_set, list) or not eval_set:
        raise EvalSetupError("eval set must be a non-empty JSON list")
    seen = set()
    for i, item in enumerate(eval_set):
        q = item.get("query") if isinstance(item, dict) else None
        if not isinstance(q, str) or not q.strip():
            raise EvalSetupError(f"item {i}: `query` must be a non-empty string")
        if not isinstance(item.get("should_trigger"), bool):
            raise EvalSetupError(f"item {i}: `should_trigger` must be true or false, not "
                                 f"{item.get('should_trigger')!r}")
        if q in seen:
            raise EvalSetupError(f"item {i}: duplicate query {q[:60]!r}")
        seen.add(q)


def installed_scope(skill_path: Path) -> str | None:
    """'project' or 'user' when skill_path is where Claude Code loads skills from, else None."""
    sp = skill_path.resolve()
    if sp.parent.name != "skills" or sp.parent.parent.name != ".claude":
        return None
    return "user" if sp.parent.parent.parent == Path.home().resolve() else "project"


def resolve_project_root(skill_path: Path, explicit: str | None) -> Path:
    """The directory every `claude -p` runs from.

    It decides which CLAUDE.md, project skills and settings the description competes against,
    so it is derived from the skill's own location rather than from wherever the script runs.
    """
    home = Path.home().resolve()
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not root.is_dir():
            raise EvalSetupError(f"--project-root {root} is not a directory")
        return root
    if installed_scope(skill_path) == "project":
        return skill_path.resolve().parent.parent.parent
    root = Path.cwd().resolve()
    if root == home or root == home / ".claude" or (home / ".claude") in root.parents:
        raise EvalSetupError(
            f"cannot infer the project to evaluate in (current directory is {root}). Pass "
            f"--project-root <the repo the skill is meant for>, or --project-root ~ for a skill "
            f"meant to work anywhere.")
    return root


# --------------------------------------------------------------------------- one run

class StreamJudge:
    """Decides one run from `claude -p --output-format stream-json` events.

    feed() returns None while undecided, else (outcome, detail). The caller kills the child as
    soon as a decision is returned, so a decision on a side-effecting tool's block start comes
    before that tool executes.
    """

    def __init__(self, target: str, bare_name: str, target_paths: set[str],
                 check_listing: bool, max_read_only: int):
        self.target = target
        self.bare_name = bare_name
        self.target_paths = target_paths
        self.check_listing = check_listing
        self.max_read_only = max_read_only
        self.read_only_calls = 0
        self.block = None          # (tool name, accumulated partial json) for the open block
        self.saw_init = False
        self.saw_partial = False   # partial events already judged every tool_use

    def _skill_decision(self, skill: str):
        if skill == self.target:
            return FIRED, "skill"
        return NOT_FIRED, f"competitor:{skill}"

    def _complete_tool(self, name: str, inp: dict):
        if name == "Skill":
            return self._skill_decision(str(inp.get("skill", "")))
        if name == "Read":
            path = str(inp.get("file_path", ""))
            try:
                resolved = str(Path(path).resolve())
            except (OSError, ValueError):
                resolved = path
            if resolved in self.target_paths:
                return FIRED, "read"
        if name in READ_ONLY_TOOLS:
            self.read_only_calls += 1
            if self.read_only_calls >= self.max_read_only:
                return NOT_FIRED, f"no-skill-after-{self.max_read_only}-read-only-calls"
            return None
        return NOT_FIRED, f"preempted:{name}"

    def feed(self, e: dict):
        etype = e.get("type")
        if etype == "system" and e.get("subtype") == "init":
            self.saw_init = True
            listed = set(e.get("skills") or []) | set(e.get("slash_commands") or [])
            if self.check_listing and self.target not in listed:
                return ERROR, f"target-not-listed:{self.target}"
            rivals = sorted(s for s in listed if s != self.target and
                            (s == self.bare_name or s.endswith(":" + self.bare_name)))
            if self.check_listing and self.target.startswith(PROBE_PLUGIN + ":") and rivals:
                return ERROR, f"shadowed-by:{','.join(rivals)}"
            return None

        if etype == "stream_event":
            self.saw_partial = True
            se = e.get("event", {})
            st = se.get("type")
            if st == "content_block_start":
                cb = se.get("content_block", {})
                if cb.get("type") != "tool_use":
                    self.block = None
                    return None
                name = cb.get("name", "")
                if name != "Skill" and name not in READ_ONLY_TOOLS:
                    return NOT_FIRED, f"preempted:{name}"     # before it can execute
                self.block = (name, "")
                return None
            if st == "content_block_delta" and self.block:
                d = se.get("delta", {})
                if d.get("type") == "input_json_delta":
                    name, acc = self.block
                    acc += d.get("partial_json", "")
                    self.block = (name, acc)
                    if name == "Skill":
                        m = SKILL_FIELD.search(acc)
                        if m:                                # decide before the skill loads
                            return self._skill_decision(json.loads(f'"{m.group(1)}"'))
                return None
            if st == "content_block_stop" and self.block:
                name, acc = self.block
                self.block = None
                try:
                    inp = json.loads(acc) if acc else {}
                except json.JSONDecodeError:
                    inp = {}
                return self._complete_tool(name, inp)
            return None

        if etype == "assistant" and not self.saw_partial:
            # Fallback for when partial events are missing: judge complete tool_use items.
            for c in e.get("message", {}).get("content", []):
                if c.get("type") == "tool_use":
                    d = self._complete_tool(c.get("name", ""), c.get("input") or {})
                    if d:
                        return d
            return None

        if etype == "result":
            if e.get("is_error") or str(e.get("subtype", "")).startswith("error"):
                return ERROR, f"result:{e.get('subtype')}"
            return NOT_FIRED, "no-skill"
        return None

    def at_eof(self):
        return ERROR, ("eof-before-result" if self.saw_init else "eof-before-init")


def _write_probe_plugin(root: Path, name: str, description: str, body: str) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": PROBE_PLUGIN, "version": "0.0.0", "description": "trigger eval probe"}),
        encoding="utf-8")
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True)
    indented = "\n  ".join(description.split("\n"))
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: |\n  {indented}\n---\n\n{body}", encoding="utf-8")
    return skill_dir / "SKILL.md"


def _kill(p: subprocess.Popen) -> None:
    if p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()
    p.wait()


def run_single_query(query: str, skill_name: str, description: str, timeout: int,
                     project_root: str, model: str | None = None, *, mode: str = "probe",
                     skill_path: str | None = None, body: str = "",
                     max_read_only: int = 8) -> tuple[str, str]:
    """Run one query once. Returns (outcome, detail)."""
    tmp = None
    try:
        cmd = ["claude", "-p", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--no-session-persistence"]
        if model:
            cmd += ["--model", model]
        if mode == "installed":
            target = skill_name
            target_paths = {str((Path(skill_path) / "SKILL.md").resolve())}
        else:
            tmp = Path(tempfile.mkdtemp(prefix="trigger-eval-"))
            probe_md = _write_probe_plugin(tmp / "plugin", skill_name, description, body)
            cmd += ["--plugin-dir", str(tmp / "plugin")]
            target = f"{PROBE_PLUGIN}:{skill_name}"
            target_paths = {str(probe_md.resolve())}

        judge = StreamJudge(target, skill_name, target_paths, check_listing=True,
                            max_read_only=max_read_only)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        err_file = tempfile.TemporaryFile()
        with _LIVE_LOCK:                  # checked under the lock, so a stop cannot miss this child
            if _STOP.is_set():
                err_file.close()
                return ERROR, "stopped"
            p = subprocess.Popen(cmd, cwd=project_root, env=env, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=err_file, start_new_session=True)
            _LIVE.add(p)
        lines: queue.Queue = queue.Queue()

        def pump():
            for raw in p.stdout:          # readline semantics: the final line needs no newline
                lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        try:
            p.stdin.write(query.encode("utf-8"))   # stdin, so a query starting with "-" is text
            p.stdin.close()
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    return ERROR, "timeout"
                try:
                    raw = lines.get(timeout=left)
                except queue.Empty:
                    return ERROR, "timeout"
                if raw is None:
                    err_file.seek(0)
                    tail = err_file.read().decode("utf-8", "replace").strip().splitlines()[-1:]
                    outcome, detail = judge.at_eof()
                    return outcome, detail + (f" ({tail[0][:120]})" if tail else "")
                try:
                    e = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                d = judge.feed(e)
                if d:
                    return d
        finally:
            _kill(p)
            with _LIVE_LOCK:
                _LIVE.discard(p)
            err_file.close()
    except EvalSetupError:
        raise
    except Exception as exc:                       # an instrument failure, never a "did not fire"
        return ERROR, f"exception:{type(exc).__name__}:{exc}"
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- batch

def choose_mode(skill_path: Path, skill_name: str, description: str) -> str:
    """installed when the description under test is exactly what is installed at skill_path."""
    if installed_scope(skill_path) is None:
        return "probe"
    if skill_path.resolve().name != skill_name:
        raise EvalSetupError(f"installed skill folder {skill_path.name!r} does not match its "
                             f"name {skill_name!r}; Claude Code lists it by folder name")
    _, installed_desc, _ = parse_skill_md(skill_path)
    return "installed" if " ".join(installed_desc.split()) == " ".join(description.split()) else "probe"


def run_eval(eval_set: list[dict], skill_name: str, description: str, num_workers: int,
             timeout: int, project_root: Path, runs_per_query: int = 1,
             trigger_threshold: float = 0.5, model: str | None = None, *,
             skill_path: Path | None = None, max_read_only: int = 8) -> dict:
    """Run every query runs_per_query times and aggregate per query.

    Result rows keep the historical keys (`triggers`, `runs`, `pass`) so run_loop, the report and
    improve_description keep working. `runs` counts VALID runs only; `pass` is None when a query
    is inconclusive (fewer than half its runs valid).
    """
    validate_name(skill_name)
    validate_eval_set(eval_set)
    if runs_per_query < 1:
        raise EvalSetupError("--runs-per-query must be at least 1")
    if not 0 < trigger_threshold <= 1:
        raise EvalSetupError("--trigger-threshold must be in (0, 1]")
    if timeout <= 0 or num_workers < 1:
        raise EvalSetupError("--timeout and --num-workers must be positive")

    body = ""
    mode = "probe"
    if skill_path is not None:
        _, _, content = parse_skill_md(skill_path)
        body = re.sub(r"\A---\n.*?\n---\n", "", content, count=1, flags=re.S)
        mode = choose_mode(skill_path, skill_name, description)

    jobs = [(i, r) for i in range(len(eval_set)) for r in range(runs_per_query)]

    def work(job):
        i, _ = job
        return i, run_single_query(eval_set[i]["query"], skill_name, description, timeout,
                                   str(project_root), model, mode=mode,
                                   skill_path=str(skill_path) if skill_path else None,
                                   body=body, max_read_only=max_read_only)

    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        outcomes = list(ex.map(work, jobs))

    per_item: dict[int, list[tuple[str, str]]] = {i: [] for i in range(len(eval_set))}
    for i, o in outcomes:
        per_item[i].append(o)

    results, error_reasons = [], Counter()
    min_valid = math.ceil(runs_per_query / 2)
    for i, item in enumerate(eval_set):
        runs = per_item[i]
        fired = sum(1 for o, _ in runs if o == FIRED)
        not_fired = sum(1 for o, _ in runs if o == NOT_FIRED)
        errors = [d for o, d in runs if o == ERROR]
        error_reasons.update(d.split(" (")[0] for d in errors)
        valid = fired + not_fired
        rate = fired / valid if valid else None
        if valid < min_valid:
            did_pass = None
        elif item["should_trigger"]:
            did_pass = rate >= trigger_threshold
        else:
            did_pass = rate < trigger_threshold
        results.append({
            "query": item["query"],
            "should_trigger": item["should_trigger"],
            "trigger_rate": rate,
            "triggers": fired,
            "runs": valid,
            "errors": len(errors),
            "pass": did_pass,
            "outcomes": dict(Counter(d.split(" (")[0] for _, d in runs)),
        })

    passed = sum(1 for r in results if r["pass"] is True)
    failed = sum(1 for r in results if r["pass"] is False)
    inconclusive = sum(1 for r in results if r["pass"] is None)
    return {
        "skill_name": skill_name,
        "description": description,
        "mode": mode,
        "project_root": str(project_root),
        "results": results,
        "summary": {"total": len(results), "passed": passed, "failed": failed,
                    "inconclusive": inconclusive, "error_reasons": dict(error_reasons)},
    }


def main():
    ap = argparse.ArgumentParser(description="Run trigger evaluation for a skill description")
    ap.add_argument("--eval-set", required=True, help="Path to eval set JSON file")
    ap.add_argument("--skill-path", required=True, help="Path to skill directory")
    ap.add_argument("--description", default=None, help="Description to test (default: the skill's own)")
    ap.add_argument("--project-root", default=None,
                    help="Directory to run claude from (default: derived from an installed project "
                         "skill's path, else the current directory)")
    ap.add_argument("--num-workers", type=int, default=6, help="Parallel runs")
    ap.add_argument("--timeout", type=int, default=120, help="Seconds per run before it is an ERROR")
    ap.add_argument("--runs-per-query", type=int, default=3)
    ap.add_argument("--trigger-threshold", type=float, default=0.5)
    ap.add_argument("--max-read-only", type=int, default=8,
                    help="Read-only tool calls allowed before a run counts as not fired")
    ap.add_argument("--model", default=None, help="Model for claude -p (default: user's configured model)")
    ap.add_argument("--verbose", action="store_true", help="Print progress to stderr")
    args = ap.parse_args()

    if shutil.which("claude") is None:
        sys.exit("Error: `claude` CLI not found on PATH")
    install_signal_handlers()
    skill_path = Path(args.skill_path)
    if not (skill_path / "SKILL.md").exists():
        sys.exit(f"Error: No SKILL.md found at {skill_path}")
    try:
        name, original_description, _ = parse_skill_md(skill_path)
        description = args.description or original_description
        project_root = resolve_project_root(skill_path, args.project_root)
        eval_set = json.loads(Path(args.eval_set).read_text())
        output = run_eval(eval_set=eval_set, skill_name=name, description=description,
                          num_workers=args.num_workers, timeout=args.timeout,
                          project_root=project_root, runs_per_query=args.runs_per_query,
                          trigger_threshold=args.trigger_threshold, model=args.model,
                          skill_path=skill_path, max_read_only=args.max_read_only)
    except EvalSetupError as exc:
        sys.exit(f"Error: {exc}")

    if args.verbose:
        s = output["summary"]
        print(f"mode={output['mode']} project_root={output['project_root']}", file=sys.stderr)
        print(f"Results: {s['passed']} passed, {s['failed']} failed, {s['inconclusive']} inconclusive "
              f"of {s['total']}", file=sys.stderr)
        if s["error_reasons"]:
            print(f"  run errors (excluded, never counted as 'did not fire'): {s['error_reasons']}",
                  file=sys.stderr)
        for r in output["results"]:
            status = {True: "PASS", False: "FAIL", None: "INCONCLUSIVE"}[r["pass"]]
            print(f"  [{status}] fired {r['triggers']}/{r['runs']} (+{r['errors']} errors) "
                  f"expected={r['should_trigger']}: {r['query'][:70]}  {r['outcomes']}", file=sys.stderr)
    print(json.dumps(output, indent=2))
    s = output["summary"]
    if s["total"] and s["inconclusive"] == s["total"]:
        sys.exit("Error: every query was inconclusive - the instrument failed; see error_reasons")


if __name__ == "__main__":
    main()
