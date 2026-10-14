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
import re
import sqlite3
import time

from labeler import Item, Part, Source, paragraphs

from .blocks import State
from .rules import RuleSet, _toml_value

SEVERITY_PRIORITY = {"attention": 3, "watch": 2, "?": 1}
# Alert and investigation ids start over when the rule engine is reset, so
# their keys carry the time too: a new alert 1 is not the old alert 1.


def _shape(text):
    """Loose shape of a log pattern, for grouping near-duplicate findings:
    placeholders, numbers and paths dropped, so two findings about the same
    problem in different files or lines land together."""
    text = re.sub(r"<[A-Z0-9]+>|/\S+|\d+", "", text or "")
    return " ".join(text.split()).lower()[:120]


def _field_order(k):
    # MESSAGE, then fields the program set, then journald's (_X), then its
    # addresses (__X)
    return (k != "MESSAGE", k.startswith("_"), k.startswith("__"), k)


def fields_text(d, limit=500):
    """A dict as "key=value" lines (a labeler "fields" part). Newlines in
    values are shown as ⏎ so each field stays on one line."""
    lines = []
    for k in sorted(d, key=_field_order):
        v = d[k] if isinstance(d[k], str) else json.dumps(d[k], ensure_ascii=False)
        v = v.replace("\n", " ⏎ ")
        lines.append(f"{k}={v[:limit] + '…' if len(v) > limit else v}")
    return "\n".join(lines)


def _json(text, default):
    try:
        return json.loads(text) if text else default
    except json.JSONDecodeError:
        return default


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
                Part("where", "field", f"{host} {ident}", "where"),
            ]
            if fb or note:
                context.append(Part("feedback", "field",
                                    " ".join(x for x in (fb, note) if x), "earlier feedback"))
            yield Item(
                source=self.name, key=f"finding:{fid}",
                title=f"{verdict}: {summary or '(no summary)'}",
                parts=paragraphs(answer), context=context, question=self.QUESTION,
                priority=SEVERITY_PRIORITY.get(verdict, 0) + (t or 0) / 1e10,
                link=f"/f/{fid}", group=f"finding|{host}|{ident}|{_shape(tmpl)}",
                live=[Part("seen", "field", f"seen {total} times", "seen")])

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
                source=self.name, key=f"investigation:{iid}@{int(t or 0)}",
                title=f"{rule}: {last[0][:120]}",
                parts=paragraphs(report),
                context=[Part("events", "code", _events_text(evs), "events it looked at")],
                refs=[("argus.rule", rule)], question=self.QUESTION,
                priority=2 + (t or 0) / 1e10, link=f"/rules/{rule}",
                group=f"investigation|{rule}")


class Alerts(Source, _DB):
    name = "argus.alert"

    def items(self):
        State(self.db)  # creates the tables, adds new columns
        rows = self.db.execute(
            "SELECT id, rule, t, severity, summary, message, data, event FROM alerts "
            "ORDER BY id DESC LIMIT 1000").fetchall()
        for aid, rule, t, severity, summary, message, data, event in rows:
            data = _json(data, None)
            event = _json(event, None)
            yield Item(
                source=self.name, key=f"alert:{aid}@{int(t or 0)}",
                title=f"{severity}: {summary}",
                parts=[Part("summary", "text", summary or "", "alert"),
                       Part("severity", "field", severity or "", "severity")],
                context=[Part("event", "fields", fields_text(event),
                              "event that raised it, every field") if event else
                         Part("message", "code", message or "", "line that raised it"),
                         Part("data", "fields", fields_text(data),
                              "captured by the rule's regex") if isinstance(data, dict)
                         else Part("data", "code", json.dumps(data), "values from the rule"),
                         Part("when", "field", _when(t), "when")],
                refs=[("argus.rule", rule)],
                question="Was this alert worth your attention? If not, mark what in "
                         "the rule below made it fire.",
                priority=SEVERITY_PRIORITY.get(severity, 0) + (t or 0) / 1e10,
                link=f"/rules/{rule}", group=f"alert|{rule}")


class Rules(Source, _DB):
    name = "argus.rule"

    def __init__(self, db, ruleset=None):
        super().__init__(db)
        self.ruleset = ruleset

    def items(self):
        rs = self.ruleset or RuleSet()
        State(self.db)  # creates the tables, adds new columns
        stats = {r: (h, last) for r, h, last in
                 self.db.execute("SELECT rule, hits, last_hit FROM rule_stats")}
        examples = {}
        for rule, event in self.db.execute("SELECT rule, event FROM rule_events ORDER BY id"):
            examples.setdefault(rule, []).append(_json(event, {}))
        compiled = {c.rule["name"]: c for cs in rs.compiled.values() for c in cs}
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
                                "lines it was written for")]
                c = compiled.get(r["name"])
                for i, e in enumerate(examples.get(r["name"], [])[:3], 1):
                    groups = c.test(e) if c else None
                    if groups:
                        context.append(Part(f"captured{i}", "fields", fields_text(groups),
                                            f"example {i}: what the regex captured"))
                    context.append(Part(f"event{i}", "fields", fields_text(e),
                                        f"example {i}: the whole event"))
                if r.get("rejected"):
                    context.insert(0, Part("rejected", "code", "\n".join(
                        f"{k} = {_toml_value(v)}" for k, v in r["rejected"].items()),
                        "the model's last rejected attempt"))
                if r.get("flagged"):
                    context.insert(0, Part("review", "text", r.get("review", ""),
                                           "flagged for review because"))
                yield Item(
                    source=self.name, key=r["name"], title=f"{r['name']} ({src})",
                    parts=parts, context=context,
                    question="Is this a good rule: does it match the right lines, and "
                             "does it do the right thing with them?",
                    priority=(2 if r.get("flagged") else 0) + min(hits or 0, 1e6) / 1e6,
                    link=f"/rules/{r['name']}",
                    # Fallbacks from one source are near-duplicates of each other.
                    group=f"fallback|{src}" if r["name"].startswith("unlearned_") else "",
                    live=[Part("stats", "field", f"{src} · {hits} hits, last {_when(last)}"
                               f" · created {r.get('created', '?')}", "stats")])


def sources(db, ruleset=None):
    return [Reports(db), Alerts(db), Rules(db, ruleset)]
