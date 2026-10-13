"""Pages for the rule engine: all rules, one rule, and alerts."""

import json
import urllib.parse

from .blocks import SCHEMA as BLOCKS_SCHEMA
from .engine import SCHEMA as ENGINE_SCHEMA
from .rules import RuleSet, dump_rules
from .serve_util import esc, page, when

NAV = "<nav><a href=/>findings</a><a href=/rules>rules</a></nav>"


def _q(name):
    return urllib.parse.quote(name)


def _actions(actions):
    def one(a):
        params = ", ".join(f"{k}={json.dumps(v) if not isinstance(v, (dict, list)) else '…'}"
                           for k, v in a.items() if k != "block")
        return f"{a.get('block', '?')}({params})"
    return "; ".join(one(a) for a in actions or [])


def rules_index(db):
    db.executescript(BLOCKS_SCHEMA + ENGINE_SCHEMA)
    rs = RuleSet()
    stats = {r[0]: r[1:] for r in db.execute("SELECT rule, hits, last_hit FROM rule_stats")}
    alerts = db.execute(
        "SELECT id, rule, t, severity, summary, message FROM alerts WHERE acked=0 "
        "ORDER BY severity='attention' DESC, t DESC LIMIT 200").fetchall()
    run = db.execute("SELECT t, events, matched, learned, pending, seconds FROM runs "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    queued = db.execute("SELECT count(*) FROM investigations WHERE status='queued'"
                        ).fetchone()[0]

    out = [NAV]
    if run:
        t, n, matched, learned, pending, secs = run
        out.append(f"<p class=muted>last run {when(t)}: {n} lines, {matched} matched by "
                   f"rules, {learned} rules learned, {pending} lines pending, {secs}s. "
                   f"{len(rs.all())} rules; {queued} investigations queued.</p>")
    if rs.broken:
        out.append("<h2 class=attention>Rules that don't compile</h2><ul>" + "".join(
            f"<li><code>{esc(n)}</code>: {esc(e)}</li>" for n, e in rs.broken.items()) + "</ul>")

    out.append("<h2>Alerts</h2>")
    if alerts:
        out.append("<table><tr><th></th><th>alert</th><th>when</th><th></th></tr>" + "".join(
            f"<tr><td class={esc(sev)}>{esc(sev)}</td>"
            f"<td>{esc(summary)}<br><a href=/rules/{_q(rule)}><code class=muted>{esc(rule)}</code></a>"
            f"<br><code class=muted>{esc(msg[:200])}</code></td><td>{when(t)}</td>"
            f"<td><form method=post action=/ack><input type=hidden name=id value={aid}>"
            f"<button>ack</button></form></td></tr>"
            for aid, rule, t, sev, summary, msg in alerts) + "</table>")
    else:
        out.append("<p class=muted>No open alerts.</p>")

    flagged = [(src, r) for src, rules in rs.by_source.items() for r in rules if r.get("flagged")]
    if flagged:
        out.append("<h2 class=watch>Flagged for review</h2><table>" + "".join(
            f"<tr><td><a href=/rules/{_q(r['name'])}>{esc(r['name'])}</a><br>"
            f"<code class=muted>{esc(src)}</code></td><td>{esc(r.get('review', ''))}</td>"
            f"<td>{stats.get(r['name'], (0,))[0]} hits</td></tr>" for src, r in flagged)
            + "</table>")

    out.append("<h2>All rules</h2>")
    for src in sorted(rs.by_source):
        rows = "".join(
            f"<tr><td><a href=/rules/{_q(r['name'])}>{esc(r['name'])}</a>"
            f"{' <span class=watch>flagged</span>' if r.get('flagged') else ''}<br>"
            f"<code class=muted>{esc(r.get('regex', ''))[:160]}</code></td>"
            f"<td>{esc(_actions(r.get('actions')))}</td>"
            f"<td>{stats.get(r['name'], (0, None))[0]}<br>"
            f"<span class=muted>{when(stats.get(r['name'], (0, None))[1])}</span></td></tr>"
            for r in rs.by_source[src])
        out.append(f"<h3><code>{esc(src)}</code></h3><table><tr><th>rule</th><th>actions</th>"
                   f"<th>hits / last</th></tr>{rows}</table>")
    if not rs.by_source:
        out.append("<p class=muted>No rules yet.</p>")
    return page("argus rules", "".join(out))


def rule_detail(db, name):
    db.executescript(BLOCKS_SCHEMA + ENGINE_SCHEMA)
    rs = RuleSet()
    src, rule = rs.get(name)
    if rule is None:
        return page("not found", NAV + "<p>No such rule.</p>")
    hits, first, last = db.execute("SELECT hits, first_hit, last_hit FROM rule_stats "
                                   "WHERE rule=?", (name,)).fetchone() or (0, None, None)
    out = [NAV, f"<h2>{esc(name)}</h2><p><code>{esc(src)}</code> · {hits} hits, "
           f"first {when(first)}, last {when(last)}</p>"]
    if rule.get("flagged"):
        out.append(f"<p class=watch>Flagged for review: {esc(rule.get('review', ''))}</p>")
    out.append(f"<h3>Rule</h3><pre>{esc(dump_rules(src, [rule]).split(chr(10), 3)[3])}</pre>")

    def table(title, sql, cols, fmt):
        rows = db.execute(sql, (name,)).fetchall()
        if rows:
            out.append(f"<h3>{title}</h3><table><tr>" + "".join(f"<th>{c}</th>" for c in cols)
                       + "</tr>" + "".join("<tr>" + "".join(f"<td>{x}</td>" for x in fmt(r))
                                           + "</tr>" for r in rows) + "</table>")

    table("Alerts", "SELECT t, severity, summary, acked FROM alerts WHERE rule=? "
          "ORDER BY t DESC LIMIT 50", ["when", "severity", "summary", "acked"],
          lambda r: (when(r[0]), esc(r[1]), esc(r[2]), "yes" if r[3] else ""))
    table("Investigations", "SELECT t, status, report FROM investigations WHERE rule=? "
          "ORDER BY t DESC LIMIT 10", ["when", "status", "report"],
          lambda r: (when(r[0]), esc(r[1]), f"<pre>{esc(r[2] or '')}</pre>"))
    table("Counters", "SELECT key, window_start, count FROM counters WHERE rule=? "
          "ORDER BY window_start DESC LIMIT 30", ["block", "window", "count"],
          lambda r: (esc(r[0]), when(r[1]), r[2]))
    table("Tracked values", "SELECT field, value, t FROM tracked WHERE rule=?",
          ["field", "value", "since"], lambda r: (esc(r[0]), esc(r[1]), when(r[2])))
    table("Distinct values", "SELECT field, value, first_seen FROM distinct_values "
          "WHERE rule=? ORDER BY first_seen DESC LIMIT 50", ["field", "value", "first seen"],
          lambda r: (esc(r[0]), esc(r[1]), when(r[2])))
    table("Extracted", "SELECT t, data FROM extracts WHERE rule=? ORDER BY t DESC LIMIT 50",
          ["when", "data"], lambda r: (when(r[0]), f"<code>{esc(r[1])}</code>"))
    table("Collected lines", "SELECT t, message FROM collected WHERE rule=? "
          "ORDER BY t DESC LIMIT 100", ["when", "line"],
          lambda r: (when(r[0]), f"<code>{esc(r[1])}</code>"))
    return page(f"argus: {name}", "".join(out))


def ack(db, alert_id):
    db.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
    db.commit()
