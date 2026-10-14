"""The batch run: read new journal events, dispatch them through the rules,
and learn rules for whatever nothing matched.

    python -m argus rules run       one batch (the timer runs this)

Flow of one run:
1. Read events after the saved cursor. The first run only records the
   current end of the journal: watching starts from "now". Backfill of older
   history is a separate step.
2. Each event goes to its source's rules; the first match wins and its
   actions run. Flagged rules also collect their lines until reviewed.
   Unit-stdout copies of container lines are skipped (see
   events.mark_mirrored), and traceback lines are joined into one event
   (events.join_multiline).
3. Unmatched events are grouped by source and rough shape. The biggest
   groups (up to --learn) go to the model, one group per rule. After each
   new rule, the remaining groups are re-checked against it.
4. Groups not learned this run are kept as pending (events in SQLite) and
   come first next run.
5. Queued investigations run last (up to --investigate).
"""

import bisect
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict

from . import blocks, events
from .agent import DATA_DIR, Agent
from .learn import Learner
from .llm import own_requests as llm_requests, prune_requests
from .patterns import template
from .rules import RuleSet

SCHEMA = """
CREATE TABLE IF NOT EXISTS engine_cursors (host TEXT PRIMARY KEY, cursor TEXT);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY, host TEXT, source TEXT, shape TEXT, t REAL, event TEXT);
CREATE TABLE IF NOT EXISTS learned (
    id INTEGER PRIMARY KEY, t REAL, host TEXT, source TEXT, rule TEXT,
    accepted INTEGER, lines INTEGER, transcript TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, t REAL, host TEXT, events INTEGER, matched INTEGER,
    learned INTEGER, pending INTEGER, seconds REAL);
"""
PENDING_PER_GROUP = 20   # events kept per pending group; enough to learn from
SOURCE_LINES = 50000     # lines kept per source per batch for rule checks
# The unit serving the model. Its unmatched lines written while argus's own
# requests ran are argus talking to itself and are never learned from.
LLM_UNIT = os.environ.get("ARGUS_LLM_UNIT", "llm-server.service")
OWN_SLACK = (2, 10)      # seconds before/after a request that still count

INVESTIGATE_SYSTEM = """You are argus, watching unattended Linux machines.
"local" is a Fedora workstation used for ML experiments; "moneta" is a NixOS
NAS with ZFS. A rule asked for these journal events to be investigated.
Look into them with the run tool (read-only), then write a short report for
the owner: what happened, whether it matters, and what (if anything) they
should do. End with one line: VERDICT: ignore|watch|attention - <summary>"""


