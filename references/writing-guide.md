<!-- Copyright 2026 Willian Duque. SPDX-License-Identifier: Apache-2.0 -->

# Skill Writing Guide

How a skill is laid out and how to write its content. Read this before writing a new
SKILL.md, and when revising one that has grown past ~500 lines.

- [Anatomy of a Skill](#anatomy-of-a-skill)
- [Progressive Disclosure](#progressive-disclosure)
- [Principle of Lack of Surprise](#principle-of-lack-of-surprise)
- [Writing Patterns](#writing-patterns)
- [Writing a prohibition: substitute, override, and leave an exit](#writing-a-prohibition-substitute-override-and-leave-an-exit)

## Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description required)
│   └── Markdown instructions
└── Bundled Resources (optional)
    ├── scripts/    - Executable code for deterministic/repetitive tasks
    ├── references/ - Docs loaded into context as needed
    └── assets/     - Files used in output (templates, icons, fonts)
```

## Progressive Disclosure

Skills use a three-level loading system:
1. **Metadata** (name + description) - Always in context (~100 words)
2. **SKILL.md body** - In context whenever skill triggers (<500 lines ideal)
3. **Bundled resources** - As needed (unlimited, scripts can execute without loading)

These word counts are approximate and you can feel free to go longer if needed.

**Key patterns:**
- Keep SKILL.md under 500 lines; if you're approaching this limit, add an additional layer of hierarchy along with clear pointers about where the model using the skill should go next to follow up.
- Reference files clearly from SKILL.md with guidance on when to read them
- For large reference files (>300 lines), include a table of contents

**Domain organization**: When a skill supports multiple domains/frameworks, organize by variant:
```
cloud-deploy/
├── SKILL.md (workflow + selection)
└── references/
    ├── aws.md
    ├── gcp.md
    └── azure.md
```
Claude reads only the relevant reference file.

## Principle of Lack of Surprise

This goes without saying, but skills must not contain malware, exploit code, or any content that could compromise system security. A skill's contents should not surprise the user in their intent if described. Don't go along with requests to create misleading skills or skills designed to facilitate unauthorized access, data exfiltration, or other malicious activities. Things like a "roleplay as an XYZ" are OK though.

## Writing Patterns

Prefer using the imperative form in instructions.

**Defining output formats** - You can do it like this:
```markdown
## Report structure
ALWAYS use this exact template:
# [Title]
## Executive summary
## Key findings
## Recommendations
```

**Examples pattern** - It's useful to include examples. You can format them like this (but if "Input" and "Output" are in the examples you might want to deviate a little):
```markdown
## Commit message format
**Example 1:**
Input: Added user authentication with JWT tokens
Output: feat(auth): implement JWT-based authentication
```

## Writing a prohibition: substitute, override, and leave an exit

A skill that bans a tool, API, flag or pattern is not done when it has said "don't". A bare ban
leaves the model to improvise a replacement, and improvising is how a ban gets routed around.
Three moves, all of them:

1. **Substitute, keyed to the use case — not one blanket alternative.** The banned thing served
   several purposes; name the right replacement for each, as a table. Otherwise the reader has a gap
   to fill at exactly the moment you told them to stop.

   | Instead of | Use | For |
   |---|---|---|
   | `search` | `searchJiraIssuesUsingJql` | finding Jira issues |
   | `search` | `getJiraIssue` | you already have the key |

2. **Name and override the competing instruction.** If the banned thing argues for itself anywhere
   the model will read — a tool description, a framework default, a docstring, a lint rule — quote
   that text and say explicitly that this file overrides it. A prohibition that doesn't name its
   opponent loses, because the opponent is re-read on every call and your skill is not.

3. **Leave a supervised escape hatch.** Name the residual case the ban would wrongly block and route
   it — usually "ask the user first" — so a legitimate need isn't dead-ended into a silent workaround.
   A ban is a reject branch, and a reject branch owes a recovery path.

**Date a ban that rests on a measurement.** If it exists because of something observed — a version, a
price, a tool's current behaviour — record what was measured, when, and what would make it stale.
Otherwise the ban outlives its reason and nobody can tell.
