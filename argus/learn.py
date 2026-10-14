"""The slow path: ask the model to write a rule for lines no rule matches.

The model gets a group of similar unmatched events from one source, the
rules that source already has, and tools:

  propose_rule   submit a rule; argus validates it and answers accepted or
                 with the problems, so the model can fix them and retry
  test_regex     try a regex on the example lines before proposing
  run            the usual read-only commands, to judge how much to care

If no valid rule comes out of it, argus stores a fallback that matches only
the exact line, collects what it matches and is flagged for review.
"""

import json
import re
from types import SimpleNamespace

from . import blocks, rules, tools
from .agent import Agent
from .events import MATCHABLE, summarize

MAX_PROPOSALS = 4

SYSTEM = f"""You are argus, which watches the systemd journal of unattended
Linux machines. "local" is a Fedora workstation used for ML experiments;
"moneta" is a NixOS NAS with ZFS.

argus handles every journal line with rules. You are shown lines that no rule
matches yet, all from one source and similar to each other. Write ONE rule
that matches these lines and future lines like them, and decide what argus
should do with such lines from now on.

A rule has:
- name: unique snake_case slug saying what the lines are, e.g. sshd_auth_failed
- match: optional exact field values, e.g. {{"MESSAGE_ID": "..."}} or
  {{"_KERNEL_SUBSYSTEM": "usb"}}. Only these fields: {", ".join(sorted(MATCHABLE))}.
  Prefer MESSAGE_ID or CODE_FILE + CODE_LINE when the lines have them: exact
  and cheap.
- regex: Python regex searched in MESSAGE. A MESSAGE can hold several lines
  when argus has joined a traceback into one event; then match the
  exception line, e.g. (?m)^KeyError: (?P<key>.*)$. Use named groups (?P<name>...) for
  the parts that vary (numbers, devices, users, addresses, paths) so actions
  can use them. Keep enough fixed text that it only matches this kind of
  line, not everything from the source.
- actions: a list of blocks, each {{"block": name, ...params}}:
{blocks.describe_blocks()}

How to choose actions:
- routine, expected chatter: ignore, or count if the volume might matter
- errors and warnings: count, plus threshold to flag when it gets frequent;
  flag directly (attention) only for things a human must fix soon
- changing state (link up/down, pool health, versions): track with on_change
- new users, devices, addresses: distinct with on_new
- something you can't judge from the lines alone: investigate
- collect only lines a human should read later (errors, rare events), not
  routine chatter; never combine ignore with other blocks
- severity attention is for things that need fixing soon; routine changes
  (DHCP leases, NAT endpoints, ports) are watch at most, or just track
- distinct and track need a field that repeats (a unit, user, device), not
  one that differs on every line (timestamps, uptimes, counters, ports)
Use the run tool to check the machine if that helps you judge.

Call propose_rule when ready. If it reports problems, fix them and call it
again. When it says accepted, reply with one short sentence and stop.
If a needed building block doesn't exist, use it anyway with sensible
parameters: the rule will be flagged for a human to review."""


