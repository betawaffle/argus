"""argus command line.

  python -m argus ask "why is the fan loud?"      free-form question, read-only tools
  python -m argus scan --baseline                 learn current patterns, no model
  python -m argus scan [--host moneta]            investigate new or spiking patterns
"""

import argparse
import json
import re
import sys
import time

from .agent import DATA_DIR, Agent
from .patterns import Store, read_journal

ASK_SYSTEM = """You are argus, a careful sysadmin assistant with read-only access
to the machines you are asked about. Investigate with the run tool before
answering. Be concise and say what you checked."""

SCAN_SYSTEM = """You are argus, watching unattended Linux machines. You are given
one log pattern that is new or suddenly frequent. Investigate it with the run
tool (narrow journalctl queries, systemctl status, df, zpool status, ...), then
decide whether a human should care. Machines: "local" is a Fedora workstation
used for ML experiments; "moneta" is a NixOS NAS with ZFS.

End your answer with one plain-text line, nothing after it:
VERDICT: ignore|watch|attention - <one sentence summary>"""

FINDINGS = """CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY, pattern_id INTEGER, t REAL,
    verdict TEXT, summary TEXT, answer TEXT, transcript TEXT)"""


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
    store.db.execute(FINDINGS)
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
        answer = agent.run(json.dumps(p, indent=1), on_event=show)
        verdict, summary = parse_verdict(answer)
        store.db.execute(
            "INSERT INTO findings (pattern_id, t, verdict, summary, answer, "
            "transcript) VALUES (?,?,?,?,?,?)",
            (pid, time.time(), verdict, summary, answer, str(agent.transcript)))
        store.db.commit()
        print(f"  -> {verdict}: {summary}")


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
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
