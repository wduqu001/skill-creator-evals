#!/usr/bin/env python3
# Copyright 2026 Willian Duque
# SPDX-License-Identifier: Apache-2.0
"""Map skill-to-skill references and flag the ones the agent cannot traverse.

A skill with `disable-model-invocation: true` has no description in the agent's
reach, so *only a human typing its name* can start it -- neither the model nor
another skill can. A SKILL.md that instructs the agent to "run /that-skill"
therefore names an edge that silently does nothing at runtime. Nothing errors;
the step is just skipped, which makes this class of bug invisible until someone
notices the work never happened.

The check is textual, so it reports rather than judges. Two mentions of a
user-invoked skill are legitimate and expected:

  1. Router skills, which exist to list other skills for the human to pick from.
     Use --router to exclude them.
  2. User-directed advice -- "run /setup if you haven't" -- addressed to the
     human, not the agent. These read as REVIEW below; the quoted line is there
     so you can tell the two apart at a glance.

The tell for a genuine defect is agent-directed phrasing: "hand off to /x",
"use /x", "invoke /x" pointing at a user-invoked skill.

Usage:
  python -m scripts.check_invocation_edges <root>
  python -m scripts.check_invocation_edges <root> --router <router-skill> --quiet
"""

import argparse
import re
import sys
from pathlib import Path

try:
    from .utils import discover_skills, is_user_invoked, read_frontmatter
except ImportError:  # invoked as a plain script rather than `python -m`
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils import discover_skills, is_user_invoked, read_frontmatter

MENTION_RE = re.compile(r"/([a-z][a-z0-9-]{2,})\b")
FENCE_RE = re.compile(r"^\s*```")


def build_index(root: Path) -> dict[str, bool]:
    """Map skill name -> is_user_invoked, keyed by frontmatter name and dir name."""
    index: dict[str, bool] = {}
    for skill in discover_skills(root):
        frontmatter = read_frontmatter(skill["path"])
        if frontmatter is None:
            continue
        user_invoked = is_user_invoked(frontmatter)
        index[skill["dir"]] = user_invoked
        if frontmatter.get("name"):
            index[frontmatter["name"]] = user_invoked
    return index


def collect_edges(root: Path, index: dict[str, bool]) -> tuple[list, list]:
    unreachable, valid = [], []

    for skill in discover_skills(root):
        source = skill["dir"]
        if source not in index:
            continue
        in_fence = False
        for lineno, line in enumerate(skill["path"].read_text(encoding="utf-8").split("\n"), 1):
            if FENCE_RE.match(line):  # a /path inside a code block is not a reference
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for match in MENTION_RE.finditer(line):
                target = match.group(1)
                if target == source or target not in index:
                    continue
                edge = (source, target, lineno, line.strip()[:120])
                (unreachable if index[target] else valid).append(edge)

    return unreachable, valid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", type=Path, help="collection root (e.g. ~/.claude/skills)")
    parser.add_argument("--router", default="",
                        help="comma-separated skills that exist to list others; excluded from REVIEW")
    parser.add_argument("--quiet", action="store_true", help="suppress the valid-edge listing")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    routers = {r.strip() for r in args.router.split(",") if r.strip()}
    index = build_index(root)
    if not index:
        print(f"no skills found under {root}")
        return 2

    unreachable, valid = collect_edges(root, index)
    flagged = [e for e in unreachable if e[0] not in routers]
    skipped_router = len(unreachable) - len(flagged)

    print(f"{len(index)} skill names indexed "
          f"({sum(1 for v in index.values() if v)} user-invoked entries)\n")

    print("=== REVIEW: references to user-invoked skills (agent cannot traverse) ===")
    for source, target, lineno, text in flagged:
        print(f"  {source} -> {target}   {source}/SKILL.md:{lineno}")
        print(f"      {text}")
    if not flagged:
        print("  none")
    if skipped_router:
        print(f"\n  ({skipped_router} excluded as router listings: {', '.join(sorted(routers))})")

    if not args.quiet:
        print("\n=== VALID: references to model-invoked skills ===")
        for source, target in sorted({(e[0], e[1]) for e in valid}):
            print(f"  {source} -> {target}")
        if not valid:
            print("  none")

    print(f"\n{len(flagged)} to review, {len({(e[0], e[1]) for e in valid})} valid unique edges")
    return 0


if __name__ == "__main__":
    sys.exit(main())
