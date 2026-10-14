"""argus's adapters for the labeling tool (labeler/): what can be judged.

The labeling core knows nothing about argus; these turn argus's own data
into labeler Items. Three sources:

    argus.report  model-written reports: daily-scan findings and rule-engine
                  investigations. One part per paragraph, so "went wrong
                  from here" can point at the paragraph where it went off.
    argus.alert   alerts raised by rules. A bad verdict means the alert
                  shouldn't have been raised; the rule that raised it is a
                  related item, so marks can point at what in it is at fault.
    argus.rule    model-written rules: is this a good rule?
"""

import json
import sqlite3
import time

from labeler import Item, Part, Source, paragraphs

from .blocks import SCHEMA as BLOCKS_SCHEMA
from .rules import RuleSet, _toml_value

SEVERITY_PRIORITY = {"attention": 3, "watch": 2, "?": 1}


def _when(t):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def _events_text(events):
    """Investigation events (summaries) as readable lines."""
    lines = []
    for e in events:
        meta = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("MESSAGE", "source"))
        lines.append(f"[{e.get('source', '?')}] {e.get('MESSAGE', '')}"
                     + (f"\n    {meta}" if meta else ""))
    return "\n".join(lines)


class _DB:
    def __init__(self, db):
        self.db = db if isinstance(db, sqlite3.Connection) else sqlite3.connect(db)

    def has(self, table):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                               (table,)).fetchone() is not None


class Reports(Source, _DB):
    name = "argus.report"
    QUESTION = "Is this report right? Is its verdict right?"

    def items(self):
        if self.has("findings"):
            yield from self._findings()
        if self.has("investigations"):
            yield from self._investigations()

    def _findings(self):
        rows = self.db.execute("""
            SELECT f.id, f.t, f.verdict, f.summary, f.answer, f.feedback, f.note,
                   p.host, p.ident, p.template, p.sample, p.total
            FROM findings f JOIN patterns p ON p.id = f.pattern_id
            WHERE f.answer IS NOT NULL AND f.answer != ''
            ORDER BY f.id DESC LIMIT 1000""").fetchall()
        for (fid, t, verdict, summary, answer, fb, note, host, ident, tmpl, sample,
             total) in rows:
            context = [
                Part("pattern", "code", tmpl or "", "log pattern"),
                Part("sample", "code", sample or "", "example line"),
                Part("where", "field", f"{host} {ident}, seen {total} times", "where"),
            ]
            if fb or note:
                context.append(Part("feedback", "field",
                                    " ".join(x for x in (fb, note) if x), "earlier feedback"))
            yield Item(
                source=self.name, key=f"finding:{fid}",
                title=f"{verdict}: {summary or '(no summary)'}",
                parts=paragraphs(answer), context=context, question=self.QUESTION,
                priority=SEVERITY_PRIORITY.get(verdict, 0) + (t or 0) / 1e10,
                link=f"/f/{fid}")

    def _investigations(self):
        rows = self.db.execute(
            "SELECT id, rule, t, report, events FROM investigations "
            "WHERE status='done' ORDER BY id DESC LIMIT 1000").fetchall()
        for iid, rule, t, report, events in rows:
            try:
                evs = json.loads(events or "[]")
            except json.JSONDecodeError:
                evs = []
            last = (report or "").strip().splitlines()[-1:] or [""]
            yield Item(
                source=self.name, key=f"investigation:{iid}",
                title=f"{rule}: {last[0][:120]}",
                parts=paragraphs(report),
                context=[Part("events", "code", _events_text(evs), "events it looked at")],
                refs=[("argus.rule", rule)], question=self.QUESTION,
                priority=2 + (t or 0) / 1e10, link=f"/rules/{rule}")


class Alerts(Source, _DB):
    name = "argus.alert"

    def items(self):
        self.db.executescript(BLOCKS_SCHEMA)
        rows = self.db.execute(
            "SELECT id, rule, t, severity, summary, message, data FROM alerts "
            "ORDER BY id DESC LIMIT 1000").fetchall()
        for aid, rule, t, severity, summary, message, data in rows:
            yield Item(
                source=self.name, key=f"alert:{aid}",
                title=f"{severity}: {summary}",
                parts=[Part("summary", "text", summary or "", "alert"),
                       Part("severity", "field", severity or "", "severity")],
                context=[Part("message", "code", message or "", "line that raised it"),
                         Part("data", "code", data or "", "values from the rule"),
                         Part("when", "field", _when(t), "when")],
                refs=[("argus.rule", rule)],
                question="Was this alert worth your attention? If not, mark what in "
                         "the rule below made it fire.",
                priority=SEVERITY_PRIORITY.get(severity, 0) + (t or 0) / 1e10,
                link=f"/rules/{rule}")


class Rules(Source, _DB):
    name = "argus.rule"

    def __init__(self, db, ruleset=None):
        super().__init__(db)
        self.ruleset = ruleset

    def items(self):
        rs = self.ruleset or RuleSet()
        self.db.executescript(BLOCKS_SCHEMA)
        stats = {r: (h, last) for r, h, last in
                 self.db.execute("SELECT rule, hits, last_hit FROM rule_stats")}
        for src, rules in rs.by_source.items():
            for r in rules:
                hits, last = stats.get(r["name"], (0, None))
                parts = []
                if r.get("match"):
                    parts.append(Part("match", "code", _toml_value(r["match"]), "match fields"))
                if r.get("regex"):
                    parts.append(Part("regex", "code", r["regex"], "regex on MESSAGE"))
                parts.append(Part("actions", "code", "\n".join(
                    _toml_value(a) for a in r.get("actions", [])), "actions"))
                context = [Part("samples", "code", "\n".join(r.get("samples", [])),
                                "lines it was written for"),
                           Part("stats", "field", f"{src} · {hits} hits, last {_when(last)}"
                                f" · created {r.get('created', '?')}", "stats")]
                if r.get("flagged"):
                    context.insert(0, Part("review", "text", r.get("review", ""),
                                           "flagged for review because"))
                yield Item(
                    source=self.name, key=r["name"], title=f"{r['name']} ({src})",
                    parts=parts, context=context,
                    question="Is this a good rule: does it match the right lines, and "
                             "does it do the right thing with them?",
                    priority=(2 if r.get("flagged") else 0) + min(hits or 0, 1e6) / 1e6,
                    link=f"/rules/{r['name']}")


def sources(db, ruleset=None):
    return [Reports(db), Alerts(db), Rules(db, ruleset)]
