"""Building blocks: what a rule does with the events it matches.

A rule's action is a list of blocks, each a dict with a "block" key and that
block's parameters, e.g.

    [{block = "count", window = "day"},
     {block = "threshold", n = 5, window = "day",
      then = {block = "flag", severity = "watch", summary = "..."}}]

Blocks get the whole event plus the regex's named groups. While a rule
handles the lines of the run it was learned in, ctx["quiet"] is set:
distinct and track record values without running on_new/on_change, so a new
rule doesn't call everything it sees first "new". Their state lives
in SQLite (State below), never in the rules repo.
"""

import json
import sqlite3
import time

WINDOWS = {"hour": 3600, "day": 86400, "week": 7 * 86400}
SEVERITIES = ("watch", "attention")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_stats (
    rule TEXT PRIMARY KEY, hits INTEGER, first_hit REAL, last_hit REAL);
CREATE TABLE IF NOT EXISTS counters (
    rule TEXT, key TEXT, window_start REAL, count INTEGER,
    PRIMARY KEY (rule, key, window_start));
CREATE TABLE IF NOT EXISTS extracts (
    id INTEGER PRIMARY KEY, rule TEXT, t REAL, data TEXT);
CREATE TABLE IF NOT EXISTS collected (
    id INTEGER PRIMARY KEY, rule TEXT, t REAL, cursor TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY, rule TEXT, t REAL, severity TEXT, summary TEXT,
    message TEXT, cursor TEXT, data TEXT, acked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY, rule TEXT, t REAL, status TEXT, events TEXT,
    report TEXT, transcript TEXT);
CREATE TABLE IF NOT EXISTS tracked (
    rule TEXT, field TEXT, value TEXT, t REAL, PRIMARY KEY (rule, field));
CREATE TABLE IF NOT EXISTS distinct_values (
    rule TEXT, field TEXT, value TEXT, first_seen REAL,
    PRIMARY KEY (rule, field, value));
