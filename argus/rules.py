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
from .patterns import PRIORITY_NAMES, template

RULES_DIR = Path(os.environ.get("ARGUS_RULES", Path.home() / "work/argus-rules"))
NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
STRUCTURED = {"MESSAGE_ID", "CODE_FILE", "_AUDIT_TYPE_NAME", "PODMAN_EVENT"}
MIN_LITERAL = 6        # literal characters a regex needs when nothing else narrows it
FALLBACK_KEEP = 20     # matching lines a fallback keeps so a human can see them
MAX_TEMPLATES = 20     # shapes a rule may cover before it's flagged for review
TOO_BROAD = 0.95       # share of a source's shapes a rule may never cover
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


FIELD_ORDER = ["name", "match", "regex", "actions", "flagged", "fallback", "review",
               "rejected", "created", "samples"]


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
        rules = self.by_source.setdefault(src, [])
        rules.append(rule)
        # A real rule replaces the fallbacks it covers: they'd match first.
        dropped = []
        if not is_fallback(rule):
            c = Compiled(rule)
            for r in list(rules):
                if is_fallback(r) and any(c.test_message(s) is not None
                                          for s in r.get("samples", [])):
                    rules.remove(r)
                    dropped.append(r["name"])
        self._compile(src)
        self._save(src, f"add {rule['name']} ({src}): {why}"
                   + (f"; replaces {', '.join(dropped)}" if dropped else ""))

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


