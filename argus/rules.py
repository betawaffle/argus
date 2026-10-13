"""Rules: what they look like, where they live, how they match and validate.

A rule (one [[rule]] table in its source's TOML file):

    name    = "i8042_probe_failed"            unique snake_case slug
    match   = { _KERNEL_SUBSYSTEM = "serio" }   optional exact field matches
    regex   = 'probe .* failed with error -(?P<err>\\d+)'   on MESSAGE
    actions = [ { block = "count", window = "day" } ]
    flagged = false                           needs human review?
    review  = ""                              why it was flagged
    created = "2026-10-13T21:40:00"
    samples = ["...", ...]                    lines it was written for (its tests)

Rules live in their own git repo (default ~/work/argus-rules), one file per
source, oldest rule first. At dispatch the first matching rule wins, so the
older rule always wins an overlap. Every change is a commit, so the repo's
history is a record of what argus learned.
"""

import json
import os
import re
import subprocess
import time
import tomllib
from pathlib import Path

from . import blocks
from .events import MATCHABLE, source
from .patterns import template

RULES_DIR = Path(os.environ.get("ARGUS_RULES", Path.home() / "work/argus-rules"))
NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
STRUCTURED = {"MESSAGE_ID", "CODE_FILE", "_AUDIT_TYPE_NAME", "PODMAN_EVENT"}
MIN_LITERAL = 6        # literal characters a regex needs when nothing else narrows it
MAX_TEMPLATES = 20     # distinct message shapes one rule may cover in a batch
MAX_SAMPLES = 5


# --- TOML ---------------------------------------------------------------------
# tomllib reads TOML but there's no writer in the stdlib. Rules only use
# strings, numbers, booleans, lists and tables, so a small one will do.

def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        # Literal strings keep regexes readable (no doubled backslashes).
        if "'" not in v and "\n" not in v and not any(ord(c) < 32 for c in v):
            return f"'{v}'"
        return json.dumps(v)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}"
                                for k, x in v.items()) + " }"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _toml_key(k):
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


FIELD_ORDER = ["name", "match", "regex", "actions", "flagged", "review",
               "created", "samples"]


def dump_rules(src, rules):
    out = [f"# argus rules for source {src!r}. Oldest first; the first match wins.",
           f"source = {_toml_value(src)}", ""]
    for r in rules:
        out.append("[[rule]]")
        keys = [k for k in FIELD_ORDER if k in r] + sorted(set(r) - set(FIELD_ORDER))
        for k in keys:
            if k == "samples":
                out.append("samples = [")
                out += [f"  {_toml_value(s)}," for s in r[k]]
                out.append("]")
            else:
                out.append(f"{_toml_key(k)} = {_toml_value(r[k])}")
        out.append("")
    return "\n".join(out)


def filename(src):
    return re.sub(r"[^A-Za-z0-9@._-]", "_", src) + ".toml"


# --- matching -----------------------------------------------------------------

class Compiled:
    def __init__(self, rule):
        self.rule = rule
        self.name = rule["name"]
        self.match = rule.get("match", {})
        self.regex = re.compile(rule["regex"]) if rule.get("regex") else None

    def test(self, event):
        """Named groups if the rule matches event, else None."""
        for k, v in self.match.items():
            if str(event.get(k)) != str(v):
                return None
        return self.test_message(event.get("MESSAGE", ""))

    def test_message(self, message):
        """Like test, but on the message alone (for stored sample lines)."""
        if self.regex is None:
            return {}
        # Capped: a model-written regex can backtrack badly on huge lines.
        m = self.regex.search(message[:4096])
        if not m:
            return None
        return {k: v for k, v in m.groupdict().items() if v is not None}


class RuleSet:
    def __init__(self, path=RULES_DIR):
        self.path = Path(path)
        self.by_source = {}   # source -> [rule dicts], oldest first
        self.compiled = {}    # source -> [Compiled]
        self.broken = {}      # rule name -> error, for rules that don't compile
        if self.path.exists():
            for f in sorted(self.path.glob("*.toml")):
                data = tomllib.loads(f.read_text())
                self.by_source[data["source"]] = data.get("rule", [])
        for src in self.by_source:
            self._compile(src)

    def _compile(self, src):
        out = []
        for r in self.by_source[src]:
            try:
                out.append(Compiled(r))
            except re.error as e:  # a hand edit gone wrong; skip, don't crash
                self.broken[r["name"]] = str(e)
        self.compiled[src] = out

    def all(self):
        return [r for rules in self.by_source.values() for r in rules]

    def get(self, name):
        for src, rules in self.by_source.items():
            for r in rules:
                if r["name"] == name:
                    return src, r
        return None, None

    def match(self, event, src=None):
        """(rule, groups) for the first matching rule, or (None, None)."""
        for c in self.compiled.get(src or source(event), ()):
            groups = c.test(event)
            if groups is not None:
                return c.rule, groups
        return None, None

    def add(self, src, rule, why):
        self.by_source.setdefault(src, []).append(rule)
        self._compile(src)
        self._save(src, f"add {rule['name']} ({src}): {why}")

    def replace(self, src, rule, why):
        rules = self.by_source[src]
        i = next(i for i, r in enumerate(rules) if r["name"] == rule["name"])
        rules[i] = rule
        self._compile(src)
        self._save(src, f"update {rule['name']} ({src}): {why}")

    def _save(self, src, message):
        self.path.mkdir(parents=True, exist_ok=True)
        f = self.path / filename(src)
        f.write_text(dump_rules(src, self.by_source[src]))
        git = ["git", "-C", str(self.path), "-c", "user.name=argus",
               "-c", "user.email=argus@localhost"]
        if not (self.path / ".git").exists():
            subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", f.name], check=True)
        subprocess.run(git + ["commit", "-q", "-m", message[:200]], check=True,
                       stdout=subprocess.DEVNULL)


