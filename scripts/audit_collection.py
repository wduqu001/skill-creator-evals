#!/usr/bin/env python3
# Copyright 2026 Willian Duque
# SPDX-License-Identifier: Apache-2.0
"""Audit a whole collection of skills for structural consistency.

`quick_validate.py` checks one skill's frontmatter. This checks a *set* of them,
plus the cross-references that tie a collection together and rot silently: a
skill added to disk but never listed in the plugin manifest, a docs page left
behind by a rename, a reference file a SKILL.md still points at after it moved.
Nothing here reads the prose -- it only checks claims that can be mechanically
falsified.

Layout is auto-detected (flat or bucketed), so this runs against ~/.claude/skills
and against a bucketed public repo without configuration.

Checks always run:
  - frontmatter parses; `name` matches the directory; `description` present
  - description length against the 1024-char spec limit
  - every markdown-link pointer out of a SKILL.md resolves on disk

Checks that run only when the artifact exists (reported as SKIPPED otherwise, so
an absent manifest never reads as a clean bill of health):
  - .claude-plugin/plugin.json  -- entries resolve; skills on disk are listed
  - README.md                   -- promoted skills are linked
  - docs/                       -- promoted skills have a page

Usage:
  python -m scripts.audit_collection <root>
  python -m scripts.audit_collection <root> --promoted engineering,productivity
  python -m scripts.audit_collection <root> --require-docs --quiet
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from .utils import discover_skills, is_user_invoked, read_frontmatter
except ImportError:  # invoked as a plain script rather than `python -m`
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils import discover_skills, is_user_invoked, read_frontmatter

# The documented spec limit, and what quick_validate.py enforces. Exceeding it is
# worth knowing about but is not by itself proof of breakage.
DESCRIPTION_SPEC_LIMIT = 1024

# Where the listing was *observed* to truncate a description mid-word (measured
# 2026-08-13: a 1432-char description survived intact, a 1659-char one was cut at
# ~1535). Past this, trailing text is silently dropped -- and since negative
# triggers ("do NOT use for...") conventionally sit at the end of a description,
# the part that stops a skill over-triggering is the first part to disappear.
DESCRIPTION_TRUNCATION_OBSERVED = 1500

# Only markdown links are treated as pointers. A backticked `FOO.md` is usually a
# file the skill tells the agent to CREATE in the user's workspace, not a
# reference file shipped beside it -- counting those produces false "broken
# pointer" reports, which is how a clean collection gets flagged as broken.
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#]+\.md)(?:#[^)]*)?\)")


def audit(root: Path, promoted: set[str], require_docs: bool) -> tuple[list, list, list, list]:
    problems: list[str] = []
    warnings: list[str] = []
    skipped: list[str] = []
    rows: list[tuple] = []

    skills = discover_skills(root)
    if not skills:
        problems.append(f"no SKILL.md found anywhere under {root}")
        return rows, problems, warnings, skipped

    plugin_path = root / ".claude-plugin" / "plugin.json"
    plugin_entries: list[str] = []
    if plugin_path.exists():
        try:
            plugin_entries = json.loads(plugin_path.read_text(encoding="utf-8")).get("skills", [])
        except json.JSONDecodeError as exc:
            problems.append(f".claude-plugin/plugin.json is not valid JSON: {exc}")
    else:
        skipped.append("plugin manifest cross-check (no .claude-plugin/plugin.json)")
    listed = {Path(entry).name for entry in plugin_entries}

    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else None
    if readme is None:
        skipped.append("README cross-check (no README.md at root)")

    docs_root = root / "docs"
    if require_docs and not docs_root.is_dir():
        skipped.append("docs-page cross-check (no docs/ directory)")

    for skill in skills:
        skill_md, dirname, bucket = skill["path"], skill["dir"], skill["bucket"]
        rel = skill_md.relative_to(root)
        frontmatter = read_frontmatter(skill_md)

        if frontmatter is None:
            problems.append(f"{rel}: no parseable YAML frontmatter")
            continue

        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        user_invoked = is_user_invoked(frontmatter)

        if not name:
            problems.append(f"{rel}: missing 'name' in frontmatter")
        elif name != dirname:
            problems.append(f"{rel}: frontmatter name '{name}' != directory '{dirname}'")

        if not description:
            problems.append(f"{rel}: missing 'description' -- nothing can trigger this skill")
        elif len(description) > DESCRIPTION_TRUNCATION_OBSERVED:
            problems.append(
                f"{rel}: description is {len(description)} chars -- past the ~"
                f"{DESCRIPTION_TRUNCATION_OBSERVED} point where the listing was seen to cut "
                f"mid-word, so the tail (usually the negative triggers) is being dropped"
            )
        elif len(description) > DESCRIPTION_SPEC_LIMIT:
            warnings.append(
                f"{rel}: description is {len(description)} chars, over the documented "
                f"{DESCRIPTION_SPEC_LIMIT} limit (observed to still render in full -- tighten it "
                f"before it grows into the truncation zone)"
            )

        text = skill_md.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not ((skill_md.parent / target).exists() or (root / target.lstrip("./")).exists()):
                problems.append(f"{rel}: pointer does not resolve -> {target}")

        # Cross-references only bind for skills the collection actually promotes.
        # With no --promoted set, every skill counts as promoted.
        is_promoted = (bucket in promoted) if promoted else True

        if plugin_entries and is_promoted and dirname not in listed:
            problems.append(f"{rel}: on disk but absent from plugin.json skills[]")
        if plugin_entries and promoted and not is_promoted and dirname in listed:
            problems.append(f"{rel}: in a non-promoted bucket but shipped in plugin.json")
        if readme is not None and is_promoted and dirname not in readme:
            problems.append(f"{rel}: promoted but never mentioned in README.md")
        if require_docs and docs_root.is_dir() and is_promoted:
            pages = list(docs_root.rglob(f"{dirname}.md"))
            if not pages:
                problems.append(f"{rel}: promoted but has no docs page named {dirname}.md")

        rows.append(
            (bucket or "-", dirname, len(description), len(text),
             "user" if user_invoked else "model")
        )

    for entry in plugin_entries:
        if not (root / entry.lstrip("./")).exists():
            problems.append(f"plugin.json: entry '{entry}' does not exist on disk")

    return rows, problems, warnings, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("root", type=Path, help="collection root (e.g. ~/.claude/skills)")
    parser.add_argument(
        "--promoted", default="",
        help="comma-separated buckets whose skills must be published; "
             "omit when every skill counts as promoted",
    )
    parser.add_argument("--require-docs", action="store_true",
                        help="require a docs page per promoted skill")
    parser.add_argument("--quiet", action="store_true", help="suppress the per-skill table")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    promoted = {b.strip() for b in args.promoted.split(",") if b.strip()}
    rows, problems, warnings, skipped = audit(root, promoted, args.require_docs)

    if rows and not args.quiet:
        print(f"{'bucket':<14} {'skill':<34} {'desc':>5} {'bytes':>7}  invocation")
        for bucket, name, desc_len, size, invocation in rows:
            print(f"{bucket:<14} {name:<34} {desc_len:>5} {size:>7}  {invocation}")
        print()

    for note in skipped:
        print(f"  SKIPPED: {note}")
    if skipped:
        print()

    print(f"=== {len(rows)} skills audited, {len(problems)} problems, "
          f"{len(warnings)} warnings ===")
    for problem in problems:
        print(f"  ! {problem}")
    for warning in warnings:
        print(f"  ~ {warning}")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
