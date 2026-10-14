"""The labeling core: items, labels, marks, and where they're stored.

Nothing here knows about argus. A *source adapter* turns its own data into
Items; the core snapshots them, queues them for review, and stores the
Labels people give. See docs/labeling-design.md.

    Item   = an ordered list of Parts to judge, plus Parts of context
    Label  = one judgement of one item version: verdict + Marks
    Mark   = "this span of this part is wrong / good / unsupported / unclear /
              should be something else / where it went wrong / where
              something is missing", with a reason flag and a note
    Link   = "this item is a duplicate of / related to that one", left for a
              later pass (a person or a model) to interpret
"""

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field

VERDICTS = ("good", "bad", "unsure", "skip")
KINDS = ("wrong", "good", "from_here", "missing",
         "unsupported",       # might be true, but nothing shows it
         "unclear",           # can't follow it: unexplained term or reasoning
         "should_be",         # a correction; the mark's `fix` says what instead
         "no_better_option")  # wrong only because the choices didn't allow better
LINK_KINDS = ("duplicate", "related")
PART_KINDS = ("text", "code", "field")


@dataclass
class Part:
    id: str               # unique within the item, e.g. "regex" or "p3"
    kind: str             # text | code | field: only changes how it's shown
    text: str
    label: str = ""       # heading to show; defaults to the id


@dataclass
class Item:
    source: str           # which adapter, e.g. "argus.report"
    key: str              # stable id within the source
    title: str
    parts: list           # [Part], in order: "from here" means this and after
    context: list = field(default_factory=list)   # [Part], shown, also markable
    refs: list = field(default_factory=list)      # [(source, key)] related items
    question: str = ""    # what's being asked, e.g. "Is this report right?"
    priority: float = 0   # higher is reviewed first
    link: str = ""        # where to see it in its own app, if anywhere
    group: str = ""       # items with the same group are near-duplicates

    def snapshot(self):
        """What gets stored and hashed: everything shown to the labeler."""
        d = asdict(self)
        d.pop("priority")  # changes all the time and isn't shown
        d.pop("group")     # how the adapter clusters items, not their content
        d["refs"] = [list(r) for r in self.refs]
        return d


def version_of(snapshot):
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def paragraphs(text, prefix="p"):
    """Split prose into one Part per paragraph (blank-line separated)."""
    chunks = [c.strip("\n") for c in re.split(r"\n\s*\n", text or "") if c.strip()]
    return [Part(f"{prefix}{i + 1}", "text", c, "") for i, c in enumerate(chunks)]