def _schema(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


SCHEMAS = [
    _schema("propose_rule", "Submit a rule. Returns accepted, or problems to fix.", {
        "name": {"type": "string"},
        "match": {"type": "object", "description": "field -> exact value"},
        "regex": {"type": "string"},
        "actions": {"type": "array", "items": {"type": "object"}},
        "replace": {"type": "boolean", "description":
                    "true to replace the existing rule with this name (a merge)"},
        "reason": {"type": "string", "description": "one line: what and why"},
    }, ["name", "actions", "reason"]),
    _schema("test_regex", "Show which example lines a regex matches, with groups.", {
        "regex": {"type": "string"}}, ["regex"]),
    tools.SCHEMAS[0],
]


class Learner:
    def __init__(self, ruleset, llm=None):
        self.ruleset = ruleset
        self.llm = llm

    def learn(self, src, examples, recent=()):
        """Make a rule for examples (events from src). Returns (rule, accepted,
        transcript path). Always returns a rule: a fallback if all else fails."""
        outcome = {"rule": None, "problems": [], "proposals": 0}

        def propose_rule(name, actions, reason="", match=None, regex=None,
                         replace=False):
            outcome["proposals"] += 1
            if outcome["proposals"] > MAX_PROPOSALS:
                return "problem: too many attempts; argus will store a fallback. Stop now."
            proposal = {"name": name, "match": match or {}, "regex": regex,
                        "actions": actions}
            rule = rules.make(proposal, examples)
            existing_src, existing = self.ruleset.get(name)
            if replace and existing is not None:
                if existing_src != src:
                    return f"problem: {name} belongs to source {existing_src}, not {src}"
                # A merge must still handle what the old rule was written for.
                rule["samples"] = list(dict.fromkeys(
                    existing.get("samples", []) + rule["samples"]))[:rules.MAX_SAMPLES]
                old_samples = [{"MESSAGE": s} for s in existing.get("samples", [])]
                problems = rules.check(rule, src, _without(self.ruleset, name),
                                       examples, recent)
                if not problems and rule.get("regex"):
                    c = rules.Compiled(rule)
                    missed = [s for s in old_samples
                              if c.test_message(s["MESSAGE"]) is None]
                    if missed:
                        problems.append("the merged rule no longer matches the old "
                                        f"rule's sample {missed[0]['MESSAGE'][:200]!r}")
            else:
                problems = rules.check(rule, src, self.ruleset, examples, recent)
            unknown = [p for p in problems if "unknown block" in p]
            real = [p for p in problems if p not in unknown]
            if real:
                outcome["problems"] = real
                if outcome["proposals"] >= MAX_PROPOSALS:
                    return "problem: too many attempts; stop now."
                return "problems:\n- " + "\n- ".join(real)
            if unknown:  # valid apart from blocks we don't have yet
                rule["flagged"] = True
                rule["review"] = "proposes new building blocks: " + "; ".join(unknown)
            why = reason or "learned"
            if replace and existing is not None:
                self.ruleset.replace(src, rule, why)
            else:
                self.ruleset.add(src, rule, why)
            outcome["rule"] = rule
            return "accepted" + (" (flagged for review: new building block)" if unknown else "")

        def test_regex(regex):
            try:
                rx = re.compile(regex)
            except re.error as e:
                return f"doesn't compile: {e}"
            out = []
            for e in examples[:10]:
                m = rx.search(e["MESSAGE"])
                out.append(f"{'MATCH' if m else 'no   '} {e['MESSAGE'][:200]!r}"
                           + (f" groups={m.groupdict()}" if m and m.groupdict() else ""))
            others = sum(1 for e in recent if rx.search(e["MESSAGE"]))
            out.append(f"also matches {others} of {len(recent)} other recent lines "
                       "from this source")
            return "\n".join(out)

        toolset = SimpleNamespace(
            SCHEMAS=SCHEMAS,
            FUNCS={"propose_rule": propose_rule, "test_regex": test_regex,
                   "run": tools.run})
        agent = Agent(SYSTEM, llm=self.llm, tools=toolset, max_steps=14)
        try:
            agent.run(self._task(src, examples), on_event=None)
        except Exception as e:  # model or server trouble: fall back, keep going
            outcome["problems"].append(f"error while learning: {e}")
        if outcome["rule"]:
            return outcome["rule"], True, agent.transcript
        why = ("no valid rule: " + "; ".join(outcome["problems"])[:500]
               if outcome["problems"] else "the model didn't propose a rule")
        rule = rules.fallback(examples, self._fallback_name(src), why)
        self.ruleset.add(src, rule, "fallback")
        return rule, False, agent.transcript

    def _task(self, src, examples):
        existing = self.ruleset.by_source.get(src, [])
        lines = [f"Source: {src}", f"Unmatched lines in this group: {len(examples)}",
                 "", "Example events (volatile fields removed):"]
        seen = set()
        for e in examples:
            if e["MESSAGE"] in seen:
                continue
            seen.add(e["MESSAGE"])
            lines.append(json.dumps(summarize(e)))
            if len(seen) >= 6:
                break
        if existing:
            lines += ["", f"Existing rules for {src} (older first; first match wins):"]
            for r in existing[-30:]:
                lines.append(json.dumps({k: r[k] for k in ("name", "match", "regex", "actions")
                                         if k in r}))
        return "\n".join(lines)

    def _fallback_name(self, src):
        base = re.sub(r"[^a-z0-9]+", "_", src.lower()).strip("_")[:40] or "unknown"
        n = 1
        while self.ruleset.get(f"unlearned_{base}_{n}")[1]:
            n += 1
        return f"unlearned_{base}_{n}"


def _without(ruleset, name):
    """A view of ruleset minus one rule, for validating a replacement."""
    view = SimpleNamespace(
        compiled={s: [c for c in cs if c.name != name] for s, cs in ruleset.compiled.items()},
        get=lambda n: (None, None) if n == name else ruleset.get(n))
    return view
