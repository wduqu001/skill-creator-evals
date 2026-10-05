# skill-creator-evals

A fork of Anthropic's [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)
skill for Claude Code. It adds a rewritten trigger evaluation, which measures whether a skill's
description actually makes Claude use the skill, and tools for auditing a whole folder of skills.

Not affiliated with or endorsed by Anthropic.

## What's different

**Trigger evaluation** (`scripts/run_eval.py`, `scripts/run_loop.py`):

- **Parallel runs don't interfere.** Each run mounts the description under test as its own
  temporary plugin (`--plugin-dir`). No run sees another run's copy, so parallel and serial runs
  score the same.
- **Installed skills are measured as installed.** When the description under test is the one
  already installed, nothing is written; a run counts the real skill being invoked by name.
- **Runs start in the right project.** Every run starts in the project the skill is for, so the
  description competes with that repo's `CLAUDE.md` and skills, as it does in real use.
- **Errors are not misses.** Every run ends as *fired*, *not fired* or *error*. A timeout or a
  crashed run is reported separately and never counted as "did not trigger". A query without
  enough valid runs is marked inconclusive.
- **Nothing with side effects runs.** A run is stopped as soon as a tool such as Bash, a file edit
  or an MCP call starts, before it executes. A run that picks a different skill records which one.
- **Interrupting is safe.** Ctrl-C or SIGTERM stops every running probe.

**Collection audits:**

- `scripts/audit_collection.py` checks a folder of skills for names that don't match their
  folder, descriptions long enough to be truncated, and pointers that lead nowhere.
- `scripts/check_invocation_edges.py` maps which skills point at which, and flags pointers the
  agent can't follow.

**Also:**

- `scripts/trigger_sim_local.py` is a cheap local-model stand-in for comparing two descriptions.
- `references/writing-guide.md` covers skill layout and how to write a prohibition.
- `scripts/utils.py` and `scripts/quick_validate.py` no longer require PyYAML; `quick_validate.py`
  still uses it when it is installed.

Modified files carry a notice saying so.

## Install

```bash
git clone https://github.com/wduqu001/skill-creator-evals ~/.claude/skills/skill-creator
```

Needs Python 3.10+ and the `claude` CLI on your PATH. Tested with Claude Code 2.1.289.

## Use

From the skill folder:

```bash
# Does this description trigger? Runs from the skill's own project when it is installed there.
python -m scripts.run_eval --eval-set evals.json --skill-path <skill> --verbose

# A draft, or a skill meant for another repo: say which project to run in.
python -m scripts.run_eval --eval-set evals.json --skill-path <draft> --project-root <repo> --verbose

# Offline tests
python3 -m unittest scripts.test_run_eval -v
```

An eval set is a JSON list of `{"query": "...", "should_trigger": true}` items.

## License

Apache 2.0, see [LICENSE.txt](LICENSE.txt). The original skill-creator is copyright Anthropic,
PBC. Changes and additions are copyright 2026 Willian Duque.