CREATE INDEX IF NOT EXISTS collected_rule ON collected (rule);
CREATE INDEX IF NOT EXISTS extracts_rule ON extracts (rule, t);
"""


class State:
    def __init__(self, db):
        self.db = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(db)
        self.db.executescript(SCHEMA)

    def hit(self, rule, t):
        self.db.execute(
            "INSERT INTO rule_stats VALUES (?, 1, ?, ?) ON CONFLICT DO UPDATE SET "
            "hits = hits + 1, last_hit = max(last_hit, excluded.last_hit)",
            (rule, t, t))

    def alert(self, rule, severity, summary, ctx):
        e = ctx["event"]
        self.db.execute(
            "INSERT INTO alerts (rule, t, severity, summary, message, cursor, data) "
            "VALUES (?,?,?,?,?,?,?)",
            (rule, ctx["t"], severity, summary, e.get("MESSAGE", "")[:2000],
             e.get("__CURSOR"), json.dumps(ctx["groups"])))


def event_time(event):
    try:
        return int(event["__REALTIME_TIMESTAMP"]) / 1e6
    except (KeyError, ValueError):
        return time.time()


def window_start(t, window):
    if window == "day":  # local midnight, so "per day" means what you'd expect
        lt = time.localtime(t)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    size = WINDOWS[window]
    return t - t % size


# --- the blocks -------------------------------------------------------------
# Each takes (params, ctx, key). ctx has rule, event, groups, t, state.
# key identifies this block's position in the action, so two count blocks in
# one rule keep separate counters.

def _ignore(p, ctx, key):
    pass


def _bump(ctx, key, window):
    start = window_start(ctx["t"], window)
    db = ctx["state"].db
    db.execute("INSERT INTO counters VALUES (?,?,?,1) ON CONFLICT DO UPDATE "
               "SET count = count + 1", (ctx["rule"], key, start))
    return db.execute("SELECT count FROM counters WHERE rule=? AND key=? AND "
                      "window_start=?", (ctx["rule"], key, start)).fetchone()[0]


def _count(p, ctx, key):
    _bump(ctx, key, p.get("window", "day"))


def _threshold(p, ctx, key):
    if _bump(ctx, key, p.get("window", "day")) == p["n"]:  # fire once per window
        sub = {**ctx, "groups": {**ctx["groups"], "n": p["n"]}}
        run_actions(p["then"], sub, key + ".then")


def _extract(p, ctx, key):
    fields = p.get("fields")
    data = dict(ctx["groups"])
    for f in fields or ():
        if f in ctx["event"]:
            data[f] = ctx["event"][f]
    if fields:
        data = {k: v for k, v in data.items() if k in fields}
    ctx["state"].db.execute("INSERT INTO extracts (rule, t, data) VALUES (?,?,?)",
                            (ctx["rule"], ctx["t"], json.dumps(data)))


class _Keep(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _flag(p, ctx, key):
    summary = p.get("summary", "")
    try:  # summaries may mention named groups: "disk {dev} failing"
        # Unknown placeholders stay as written instead of spoiling the rest.
        summary = summary.format_map(_Keep(ctx["groups"]))
    except (IndexError, ValueError):
        pass
    ctx["state"].alert(ctx["rule"], p.get("severity", "watch"), summary, ctx)


def _collect(p, ctx, key):
    db, rule = ctx["state"].db, ctx["rule"]
    limit = p.get("limit", 200)
    n = db.execute("SELECT count(*) FROM collected WHERE rule=?", (rule,)).fetchone()[0]
    if n < limit:  # keep the first N; the rule's hit counter has the total
        e = ctx["event"]
        db.execute("INSERT INTO collected (rule, t, cursor, message) VALUES (?,?,?,?)",
                   (rule, ctx["t"], e.get("__CURSOR"), e.get("MESSAGE", "")[:4000]))


def _investigate(p, ctx, key):
    # Queued, not run inline: an investigation takes the model 10-60 s.
    # A rule only queues one at a time; later hits join the open one.
    db, rule = ctx["state"].db, ctx["rule"]
    from .events import summarize
    ev = summarize(ctx["event"])
    row = db.execute("SELECT id, events FROM investigations WHERE rule=? AND "
                     "status='queued'", (rule,)).fetchone()
    if row:
        evs = json.loads(row[1])
        if len(evs) < 10:
            db.execute("UPDATE investigations SET events=? WHERE id=?",
                       (json.dumps(evs + [ev]), row[0]))
    else:
        db.execute("INSERT INTO investigations (rule, t, status, events) "
                   "VALUES (?,?,'queued',?)", (rule, ctx["t"], json.dumps([ev])))


def _value(p, ctx):
    f = p["field"]
    return ctx["groups"].get(f, ctx["event"].get(f))


def _track(p, ctx, key):
    value = _value(p, ctx)
    if value is None:
        return
    db, rule = ctx["state"].db, ctx["rule"]
    row = db.execute("SELECT value FROM tracked WHERE rule=? AND field=?",
                     (rule, p["field"])).fetchone()
    db.execute("INSERT OR REPLACE INTO tracked VALUES (?,?,?,?)",
               (rule, p["field"], str(value), ctx["t"]))
    if row and row[0] != str(value) and "on_change" in p and not ctx.get("quiet"):
        sub = {**ctx, "groups": {**ctx["groups"], "old": row[0], "new": str(value)}}
        run_actions(p["on_change"], sub, key + ".on_change")


def _distinct(p, ctx, key):
    value = _value(p, ctx)
    if value is None:
        return
    cur = ctx["state"].db.execute(
        "INSERT OR IGNORE INTO distinct_values VALUES (?,?,?,?)",
        (ctx["rule"], p["field"], str(value), ctx["t"]))
    if cur.rowcount and "on_new" in p and not ctx.get("quiet"):
        sub = {**ctx, "groups": {**ctx["groups"], "new": str(value)}}
        run_actions(p["on_new"], sub, key + ".on_new")


# name -> (function, {param: (type, required)}, description for the model)
BLOCKS = {
    "ignore": (_ignore, {}, "Do nothing. The rule's hit counter still counts."),
    "count": (_count, {"window": (str, False)},
              "Count matches per window (hour, day, week; default day)."),
    "threshold": (_threshold, {"n": (int, True), "window": (str, False),
                               "then": (object, True)},
                  "When the n-th match in a window arrives, run `then` "
                  "(a block or list of blocks). Fires once per window. "
                  "{n} available to flag."),
    "extract": (_extract, {"fields": (list, False)},
                "Store the regex's named groups (plus any listed event fields) "
                "as a row, for later analysis."),
    "flag": (_flag, {"severity": (str, True), "summary": (str, True)},
             "Raise an alert on the page. severity: watch or attention. "
             "summary may use {group} placeholders."),
    "collect": (_collect, {"limit": (int, False)},
                "Keep the matching lines (first `limit`, default 200)."),
    "investigate": (_investigate, {},
                    "Queue an investigation: the model looks into it with "
                    "read-only commands and writes a report for a human."),
    "track": (_track, {"field": (str, True), "on_change": (object, False)},
              "Remember the latest value of a field or group; run `on_change` "
              "when it changes ({old} and {new} available to flag)."),
    "distinct": (_distinct, {"field": (str, True), "on_new": (object, False)},
                 "Remember every value of a field or group seen; run `on_new` "
                 "for a value never seen before ({new} available to flag)."),
}


def walk(actions):
    """Every block in actions, including nested then/on_change/on_new."""
    if isinstance(actions, dict):
        actions = [actions]
    for a in actions if isinstance(actions, list) else ():
        if isinstance(a, dict):
            yield a
            for sub in ("then", "on_change", "on_new"):
                if sub in a:
                    yield from walk(a[sub])


def validate(actions, path="actions"):
    """Return a list of problems with an action (empty if fine)."""
    if isinstance(actions, dict):
        actions = [actions]
    if not isinstance(actions, list) or not actions:
        return [f"{path}: must be a block or a non-empty list of blocks"]
    problems = []
    for i, a in enumerate(actions):
        where = f"{path}[{i}]"
        if not isinstance(a, dict) or "block" not in a:
            problems.append(f"{where}: each block is a table with a 'block' key")
            continue
        name = a["block"]
        if name not in BLOCKS:
            problems.append(f"{where}: unknown block {name!r}")
            continue
        params = BLOCKS[name][1]
        for p, (typ, required) in params.items():
            if required and p not in a:
                problems.append(f"{where}: {name} needs {p!r}")
            elif p in a and typ is not object and not isinstance(a[p], typ):
                problems.append(f"{where}: {name}.{p} must be {typ.__name__}")
        for p in a:
            if p != "block" and p not in params:
                problems.append(f"{where}: {name} has no parameter {p!r}")
        if a.get("window", "day") not in WINDOWS:
            problems.append(f"{where}: window must be one of {', '.join(WINDOWS)}")
        if name == "flag" and a.get("severity") not in SEVERITIES:
            problems.append(f"{where}: severity must be watch or attention")
        for sub in ("then", "on_change", "on_new"):
            if sub in a:
                problems += validate(a[sub], f"{where}.{sub}")
    return problems


def run_actions(actions, ctx, key="a"):
    if isinstance(actions, dict):
        actions = [actions]
    for i, a in enumerate(actions):
        spec = BLOCKS.get(a.get("block"))
        if spec:  # unknown blocks are skipped; the rule is flagged for review
            spec[0](a, ctx, f"{key}{i}")


def describe_blocks():
    return "\n".join(f"- {name}({', '.join(p + ('' if req else '?') for p, (_, req) in params.items())}): {doc}"
                     for name, (_, params, doc) in BLOCKS.items())
