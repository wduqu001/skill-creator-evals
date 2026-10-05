# Modified by Willian Duque, 2026, from Anthropic's skill-creator (anthropics/skills, Apache-2.0).
"""Shared utilities for skill-creator scripts."""

import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def discover_skills(root: Path) -> list[dict]:
    """Find every skill under root, auto-detecting the collection's layout.

    Handles both layouts in the wild without being told which is which:
      flat      <root>/<name>/SKILL.md              (e.g. ~/.claude/skills)
      bucketed  <root>/<bucket>/<name>/SKILL.md     (e.g. skills/engineering/tdd)

    Returns dicts with keys: path (to SKILL.md), dir (the skill's folder name),
    bucket (folder above it, or None when the skill sits directly under root).
    """
    root = Path(root).resolve()
    found = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        if SKIP_DIRS & set(skill_md.parts):
            continue
        parent = skill_md.parent
        bucket = parent.parent.name if parent.parent != root else None
        found.append({"path": skill_md, "dir": parent.name, "bucket": bucket})
    return found


def read_frontmatter(skill_md: Path) -> dict | None:
    """Parse a SKILL.md's YAML frontmatter into a flat dict of top-level scalars.

    Deliberately stdlib-only -- PyYAML is not installed in every environment a
    skill runs in, and a validator that cannot start is worse than none. Handles
    the folded/literal block scalars (>, |, >-, |-) that long descriptions use.
    Returns None when there is no parseable frontmatter.
    """
    match = _FRONTMATTER_RE.match(skill_md.read_text(encoding="utf-8"))
    if not match:
        return None

    out: dict[str, str] = {}
    key = None
    for line in match.group(1).split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            out[key] = value.strip().strip("\"'")
            if out[key] in (">", "|", ">-", "|-"):  # block scalar: body follows
                out[key] = ""
        elif key:  # indented continuation of the previous key
            out[key] = (out[key] + " " + line.strip().strip("\"'")).strip()
    return out


def is_user_invoked(frontmatter: dict) -> bool:
    """True when only a human can reach this skill.

    `disable-model-invocation: true` strips the description from the agent's
    reach, so neither the model nor another skill can fire it.
    """
    return str(frontmatter.get("disable-model-invocation", "")).lower() == "true"



def parse_skill_md(skill_path: Path) -> tuple[str, str, str]:
    """Parse a SKILL.md file, returning (name, description, full_content)."""
    content = (skill_path / "SKILL.md").read_text()
    lines = content.split("\n")

    if lines[0].strip() != "---":
        raise ValueError("SKILL.md missing frontmatter (no opening ---)")

    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("SKILL.md missing frontmatter (no closing ---)")

    name = ""
    description = ""
    frontmatter_lines = lines[1:end_idx]
    i = 0
    while i < len(frontmatter_lines):
        line = frontmatter_lines[i]
        if line.startswith("name:"):
            name = line[len("name:"):].strip().strip('"').strip("'")
        elif line.startswith("description:"):
            value = line[len("description:"):].strip()
            # Handle YAML multiline indicators (>, |, >-, |-)
            if value in (">", "|", ">-", "|-"):
                continuation_lines: list[str] = []
                i += 1
                while i < len(frontmatter_lines) and (frontmatter_lines[i].startswith("  ") or frontmatter_lines[i].startswith("\t")):
                    continuation_lines.append(frontmatter_lines[i].strip())
                    i += 1
                description = " ".join(continuation_lines)
                continue
            else:
                description = value.strip('"').strip("'")
        i += 1

    return name, description, content
