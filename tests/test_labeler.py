"""Tests for the labeling tool and argus's adapters. Run: python3 -m unittest -v"""

import json
import os
import sqlite3
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ.setdefault("ARGUS_DATA", TMP)

from argus import label_sources, rules  # noqa: E402
from argus.patterns import Store as ArgusStore  # noqa: E402
from labeler import Item, Part, Source, Store, paragraphs  # noqa: E402
from labeler.core import default_reason  # noqa: E402
from labeler.web import App  # noqa: E402


class ListSource(Source):
    def __init__(self, name, items):
        self.name, self.list, self.seen = name, items, []

    def items(self):
        return list(self.list)

    def labeled(self, item, label):
        self.seen.append((item["key"], label["verdict"]))


def item(key="a", text="first\n\nsecond", **kw):
    return Item(source="demo", key=key, title=f"item {key}", parts=paragraphs(text), **kw)


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_paragraphs(self):
        ps = paragraphs("one\nline two\n\n\n  \nthree")
        self.assertEqual([(p.id, p.text) for p in ps], [("p1", "one\nline two"), ("p2", "three")])

    def test_sync_versions(self):
        src = ListSource("demo", [item("a"), item("b")])
        self.assertEqual(self.store.sync([src]), 2)
        self.assertEqual(self.store.sync([src]), 0)            # unchanged: no new versions
        a1 = self.store.current_id("demo", "a")
        src.list = [item("a", "first\n\nchanged"), item("b")]
        self.assertEqual(self.store.sync([src]), 1)
        a2 = self.store.current_id("demo", "a")
        self.assertNotEqual(a1, a2)
        self.assertFalse(self.store.item(a1)["current"])     # old version kept, not current
        self.assertEqual(self.store.item(a1)["parts"][1]["text"], "second")
        src.list = [item("b")]                                 # a went away
        self.store.sync([src])
        self.assertIsNone(self.store.current_id("demo", "a"))

    def test_carry_forward_when_only_context_changed(self):
        ctx = lambda t: [Part("where", "field", t)]
        src = ListSource("demo", [item("a", context=ctx("seen 1 times")),
                                  item("b", context=ctx("x"))])
        self.store.sync([src])
        a1, b1 = self.store.current_id("demo", "a"), self.store.current_id("demo", "b")
        self.store.save_label(a1, "andy", "good", marks=[
            {"kind": "good", "part": "p1", "start": 0, "end": 5}], note="fine")
        self.store.save_label(b1, "andy", "bad", marks=[
            {"kind": "wrong", "part": "where"}])
        # a: only context changed -> carried. b: a marked part changed -> not.
        src.list = [item("a", context=ctx("seen 2 times")), item("b", context=ctx("y"))]
        self.store.sync([src])
        a2, b2 = self.store.current_id("demo", "a"), self.store.current_id("demo", "b")
        lab = self.store.latest_label(a2, "andy")
        self.assertEqual((lab["verdict"], lab["note"], lab["carried_from"]), ("good", "fine", a1))
        self.assertEqual(lab["marks"][0]["item_id"], a2)
        self.assertIsNone(self.store.latest_label(b2, "andy"))
        label, dropped, changed = self.store.carried_label(b2, "andy")
        self.assertEqual((label["verdict"], dropped, changed["changed"]), ("bad", 1, ["where"]))
        # judged text changed -> never carried automatically
        src.list = [item("a", "first\n\nedited", context=ctx("seen 2 times")),
                    item("b", context=ctx("y"))]
        self.store.sync([src])
        self.assertIsNone(self.store.latest_label(self.store.current_id("demo", "a"), "andy"))

    def test_priority_doesnt_make_a_new_version(self):
        src = ListSource("demo", [item("a", priority=1)])
        self.store.sync([src])
        src.list = [item("a", priority=5)]
        self.assertEqual(self.store.sync([src]), 0)

    def test_label_and_queue(self):
        src = ListSource("demo", [item("a", priority=1), item("b", priority=2)])
        self.store.sync([src])
        a, b = self.store.current_id("demo", "a"), self.store.current_id("demo", "b")
        self.assertEqual([r["id"] for r in self.store.queue("andy")], [b, a])
        self.store.save_label(b, "andy", "bad", [
            {"part": "p2", "start": 0, "end": 3, "kind": "wrong"},
            {"part": "p1", "kind": "good"},
            {"part": "p2", "start": 4, "end": 4, "kind": "missing", "reason": False},
        ], fixable=True, confidence=2, note="meh")
        self.assertEqual([r["id"] for r in self.store.queue("andy")], [a])
        self.assertEqual(len(self.store.queue("claude")), 2)  # per labeler
        lab = self.store.latest_label(b, "andy")
        self.assertEqual((lab["verdict"], lab["fixable"], lab["confidence"], lab["note"]),
                         ("bad", True, 2, "meh"))
        # Default reasons follow the verdict; an explicit one is kept.
        self.assertEqual([(m["kind"], m["reason"]) for m in lab["marks"]],
                         [("wrong", True), ("good", False), ("missing", False)])
        self.assertEqual(lab["marks"][0]["item_id"], b)

    def test_relabel_appends(self):
        self.store.sync([ListSource("demo", [item("a")])])
        a = self.store.current_id("demo", "a")
        self.store.save_label(a, "andy", "bad")
        self.store.save_label(a, "andy", "good")
        self.assertEqual(self.store.latest_label(a, "andy")["verdict"], "good")
        self.assertEqual(len(list(self.store.export())), 2)  # history kept

    def test_previous_version_label(self):
        src = ListSource("demo", [item("a")])
        self.store.sync([src])
        a1 = self.store.current_id("demo", "a")
        self.store.save_label(a1, "andy", "bad", note="old")
        src.list = [item("a", "rewritten")]
        self.store.sync([src])
        a2 = self.store.current_id("demo", "a")
        self.assertEqual([r["id"] for r in self.store.queue("andy")], [a2])
        prev_id, prev = self.store.previous_label(a2, "andy")
        self.assertEqual((prev_id, prev["note"]), (a1, "old"))

    def test_fixable_only_on_bad(self):
        self.store.sync([ListSource("demo", [item("a")])])
        a = self.store.current_id("demo", "a")
        self.store.save_label(a, "andy", "good", fixable=True)
        self.assertIsNone(self.store.latest_label(a, "andy")["fixable"])

    def test_validation(self):
        self.store.sync([ListSource("demo", [item("a")])])
        a = self.store.current_id("demo", "a")
        bad = [
            dict(verdict="great"),
            dict(verdict="bad", marks=[{"kind": "ugly"}]),
            dict(verdict="bad", marks=[{"kind": "wrong", "start": 3}]),
            dict(verdict="bad", marks=[{"kind": "wrong", "start": 3, "end": 1}]),
            dict(verdict="bad", marks=[{"kind": "from_here"}, {"kind": "from_here"}]),
            dict(verdict="bad", confidence=7),
        ]
        for kw in bad:
            with self.assertRaises(ValueError, msg=kw):
                self.store.save_label(a, "andy", **kw)

    def test_default_reason(self):
        self.assertTrue(default_reason("missing", "bad"))
        self.assertFalse(default_reason("good", "bad"))
        self.assertTrue(default_reason("good", "good"))
        self.assertFalse(default_reason("wrong", "good"))
        self.assertFalse(default_reason("wrong", "unsure"))


class NewKindsLinksGroupsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.src = ListSource("demo", [item("a", group="g1"), item("b", group="g1"),
                                       item("c", group="g1"), item("d")])
        self.store.sync([self.src])
        self.id = {k: self.store.current_id("demo", k) for k in "abcd"}

    def test_new_kinds_and_fix(self):
        a = self.id["a"]
        self.store.save_label(a, "andy", "bad", [
            {"part": "p1", "start": 0, "end": 5, "kind": "should_be", "fix": "ignore"},
            {"part": "p1", "kind": "unsupported"},
            {"part": "p2", "kind": "unclear"},
            {"part": "p2", "kind": "no_better_option"},
            {"part": "p2", "kind": "wrong", "fix": "dropped: only should_be keeps it"},
        ])
        marks = self.store.latest_label(a, "andy")["marks"]
        self.assertEqual(marks[0]["fix"], "ignore")
        self.assertNotIn("fix", marks[4])
        self.assertEqual([m["reason"] for m in marks], [True, True, True, False, True])

    def test_links(self):
        a, b = self.id["a"], self.id["b"]
        self.store.save_label(a, "andy", "bad")
        self.store.save_label(b, "andy", "skip", links=[{"kind": "duplicate", "target": a}])
        (link,) = self.store.latest_label(b, "andy")["links"]
        self.assertEqual((link["kind"], link["target"], link["title"]), ("duplicate", a, "item a"))
        self.assertEqual(self.store.recent("andy", exclude=b)[0]["id"], a)
        for bad in ([{"kind": "same", "target": a}], [{"kind": "related", "target": 999}],
                    [{"kind": "related", "target": b}]):
            with self.assertRaises(ValueError):
                self.store.save_label(b, "andy", "skip", links=bad)

    def test_groups(self):
        a = self.id["a"]
        self.assertEqual({r["id"] for r in self.store.group_of(a, "andy")},
                         {self.id["b"], self.id["c"]})
        self.assertEqual(self.store.group_of(self.id["d"], "andy"), [])
        self.store.save_label(a, "andy", "bad")
        self.assertEqual(self.store.mark_group_duplicates(a, "andy"), 2)
        lab = self.store.latest_label(self.id["c"], "andy")
        self.assertEqual((lab["verdict"], lab["links"][0]["target"]), ("skip", a))
        self.assertEqual(self.store.group_of(a, "andy"), [])

    def test_group_duplicates_only_ticked(self):
        a = self.id["a"]
        self.assertEqual(self.store.mark_group_duplicates(a, "andy", ids=[self.id["b"]]), 1)
        self.assertEqual([r["id"] for r in self.store.group_of(a, "andy")], [self.id["c"]])

    def test_skip_comes_back_last(self):
        a, b, c, d = (self.id[k] for k in "abcd")
        self.store.save_label(a, "andy", "skip")
        self.store.save_label(b, "andy", "skip")
        self.store.save_label(c, "andy", "skip", links=[{"kind": "duplicate", "target": a}])
        q = self.store.queue("andy")
        self.assertEqual([(r["id"], r["state"]) for r in q],
                         [(d, "todo"), (a, "later"), (b, "later")])  # oldest skip first
        self.assertEqual(self.store.counts("andy")["demo"], (4, 1))  # only the duplicate is done
        self.store.save_label(a, "andy", "good")
        self.assertEqual([r["id"] for r in self.store.queue("andy")], [d, b])

    def test_new_field_kinds(self):
        self.store.save_label(self.id["a"], "andy", "bad", marks=[
            {"kind": "should_use", "part": "p1", "start": 0, "end": 3},
            {"kind": "should_not_use", "part": "p1", "start": 4, "end": 5}])
        marks = self.store.latest_label(self.id["a"], "andy")["marks"]
        self.assertEqual([m["reason"] for m in marks], [True, True])

    def test_live_parts_dont_make_a_new_version(self):
        self.src.list = [item("a", group="g", live=[Part("stats", "field", "3 hits")])]
        self.assertEqual(self.store.sync([self.src]), 0)
        self.src.list = [item("a", group="g", live=[Part("stats", "field", "9 hits")])]
        self.assertEqual(self.store.sync([self.src]), 0)
        self.assertEqual(self.store.item(self.id["a"])["live"][0]["text"], "9 hits")


        # Changing an item's group must not make a new version.
        self.src.list = [item("a", group="other")]
        self.assertEqual(self.store.sync([self.src]), 0)

    def test_migrates_old_db(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE items (id INTEGER PRIMARY KEY, source TEXT, key TEXT, version TEXT,
                snapshot TEXT, first_seen REAL, last_seen REAL, priority REAL DEFAULT 0,
                current INTEGER DEFAULT 1, UNIQUE (source, key, version));
            CREATE TABLE marks (id INTEGER PRIMARY KEY, label_id INTEGER, item_id INTEGER,
                part TEXT, start INTEGER, end INTEGER, kind TEXT, reason INTEGER, note TEXT);
            INSERT INTO marks (label_id, kind, reason) VALUES (1, 'wrong', 1);""")
        old.commit()
        old.close()
        st = Store(path)
        st.sync([ListSource("demo", [item("a", group="g")])])
        self.assertEqual(st.db.execute("SELECT grp FROM items").fetchone()[0], "g")
        self.assertIsNone(st.db.execute("SELECT fix FROM marks").fetchone()[0])


class WebTest(unittest.TestCase):
    def setUp(self):
        self.src = ListSource("demo", [item("a", refs=[("other", "x")]),
                                       item("b")])
        self.other = ListSource("other", [Item("other", "x", "the x", [Part("f", "code", "x=1")])])
        self.app = App(Store(":memory:"), [self.src, self.other])

    def get(self, path, query=""):
        return self.app.handle("GET", path, query)

    def test_not_ours(self):
        self.assertIsNone(self.get("/rules"))
        self.assertIsNone(self.get("/labels"))

    def test_flow(self):
        status, _, body = self.get("/label")
        self.assertEqual(status, 200)
        self.assertIn(b"item a", body)
        status, kind, loc = self.get("/label/next", "source=demo")
        self.assertEqual((status, kind), (303, "redirect"))
        self.assertIn(b"/label/i/", loc)
        iid = self.app.store.current_id("demo", "a")
        status, _, page = self.get(f"/label/i/{iid}")
        self.assertEqual(status, 200)
        data = json.loads(page.decode().split("window.LABEL = ")[1].split(";</script>")[0]
                          .replace("<\\/", "</"))
        self.assertEqual(data["refs"][0]["title"], "the x")      # ref resolved
        ref_id = data["refs"][0]["id"]
        status, _, out = self.app.handle("POST", "/label/api/label", "", json.dumps({
            "item_id": iid, "verdict": "bad", "fixable": True,
            "marks": [{"item_id": ref_id, "part": "f", "start": 2, "end": 3,
                       "kind": "wrong", "reason": None, "note": "should be 2"}]}).encode())
        self.assertEqual(status, 200, out)
        self.assertEqual(self.src.seen, [("a", "bad")])
        lab = self.app.store.latest_label(iid, "andy")
        self.assertEqual(lab["marks"][0]["item_id"], ref_id)     # a mark into the ref
        status, _, loc = self.get("/label/next", f"source=demo&after={iid}")
        self.assertNotIn(f"/i/{iid}".encode(), loc)

    def test_queue_collapses_groups_and_group_api(self):
        app = App(Store(":memory:"), [ListSource("demo", [item(k, group="g") for k in "abc"]
                                                 + [item("solo")])])
        body = app.handle("GET", "/label", "")[2].decode()
        self.assertIn("+2 like this", body)
        self.assertEqual(body.count("/label/i/"), 2)
        a = app.store.current_id("demo", "a")
        status, _, out = app.handle("POST", "/label/api/group_duplicates", "",
                                    json.dumps({"item_id": a}).encode())
        self.assertEqual((status, json.loads(out)["n"]), (200, 2))

    def test_bad_post(self):
        self.get("/label")
        status, _, _ = self.app.handle("POST", "/label/api/label", "",
                                       json.dumps({"item_id": 1, "verdict": "nah"}).encode())
        self.assertEqual(status, 400)

    def test_static_stays_in_its_folder(self):
        self.assertEqual(self.get("/label/static/label.js")[0], 200)
        self.assertEqual(self.get("/label/static/../web.py")[0], 404)

    def test_export(self):
        self.get("/label")
        self.app.store.save_label(1, "andy", "good")
        status, _, body = self.get("/label/export.jsonl")
        rows = [json.loads(x) for x in body.decode().splitlines()]
        self.assertEqual(rows[0]["verdict"], "good")
        self.assertIn("parts", rows[0]["item"])


class ArgusSourcesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = ArgusStore(os.path.join(self.dir, "argus.db")).db
        list(label_sources.Alerts(self.db).items())  # creates the block tables
        self.db.execute("INSERT INTO patterns (id, host, ident, template, total, sample) "
                        "VALUES (1, 'local', 'kernel', 'probe failed <n>', 4, 'probe failed 5')")
        self.db.execute("INSERT INTO findings (pattern_id, t, verdict, summary, answer) VALUES "
                        "(1, 1, 'watch', 'flaky keyboard', 'It looks bad.\n\nReally.\n\n"
                        "VERDICT: watch - flaky keyboard')")
        self.db.execute("INSERT INTO findings (pattern_id, t, verdict, summary) "
                        "VALUES (1, 1, 'baseline', '')")
        self.db.execute("INSERT INTO investigations (rule, t, status, events, report) VALUES "
                        "('demo_rule', 2, 'done', ?, 'Checked it.\n\nVERDICT: ignore - fine')",
                        (json.dumps([{"MESSAGE": "boom", "source": "kernel"}]),))
        self.db.execute("INSERT INTO alerts (rule, t, severity, summary, message, data) "
                        "VALUES ('demo_rule', 3, 'watch', 'boom 5x', 'boom', '{}')")
        self.db.commit()
        self.rs = rules.RuleSet(os.path.join(self.dir, "rules"))
        self.rs.by_source["kernel"] = [{
            "name": "demo_rule", "regex": "boom", "actions": [{"block": "alert"}],
            "samples": ["boom"], "flagged": True, "review": "too broad"}]

    def test_items(self):
        reports = list(label_sources.Reports(self.db).items())
        self.assertEqual(sorted(i.key for i in reports), ["finding:1", "investigation:1"])
        finding = next(i for i in reports if i.key == "finding:1")
        self.assertEqual(len(finding.parts), 3)                  # one part per paragraph
        inv = next(i for i in reports if i.key == "investigation:1")
        self.assertEqual(inv.refs, [("argus.rule", "demo_rule")])
        (alert,) = label_sources.Alerts(self.db).items()
        self.assertEqual(alert.refs, [("argus.rule", "demo_rule")])
        (rule,) = label_sources.Rules(self.db, self.rs).items()
        self.assertEqual([p.id for p in rule.parts], ["regex", "actions"])
        self.assertEqual(rule.context[0].id, "review")           # flagged: reason shown first

    def test_whole_events_and_captures(self):
        from argus.blocks import State
        st = State(self.db)
        ev = {"MESSAGE": "boom 7", "PRIORITY": "3", "_SYSTEMD_UNIT": "x.service",
              "__CURSOR": "c1", "CODE_LINE": "12"}
        for _ in range(7):
            st.keep_example("demo_rule", ev, 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM rule_events").fetchone()[0], 5)
        self.rs.by_source["kernel"][0]["regex"] = r"boom (?P<n>\d+)"
        self.rs._compile("kernel")
        (rule,) = label_sources.Rules(self.db, self.rs).items()
        ctx = {p.id: p for p in rule.context}
        self.assertEqual(ctx["captured1"].text, "n=7")
        self.assertEqual(ctx["event1"].kind, "fields")
        self.assertEqual(ctx["event1"].text.splitlines(),
                         ["MESSAGE=boom 7", "CODE_LINE=12", "PRIORITY=3",
                          "_SYSTEMD_UNIT=x.service", "__CURSOR=c1"])
        self.assertNotIn("event4", ctx)                          # three shown
        self.assertEqual(rule.live[0].id, "stats")
        st.alert("demo_rule", "watch", "boom", {"event": ev, "t": 5, "groups": {"n": "7"}})
        alert = next(iter(label_sources.Alerts(self.db).items()))
        self.assertEqual([p.id for p in alert.context][:2], ["event", "data"])
        self.assertEqual(alert.context[1].text, "n=7")

    def test_sync_through_store(self):
        store = Store(":memory:")
        store.sync(label_sources.sources(self.db, self.rs))
        self.assertEqual(store.counts("andy"), {"argus.alert": (1, 0), "argus.report": (2, 0),
                                                "argus.rule": (1, 0)})


if __name__ == "__main__":
    unittest.main()