class Engine:
    def __init__(self, host="local", db=None, ruleset=None, llm=None, log=print):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.db = db or sqlite3.connect(DATA_DIR / "argus.db")
        self.db.executescript(SCHEMA)
        self.state = blocks.State(self.db)
        self.rules = ruleset or RuleSet()
        self.llm = llm
        self.learner = Learner(self.rules, llm=llm)
        self.log = log
        self.new_rules = set()   # learned this run: their first lines are a baseline

    # --- cursor ---------------------------------------------------------------
    def cursor(self):
        row = self.db.execute("SELECT cursor FROM engine_cursors WHERE host=?",
                              (self.host,)).fetchone()
        return row[0] if row else None

    def set_cursor(self, cursor):
        self.db.execute("INSERT OR REPLACE INTO engine_cursors VALUES (?,?)",
                        (self.host, cursor))

    # --- dispatch -------------------------------------------------------------
    def dispatch(self, event, src=None):
        """Run the matching rule's actions. True if a rule matched."""
        src = src or events.source(event)
        rule, groups = self.rules.match(event, src)
        if rule is None:
            return False
        t = blocks.event_time(event)
        self.state.hit(rule["name"], t)
        self.state.keep_example(rule["name"], event, t)
        ctx = {"rule": rule["name"], "event": event, "groups": groups,
               "t": t, "state": self.state, "quiet": rule["name"] in self.new_rules}
        actions = rule.get("actions", [])
        if rule.get("flagged") and not any(a.get("block") == "collect" for a in actions):
            actions = actions + [{"block": "collect"}]
        blocks.run_actions(actions, ctx)
        return True

    # --- one batch ------------------------------------------------------------
    def run(self, learn_limit=10, investigate_limit=3, events_iter=None, since=None):
        t0 = time.time()
        cursor = self.cursor()
        if since and events_iter is None:  # testing: read a window, ignore the cursor
            events_iter = events.read(self.host, since=since)
        if cursor is None and events_iter is None:
            self.set_cursor(events.latest_cursor(self.host))
            self.db.commit()
            self.log("first run: watching from now on")
            return
        groups = defaultdict(list)   # (source, shape) -> events
        n = matched = mirrored = own = 0
        self._own = llm_requests(time.time() - 3 * 86400)
        for e in self._pending():
            self._sort(e, groups)
        source_lines = defaultdict(list)   # this batch's lines per source, for checks
        t_first = t_last = None
        stream = events_iter if events_iter is not None else events.read(self.host, cursor)
        for e in events.join_multiline(events.mark_mirrored(stream)):
            n += 1
            cursor = e.get("__CURSOR", cursor)
            if e.get("__MIRRORED"):  # the container copy of the line is handled
                mirrored += 1
                continue
            src = events.source(e)
            t = blocks.event_time(e)
            t_first, t_last = t_first or t, t
            if len(source_lines[src]) < SOURCE_LINES:
                source_lines[src].append(e)
            if self.dispatch(e, src):
                matched += 1
            elif self._is_own(src, t):
                own += 1
            else:
                groups[(src, template(e["MESSAGE"]))].append(e)
            if n % 5000 == 0:
                self.set_cursor(cursor)
                self.db.commit()
        if cursor:
            self.set_cursor(cursor)
        self.db.commit()
        self.log(f"{n} events, {matched} matched, {mirrored} container echoes "
                 f"skipped, {own} unmatched lines from argus's own model requests "
                 f"skipped, {len(groups)} unmatched groups")

        # Hours the batch covers: rules' rates (and so thresholds) are judged
        # against it. Under an hour says too little about a normal day.
        hours = (t_last - t_first) / 3600 if t_first else 0
        self.learner.hours = hours if hours >= 1 else None
        learned, tried = 0, set()
        while learned < learn_limit:
            todo = {k: v for k, v in groups.items() if k not in tried}
            if not todo:
                break
            (src, shape), evs = max(todo.items(), key=lambda kv: len(kv[1]))
            tried.add((src, shape))
            self.log(f"learning [{src}] {len(evs)}x {shape[:100]!r}")
            others = sorted(((k[1], len(v)) for k, v in groups.items()
                             if k[0] == src and k != (src, shape)), key=lambda x: -x[1])
            rule, ok, transcript = self.learner.learn(src, evs, source_lines.get(src, []),
                                                      others=others)
            learned += 1
            self.new_rules.add(rule["name"])
            self.db.execute(
                "INSERT INTO learned (t, host, source, rule, accepted, lines, "
                "transcript) VALUES (?,?,?,?,?,?,?)",
                (time.time(), self.host, src, rule["name"], int(ok), len(evs),
                 str(transcript)))
            self.log(f"  -> {'rule' if ok else 'FALLBACK'} {rule['name']}: "
                     f"{json.dumps(rule.get('actions'))}")
            # The new rule handles its own lines, and maybe other groups too.
            # (A fallback matches its group's template, so it covers the
            # group; anything it misses waits in pending rather than being
            # learned again this run.)
            for key in [k for k in groups if k[0] == src]:
                left = [e for e in groups[key] if not self.dispatch(e, src)]
                if left:
                    groups[key] = left
                else:
                    del groups[key]
            self.db.commit()

        self._save_pending(groups)
        prune_requests(time.time() - 3 * 86400)
        self.db.execute(
            "INSERT INTO runs (t, host, events, matched, learned, pending, seconds) "
            "VALUES (?,?,?,?,?,?,?)",
            (t0, self.host, n, matched, learned, sum(map(len, groups.values())),
             round(time.time() - t0, 1)))
        self.db.commit()
        self.investigate(investigate_limit)

    def _is_own(self, src, t):
        """Was this line from the model server, written during one of argus's
        own requests?"""
        if not src.startswith(LLM_UNIT) or not self._own:
            return False
        i = bisect.bisect_right(self._own, (t + OWN_SLACK[0], float("inf"))) - 1
        # requests don't overlap much; look at the few that started before t
        for start, end in self._own[max(0, i - 3):i + 1]:
            if start - OWN_SLACK[0] <= t <= end + OWN_SLACK[1]:
                return True
        return False

    def _sort(self, e, groups):
        src = events.source(e)
        if not self.dispatch(e, src):
            groups[(src, template(e["MESSAGE"]))].append(e)

    def _pending(self):
        rows = self.db.execute("SELECT event FROM pending WHERE host=? ORDER BY id",
                               (self.host,)).fetchall()
        self.db.execute("DELETE FROM pending WHERE host=?", (self.host,))
        return [json.loads(r[0]) for r in rows]

    def _save_pending(self, groups):
        for (src, shape), evs in groups.items():
            for e in evs[:PENDING_PER_GROUP]:
                self.db.execute(
                    "INSERT INTO pending (host, source, shape, t, event) VALUES (?,?,?,?,?)",
                    (self.host, src, shape, blocks.event_time(e), json.dumps(e)))

    # --- examples ---------------------------------------------------------------
    def keep_examples(self, since, events_iter=None):
        """Keep whole example events for rules that have none yet, from a window
        of the journal. Only reads: no actions run, no counts change, no
        cursor moves. Returns how many events were kept."""
        stream = events_iter if events_iter is not None else events.read(
            self.host, since=since)
        before = self.db.execute("SELECT count(*) FROM rule_events").fetchone()[0]
        for n, e in enumerate(events.join_multiline(events.mark_mirrored(stream)), 1):
            if e.get("__MIRRORED"):
                continue
            rule, _ = self.rules.match(e)
            if rule is not None:
                self.state.keep_example(rule["name"], e, blocks.event_time(e))
            if n % 5000 == 0:
                self.db.commit()
        self.db.commit()
        return self.db.execute("SELECT count(*) FROM rule_events").fetchone()[0] - before

    # --- investigations ---------------------------------------------------------
    def investigate(self, limit):
        rows = self.db.execute(
            "SELECT id, rule, events FROM investigations WHERE status='queued' "
            "ORDER BY t LIMIT ?", (limit,)).fetchall()
        for iid, rule, evs in rows:
            self.log(f"investigating {rule}")
            agent = Agent(INVESTIGATE_SYSTEM, llm=self.llm)
            try:
                report = agent.run(f"Rule: {rule}\nEvents:\n{evs}")
                status = "done"
            except Exception as e:
                report, status = f"error: {e}", "error"
            self.db.execute("UPDATE investigations SET status=?, report=?, "
                            "transcript=? WHERE id=?",
                            (status, report, str(agent.transcript), iid))
            self.db.commit()


