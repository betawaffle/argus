"""Tests for the rule engine. Run: python3 -m unittest -v"""

import json
import os
import sqlite3
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp()
os.environ["ARGUS_DATA"] = TMP  # before importing argus: transcripts go here

from argus import blocks, engine, events, learn, rules  # noqa: E402


def ev(msg, cursor="c", t=1_760_000_000, **fields):
    base = {"_TRANSPORT": "journal", "_SYSTEMD_UNIT": "demo.service",
            "MESSAGE": msg, "__CURSOR": cursor,
            "__REALTIME_TIMESTAMP": str(int(t * 1e6)), "_PID": "123"}
    return {**base, **fields}


class FakeLLM:
    """Answers each chat with the next scripted tool call, then a final reply."""

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.calls = 0

    def chat(self, messages, tools=None, **_):
        self.calls += 1
        if messages[-1]["role"] == "tool" and messages[-1]["content"].startswith("accepted"):
            return {"role": "assistant", "content": "done"}, {}, {}
        if not self.proposals:
            return {"role": "assistant", "content": "giving up"}, {}, {}
        args = self.proposals.pop(0)
        call = {"id": f"call{self.calls}", "type": "function",
                "function": {"name": "propose_rule", "arguments": json.dumps(args)}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}, {}, {}


class TestToml(unittest.TestCase):
    def test_roundtrip(self):
        rule = {"name": "x_y", "match": {"_KERNEL_SUBSYSTEM": "usb"},
                "regex": r"error -(?P<err>\d+) on 'quoted'\path",
                "actions": [{"block": "threshold", "n": 5, "window": "day",
                             "then": {"block": "flag", "severity": "watch",
                                      "summary": "lots of {err}"}}],
                "flagged": False, "created": "2026-10-13T00:00:00",
                "samples": ["line \"one\"", "tab\there", "unicode é"]}
        text = rules.dump_rules("a/b.service", [rule])
        data = tomllib.loads(text)
        self.assertEqual(data["source"], "a/b.service")
        self.assertEqual(data["rule"], [rule])


