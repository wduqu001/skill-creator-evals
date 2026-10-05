#!/usr/bin/env python3
# Copyright 2026 Willian Duque
# SPDX-License-Identifier: Apache-2.0
"""Simulate skill triggering with a LOCAL model, to iterate on a description cheaply.

Why not `claude -p`: driving the real CLI makes the model process Claude Code's whole
system prompt (~20-30k tokens) per query. Against a local 12B at ~240 tok/s prompt
processing that is minutes per query — measured 2026-08-25: four runs took 10 minutes
and all timed out. The query is a rounding error next to the preamble.

So ask the triggering question directly instead: here are the installed skills, here is
a user message, which skill would you open? That is a ~4k prompt, and it puts the
description in front of its REAL competitors, which is what actually decides triggering.

This is a PROXY. A local 12B does not choose the way Opus does, so treat the output as a
relative signal between two candidate descriptions, not as a prediction of production
behaviour. Confirm a winner with `run_eval.py` (real CLI) before shipping.
Always read the control line: if the model picks the same skill for everything, or never
picks anything, it is not discriminating and the run says nothing.

Usage:
  python3 -m scripts.trigger_sim_local --skill jira-ticket \
      --eval-set trigger-evals.json --runs 3 [--model gemma4:12b-mlx-32k]
"""
import argparse, collections, json, os, re, sys, urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
FM = re.compile(r"\A---\n(.*?)\n---", re.S)


def load_skills(skills_dir):
    out = {}
    for name in sorted(os.listdir(skills_dir)):
        md = os.path.join(skills_dir, name, "SKILL.md")
        if not os.path.isfile(md):
            continue
        m = FM.match(open(md, errors="replace").read())
        if not m:
            continue
        fm = m.group(1)
        d = re.search(r"^description:\s*(?:>-|>|\|-|\|)?\s*\n((?:[ \t]+.*\n?)*)|^description:(.*)$",
                      fm + "\n", re.M)
        if not d:
            continue
        desc = " ".join((d.group(1) or d.group(2) or "").split())
        n = re.search(r"^name:\s*(\S+)", fm, re.M)
        out[(n.group(1) if n else name)] = desc
    return out


def ask(model, catalog, query, think=False, timeout=600):
    listing = "\n".join(f"- {k}: {v}" for k, v in catalog.items())
    system = (
        "You route user messages to skills. You are given the installed skills and one "
        "user message. Choose the ONE skill whose description best matches, or \"none\" "
        "if no skill applies and a general assistant should just answer.\n"
        "Reply with ONLY a JSON object: {\"skill\": \"<name or none>\"}"
    )
    body = {
        "model": model, "stream": False, "think": think,
        "options": {"temperature": 0, "num_ctx": 32768},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"INSTALLED SKILLS:\n{listing}\n\nUSER MESSAGE:\n{query}"},
        ],
    }
    req = urllib.request.Request(f"{HOST}/api/chat",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        out = json.loads(r.read())
    txt = (out.get("message", {}).get("content") or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return (json.loads(m.group(0)).get("skill") or "").strip()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--eval-set", required=True)
    ap.add_argument("--model", default="gemma4:12b-mlx-32k")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--skills-dir", default=os.path.expanduser("~/.claude/skills"))
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    catalog = load_skills(a.skills_dir)
    if a.skill not in catalog:
        sys.exit(f"{a.skill} not found in {a.skills_dir}")
    evals = json.load(open(a.eval_set))

    rows, picks = [], collections.Counter()
    for item in evals:
        got = []
        for _ in range(a.runs):
            got.append(ask(a.model, catalog, item["query"], a.think))
        for g in got:
            picks[g] += 1
        hit = sum(1 for g in got if g == a.skill)
        rate = hit / len(got)
        rows.append({"query": item["query"], "should_trigger": item["should_trigger"],
                     "trigger_rate": rate, "picks": got,
                     "pass": (rate >= 0.5) == item["should_trigger"]})

    pos = [r for r in rows if r["should_trigger"]]
    neg = [r for r in rows if not r["should_trigger"]]
    f_ok, h_ok = sum(1 for r in pos if r["pass"]), sum(1 for r in neg if r["pass"])
    print(f"skill: {a.skill}   model: {a.model}   runs/query: {a.runs}   (LOCAL PROXY)")
    print(f"  should-trigger fired : {f_ok}/{len(pos)}")
    print(f"  should-NOT     held  : {h_ok}/{len(neg)}")
    print(f"  accuracy             : {f_ok + h_ok}/{len(rows)}")

    distinct = [p for p in picks if p]
    print(f"  CONTROL: {len(distinct)} distinct skills chosen across all runs; "
          f"top picks {picks.most_common(4)}")
    if len(distinct) <= 1:
        print("  !! the model is not discriminating between skills -- this run says nothing")

    for r in rows:
        if not r["pass"]:
            tag = "MISS  " if r["should_trigger"] else "FALSE+"
            print(f"    {tag} rate={r['trigger_rate']:.2f} picked={set(x for x in r['picks'])} "
                  f"{r['query'][:60]}")

    if a.json:
        json.dump({"skill": a.skill, "model": a.model, "rows": rows,
                   "fired": [f_ok, len(pos)], "held": [h_ok, len(neg)]},
                  open(a.json, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
