"""argus command line.

  python -m argus ask "why is the fan loud?"      free-form question, read-only tools
  python -m argus scan --baseline                 learn current patterns, no model
  python -m argus scan [--host moneta]            investigate new or spiking patterns
  python -m argus serve [--port 8082]             page to browse findings and give feedback
  python -m argus rules run [--learn 10]          rule engine: dispatch new lines, learn rules
  python -m argus rules list                      show the rules and their hit counts
"""

import argparse
import json
import re
import sys
import time

from . import engine, serve
from .agent import DATA_DIR, Agent
from .patterns import Store, read_journal

ASK_SYSTEM = """You are argus, a careful sysadmin assistant with read-only access
to the machines you are asked about. Investigate with the run tool before
answering. Be concise and say what you checked."""

SCAN_SYSTEM = """You are argus, watching unattended Linux machines. You are given
one log pattern that is new or suddenly frequent. Investigate it with the run
tool (narrow journalctl queries, systemctl status, df, zpool status, ...), then
decide whether a human should care. You may be shown earlier verdicts on the
same service and the owner's feedback on them; respect that feedback. Machines: "local" is a Fedora workstation
used for ML experiments; "moneta" is a NixOS NAS with ZFS.

End your answer with one plain-text line, nothing after it:
VERDICT: ignore|watch|attention - <one sentence summary>"""

def show(kind, data):
    if kind == "tool":
        args = json.loads(data["call"]["function"].get("arguments") or "{}")
        print(f"  $ {args.get('command')}  [{args.get('host', 'local')}]",
              file=sys.stderr)


# Plain text, not JSON: asked for JSON, gpt-oss switches to its constrained
# JSON channel, which llama.cpp's parser rejects with HTTP 500.
VERDICT = re.compile(r"VERDICT:\W*(ignore|watch|attention)\W*(.*)", re.I)


def parse_verdict(answer):
    for line in reversed(answer.strip().splitlines()):
        m = VERDICT.search(line)
        if m:
            return m.group(1).lower(), m.group(2).strip()
        m = re.search(r"\{.*\}", line)
        if m:
            try:
                v = json.loads(m.group(0))
                return v.get("verdict", "?"), v.get("summary", "")
            except json.JSONDecodeError:
                pass
    return "?", answer.strip()[:200]


def cmd_ask(args):
    print(Agent(ASK_SYSTEM).run(args.question, on_event=show))


def cmd_scan(args):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = Store(DATA_DIR / "argus.db")
    cursor = store.cursor(args.host)
    new = store.ingest(args.host, read_journal(args.host, args.since, cursor))
    if args.baseline:
        # Treat everything seen so far as normal background noise.
        store.db.execute(
            "INSERT INTO findings (pattern_id, t, verdict, summary) "
            "SELECT id, ?, 'baseline', '' FROM patterns WHERE host=? AND id NOT IN "
            "(SELECT pattern_id FROM findings)", (time.time(), args.host))
        store.db.commit()
        print(f"baselined {len(new)} new patterns", file=sys.stderr)
        return
    # Anything never judged (including leftovers past --limit), plus spikes.
    unjudged = {r[0] for r in store.db.execute(
        "SELECT id FROM patterns WHERE host=? AND id NOT IN "
        "(SELECT pattern_id FROM findings)", (args.host,))}
    todo = sorted(unjudged | set(store.spikes(time.strftime("%Y-%m-%d"))))
    print(f"{len(new)} new patterns, {len(todo)} to look at", file=sys.stderr)
    for pid in todo[: args.limit]:
        p = store.describe(pid)
        print(f"\n[{p['host']}] {p['ident']}: {p['template']}", file=sys.stderr)
        agent = Agent(SCAN_SYSTEM)
        task = json.dumps(p, indent=1)
        past = store.history(pid)
        if past:
            task += "\n\nEarlier verdicts on the same service:\n" + "\n".join(
                f"- {t!r}: {v} ({s})"
                + (f"; owner {fb}" + (f": {n}" if n else "") if fb else "")
                for t, v, s, fb, n in past)
        answer = agent.run(task, on_event=show)
        verdict, summary = parse_verdict(answer)
        store.db.execute(
            "INSERT INTO findings (pattern_id, t, verdict, summary, answer, "
            "transcript) VALUES (?,?,?,?,?,?)",
            (pid, time.time(), verdict, summary, answer, str(agent.transcript)))
        store.db.commit()
        print(f"  -> {verdict}: {summary}")