class TestSource(unittest.TestCase):
    def test_sources(self):
        S = events.source
        self.assertEqual(S({"_TRANSPORT": "kernel"}), "kernel")
        self.assertEqual(S({"_TRANSPORT": "audit", "_AUDIT_TYPE_NAME": "USER_LOGIN"}),
                         "audit/USER_LOGIN")
        self.assertEqual(S({"_TRANSPORT": "journal", "_SYSTEMD_UNIT": "user@1000.service",
                            "_SYSTEMD_USER_UNIT": "llm-server.service"}), "llm-server.service")
        self.assertEqual(S({"_TRANSPORT": "stdout", "_SYSTEMD_UNIT": "run-p5734-i57425.scope"}),
                         "run.scope")
        self.assertEqual(S({"_TRANSPORT": "journal", "_SYSTEMD_USER_UNIT": "llm-server.service",
                            "CONTAINER_ID": "abc"}), "llm-server.service/container")
        self.assertEqual(S({"_TRANSPORT": "syslog", "_EXE": "/usr/bin/foo"}), "exe/foo")


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.rs = rules.RuleSet(tempfile.mkdtemp())

    def rule(self, **kw):
        return rules.make({"name": "demo_rule", "regex": r"disk (?P<d>\w+) slow",
                           "actions": [{"block": "count"}], **kw}, [])

    def test_good(self):
        self.assertEqual(rules.check(self.rule(), "demo.service", self.rs,
                                     [ev("disk sda slow")]), [])

    def test_must_match_examples(self):
        p = rules.check(self.rule(), "demo.service", self.rs, [ev("disk is fine")])
        self.assertIn("doesn't match", p[0])

    def test_unspecific(self):
        p = rules.check(self.rule(regex=r"(?P<all>.*)"), "demo.service", self.rs, [ev("x")])
        self.assertTrue(any("unspecific" in x for x in p))

    def test_structured_needs_no_regex(self):
        r = rules.make({"name": "unit_started", "match": {"MESSAGE_ID": "39f5"},
                        "actions": [{"block": "ignore"}]}, [])
        self.assertEqual(rules.check(r, "demo.service", self.rs,
                                     [ev("Started x", MESSAGE_ID="39f5")]), [])

    def test_identity_field(self):
        r = self.rule(regex=r"^(?P<up>[\d.]+) srv slot (?P<slot>\d)", actions=[
            {"block": "distinct", "field": "up",
             "on_new": {"block": "flag", "severity": "watch", "summary": "new {up}"}}])
        lines = [ev(f"{i}.46.111.{i}36 srv slot {i % 2}") for i in range(10)]
        p = rules.check(r, "demo.service", self.rs, lines)
        self.assertTrue(any("isn't an identity" in x for x in p), p)
        r["actions"][0]["field"] = "slot"   # repeats: fine
        self.assertEqual(rules.check(r, "demo.service", self.rs, lines), [])

    def test_code_file_on_container_output(self):
        r = rules.make({"name": "any_line", "match": {"CODE_FILE": "src/ctr_logging.c"},
                        "regex": "x", "actions": [{"block": "count"}]}, [])
        e = [ev("x", CODE_FILE="src/ctr_logging.c", CONTAINER_ID="abc")]
        self.assertTrue(any("unspecific" in x for x in
                            rules.check(r, "demo.service/container", self.rs, e)))
        self.assertEqual(rules.check(r, "demo.service", self.rs, e), [])

    def test_volatile_field(self):
        p = rules.check(self.rule(match={"_PID": "123"}), "demo.service", self.rs,
                        [ev("disk sda slow")])
        self.assertTrue(any("can't match on _PID" in x for x in p))

    def test_bad_block(self):
        p = rules.check(self.rule(actions=[{"block": "flag", "severity": "loud"}]),
                        "demo.service", self.rs, [ev("disk sda slow")])
        self.assertTrue(any("summary" in x for x in p) and any("severity" in x for x in p))

    def test_too_broad(self):
        # Same shape once numbers are templated: one kind of line, not broad.
        same = [ev(f"thing {i} happened") for i in range(50)]
        self.assertEqual(rules.check(self.rule(regex=r"thing (?P<n>\d+) happened"),
                                     "demo.service", self.rs, same[:1], same), [])
        # Many shapes, nearly all of the source's: too broad.
        mixed = [ev(f"{i} {'x' * i} happened {'y' * (30 - i)}") for i in range(1, 30)]
        p = rules.check(self.rule(regex=r"^(?P<n>\d+) .* happened"), "demo.service",
                        self.rs, mixed[:1], mixed)
        self.assertTrue(any("too broad" in x for x in p), p)
        # Many shapes, but a minority of the source's: accepted, for review.
        other = [ev(f"other {'z' * i} line {'w' * (40 - i)}") for i in range(40)]
        p = rules.check(self.rule(regex=r"^(?P<n>\d+) .* happened"), "demo.service",
                        self.rs, mixed[:1], mixed + other)
        self.assertEqual([x[:14] for x in p], ["review: broad,"])

    def test_name_conflict_and_overlap(self):
        older = rules.make({"name": "disk_slow", "regex": r"disk (?P<d>\w+) slow",
                            "actions": [{"block": "count"}]}, [ev("disk sda slow")])
        self.rs.add("demo.service", older, "test")
        p = rules.check(self.rule(name="disk_slow"), "demo.service", self.rs,
                        [ev("disk sda slow")])
        self.assertTrue(any("taken" in x for x in p))
        newer = self.rule(name="disk_anything", regex=r"disk (?P<d>\w+) \w+")
        p = rules.check(newer, "demo.service", self.rs, [ev("disk sdb fast")])
        self.assertTrue(any("overlaps with older rule 'disk_slow'" in x for x in p))