def check(rule, src, ruleset, examples, recent=(), hours=None):
    """Problems with a proposed rule (empty list if it's good).

    examples: the events it was written for; it must match all of them.
    recent:   this batch's events from the same source, for the broadness and
              threshold checks.
    hours:    how long the batch covers; thresholds are only checked when it
              is long enough to say what a normal rate is.
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
    narrowing = STRUCTURED & set(match)
    if src.endswith("/container"):
        # All container output is forwarded by the same conmon call, so
        # CODE_FILE/CODE_LINE there say nothing about the line.
        narrowing -= {"CODE_FILE"}
    if not narrowing and literal_chars(regex) < MIN_LITERAL:
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
        if older.name == name or is_fallback(older.rule):
            continue  # a covered fallback is dropped when this rule is added
        hits = [s for s in older.rule.get("samples", [])
                if c.test_message(s) is not None]
        if hits:
            p.append(f"overlaps with older rule {older.name!r} (it also matches "
                     f"{hits[0][:200]!r}); the older rule wins. Narrow this one, or "
                     f"merge: propose_rule with name={older.name!r}, replace=true and "
                     f"a regex covering both. Older rule: {json.dumps(older.rule)}")
    p += identity_problems(rule, c, examples, recent)
    if hours:
        p += threshold_problems(rule, c, recent, hours)
    p += broadness_problems(c, src, examples, recent)
    all_shapes = {template(e["MESSAGE"]) for e in recent}
    shapes = {template(e["MESSAGE"]) for e in recent if c.test(e) is not None}
    if len(shapes) > MAX_TEMPLATES and len(shapes) > TOO_BROAD * len(all_shapes):
        p.append(f"too broad: matches {len(shapes)} of the {len(all_shapes)} "
                 f"differently shaped lines from this source, e.g. {sorted(shapes)[:3]}")
    elif len(shapes) > MAX_TEMPLATES:
        # One rule for one kind of line (all GET requests, say) is what we
        # want, so this isn't a rejection; a human takes a look.
        p.append(f"review: broad, covers {len(shapes)} differently shaped lines "
                 f"from this source, e.g. {sorted(shapes)[:3]}")
    return p


def threshold_problems(rule, c, recent, hours):
    """A threshold that a normal day reaches alerts every day; one far above
    normal never fires. Judged from what the regex matches in this batch."""
    p = []
    thresholds = [a for a in blocks.walk(rule.get("actions"))
                  if a.get("block") == "threshold" and isinstance(a.get("n"), int)]
    if not thresholds:
        return p
    hits = sum(1 for e in recent if c.test(e) is not None)
    for a in thresholds:
        window = a.get("window", "day")
        normal = hits / hours * blocks.WINDOWS.get(window, 86400) / 3600
        good = max(5, round(normal * 4))
        if a["n"] < 2 * normal:
            p.append(f"threshold n={a['n']} per {window} is too low: this rule "
                     f"matched {hits} lines in {hours:.1f} hours, about "
                     f"{normal:.0f} per {window} normally, so it would alert on a "
                     f"normal {window}. Use about n={good}.")
        elif a["n"] > 20 and a["n"] > 10 * max(normal, 1):
            p.append(f"threshold n={a['n']} per {window} is too high: this rule "
                     f"matched {hits} lines in {hours:.1f} hours, about "
                     f"{normal:.0f} per {window}, so it would never fire. "
                     f"Use about n={good}.")
    return p


def _priority(e):
    try:
        return int(e.get("PRIORITY"))
    except (TypeError, ValueError):
        return None  # audit entries have none


def broadness_problems(c, src, examples, recent=()):
    """Is the rule so broad it would swallow lines it wasn't written for?

    Covering many differently shaped lines is fine (routine chatter from one
    daemon comes in many shapes; check() flags that for review and rejects
    only nearly all of a source's shapes). Sweeping in lines more severe than
    the examples (warnings or errors when it was written for info lines) is
    not: it would hide trouble.
    """
    p = []
    matched = [e for e in recent if c.test(e) is not None]
    if not matched:
        return p
    own = {template(e["MESSAGE"]) for e in examples}
    extra = [e for e in matched if template(e["MESSAGE"]) not in own]
    if not src.endswith("/container"):  # podman logs all container output at err
        mine = [x for x in map(_priority, examples) if x is not None]
        floor = min(mine) if mine else None
        worse = [e for e in extra if floor is not None and _priority(e) is not None
                 and _priority(e) < floor and _priority(e) <= 4]
        if worse:
            name = PRIORITY_NAMES[_priority(worse[0])]
            p.append(f"too broad: written for priority {PRIORITY_NAMES[floor]} lines but "
                     f"also matches {len(worse)} more severe ones, e.g. ({name}) "
                     f"{worse[0]['MESSAGE'][:200]!r}. Don't let it swallow warnings "
                     "or errors; narrow it.")
    return p


def identity_problems(rule, c, examples, recent=()):
    """distinct/track on a field that differs on nearly every line (a
    timestamp, a counter) would raise an alert per line."""
    p = []
    lines = list({id(e): e for e in [*examples, *recent]}.values())
    for a in blocks.walk(rule.get("actions")):
        if a.get("block") not in ("distinct", "track") or not (
                a.get("on_new") or a.get("on_change")):
            continue
        f = a.get("field")
        values = []
        for e in lines:
            g = c.test(e)
            if g is not None and g.get(f, e.get(f)) is not None:
                values.append(str(g.get(f, e.get(f))))
        if len(values) >= 5 and len(set(values)) > 0.8 * len(values):
            p.append(f"{a['block']} on {f!r}: it has {len(set(values))} different "
                     f"values in {len(values)} lines (e.g. {values[0]!r}, "
                     f"{values[1]!r}), so {'on_new' if a['block'] == 'distinct' else 'on_change'}"
                     " would fire on almost every line. That field isn't an "
                     "identity; use extract or count for it, or pick a field "
                     "that repeats.")
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


# What each template placeholder can stand for, as a regex.
PLACEHOLDERS = [
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
     r"[0-9a-fA-F-]{36}"),
    ("ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), r"[0-9.]+(?::\d+)?"),
    ("ip6", re.compile(r"\b[0-9a-f]*:[0-9a-f]*:[0-9a-f:]+\b", re.I), r"[0-9a-fA-F:]+"),
    ("hex", re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), r"(?:0x)?[0-9a-fA-F]+"),
    ("dur", re.compile(r"(?<![\d.])(?:\d+(?:\.\d+)?(?:ms|us|µs|ns|h|m|s|d))+(?![A-Za-z\d])"),
     r"[\d.a-zµ]+"),
    ("n", re.compile(r"(?<![A-Za-z_\d])\d+(?:\.\d+)?"), r"\d+(?:\.\d+)?"),
]


def template_regex(message):
    """A regex for message's whole template group: the fixed text escaped,
    and each number, address or id a named group (n1, ip1, hex1, ...)."""
    spans = []
    for kind, rx, _ in PLACEHOLDERS:  # same precedence as patterns.SUBS
        for m in rx.finditer(message):
            if not any(m.start() < e and s < m.end() for s, e, _ in spans):
                spans.append((m.start(), m.end(), kind))
    out, pos, n = [], 0, {}
    for s, e, kind in sorted(spans):
        n[kind] = n.get(kind, 0) + 1
        out.append(re.escape(message[pos:s]))
        out.append(f"(?P<{kind}{n[kind]}>{dict((k, r) for k, _, r in PLACEHOLDERS)[kind]})")
        pos = e
    out.append(re.escape(message[pos:]))
    return "^" + "".join(out) + "$"


def fallback(examples, name, why, rejected=None):
    """When no proper rule could be made: a flagged rule for this group of
    lines that counts them and keeps the first few for a human to read.

    It matches the group's template (numbers, addresses and ids as named
    groups), so one fallback covers the whole group instead of one exact
    line. If that doesn't match every example, it falls back to the exact
    first line."""
    regex = template_regex(examples[0]["MESSAGE"])
    c = re.compile(regex)
    if any(not c.search(e["MESSAGE"][:4096]) for e in examples):
        regex = "^" + re.escape(examples[0]["MESSAGE"]) + "$"
    rule = make({"name": name, "regex": regex,
                 "actions": [{"block": "count", "window": "day"},
                             {"block": "collect", "limit": FALLBACK_KEEP}]},
                examples, flagged=True, review=why)
    rule["fallback"] = True
    if rejected:
        rule["rejected"] = rejected

    return rule


def is_fallback(rule):
    return bool(rule.get("fallback")) or rule.get("name", "").startswith("unlearned_")