RESET_TABLES = ("rule_stats", "counters", "extracts", "collected", "alerts", "rule_events",
                "investigations", "tracked", "distinct_values", "engine_cursors",
                "pending", "learned", "runs")


def reset(args):
    """Start the rule engine over: set the rules repo aside and clear its
    state. The older scan's findings are left alone."""
    from .rules import RULES_DIR
    if RULES_DIR.exists():
        aside = RULES_DIR.with_name(RULES_DIR.name + time.strftime(".old-%Y%m%d-%H%M%S"))
        RULES_DIR.rename(aside)
        print(f"rules moved to {aside}")
    db = sqlite3.connect(DATA_DIR / "argus.db")
    for table in RESET_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.commit()
    print("rule engine state cleared")


def examples(args):
    eng = Engine(args.host, log=lambda m: print(m, file=sys.stderr))
    print(f"kept {eng.keep_examples(args.since)} example events")


def import_requests(args):
    """Record past model traffic from transcripts (see llm.import_transcripts)."""
    from .llm import import_transcripts
    n = import_transcripts(DATA_DIR / "transcripts", time.time() - 3 * 86400)
    print(f"{n} request windows recorded from transcripts")


def main(args):
    eng = Engine(args.host, log=lambda m: print(m, file=sys.stderr))
    eng.run(learn_limit=args.learn, investigate_limit=args.investigate,
            since=args.since)