class TestBlocks(unittest.TestCase):
    def setUp(self):
        self.state = blocks.State(sqlite3.connect(":memory:"))

    def run_rule(self, actions, groups=None, n=1, t=1_760_000_000):
        for i in range(n):
            ctx = {"rule": "r", "event": ev("m"), "groups": groups or {},
                   "t": t + i, "state": self.state}
            blocks.run_actions(actions, ctx)

    def alerts(self):
        return self.state.db.execute("SELECT severity, summary FROM alerts").fetchall()

    def test_threshold_fires_once(self):
        self.run_rule([{"block": "threshold", "n": 3, "window": "day",
                        "then": {"block": "flag", "severity": "watch",
                                 "summary": "{dev} x3"}}], {"dev": "sda"}, n=5)
        self.assertEqual(self.alerts(), [("watch", "sda x3")])

    def test_track_on_change(self):
        a = [{"block": "track", "field": "state",
              "on_change": {"block": "flag", "severity": "attention",
                            "summary": "{old} -> {new}"}}]
        self.run_rule(a, {"state": "ONLINE"})
        self.run_rule(a, {"state": "ONLINE"})
        self.run_rule(a, {"state": "DEGRADED"})
        self.assertEqual(self.alerts(), [("attention", "ONLINE -> DEGRADED")])

    def test_distinct_on_new(self):
        a = [{"block": "distinct", "field": "user",
              "on_new": {"block": "flag", "severity": "watch", "summary": "new {user}"}}]
        for u in ["a", "b", "a"]:
            self.run_rule(a, {"user": u})
        self.assertEqual(self.alerts(), [("watch", "new a"), ("watch", "new b")])

    def test_threshold_n_and_quiet(self):
        self.run_rule([{"block": "threshold", "n": 2, "then": {
            "block": "flag", "severity": "watch", "summary": "over {n}"}}], n=2)
        self.assertEqual(self.alerts(), [("watch", "over 2")])
        a = [{"block": "distinct", "field": "u",
              "on_new": {"block": "flag", "severity": "watch", "summary": "new {new}"}}]
        ctx = {"rule": "q", "event": ev("m"), "groups": {"u": "a"}, "t": 1,
               "state": self.state, "quiet": True}
        blocks.run_actions(a, ctx)          # baseline: recorded, no alert
        blocks.run_actions(a, {**ctx, "quiet": False})
        blocks.run_actions(a, {**ctx, "quiet": False, "groups": {"u": "b"}})
        self.assertEqual(self.alerts()[1:], [("watch", "new b")])

    def test_distinct_new_and_unknown_placeholders(self):
        a = [{"block": "distinct", "field": "peer",
              "on_new": {"block": "flag", "severity": "watch",
                         "summary": "peer {new} via {via}"}}]
        self.run_rule(a, {"peer": "x"})
        self.assertEqual(self.alerts(), [("watch", "peer x via {via}")])

    def test_collect_limit_and_extract(self):
        self.run_rule([{"block": "collect", "limit": 2}, {"block": "extract"}],
                      {"dev": "sda"}, n=4)
        db = self.state.db
        self.assertEqual(db.execute("SELECT count(*) FROM collected").fetchone()[0], 2)
        self.assertEqual(db.execute("SELECT data FROM extracts LIMIT 1").fetchone()[0],
                         '{"dev": "sda"}')