# --- validation ---------------------------------------------------------------

def literal_chars(regex):
    """Rough count of literal characters in a regex: how specific it is."""
    s = re.sub(r"\(\?P<\w+>", "(", regex)
    s = re.sub(r"\\[dDsSwWbB]|\\.", "", s)
    s = re.sub(r"\[[^\]]*\]|\{[\d,]*\}", "", s)
    return len(re.sub(r"[.^$*+?()|]", "", s))


def check(rule, src, ruleset, examples, recent=()):
    """Problems with a proposed rule (empty list if it's good).

    examples: the events it was written for; it must match all of them.
    recent:   other recent events from the same source, for the broadness check.
    """
    p = []
    name = rule.get("name", "")
    if not NAME.match(name):
        p.append("name must be snake_case: lowercase letters, digits, _; 3-64 chars")
    other_src, other = ruleset.get(name)
    if other and other is not rule:
        p.append(f"name {name!r} is taken by this rule in {other_src!r}: "
                 f"{json.dumps(other)}. Pick another name, or if they describe "
                 "the same thing, propose a merged rule with replace=true.")
    match = rule.get("match", {})
    if not isinstance(match, dict):
        p.append("match must be a table of field = value")
        match = {}
    bad = [k for k in match if k not in MATCHABLE]
    if bad:
        p.append(f"can't match on {', '.join(bad)}: these change between runs or "
                 f"boots. Matchable fields: {', '.join(sorted(MATCHABLE))}")
    regex = rule.get("regex") or ""
    try:
        re.compile(regex)
    except re.error as e:
        return p + [f"regex doesn't compile: {e}"]
    if not (STRUCTURED & set(match)) and literal_chars(regex) < MIN_LITERAL:
        p.append(f"too unspecific: the regex needs at least {MIN_LITERAL} literal "
                 "characters unless match uses MESSAGE_ID or CODE_FILE")
    p += blocks.validate(rule.get("actions"))
    if p:
        return p

    c = Compiled(rule)
    missed = [e["MESSAGE"] for e in examples if c.test(e) is None]
    if missed:
        p.append(f"doesn't match {len(missed)} of the {len(examples)} example "
                 f"lines, e.g. {missed[0][:300]!r}")
    for older in ruleset.compiled.get(src, ()):
        if older.name == name:
            continue
        hits = [s for s in older.rule.get("samples", [])
                if c.test_message(s) is not None]
        if hits:
            p.append(f"overlaps with older rule {older.name!r} (it also matches "
                     f"{hits[0][:200]!r}); the older rule wins, so narrow this one "
                     f"or merge them: {json.dumps(older.rule)}")
    shapes = {template(e["MESSAGE"]) for e in recent if c.test(e) is not None}
    if len(shapes) > MAX_TEMPLATES:
        p.append(f"too broad: matches {len(shapes)} differently shaped lines from "
                 f"this source, e.g. {sorted(shapes)[:3]}")
    return p


def make(proposal, examples, flagged=False, review=""):
    """Turn a model's proposal into a stored rule."""
    rule = {"name": proposal["name"]}
    if proposal.get("match"):
        rule["match"] = proposal["match"]
    if proposal.get("regex"):
        rule["regex"] = proposal["regex"]
    rule["actions"] = proposal.get("actions") or [{"block": "collect"}]
    rule["flagged"] = flagged
    if review:
        rule["review"] = review
    rule["created"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rule["samples"] = list(dict.fromkeys(e["MESSAGE"][:500] for e in examples))[:MAX_SAMPLES]
    return rule


def fallback(examples, name, why):
    """When no proper rule could be made: match only this exact line, collect
    its matches and flag it for review."""
    e = examples[0]
    return make({"name": name, "regex": "^" + re.escape(e["MESSAGE"]) + "$",
                 "actions": [{"block": "collect"}]},
                [e], flagged=True, review=why)