class Source:
    """What an adapter implements. Only items() is required."""
    name = "?"

    def items(self):
        """Every item that could be labeled now."""
        raise NotImplementedError

    def labeled(self, item, label):
        """Called after a label is saved. Default: do nothing."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY, source TEXT, key TEXT, version TEXT,
    snapshot TEXT, first_seen REAL, last_seen REAL, priority REAL DEFAULT 0,
    current INTEGER DEFAULT 1,
    UNIQUE (source, key, version));
CREATE INDEX IF NOT EXISTS items_current ON items (current, source);
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY, item_id INTEGER, by TEXT, t REAL,
    verdict TEXT, fixable INTEGER, confidence INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS labels_item ON labels (item_id, by);
CREATE TABLE IF NOT EXISTS marks (
    id INTEGER PRIMARY KEY, label_id INTEGER,
    item_id INTEGER,          -- the item version the part belongs to (may be a ref)
    part TEXT,                -- NULL: the whole item
    start INTEGER, end INTEGER,   -- NULL: the whole part; start=end: a point
    kind TEXT, reason INTEGER, note TEXT, fix TEXT);
CREATE INDEX IF NOT EXISTS marks_label ON marks (label_id);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY, label_id INTEGER, kind TEXT,
    target INTEGER,           -- the item version linked to
    note TEXT);
CREATE INDEX IF NOT EXISTS links_label ON links (label_id);
"""
# Columns added after the first release: (table, column, type).
MIGRATIONS = [("marks", "fix", "TEXT"), ("items", "grp", "TEXT DEFAULT ''")]


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # --- items --------------------------------------------------------------
    def sync(self, sources):
        """Snapshot every item the sources offer. Returns how many are new.

        An item whose content changed gets a new version; older versions
        stay (labels point at them) but are no longer current.
        """
        now, new = time.time(), 0
        for src in sources:
            seen = set()
            for it in src.items():
                snap = it.snapshot()
                ver = version_of(snap)
                seen.add(it.key)
                row = self.db.execute(
                    "SELECT id FROM items WHERE source=? AND key=? AND version=?",
                    (it.source, it.key, ver)).fetchone()
                if row:
                    self.db.execute("UPDATE items SET last_seen=?, priority=?, grp=?, "
                                    "current=1 WHERE id=?",
                                    (now, it.priority, it.group, row[0]))
                    continue
                self.db.execute("UPDATE items SET current=0 WHERE source=? AND key=?",
                                (it.source, it.key))
                self.db.execute(
                    "INSERT INTO items (source, key, version, snapshot, first_seen, "
                    "last_seen, priority, grp) VALUES (?,?,?,?,?,?,?,?)",
                    (it.source, it.key, ver, json.dumps(snap, ensure_ascii=False),
                     now, now, it.priority, it.group))
                new += 1
            # Items the source no longer offers (deleted rules, pruned rows).
            gone = [k for (k,) in self.db.execute(
                "SELECT key FROM items WHERE source=? AND current=1", (src.name,))
                if k not in seen]
            for k in gone:
                self.db.execute("UPDATE items SET current=0 WHERE source=? AND key=?",
                                (src.name, k))
        self.db.commit()
        return new

    def item(self, item_id):
        row = self.db.execute("SELECT id, source, key, version, snapshot, current "
                              "FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        iid, source, key, version, snap, current = row
        return {"id": iid, "version": version, "current": bool(current),
                **json.loads(snap)}

    def current_id(self, source, key):
        row = self.db.execute("SELECT id FROM items WHERE source=? AND key=? AND current=1",
                              (source, key)).fetchone()
        return row[0] if row else None

    def queue(self, by, source=None, include_labeled=False, limit=500):
        """Current items, unlabeled (by `by`) first, highest priority first."""
        sql = """
            SELECT i.id, i.source, i.key, json_extract(i.snapshot, '$.title'),
                   i.priority, i.first_seen, i.grp,
                   (SELECT verdict FROM labels l WHERE l.item_id = i.id AND l.by = ?
                    ORDER BY l.id DESC LIMIT 1) AS verdict
            FROM items i WHERE i.current = 1 """
        args = [by]
        if source:
            sql += "AND i.source = ? "
            args.append(source)
        sql += "ORDER BY verdict IS NOT NULL, i.priority DESC, i.first_seen DESC, i.id DESC"
        rows = self.db.execute(sql, args).fetchall()
        if not include_labeled:
            rows = [r for r in rows if r[7] is None]
        return [dict(zip(("id", "source", "key", "title", "priority", "first_seen",
                          "group", "verdict"), r)) for r in rows[:limit]]

    def group_of(self, item_id, by, unlabeled_only=True):
        """Other current items in the same group (near-duplicates)."""
        row = self.db.execute("SELECT source, grp FROM items WHERE id=?",
                              (item_id,)).fetchone()
        if not row or not row[1]:
            return []
        return [r for r in self.queue(by, row[0], include_labeled=not unlabeled_only,
                                      limit=10_000)
                if r["group"] == row[1] and r["id"] != item_id]

    def recent(self, by, limit=30, exclude=None):
        """Items `by` labeled most recently (latest version labeled), for linking."""
        rows = self.db.execute("""
            SELECT i.id, i.source, json_extract(i.snapshot, '$.title'), l.verdict
            FROM labels l JOIN items i ON i.id = l.item_id
            WHERE l.by = ? AND l.id IN (SELECT max(id) FROM labels WHERE by = ?
                                        GROUP BY item_id)
            ORDER BY l.id DESC LIMIT ?""", (by, by, limit + 1)).fetchall()
        return [dict(zip(("id", "source", "title", "verdict"), r))
                for r in rows if r[0] != exclude][:limit]

    def counts(self, by):
        """{source: (current items, labeled by `by`)}"""
        return {s: (n, done) for s, n, done in self.db.execute("""
            SELECT source, count(*), sum(EXISTS (SELECT 1 FROM labels l
                                                 WHERE l.item_id = i.id AND l.by = ?))
            FROM items i WHERE current = 1 GROUP BY source ORDER BY source""", (by,))}

    # --- labels -------------------------------------------------------------
    def save_label(self, item_id, by, verdict, marks=(), fixable=None, confidence=None,
                   note="", links=()):
        """Append a label. Labels are never edited: the latest one per labeler counts.

        marks: dicts with kind, and optionally item_id, part, start, end,
        reason, note, fix (for should_be). A mark's reason defaults to whether
        it points the same way as the verdict (see default_reason).
        links: dicts with kind (duplicate or related), target (an item id)
        and optionally note.
        """
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        if confidence is not None and confidence not in (1, 2, 3):
            raise ValueError("confidence must be 1, 2 or 3")
        marks = [dict(m) for m in marks]
        if sum(m.get("kind") == "from_here" for m in marks) > 1:
            raise ValueError("at most one from_here mark per label")
        for m in marks:
            if m.get("kind") not in KINDS:
                raise ValueError(f"mark kind must be one of {KINDS}")
            if (m.get("start") is None) != (m.get("end") is None):
                raise ValueError("a span needs both start and end")
            if m.get("start") is not None and not 0 <= m["start"] <= m["end"]:
                raise ValueError("bad span")
            if m.get("reason") is None:
                m["reason"] = default_reason(m["kind"], verdict)
        links = [dict(x) for x in links]
        for x in links:
            if x.get("kind") not in LINK_KINDS:
                raise ValueError(f"link kind must be one of {LINK_KINDS}")
            if not self.db.execute("SELECT 1 FROM items WHERE id=?",
                                   (x.get("target"),)).fetchone():
                raise ValueError("link target is not an item")
            if x["target"] == item_id:
                raise ValueError("an item can't link to itself")
        cur = self.db.execute(
            "INSERT INTO labels (item_id, by, t, verdict, fixable, confidence, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (item_id, by, time.time(), verdict,
             None if verdict != "bad" or fixable is None else int(bool(fixable)),
             confidence, note or ""))
        lid = cur.lastrowid
        for m in marks:
            self.db.execute(
                "INSERT INTO marks (label_id, item_id, part, start, end, kind, reason, "
                "note, fix) VALUES (?,?,?,?,?,?,?,?,?)",
                (lid, m.get("item_id") or item_id, m.get("part"), m.get("start"),
                 m.get("end"), m["kind"], int(bool(m["reason"])), m.get("note") or "",
                 m.get("fix") if m["kind"] == "should_be" else None))
        for x in links:
            self.db.execute("INSERT INTO links (label_id, kind, target, note) "
                            "VALUES (?,?,?,?)", (lid, x["kind"], x["target"],
                                                 x.get("note") or ""))
        self.db.commit()
        return lid

    def latest_label(self, item_id, by):
        row = self.db.execute(
            "SELECT id, t, verdict, fixable, confidence, note FROM labels "
            "WHERE item_id=? AND by=? ORDER BY id DESC LIMIT 1", (item_id, by)).fetchone()
        if not row:
            return None
        lid, t, verdict, fixable, confidence, note = row
        return {"id": lid, "t": t, "verdict": verdict,
                "fixable": None if fixable is None else bool(fixable),
                "confidence": confidence, "note": note, "marks": self._marks(lid),
                "links": self._links(lid)}

    def _marks(self, lid):
        marks = [dict(zip(("item_id", "part", "start", "end", "kind", "reason", "note",
                           "fix"), m))
                 for m in self.db.execute(
                     "SELECT item_id, part, start, end, kind, reason, note, fix FROM marks "
                     "WHERE label_id=? ORDER BY id", (lid,))]
        for m in marks:
            m["reason"] = bool(m["reason"])
            if m["fix"] is None:
                del m["fix"]
        return marks

    def _links(self, lid):
        return [{"kind": k, "target": t, "note": n,
                 "title": self.db.execute("SELECT json_extract(snapshot, '$.title') "
                                          "FROM items WHERE id=?", (t,)).fetchone()[0]}
                for k, t, n in self.db.execute(
                    "SELECT kind, target, note FROM links WHERE label_id=? ORDER BY id",
                    (lid,))]

    def mark_group_duplicates(self, item_id, by):
        """Label every unlabeled near-duplicate of item_id as a skipped
        duplicate of it. Returns how many."""
        others = self.group_of(item_id, by)
        for o in others:
            self.save_label(o["id"], by, "skip",
                            links=[{"kind": "duplicate", "target": item_id,
                                    "note": "marked with its whole group"}])
        return len(others)

    def previous_label(self, item_id, by):
        """The latest label on an older version of the same item, if any."""
        row = self.db.execute("""
            SELECT l.item_id FROM labels l JOIN items i ON i.id = l.item_id
            JOIN items cur ON cur.source = i.source AND cur.key = i.key
            WHERE cur.id = ? AND l.item_id != ? AND l.by = ?
            ORDER BY l.id DESC LIMIT 1""", (item_id, item_id, by)).fetchone()
        return (row[0], self.latest_label(row[0], by)) if row else (None, None)

    def export(self, by=None):
        """Every label with its item snapshot, oldest first (for JSONL)."""
        sql = "SELECT id, item_id, by FROM labels"
        args = ()
        if by:
            sql, args = sql + " WHERE by=?", (by,)
        for lid, item_id, who in self.db.execute(sql + " ORDER BY id", args).fetchall():
            row = self.db.execute(
                "SELECT t, verdict, fixable, confidence, note FROM labels WHERE id=?",
                (lid,)).fetchone()
            yield {"label": lid, "by": who,
                   **dict(zip(("t", "verdict", "fixable", "confidence", "note"), row)),
                   "marks": self._marks(lid), "links": self._links(lid),
                   "item": self.item(item_id)}


def default_reason(kind, verdict):
    """Is a mark a reason for the verdict, unless the labeler says otherwise?
    Faults are reasons for a bad verdict, good marks for a good one.
    no_better_option blames the system, not the item, so it's never a default
    reason."""
    if verdict == "bad":
        return kind not in ("good", "no_better_option")
    if verdict == "good":
        return kind == "good"
    return False