class TestEngine(unittest.TestCase):
    def test_learn_dispatch_fallback(self):
        rules_dir = tempfile.mkdtemp()
        rs = rules.RuleSet(rules_dir)
        llm = FakeLLM([
            # group 1 (biggest): first attempt broken, second good
            {"name": "disk_slow", "regex": "(", "actions": [{"block": "count"}],
             "reason": "x"},
            {"name": "disk_slow", "regex": r"disk (?P<dev>\w+) is slow",
             "actions": [{"block": "count"},
                         {"block": "threshold", "n": 3, "window": "day",
                          "then": {"block": "flag", "severity": "watch",
                                   "summary": "{dev} slow"}}],
             "reason": "x"},
            # group 2: a new building block -> accepted but flagged
            {"name": "fan_speed", "regex": r"fan at (?P<rpm>\d+) rpm",
             "actions": [{"block": "series", "field": "rpm"}], "reason": "x"},
        ])
        eng = engine.Engine("local", db=sqlite3.connect(":memory:"), ruleset=rs,
                            llm=llm, log=lambda m: None)
        batch = ([ev(f"disk {i} is slow", cursor=f"c{i}") for i in range(4)]
                 + [ev("fan at 1200 rpm", cursor="c9"), ev("fan at 1300 rpm", cursor="c10")]
                 + [ev("something odd", cursor="c11")])
        eng.run(learn_limit=3, events_iter=batch)

        self.assertEqual([r["name"] for r in rs.all()][:2], ["disk_slow", "fan_speed"])
        self.assertTrue(rs.get("fan_speed")[1]["flagged"])
        third = rs.all()[2]  # the model gave up: fallback, flagged, exact line
        self.assertTrue(third["name"].startswith("unlearned_demo_service"))
        self.assertTrue(third["flagged"])
        hits = dict(eng.db.execute("SELECT rule, hits FROM rule_stats"))
        self.assertEqual(hits, {"disk_slow": 4, "fan_speed": 2, third["name"]: 1})
        self.assertEqual(eng.db.execute("SELECT summary FROM alerts").fetchall(),
                         [("2 slow",)])
        # flagged rules collect their lines
        self.assertEqual(eng.db.execute(
            "SELECT count(*) FROM collected WHERE rule='fan_speed'").fetchone()[0], 2)
        # rules are committed to git, one file per source
        log = subprocess.run(["git", "-C", rules_dir, "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 3)
        self.assertEqual(sorted(p.name for p in Path(rules_dir).glob("*.toml")),
                         ["demo.service.toml"])

        # Next batch: known lines need no model; the cursor moved on.
        calls = llm.calls
        eng.run(learn_limit=3, events_iter=[ev("disk 9 is slow", cursor="c12")])
        self.assertEqual(llm.calls, calls)
        self.assertEqual(eng.cursor(), "c12")
        # reload from disk gives the same rules
        self.assertEqual(rules.RuleSet(rules_dir).all(), rs.all())

    def test_container_echo_skipped(self):
        rs = rules.RuleSet(tempfile.mkdtemp())
        rs.add("demo.service/container", rules.make(
            {"name": "slot_release", "regex": "slot release",
             "actions": [{"block": "count"}]}, []), "t")
        eng = engine.Engine("local", db=sqlite3.connect(":memory:"), ruleset=rs,
                            llm=FakeLLM([]), log=lambda m: None)
        batch = [ev("srv slot release\n", cursor="c1", CONTAINER_ID="abc"),
                 ev("srv slot release\n", cursor="c2", _TRANSPORT="stdout"),
                 ev("srv slot release", cursor="c3", _TRANSPORT="stdout")]
        eng.run(learn_limit=0, events_iter=batch)
        self.assertEqual(dict(eng.db.execute("SELECT rule, hits FROM rule_stats")),
                         {"slot_release": 1})
        self.assertEqual(eng.db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
        self.assertEqual(eng.cursor(), "c3")

    def test_join_traceback(self):
        t = 1_760_000_000
        c = {"CONTAINER_ID": "w"}
        lines = ["Traceback (most recent call last):",
                 '  File "/app/x.py", line 3, in f', "    return d[k]",
                 "           ^^^^", "KeyError: 'Session not found'"]
        batch = [ev("before", cursor="c0", t=t, **c)]
        batch += [ev(m, cursor=f"c{i + 1}", t=t + 0.01 * i, **c) for i, m in enumerate(lines)]
        batch.insert(3, ev("other process", cursor="x", t=t, _PID="9"))
        batch.append(ev("after", cursor="c9", t=t + 1, **c))
        out = list(events.join_multiline(batch))
        msgs = [e["MESSAGE"] for e in out]
        self.assertEqual(msgs, ["before", "other process", "\n".join(lines), "after"])
        self.assertEqual(out[2]["__LINES"], 5)
        self.assertNotIn("__CURSOR", out[2])
        # a traceback at the very end still advances the cursor
        tail = list(events.join_multiline(batch[:-1]))
        self.assertEqual(tail[-1]["__CURSOR"], "c5")

    def test_join_loguru_traceback(self):
        # The shape Open WebUI (loguru + asyncio) writes, abridged.
        lines = [
            "2026-10-14 12:00:00.000 | ERROR    | asyncio.runners:run:118 - "
            "Task exception was never retrieved",
            "future: <Task finished name='Task-9' coro=<f() done> exception=KeyError()>",
            "Traceback (most recent call last):",
            '  File "/x/engineio/base_server.py", line 237, in _get_socket',
            "    s = self.sockets[sid]",
            "KeyError: None",
            "",
            "During handling of the above exception, another exception occurred:",
            "",
            "Traceback (most recent call last):",
            '> File "/x/socketio/async_server.py", line 619, in _handle_event_internal',
            "    r = await server._trigger_event(data[0], namespace, sid, *data[1:])",
            "    │       └ <socketio.AsyncServer object>",
            '  File "/app/socket/main.py", line 300, in get_session',
            "    raise KeyError('Session not found')",
            "KeyError: 'Session not found'",
        ]
        c = {"CONTAINER_ID": "w"}
        batch = [ev(m, cursor=f"c{i}", t=1_760_000_000 + 0.001 * i, **c)
                 for i, m in enumerate(lines)]
        batch.append(ev("INFO: GET /api/v1/chats", cursor="c99", t=1_760_000_001, **c))
        out = list(events.join_multiline(batch))
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["__LINES"], len(lines))
        self.assertIn("Session not found", out[0]["MESSAGE"])

    def test_new_rule_is_quiet_in_its_first_run(self):
        rs = rules.RuleSet(tempfile.mkdtemp())
        llm = FakeLLM([{"name": "unit_started", "regex": r"Started (?P<unit>\S+) now",
                        "actions": [{"block": "distinct", "field": "unit", "on_new": {
                            "block": "flag", "severity": "watch", "summary": "new {new}"}}],
                        "reason": "x"}])
        eng = engine.Engine("local", db=sqlite3.connect(":memory:"), ruleset=rs,
                            llm=llm, log=lambda m: None)
        eng.run(learn_limit=1, events_iter=[ev(f"Started u{i} now", cursor=f"c{i}")
                                            for i in range(3)])
        self.assertEqual(eng.db.execute("SELECT count(*) FROM alerts").fetchone()[0], 0)
        eng2 = engine.Engine("local", db=eng.db, ruleset=rs, llm=llm, log=lambda m: None)
        eng2.run(learn_limit=1, events_iter=[ev("Started u1 now", cursor="c7"),
                                             ev("Started u9 now", cursor="c8")])
        self.assertEqual(eng.db.execute("SELECT summary FROM alerts").fetchall(),
                         [("new u9",)])

    def test_proposal_cap(self):
        rs = rules.RuleSet(tempfile.mkdtemp())
        bad = {"name": "x_rule", "regex": "(", "actions": [{"block": "count"}],
               "reason": "x"}
        good = {**bad, "regex": "disk slow"}
        llm = FakeLLM([bad] * learn.MAX_PROPOSALS + [good])
        rule, ok, _ = learn.Learner(rs, llm=llm).learn("demo.service", [ev("disk slow")])
        self.assertFalse(ok)
        self.assertTrue(rule["name"].startswith("unlearned_"))

    def test_new_rule_covers_other_groups(self):
        # "sda" and "sdb" template differently, but one rule covers both.
        rs = rules.RuleSet(tempfile.mkdtemp())
        llm = FakeLLM([{"name": "disk_slow", "regex": r"disk (?P<dev>sd\w+) is slow",
                        "actions": [{"block": "count"}], "reason": "x"}])
        eng = engine.Engine("local", db=sqlite3.connect(":memory:"), ruleset=rs,
                            llm=llm, log=lambda m: None)
        eng.run(learn_limit=5, events_iter=[ev("disk sda is slow", cursor="a"),
                                            ev("disk sdb is slow", cursor="b")])
        self.assertEqual(len(rs.all()), 1)
        self.assertEqual(dict(eng.db.execute("SELECT rule, hits FROM rule_stats")),
                         {"disk_slow": 2})
        self.assertEqual(eng.db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