def cmd_rules_list(args):
    import sqlite3
    from .rules import RuleSet
    rs = RuleSet()
    db = sqlite3.connect(DATA_DIR / "argus.db")
    try:
        stats = {r[0]: r[1:] for r in db.execute(
            "SELECT rule, hits, last_hit FROM rule_stats")}
    except sqlite3.OperationalError:
        stats = {}
    for src, rules in sorted(rs.by_source.items()):
        print(src)
        for r in rules:
            hits, last = stats.get(r["name"], (0, None))
            flag = "  FLAGGED: " + r.get("review", "") if r.get("flagged") else ""
            acts = ",".join(a.get("block", "?") for a in r.get("actions", []))
            print(f"  {r['name']:<40} {hits:>7} hits  [{acts}]{flag}")


def main():
    ap = argparse.ArgumentParser(prog="argus")
    sub = ap.add_subparsers(required=True)
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.set_defaults(func=cmd_ask)
    s = sub.add_parser("scan")
    s.add_argument("--host", default="local")
    s.add_argument("--since", default="-1d",
                   help="window for the first scan of a host; later scans resume")
    s.add_argument("--baseline", action="store_true",
                   help="record patterns without investigating them")
    s.add_argument("--limit", type=int, default=10,
                   help="max patterns to investigate per run")
    s.set_defaults(func=cmd_scan)
    v = sub.add_parser("serve")
    v.add_argument("--port", type=int, default=8082)
    v.add_argument("--bind", default="0.0.0.0")
    v.set_defaults(func=lambda a: serve.serve(DATA_DIR / "argus.db", a.bind, a.port))
    r = sub.add_parser("rules").add_subparsers(required=True)
    rr = r.add_parser("run", help="one batch: dispatch new lines, learn rules")
    rr.add_argument("--host", default="local")
    rr.add_argument("--learn", type=int, default=10,
                    help="max rules to learn this run (each is one model call)")
    rr.add_argument("--investigate", type=int, default=3,
                    help="max queued investigations to run")
    rr.add_argument("--since", help="testing only: process lines since this time "
                    "(e.g. -2h) instead of resuming from the saved cursor. Use "
                    "with scratch ARGUS_DATA and ARGUS_RULES.")
    rr.set_defaults(func=engine.main)
    rx = r.add_parser("reset", help="start over: set the rules aside, clear "
                      "the engine's state (the scan's findings stay)")
    rx.set_defaults(func=engine.reset)
    re_ = r.add_parser("examples", help="keep whole example events for rules from "
                       "a journal window (read-only otherwise), for the labeling page")
    re_.add_argument("--host", default="local")
    re_.add_argument("--since", default="-7d", help="journal window start (default -7d)")
    re_.set_defaults(func=engine.examples)
    ri = r.add_parser("import-requests", help="record argus's model traffic of "
                      "the last 3 days from its transcripts, so the model server's "
                      "lines about it aren't learned")
    ri.add_argument("--all", action="store_true",
                    help="every transcript, not just the last 3 days (for backfill)")
    ri.set_defaults(func=engine.import_requests)
    rb = r.add_parser("backfill", help="first run over the whole journal, in "
                      "windows, oldest first; then watching continues from the end. "
                      "Needs a fresh start (rules reset) and import-requests --all")
    rb.add_argument("--host", default="local")
    rb.add_argument("--hours", type=float, default=24, help="window size (default 24)")
    rb.add_argument("--learn", type=int, default=40,
                    help="max rules to learn per window (default 40)")
    rb.add_argument("--investigate", type=int, default=3,
                    help="max investigations at the end")
    rb.set_defaults(func=engine.backfill)
    rl = r.add_parser("list", help="show rules with hit counts")
    rl.set_defaults(func=cmd_rules_list)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
